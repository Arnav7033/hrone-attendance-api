# REVIEW.md

Defects found and fixed in the starter `app/main.py`:

| # | Where (function / area) | What is wrong | How you'd notice it (test, input, or symptom) | How it is fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It treats the stored punch timestamp's timezone as the shift timezone and misses the strict grace-boundary case. | For a 09:30 shift, 09:40:01 should produce 10 late minutes, not 0; UTC and IST can also disagree. | Convert the instant to IST, compare with the shift start on the attendance date, then floor elapsed whole minutes only when elapsed time is strictly over 10 minutes. |
| 2 | `compute_work_hours` | Python `round()` uses banker's rounding and binary floats, rather than decimal half-up rounding. | A duration whose hours are exactly on a half-cent boundary may round differently from R4. | Calculate from elapsed whole seconds using `Decimal` and `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | It does not advance the shift end to the next day for overnight shifts and does not apply the 30-minute minimum. | An overnight 22:00–06:00 shift or 20 minutes past shift end returns an incorrect overtime value. | Build shift end in IST on the correct calendar day; floor whole minutes and return them only when the total is at least 30. |
| 4 | `/health` | It returns `{"status":"ok"}` without checking MongoDB. | Stop MongoDB and `GET /health` still claims the service is ready. | Ping MongoDB and return HTTP 503 if the ping fails. |
| 5 | startup / indexes | No application indexes are created, including the uniqueness guarantee for attendance days. | Concurrent punch-ins can both pass a read-before-write check; query plans may use collection scans. | Create idempotent unique and query-supporting indexes at startup, retrying index setup on health checks after a temporary outage. |
| 6 | `create_employee` | It relies on check-then-insert for duplicate codes and does not rely on a database uniqueness constraint. | Simultaneous requests can both see no employee and insert duplicates. | Enforce a unique index on `employees.emp_code` and translate `DuplicateKeyError` to 409. |
| 7 | `create_employee` / response | `created_at` is a naive local datetime and is returned as a datetime, not epoch milliseconds. | The value depends on the host timezone and does not match the response schema. | Store a UTC-aware, whole-second instant and serialize it as epoch milliseconds. |
| 8 | `EmployeeIn` | The starter accepts invalid employee codes, malformed emails/dates/times, and identical shift start/end times. | Try a malformed code, invalid shift, or `shift_start == shift_end`. | Add schema constraints and return 422 for invalid input. |
| 9 | `list_employees` pagination | `skip = page * page_size` skips the first page. | With page 1 and page size 20, the first 20 employees are omitted. | Use `(page - 1) * page_size` and constrain page/page_size to the contract. |
| 10 | `list_employees` filter/ordering | `total` ignores `department` and results are not sorted by `emp_code`. | A department-filtered response may report the full collection count and unstable order. | Count with the same filter and sort ascending by `emp_code`. |
| 11 | `punch_in` employee lookup | It does not handle an unknown employee before accessing `emp["shift_start"]`. | Punching in with an unknown code can become HTTP 500 instead of 404. | Return 404 when the employee does not exist. |
| 12 | `punch_in` timestamp/date | It uses local-machine `fromtimestamp`, float conversion, and the instant's local date; it does not implement IST attendance dates or overnight shifts. | A UTC host, fractional milliseconds, or a punch just after midnight can be assigned the wrong date. | Strictly validate epoch milliseconds, truncate to whole seconds, convert to UTC/IST explicitly, and apply the overnight date rule. |
| 13 | `punch_in` status | The starter has no validation limiting status to PRESENT, WFH, or ON_DUTY. | A client can create a punch-in record with ABSENT or LEAVE. | Use a validated status literal for punch-in. |
| 14 | `punch_in` concurrency | A separate `find_one` and `insert_one` are a race condition. | Two simultaneous requests can both pass the existence check. | Rely on the unique `(emp_code, date)` index and return 409 when MongoDB rejects a duplicate. |
| 15 | `punch_in` response | It emits a synthetic `id`, even though the contract says attendance is addressed by `(emp_code, date)` and has no API id. | Response includes an unexpected `id` field. | Return only the documented attendance fields; keep MongoDB `_id` internal. |
| 16 | `list_attendance` | It loads every matching document into Python and sorts/slices in memory. | Performance and memory use degrade as the collection approaches 100,000 records. | Use MongoDB filters, count, indexed sort, skip and limit in the database. |
| 17 | `list_attendance` filters/order/serialization | Date-range order is not validated, ordering lacks the `emp_code` tie-breaker, and BSON instants/history are returned in the wrong representation. | Reversed date bounds, same-day records, or nested history timestamps fail the contract. | Validate inclusive date bounds, sort by date descending then code ascending, and serialize all instant fields as epoch milliseconds. |
| 18 | Remaining endpoints | Punch-out, regularization, all four analytics endpoints, and explain are absent. | Requests to documented paths return 404. | Implement the documented operations, aggregation pipelines, optimistic concurrency checks, and execution-statistics explain output. |

## Inspected and not considered defects

- Using `emp_code` as the business identifier is correct; MongoDB `_id` must remain internal.
- The basic elapsed-time idea in `compute_work_hours` was appropriate; its rounding method, not the duration formula, was defective.
- `load_dotenv()` without `override=True` is appropriate because real environment variables must take precedence over `.env` values.
