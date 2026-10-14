"""Employee Attendance & Analytics API (contract v2.0)."""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = PRESENT + ("ABSENT", "LEAVE")
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000

def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="employees_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_month")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="attendance_status_date")


@asynccontextmanager
async def lifespan(_app):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def fail422(message: str):
    raise HTTPException(422, message)


def strict_epoch(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not EPOCH_MIN <= value <= EPOCH_MAX:
        fail422("timestamp must be an epoch-millisecond integer in the supported range")
    try:
        instant = (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        fail422("invalid timestamp")
    return instant


def truncate_second(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms(value: Optional[datetime]):
    if value is None:
        return None
    return int(truncate_second(value).timestamp() * 1000)


def hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def attendance_date(punch_in: datetime, emp: dict) -> str:
    local = truncate_second(punch_in).astimezone(IST)
    start, end = hhmm(emp["shift_start"]), hhmm(emp["shift_end"])
    day = local.date()
    if end <= start and local.time().replace(tzinfo=None) < end:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_instant(date_str: str, shift: str, overnight_end=False) -> datetime:
    day = date.fromisoformat(date_str)
    result = datetime.combine(day, hhmm(shift), IST)
    return (result + timedelta(days=1) if overnight_end else result).astimezone(UTC)


def shift_end_instant(emp: dict, record_date: str) -> datetime:
    overnight = hhmm(emp["shift_end"]) <= hhmm(emp["shift_start"])
    return shift_instant(record_date, emp["shift_end"], overnight)


def late_minutes(punch_in: Optional[datetime], emp: dict, record_date: str) -> int:
    if punch_in is None:
        return 0
    start = shift_instant(record_date, emp["shift_start"])
    seconds = (truncate_second(punch_in) - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def rounded_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = Decimal(str((truncate_second(punch_out) - truncate_second(punch_in)).total_seconds())) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived(emp: dict, record_date: str, status: str, pin: Optional[datetime], pout: Optional[datetime]):
    if status not in PRESENT:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(pin, emp, record_date)
    if pout is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = rounded_hours(pin, pout)
    overtime = int(max(0, ((pout - shift_end_instant(emp, record_date)).total_seconds()) // 60))
    if overtime < 30:
        overtime = 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}


def as_record(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["punch_in"] = epoch_ms(out.get("punch_in"))
    out["punch_out"] = epoch_ms(out.get("punch_out"))
    out["late_minutes"] = out.get("late_minutes", 0)
    out["overtime_minutes"] = out.get("overtime_minutes", 0)
    out["half_day"] = out.get("half_day", False)
    history = []
    for entry in out.get("history", []):
        e = dict(entry)
        e["at"] = epoch_ms(e.get("at"))
        changes = {}
        for key, values in e.get("changes", {}).items():
            values = dict(values)
            if key in ("punch_in", "punch_out"):
                values["from"] = epoch_ms(values.get("from"))
                values["to"] = epoch_ms(values.get("to"))
            changes[key] = values
        e["changes"] = changes
        history.append(e)
    out["history"] = history
    return out


def month_bounds(month: str):
    try:
        first = date.fromisoformat(month + "-01")
    except ValueError:
        fail422("month must use YYYY-MM")
    next_month = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first.isoformat(), (next_month - timedelta(days=1)).isoformat()


def month_filter(month: str):
    start, end = month_bounds(month)
    return {"$gte": start, "$lte": end}


def mongo_half_up(expression, places: int):
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}}, scale]}


def working_days(start: date, end: date) -> int:
    """Count weekdays in the inclusive interval without depending on stored logs."""
    if end < start:
        return 0
    days = (end - start).days + 1
    weeks, extra = divmod(days, 7)
    count = weeks * 5
    count += sum(1 for i in range(extra) if (start.weekday() + i) % 7 < 5)
    return count


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return values

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            strict_epoch(value)
        return value

    @field_validator("status")
    @classmethod
    def check_status(cls, value):
        if value not in PRESENT:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return values

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            strict_epoch(value)
        return value


class RegularizeIn(StrictModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            strict_epoch(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_null_punch_values(cls, values):
        if isinstance(values, dict) and any(k in values and values[k] is None for k in ("status", "punch_in", "punch_out")):
            raise ValueError("status and punch timestamps cannot be null")
        return values

    @field_validator("status")
    @classmethod
    def check_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = doc["joined_on"].isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return {**{k: v for k, v in doc.items() if k != "_id"}, "created_at": epoch_ms(doc["created_at"])}


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        item["created_at"] = epoch_ms(item.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    instant = strict_epoch(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record_day = attendance_date(instant, emp)
    doc = {"emp_code": body.emp_code, "date": record_day, "status": body.status, "punch_in": instant, "punch_out": None,
           "work_hours": None, "late_minutes": late_minutes(instant, emp, record_day), "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return as_record(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from and date_to and date_from > date_to:
        fail422("date_from must be on or before date_to")
    query = {}
    if emp_code is not None: query["emp_code"] = emp_code
    if status is not None: query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from: query["date"]["$gte"] = date_from.isoformat()
        if date_to: query["date"]["$lte"] = date_to.isoformat()
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [as_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    instant = strict_epoch(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    candidates = db.attendance_logs.find({"emp_code": body.emp_code, "status": {"$in": PRESENT}, "punch_in": {"$lte": instant}}).sort("punch_in", DESCENDING)
    candidate = next(iter(candidates), None)
    if not candidate:
        future_punch = db.attendance_logs.find_one({"emp_code": body.emp_code, "status": {"$in": PRESENT}, "punch_in": {"$gt": instant}})
        if future_punch:
            fail422("punch_out must be after punch_in and no more than 24 hours later")
        raise HTTPException(404, "no eligible punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already closed")
    pin = truncate_second(candidate["punch_in"])
    if instant <= pin or instant - pin > timedelta(hours=24):
        fail422("punch_out must be after punch_in and no more than 24 hours later")
    calc = derived(emp, candidate["date"], candidate["status"], pin, instant)
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": candidate["date"], "punch_in": candidate["punch_in"], "punch_out": None},
        {"$set": {"punch_out": instant, **calc}}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(409, "attendance record is already closed")
    return as_record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: date, body: RegularizeIn):
    day = date.isoformat()
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not old:
        raise HTTPException(404, "attendance record not found")
    changes_in = body.model_dump(exclude_unset=True, exclude={"reason", "regularized_by"})
    state = {"status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    for key, value in changes_in.items():
        state[key] = strict_epoch(value) if key in ("punch_in", "punch_out") else value
    if state["status"] in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_in or "punch_out" in changes_in:
            fail422("ABSENT and LEAVE cannot be combined with supplied punch times")
        state["punch_in"] = state["punch_out"] = None
    elif state["punch_in"] is None:
        fail422("presence status requires punch_in")
    if state["punch_in"] is not None and attendance_date(state["punch_in"], emp) != day:
        fail422("punch_in does not belong to this attendance date")
    if state["punch_out"] is not None and (state["punch_out"] <= state["punch_in"] or state["punch_out"] - state["punch_in"] > timedelta(hours=24)):
        fail422("punch_out must be after punch_in and within 24 hours")
    calc = derived(emp, day, state["status"], state["punch_in"], state["punch_out"])
    new_values = {**state, **calc}
    old_values = {k: old.get(k) for k in new_values}
    actual = {k: {"from": old_values[k], "to": new_values[k]} for k in new_values if old_values[k] != new_values[k]}
    if not actual:
        fail422("request makes no actual change")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": actual}
    snapshot = {k: (old[k] if k in old else {"$exists": False}) for k in ("status", "punch_in", "punch_out", "late_minutes", "work_hours", "overtime_minutes", "half_day")}
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": day, **snapshot},
        {"$set": new_values, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(409, "attendance record changed concurrently; retry")
    return as_record(updated)


def employee_month_pipeline(emp_code: str, month: str):
    return [{"$match": {"emp_code": emp_code, "date": month_filter(month)}},
            {"$set": {"weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            {"$group": {"_id": None,
             "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT)]}, {"$in": ["$weekday", [2,3,4,5,6]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
             "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
             "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
             "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
             "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    start, end = month_bounds(month)
    join = max(date.fromisoformat(emp["joined_on"]), date.fromisoformat(start))
    days = working_days(join, date.fromisoformat(end))
    row = next(iter(db.attendance_logs.aggregate(employee_month_pipeline(emp_code, month))), {})
    present = float(row.get("present_days", 0))
    pct = float(Decimal(str(present * 100 / days)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)) if days else None
    return {"emp_code": emp_code, "month": month, "working_days": days, "present_days": present,
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": pct}


def department_summary_pipeline(month: str, department: Optional[str]):
    start, end = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None: match["department"] = department
    return [{"$match": match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"weekday": {"$cond": [{"$ne": ["$logs", None]}, {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, None]} }},
        {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"},
          "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$in": ["$weekday", [2,3,4,5,6]]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
          "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
          "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
          "late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
          "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
          "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$group": {"_id": "$_id.department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"},
          "work_hours_sum": {"$sum": "$work_hours_sum"}, "work_hours_count": {"$sum": "$work_hours_count"},
          "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$late_minutes"},
          "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"}}},
        {"$set": {"avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, mongo_half_up({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2), None]}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "avg_work_hours": 1,
          "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}}]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    month_bounds(month)
    return {"month": month, "items": list(db.employees.aggregate(department_summary_pipeline(month, department)))}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    start, end = month_bounds(month)
    lookup_match = {"$expr": {"$eq": ["$emp_code", "$$code"]}}
    emp_match = {}
    if department is not None: emp_match["department"] = department
    return [{"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "let": {"code": "$_id"}, "pipeline": [
            {"$match": {"$expr": {"$and": [lookup_match["$expr"], *([{"$eq": ["$department", department]}] if department is not None else [])]}}}], "as": "employee"}},
        {"$unwind": "$employee"},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    month_bounds(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))}


def trend_pipeline(department: str, start: date, end: date):
    days = (end - start).days + 1
    return [{"$documents": [{"_id": 1}]},
      {"$set": {"dates": {"$map": {"input": {"$range": [0, days]}, "as": "n", "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start.isoformat()}}, "unit": "day", "amount": "$$n"}}}}}},
      {"$unwind": "$dates"}, {"$set": {"date": {"$dateToString": {"date": "$dates", "format": "%Y-%m-%d"}}}},
      {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
        {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$project": {"_id": 0, "emp_code": 1}}], "as": "eligible"}},
      {"$set": {"headcount": {"$size": "$eligible"}, "codes": {"$map": {"input": "$eligible", "as": "e", "in": "$$e.emp_code"}}}},
      {"$lookup": {"from": "attendance_logs", "let": {"day": "$date", "codes": "$codes"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}}], "as": "logs"}},
      {"$set": {"present_count": {"$sum": {"$map": {"input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENT)]}}}, "as": "l", "in": {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]}}}},
        "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
        "is_working_day": {"$in": [{"$dayOfWeek": "$dates"}, [2,3,4,5,6]]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, mongo_half_up({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
      {"$sort": {"dates": 1}},
      {"$setWindowFields": {"sortBy": {"dates": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
      {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]}, mongo_half_up("$moving_avg_7d", 4), None]}}}]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: date = Query(..., alias="from"), to_date: date = Query(..., alias="to")):
    if to_date < from_date or (to_date - from_date).days + 1 > 92:
        fail422("date range must be ordered and no longer than 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "department not found")
    pipeline = trend_pipeline(department, from_date, to_date)
    return {"department": department, "items": list(db.command("aggregate", 1, pipeline=pipeline, cursor={})["cursor"]["firstBatch"])}


def explain_command(collection: str, pipeline: list, query=None, sort=None, skip=0, limit=None, database_level=False):
    if database_level:
        command = {"aggregate": 1, "pipeline": pipeline, "cursor": {}}
    else:
        command = {"find": collection, "filter": query or {}, "projection": {"_id": 0}}
        if sort: command["sort"] = dict(sort)
        command["skip"] = skip
        if limit is not None: command["limit"] = limit
    return db.command("explain", command, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[date] = None, date_to: Optional[date] = None,
                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_date: Optional[date] = Query(None, alias="from"), to_date: Optional[date] = Query(None, alias="to")):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to: fail422("date_from must be on or before date_to")
        q = {}
        if emp_code is not None: q["emp_code"] = emp_code
        if status is not None: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from.isoformat()
            if date_to: q["date"]["$lte"] = date_to.isoformat()
        exp = explain_command("attendance_logs", [], q, [("date", -1), ("emp_code", 1)], (page-1)*page_size, page_size)
        coll = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: fail422("emp_code and month are required")
        month_bounds(month); pipeline = employee_month_pipeline(emp_code, month)
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats"); coll="attendance_logs"
    elif endpoint == "department_summary":
        if not month: fail422("month is required")
        month_bounds(month); pipeline = department_summary_pipeline(month, department)
        exp = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats"); coll="employees"
    elif endpoint == "late_leaderboard":
        if not month: fail422("month is required")
        month_bounds(month); pipeline = leaderboard_pipeline(month, limit, department)
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats"); coll="attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: fail422("department, from, and to are required")
        if to_date < from_date or (to_date-from_date).days+1 > 92: fail422("invalid trend date range")
        pipeline = trend_pipeline(department, from_date, to_date)
        exp = db.command("explain", {"aggregate": 1, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats"); coll="employees"
    else:
        fail422("unknown endpoint")
    return {"endpoint": endpoint, "collection": coll, "explain": exp}
