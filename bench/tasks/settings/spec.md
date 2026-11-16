In settings.py:

1. `get_setting(name)` raises KeyError for a missing setting. Give it an optional `default`
   parameter (default `None`) that is returned instead when the setting is missing.
2. Add `get_int(name, default=0)`: the setting converted to int, or `default` when missing.

Add tests for both to test_settings.py.
