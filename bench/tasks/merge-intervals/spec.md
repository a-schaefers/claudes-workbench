Bug report: `merge([[1, 4], [2, 3]])` in intervals.py returns `[[1, 3]]`; it should return
`[[1, 4]]`. Users suspect it has more bugs than this one.

Find and fix every way `merge()` disagrees with its docstring, and add regression tests for
each bug you fix to test_intervals.py.
