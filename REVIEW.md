# REVIEW.md

| # | Where | What is wrong | How to notice | How fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace-period boundary is incorrect because elapsed minutes are truncated before applying the threshold. | A punch-in at 09:40:01 for a 09:30 shift should give 10 late minutes, not 0. | Compare elapsed seconds with 600 first, then floor elapsed minutes. |
| 2 | `compute_late_minutes` | Naive datetime replacement does not correctly handle UTC instants, IST, and overnight attendance dates. | Test punches around midnight and overnight shifts. | Convert instants to IST and calculate against the attendance day's shift start. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even rather than the required half-up rounding. | Test durations that round exactly halfway at two decimal places. | Use `Decimal` and `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Overtime is counted from zero minutes instead of applying the 30-minute minimum. Overnight shifts also need next-day shift-end handling. | Test 29 and 30 minutes after shift end, including overnight shifts. | Handle overnight end dates and count overtime only at 30 minutes or more. |
| 5 | `health` | It returns success without checking whether MongoDB is reachable. | Stop MongoDB and request `/health`. | Ping MongoDB and return 503 if unavailable. |
| 6 | `EmployeeIn` | Input types and formats are not sufficiently validated. | Submit malformed email, employee code, date or shift time. | Add Pydantic field constraints and date/time validation. |
| 7 | `create_employee` | Checking duplicates before inserting is vulnerable to concurrent duplicate requests. | Send simultaneous creates for the same employee code. | Create a unique database index and translate duplicate-key errors to 409. |
| 8 | `create_employee` | `created_at` is a naive local datetime rather than a UTC instant in the API's required epoch-millisecond format. | Inspect the creation timestamp and compare its type with the contract. | Store a UTC datetime and serialize it as epoch milliseconds. |
| 9 | `list_employees` | Pagination skips the first page because the offset uses `page * page_size`. | Request page 1 with page size 2. | Use `(page - 1) * page_size`. |
| 10 | `list_employees` | The total count ignores the department filter, and results are not sorted by employee code. | Compare filtered totals and ordering with the contract. | Count the filtered query and sort by `emp_code`. |
| 11 | `punch_in` | An unknown employee can cause a key error instead of a 404. | Punch in with an unknown employee code. | Return 404 before accessing employee shift fields. |
| 12 | `punch_in` | Timestamp conversion depends on local machine time and does not enforce the millisecond, range and whole-second rules. | Submit a seconds timestamp, invalid type or fractional-second instant. | Validate epoch milliseconds, convert explicitly to UTC and truncate to whole seconds. |
| 13 | `punch_in` | Attendance date is derived from the machine's local date and does not account for overnight shift rules. | Punch into an overnight shift around midnight IST. | Derive the attendance date from the IST punch-in instant and shift. |
| 14 | `punch_in` | A check-then-insert sequence alone cannot prevent simultaneous duplicate punch-ins. | Send two concurrent punch-ins for the same employee/date. | Enforce a unique `(emp_code, date)` index and return 409 on duplicate key. |
| 15 | `punch_in` | The starter accepts arbitrary status strings. | Submit `ABSENT` or an unknown status to punch-in. | Restrict punch-in to PRESENT, WFH and ON_DUTY. |
| 16 | Attendance responses | The starter exposes an attendance `id`, although the contract addresses records by employee code and date and excludes internal IDs. | Inspect a listing or punch-in response. | Return the contract's fields without an attendance ID. |
| 17 | `list_attendance` | It loads and sorts all matching records in Python and applies pagination in memory. | Exercise the endpoint with a large dataset. | Apply filters, sorting, skip and limit in MongoDB. |
| 18 | `list_attendance` | Date range and pagination inputs are insufficiently validated, and sorting lacks the required employee-code tie-breaker. | Test reversed dates, invalid pages and equal dates. | Validate parameters, enforce page limits and sort by date descending then employee code ascending. |
| 19 | Index setup | The starter does not create indexes at startup. | Inspect indexes in a freshly seeded database and inspect query plans. | Create required indexes idempotently at startup. |
| 20 | Missing endpoints | Punch-out, regularization, all analytics endpoints and the explain endpoint are absent. | Request each endpoint from the contract. | Implement the required endpoints and their validation, aggregation and response behavior. |

## Reviewed and not considered defects

- The client-supplied `emp_code` is the employee identifier; MongoDB `_id` is not a replacement.
- The use of MongoDB collections `employees` and `attendance_logs` matches the supplied data model.
- The default API port and FastAPI entry point can follow the provided run command.