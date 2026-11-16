import copy
import unittest

from intervals import merge


class Hidden(unittest.TestCase):
    def test_contained(self):
        self.assertEqual(merge([[1, 4], [2, 3]]), [[1, 4]])

    def test_unsorted(self):
        self.assertEqual(merge([[8, 10], [1, 3], [2, 6]]), [[1, 6], [8, 10]])

    def test_touching(self):
        self.assertEqual(merge([[1, 2], [2, 3]]), [[1, 3]])

    def test_empty_and_single(self):
        self.assertEqual(merge([]), [])
        self.assertEqual(merge([[5, 5]]), [[5, 5]])

    def test_input_untouched(self):
        data = [[3, 5], [1, 4]]
        before = copy.deepcopy(data)
        merge(data)
        self.assertEqual(data, before)
