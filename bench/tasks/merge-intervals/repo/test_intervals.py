import unittest

from intervals import merge


class MergeTest(unittest.TestCase):
    def test_disjoint(self):
        self.assertEqual(merge([[1, 2], [5, 6]]), [[1, 2], [5, 6]])
