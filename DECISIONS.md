# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index defines employee identity. Unique `attendance_logs(emp_code, date)` prevents duplicate daily records. Date/code and status/date/code indexes support attendance filters and ordering; code/punch-in finds the latest punch. Department/code supports filtered employee lists, while department/joined-on and joined-on/department support headcount lookups with or without a department filter. I rejected a `late_minutes`-only index because the leaderboard filters by date then groups records.

2. **Punch-in race.** Both requests may reach the insert, but the unique compound index allows only one to succeed. The winner returns 201; the duplicate-key error becomes HTTP 409 for the other.

3. **Ties.** MongoDB `$rank` gives equal totals the same rank and skips the next rank. The limit is applied after ranking, so everyone tied at a qualifying rank is returned. Employee code gives stable ordering.

4. **Headcount.** The department summary starts from employees who joined by month-end, looks up logs, and preserves employees with no logs. It counts distinct employee codes, not attendance documents.

5. **100x data.** I would measure query plans and write volume first, then consider precomputed monthly summaries or partitioning if they became bottlenecks.
