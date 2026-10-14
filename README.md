# Employee Attendance and Analytics API

FastAPI service implementing the HROne contract v2.0. All API implementation is in `app/main.py`; employees are addressed by `emp_code`, and attendance by `(emp_code, date)`. It provides employee management, safe punch-in/out, audited corrections, four MongoDB-backed analytics endpoints, and query explain output.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (the assignment grader uses MongoDB 7)

## Setup

Create and activate a virtual environment, then install dependencies.

**Windows PowerShell**

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

**macOS or Linux**

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

Set `MONGO_URI` and `MONGO_DB` in the shell or in a local `.env` file. Shell environment variables take precedence. Never commit `.env` or real credentials. The app creates indexes idempotently at startup.

## Run

From this directory, start the server:

```sh
uvicorn app.main:app --port 8000
```

Interactive API documentation is at [http://localhost:8000/docs](http://localhost:8000/docs). Readiness is checked at `/health` and returns 503 if MongoDB cannot answer.

## Sample data and verification

`sample_seed.py` loads the nine Extended JSON sample documents. **It deletes all documents in both configured collections first.** Use it only with a disposable database, never with an existing or production dataset.

```sh
python sample_seed.py
```

Offline unit tests cover timestamp validation, UTC/IST conversion, overnight attendance dates, late grace, overtime thresholds, half-up work-hour rounding, and request validation. Run them with:

```powershell
python -m unittest discover -v
```

Syntax-check the app with:

```sh
python -m py_compile app/main.py
```

MongoDB-backed API, aggregation, query-plan, and concurrency verification requires a running MongoDB 6.0+ instance; configure the environment above and exercise the operations through `/docs` or an HTTP client. The analytics and explain endpoints execute their aggregation/query against MongoDB and cannot be meaningfully verified without it.

## Endpoints

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

Instants are stored as UTC BSON dates and returned as epoch milliseconds. Shift and attendance calendars use IST. Punch-in uniqueness and employee code uniqueness are enforced by MongoDB unique indexes. Corrections append audit entries atomically; ordinary punches do not. Aggregations read the stored derived values and preserve the contract's ranking, headcount, pagination, and date-range rules.

## Limitations

Authentication and authorization are outside the assignment contract. This kit has no automated test fixtures, so concurrency and aggregation behavior should be verified against MongoDB using realistic edge-case data beyond the nine samples.
