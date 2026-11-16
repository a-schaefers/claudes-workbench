import io
import unittest
from contextlib import redirect_stdout

import report


class ReportTest(unittest.TestCase):
    def test_total(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(report.main(["sales.csv"]), 0)
        self.assertEqual(out.getvalue(), "total 39.25\n")
