import unittest

from settings import get_setting


class SettingsTest(unittest.TestCase):
    def test_existing(self):
        self.assertEqual(get_setting("theme"), "dark")
