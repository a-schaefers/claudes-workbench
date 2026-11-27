"""The QR encoder (workbench/qr.py, #1552): the phone link's code at a terminal and in Settings.
Held to the standard's own examples (Reed-Solomon, format and version bits, the block table) and to
whole symbols another encoder made, every mask among them (tests/fixtures/qr-qrencode.json, qrencode 4.1.1's). No decoder
here: the fixtures fix every module but the mask choice, which any reader takes from the format bits."""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workbench import qr  # noqa: E402


class QrCode(unittest.TestCase):
    def test_reed_solomon_gives_the_standards_error_correction_codewords_1552(self):
        # ISO/IEC 18004 annex I's "01234567", version 1-M, and the same for "HELLO WORLD"
        self.assertEqual(qr.rs_remainder([0x10, 0x20, 0x0C, 0x56, 0x61, 0x80, *[0xEC, 0x11] * 5], 10),
                         [0xA5, 0x24, 0xD4, 0xC1, 0xED, 0x36, 0xC7, 0x87, 0x2C, 0x55])
        self.assertEqual(qr.rs_remainder([32, 91, 11, 120, 209, 114, 220, 77, 67, 64, *[236, 17] * 3], 10),
                         [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_format_and_version_bits_are_the_standards_1552(self):
        # level M's eight format strings (annex C) and two version strings (annex D)
        self.assertEqual([format(qr.format_bits(k), "015b") for k in range(8)],
                         ["101010000010010", "101000100100101", "101111001111100", "101101101001011",
                          "100010111111001", "100000011001110", "100111110010111", "100101010100000"])
        self.assertEqual((qr.version_bits(7), qr.version_bits(40)), (0x07C94, 0x28C69))

    def test_every_versions_blocks_fill_its_codewords_and_byte_capacity_is_the_standards_1552(self):
        for v in range(1, 41):
            ec, groups = qr.M_BLOCKS[v - 1]
            self.assertEqual(sum(n * (k + ec) for n, k in groups), qr.raw_modules(v) // 8, v)
        self.assertEqual([qr.capacity(v) for v in (1, 2, 5, 10, 40)], [14, 26, 84, 213, 2331])
        self.assertEqual([qr.alignment_positions(v) for v in (1, 2, 7, 32, 40)],
                         [[], [6, 18], [6, 22, 38], [6, 34, 60, 86, 112, 138], [6, 30, 58, 86, 114, 142, 170]])

    def test_whole_symbols_match_another_encoders_module_for_module_1552(self):
        # a version 1 under each of the eight masks, and a version 7 (version bits, six alignment
        # patterns, four blocks)
        cases = json.loads((ROOT / "tests/fixtures/qr-qrencode.json").read_text())["codes"]
        self.assertEqual(sorted(c["mask"] for c in cases if len(c["rows"]) == 21), list(range(8)))
        for case in cases:
            m = qr.encode(case["text"], mask=case["mask"])
            size = len(case["rows"])
            self.assertEqual(len(m), size)
            self.assertEqual([format(int("".join("1" if d else "0" for d in row), 2), "x") for row in m],
                             case["rows"], case["text"])

    def test_the_smallest_version_that_holds_the_bytes_is_used_and_too_many_are_refused_1552(self):
        self.assertEqual(len(qr.encode(b"x" * 14)), 21)
        self.assertEqual(len(qr.encode(b"x" * 15)), 25)
        self.assertEqual(len(qr.encode(b"x" * 2331)), 177)
        with self.assertRaisesRegex(ValueError, "at most 2331"):
            qr.encode(b"x" * 2332)

    def test_the_svg_and_the_half_blocks_draw_every_dark_module_with_a_quiet_zone_1552(self):
        link = "https://box.example-tail.ts.net/#token=abc"
        m = qr.encode(link)
        n = len(m)
        svg = qr.svg(link)
        self.assertIn(f'viewBox="0 0 {n + 8} {n + 8}"', svg)
        self.assertEqual(len(re.findall(r"M\d+,\d+h1v1h-1z", svg)), sum(map(sum, m)))
        self.assertIn('fill="#fff"', svg)                                # light under it, whatever the page
        text = qr.half_blocks(link, ansi=False).splitlines()
        self.assertEqual(len(text), (n + 4 + 1) // 2)
        self.assertEqual({len(line) for line in text}, {n + 4})
        self.assertEqual(text[0].strip(), "")                            # the quiet zone above
        self.assertEqual(text[1][2:2 + 7], "█▀▀▀▀▀█")                    # a finder's top edge
        # at a terminal black on white, both set: a dark theme drew it inverted
        colored = qr.half_blocks(link).splitlines()
        self.assertTrue(all(c.startswith("\x1b[30;107m") and c.endswith("\x1b[0m") for c in colored))


if __name__ == "__main__":
    unittest.main()
