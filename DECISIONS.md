# Implementation decisions

1. **Indexes.** `employees.emp_code` is unique for identity and duplicate protection. `(department, joined_on)` supports department headcount; `(department, emp_code)` supports sorted department lists; `(joined_on, department)` supports headcount scans. `(emp_code, date)` is unique per employee-day and supports monthly queries. `(date DESC, emp_code ASC)` supports attendance pages. `(emp_code, date, status)` and `(status, date)` support filtered queries. I rejected a standalone `late_minutes` index because the month date filter is more selective.

2. **Punch-in race.** Both requests can pass employee lookup, but only one insert can satisfy the unique `(emp_code, date)` index. MongoDB rejects the other insert with duplicate key; that request becomes 409 while the committed insert returns 201.

3. **Ties.** `$rank` assigns equal totals the same rank. Filtering `rank <= limit` happens after ranking, so all tied employees at the cutoff are returned, ordered by employee code.

4. **Headcount.** The summary starts from employees who joined by month end and looks up their logs. Preserving employees without matching logs means they still contribute one to department headcount.

5. **100x data.** I would benchmark the explain plans, then shard or archive by date if a single indexed MongoDB deployment no longer met latency targets.
