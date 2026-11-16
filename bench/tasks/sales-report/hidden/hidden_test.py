import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import report


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = report.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class Hidden(unittest.TestCase):
    def test_by_region(self):
        code, out, _ = run(["sales.csv", "--by", "region"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "south 13.75\neast 12.75\nnorth 12.75\ntotal 39.25\n")

    def test_totals_by_ties_and_order(self):
        rows = [{"k": "b", "amount": "1"}, {"k": "a", "amount": "1"}, {"k": "c", "amount": "5"}]
        self.assertEqual(report.totals_by(rows, "k"), [("c", 5.0), ("a", 1.0), ("b", 1.0)])

    def test_unknown_field(self):
        code, out, err = run(["sales.csv", "--by", "colour"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unknown field: colour", err)

    def test_default_unchanged(self):
        self.assertEqual(run(["sales.csv"])[1], "total 39.25\n")
