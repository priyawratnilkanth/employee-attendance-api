# Starter code review

| # | Where | What was wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Assumed a naive punch-in and used local wall time; the grace comparison did not consistently truncate to seconds. | Compare 09:40:00 and 09:40:01 for a 09:30 shift, including a UTC instant. | Convert instants to UTC, derive IST shift instants, truncate first, and apply the strict 600-second boundary. |
| 2 | `compute_work_hours` | Python `round` uses ties-to-even rather than required decimal half-up rounding. | A duration whose hundredths tie exactly. | Use `Decimal` and `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Built shift end on the same date for overnight shifts and did not enforce the 30-minute minimum. | 22:00–06:00 shift punched out the next morning; also test 29 and 30 minutes. | Resolve shift end on the next IST day for overnight shifts and floor minutes before applying the threshold. |
| 4 | Mongo client and `health` | Default timezone-naive client and health always returned 200 without pinging MongoDB. | Stop MongoDB and call `/health`; inspect stored timestamps. | Use timezone-aware UTC PyMongo and return 503 when ping fails. |
| 5 | `create_employee` | Check-then-insert allowed concurrent duplicate business codes; `created_at` was local and naive. | Send simultaneous creates with one `emp_code`. | Unique index, catch `DuplicateKeyError`, and store server UTC time. |
| 6 | Employee input and output | Missing contract validation for code, email, dates, names, shift formats, and equal shift boundaries. | Submit malformed values or equal shift start/end. | Add Pydantic schema constraints and cross-field validation. |
| 7 | `list_employees` | Filtered items but counted the unfiltered collection; skipped `page * page_size`, so page 1 skipped results; no sort or bounds. | Department filter with multiple pages. | Count the same query, sort by `emp_code`, use `(page - 1) * page_size`, and validate bounds. |
| 8 | `punch_in` | Did not check unknown employee before dereferencing; used local time and calendar date rather than IST/R1; truthiness mishandled zero; no status or timestamp validation. | Unknown employee, overnight shift, invalid milliseconds, or invalid status. | Validate inputs, use UTC instants and R1 date selection, and return 404/422 as contracted. |
| 9 | `punch_in` insert | A read-before-insert duplicate check was not concurrency-safe; response leaked Mongo `_id` as `id`. | Parallel identical requests. | Enforce unique `(emp_code, date)`, map duplicate key to 409, and serialize the contract fields only. |
| 10 | `list_attendance` | Loaded the entire collection, sorted in Python, used no page validation, and returned internal IDs. | Run against a large dataset or request page 1. | Count filtered query, apply indexed Mongo sort/skip/limit, validate dates/page size, and serialize without `_id`. |
| 11 | Missing endpoints | Punch-out, regularization, all four analytics routes, and explain were absent. | Compare registered routes with `openapi.yaml`. | Implemented each required operation and contract behavior in `app/main.py`. |
| 12 | Serialization and derived values | There was no shared conversion for BSON UTC dates, epoch milliseconds, missing legacy values, or nested history changes. | Read a legacy record or corrected record over HTTP. | Centralized conversion and default handling in `as_record`. |

## Examined and retained

- Keeping all application logic in `app/main.py` matches the starter README and assignment.
- `sample_seed.py` is intentionally destructive because it explicitly deletes collections when a developer chooses to seed; the API never calls it or resets data.
- The sample data's `half_day` and `history` omissions are explicitly valid legacy cases, not malformed records.
- `load_dotenv()` remains, while real environment variables take precedence under python-dotenv's default behavior.
