Add grouping to the sales report in report.py.

- New function `totals_by(rows, field)`: a list of `(value, total)` pairs, one per distinct value
  of `field`, where total is the sum of `amount` for those rows. Order: total descending, then
  value ascending for equal totals.
- New CLI option `--by FIELD`: before the existing `total` line, print one line per group as
  `<value> <total with 2 decimals>`, in that order. Without `--by` the output is unchanged.
- If FIELD is not a column of the CSV, print `unknown field: FIELD` to stderr and exit with
  status 2, printing nothing to stdout.

Extend test_report.py to cover the new behaviour (sales.csv is a sample input).
