import unittest

from cache import LRUCache


class Hidden(unittest.TestCase):
    def test_capacity(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                LRUCache(bad)

    def test_update_refreshes_and_does_not_grow(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        c.put("a", 10)
        self.assertEqual(len(c), 2)
        c.put("c", 3)
        self.assertNotIn("b", c)
        self.assertEqual(c.get("a"), 10)

    def test_contains_does_not_refresh(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        self.assertIn("a", c)
        c.put("c", 3)
        self.assertNotIn("a", c)

    def test_default_and_capacity_one(self):
        c = LRUCache(1)
        self.assertEqual(c.get("x", 7), 7)
        c.put("x", 1)
        c.put("y", 2)
        self.assertEqual((len(c), c.get("x"), c.get("y")), (1, None, 2))
