import unittest

from text_utils import slugify


class SlugifyTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(slugify("Hello"), "hello")
