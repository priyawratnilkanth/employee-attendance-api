"""Offline unit tests for contract rules that do not require MongoDB."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from fastapi import HTTPException

from app.main import (
    UTC, PunchInIn, RegularizeIn, attendance_date, derived, late_minutes,
    PunchOutIn, punch_out, rounded_hours, strict_epoch,
    explain_endpoint,
)


class TimeRuleTests(unittest.TestCase):
    def test_epoch_milliseconds_truncate_to_seconds(self):
        value = strict_epoch(1_783_312_500_999)
        self.assertEqual(value.microsecond, 0)
        self.assertEqual(value.tzinfo, UTC)

    def test_invalid_epoch_types_and_seconds_are_rejected(self):
        for value in (1_783_312_500, 1_783_312_500.0, "1783312500000", True):
            with self.subTest(value=value), self.assertRaises(Exception):
                strict_epoch(value)

    def test_late_grace_boundary_is_strict_and_whole_minutes(self):
        emp = {"shift_start": "09:30"}
        shift = datetime(2026, 7, 6, 4, 0, tzinfo=UTC)
        self.assertEqual(late_minutes(shift + timedelta(minutes=10), emp, "2026-07-06"), 0)
        self.assertEqual(late_minutes(shift + timedelta(minutes=10, seconds=1), emp, "2026-07-06"), 10)
        self.assertEqual(late_minutes(shift + timedelta(minutes=45, seconds=59), emp, "2026-07-06"), 45)

    def test_overnight_after_midnight_belongs_to_shift_start_day(self):
        emp = {"shift_start": "22:00", "shift_end": "06:00"}
        punch = datetime(2026, 7, 7, 0, 0, tzinfo=UTC)  # 05:30 IST
        self.assertEqual(attendance_date(punch, emp), "2026-07-06")
        out = datetime(2026, 7, 7, 1, 0, tzinfo=UTC)  # 06:30 IST
        values = derived(emp, "2026-07-06", "PRESENT", punch, out)
        self.assertEqual(values["overtime_minutes"], 30)

    def test_half_up_work_hours_and_overtime_threshold(self):
        pin = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
        self.assertEqual(rounded_hours(pin, pin + timedelta(seconds=3618)), 1.01)
        emp = {"shift_start": "09:00", "shift_end": "18:00"}
        pin = datetime(2026, 7, 6, 3, 30, tzinfo=UTC)
        shift_end = datetime(2026, 7, 6, 12, 30, tzinfo=UTC)
        self.assertEqual(derived(emp, "2026-07-06", "PRESENT", pin, shift_end + timedelta(minutes=29))["overtime_minutes"], 0)
        self.assertEqual(derived(emp, "2026-07-06", "PRESENT", pin, shift_end + timedelta(minutes=30))["overtime_minutes"], 30)

    def test_half_day_uses_rounded_hours_at_4_50_boundary(self):
        pin = datetime(2026, 7, 6, 3, 30, tzinfo=UTC)
        emp = {"shift_start": "09:00", "shift_end": "18:00"}
        below = derived(emp, "2026-07-06", "PRESENT", pin, pin + timedelta(hours=4, minutes=29))
        rounded_to_boundary = derived(emp, "2026-07-06", "PRESENT", pin, pin + timedelta(hours=4, minutes=29, seconds=59))

        self.assertEqual((below["work_hours"], below["half_day"]), (4.48, True))
        self.assertEqual((rounded_to_boundary["work_hours"], rounded_to_boundary["half_day"]), (4.5, False))


class RequestValidationTests(unittest.TestCase):
    def test_punch_in_rejects_invalid_status_and_float_timestamp(self):
        with self.assertRaises(ValidationError):
            PunchInIn(emp_code="EMP0001", status="LEAVE")
        with self.assertRaises(HTTPException) as error:
            PunchInIn(emp_code="EMP0001", punched_at=1_783_312_500_000.0)
        self.assertEqual(error.exception.status_code, 422)

    def test_regularization_does_not_accept_null_timestamp(self):
        with self.assertRaises(ValidationError):
            RegularizeIn(punch_in=None, reason="correction reason", regularized_by="hr")


class ExplainRequestTests(unittest.TestCase):
    def test_attendance_explain_preserves_employee_filter_and_sort_document(self):
        with patch("app.main.db.command", return_value={"ok": 1}) as command:
            explain_endpoint(
                "attendance_list", emp_code="EMP0001", month=None, department=None,
                limit=10, date_from=None, date_to=None, status=None,
                page=2, page_size=5, from_date=None, to_date=None,
            )

        find_command = command.call_args.args[1]
        self.assertEqual(find_command["filter"], {"emp_code": "EMP0001"})
        self.assertEqual(list(find_command["sort"].items()), [("date", -1), ("emp_code", 1)])
        self.assertEqual(find_command["skip"], 5)
        self.assertEqual(find_command["limit"], 5)


class PunchOutValidationTests(unittest.TestCase):
    def test_punch_out_before_existing_punch_in_is_validation_error(self):
        future_punch = {"punch_in": datetime(2026, 7, 10, 4, 20, tzinfo=UTC)}
        fake_db = SimpleNamespace(employees=MagicMock(), attendance_logs=MagicMock())
        fake_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        fake_db.attendance_logs.find.return_value.sort.return_value = []
        fake_db.attendance_logs.find_one.return_value = future_punch
        with patch("app.main.db", fake_db):
            with self.assertRaises(HTTPException) as error:
                punch_out(PunchOutIn(
                    emp_code="EMP0001",
                    punched_at=int(datetime(2026, 7, 10, 4, 0, tzinfo=UTC).timestamp() * 1000),
                ))

        self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
