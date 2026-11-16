`slugify()` in text_utils.py produces ugly slugs: `slugify("  Hello,  World! ")` gives
`"--hello---world--"` but should give `"hello-world"`.

Fix `slugify()` so a run of characters other than a-z and 0-9 becomes a single dash, with no
dash at the start or end. Add a regression test for the example above to test_text_utils.py.
