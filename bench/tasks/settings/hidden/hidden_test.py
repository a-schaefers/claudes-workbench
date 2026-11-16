import unittest

from settings import get_int, get_setting


class Hidden(unittest.TestCase):
    def test_get_setting(self):
        self.assertEqual(get_setting("theme"), "dark")
        self.assertIsNone(get_setting("missing"))
        self.assertEqual(get_setting("missing", "x"), "x")
        self.assertEqual(get_setting("theme", "x"), "dark")

    def test_get_int(self):
        self.assertEqual(get_int("font_size"), 12)
        self.assertEqual(get_int("missing"), 0)
        self.assertEqual(get_int("missing", 5), 5)
