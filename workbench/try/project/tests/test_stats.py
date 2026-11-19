import unittest

from stats import mean


class Mean(unittest.TestCase):
    def test_the_mean_of_some_numbers(self):
        self.assertEqual(mean([1, 2, 3, 4]), 2.5)

    def test_no_numbers_have_no_mean(self):
        with self.assertRaises(ValueError):
            mean([])


if __name__ == "__main__":
    unittest.main()
