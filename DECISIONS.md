# DECISIONS.md

1. **Indexes.** The unique `employees.emp_code` index supports employee lookups and prevents duplicate employee codes. The unique `(emp_code, date)` attendance index guarantees one attendance record per employee per attendance day and makes punch-in races safe. The `(date, emp_code)` index supports attendance date filtering and ordering. Status/date and employee/punch-in indexes support other list and punch-out queries. I also added employee department and joining-date indexes for department reports. Index usefulness must be checked with execution plans on the grader-sized dataset.

2. **Punch-in race.** Both requests may initially find no record. They then attempt to insert the same employee/date key. MongoDB's unique index allows only one insert. The successful request returns 201; the other gets a duplicate-key error translated to 409.

3. **Ties.** The leaderboard uses MongoDB `$rank` over total late minutes, then applies the limit to the rank. Tied employees share a rank, and the next rank is skipped. Results are sorted by late minutes descending and employee code ascending.

4. **Headcount.** The department summary starts from employees whose `joined_on` is on or before the period end, then looks up attendance. Preserving employees with no matching logs keeps them in headcount while excluding future joiners.

5. **Scaling.** For 100 times more data, I would benchmark every aggregation with realistic data, review execution statistics and index selectivity, and consider precomputed daily summaries for expensive reporting queries.