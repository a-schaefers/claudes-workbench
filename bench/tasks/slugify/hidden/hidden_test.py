import unittest

from text_utils import slugify


class Hidden(unittest.TestCase):
    def test_cases(self):
        cases = {"  Hello,  World! ": "hello-world", "A--B": "a-b", "---": "", "": "",
                 "Already-slug": "already-slug", "C++ & Rust 2": "c-rust-2", "x": "x"}
        for raw, want in cases.items():
            self.assertEqual(slugify(raw), want, raw)
