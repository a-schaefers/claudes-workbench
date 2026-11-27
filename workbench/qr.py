"""QR codes (ISO/IEC 18004), standard library only, for the web app's phone link (#1552,
2026-11-26): carrying a 40-character token to a phone by hand was the trip the app's main job
began with, so `wb chat url` draws the link in half-block characters at a terminal and the server
serves it as an SVG (GET /api/phone-qr.svg). Byte mode, error correction level M (15% of the
code can be lost), versions 1 to 40, the mask with the lowest penalty. No other mode or level:
a link is bytes, and M is what phone cameras read off a screen well.

`encode(data)` gives the modules (rows of bools, True dark, no quiet zone); `svg` and
`half_blocks` draw them."""
from __future__ import annotations

# Level M's blocks per version (1-40): (EC codewords per block, [(blocks, data codewords each)]),
# from the standard's table 9. Their sizes sum to the version's codewords (a test checks it).
M_BLOCKS = (
    (10, [(1, 16)]), (16, [(1, 28)]), (26, [(1, 44)]), (18, [(2, 32)]), (24, [(2, 43)]),
    (16, [(4, 27)]), (18, [(4, 31)]), (22, [(2, 38), (2, 39)]), (22, [(3, 36), (2, 37)]),
    (26, [(4, 43), (1, 44)]), (30, [(1, 50), (4, 51)]), (22, [(6, 36), (2, 37)]),
    (22, [(8, 37), (1, 38)]), (24, [(4, 40), (5, 41)]), (24, [(5, 41), (5, 42)]),
    (28, [(7, 45), (3, 46)]), (28, [(10, 46), (1, 47)]), (26, [(9, 43), (4, 44)]),
    (26, [(3, 44), (11, 45)]), (26, [(3, 41), (13, 42)]), (26, [(17, 42)]), (28, [(17, 46)]),
    (28, [(4, 47), (14, 48)]), (28, [(6, 45), (14, 46)]), (28, [(8, 47), (13, 48)]),
    (28, [(19, 46), (4, 47)]), (28, [(22, 45), (3, 46)]), (28, [(3, 45), (23, 46)]),
    (28, [(21, 45), (7, 46)]), (28, [(19, 47), (10, 48)]), (28, [(2, 46), (29, 47)]),
    (28, [(10, 46), (23, 47)]), (28, [(14, 46), (21, 47)]), (28, [(14, 46), (23, 47)]),
    (28, [(12, 47), (26, 48)]), (28, [(6, 47), (34, 48)]), (28, [(29, 46), (14, 47)]),
    (28, [(13, 46), (32, 47)]), (28, [(40, 47), (7, 48)]), (28, [(18, 47), (31, 48)]),
)
LEVEL_M = 0b00  # the format information's two bits for level M


def capacity(version: int) -> int:
    """The most bytes ``version`` holds in byte mode at level M."""
    return (data_codewords(version) * 8 - 4 - count_bits(version)) // 8


def data_codewords(version: int) -> int:
    return sum(n * k for n, k in M_BLOCKS[version - 1][1])


def count_bits(version: int) -> int:
    """The byte mode's character count field: 8 bits through version 9, 16 after."""
    return 8 if version < 10 else 16


def raw_modules(version: int) -> int:
    """The modules left for data and error correction once the function patterns are drawn."""
    n = (16 * version + 128) * version + 64
    if version >= 2:
        k = version // 7 + 2
        n -= (25 * k - 10) * k - 55
        if version >= 7:
            n -= 36
    return n


def alignment_positions(version: int) -> list[int]:
    """The rows (and columns) of the alignment patterns' centres (the standard's annex E)."""
    if version == 1:
        return []
    k = version // 7 + 2
    step = (version * 8 + k * 3 + 5) // (k * 4 - 4) * 2
    size = version * 4 + 17
    return [6] + sorted(size - 7 - i * step for i in range(k - 1))


# ---- Reed-Solomon over GF(256), x^8 + x^4 + x^3 + x^2 + 1 ----
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x = (_x << 1) ^ (0x11D if _x & 0x80 else 0)
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def rs_generator(degree: int) -> list[int]:
    """The generator polynomial (x - a^0)...(x - a^(degree-1)), highest power first, its leading 1 left out."""
    g = [1]
    for i in range(degree):
        g = [a ^ gf_mul(b, EXP[i]) for a, b in zip(g + [0], [0] + g)]
    return g[1:]


def rs_remainder(data: list[int], degree: int) -> list[int]:
    """``degree`` error correction codewords for ``data``."""
    gen = rs_generator(degree)
    rem = [0] * degree
    for b in data:
        factor = b ^ rem.pop(0)
        rem.append(0)
        for i, g in enumerate(gen):
            rem[i] ^= gf_mul(g, factor)
    return rem


def codewords(data: bytes, version: int) -> list[int]:
    """The final sequence: the data bits in codewords, padded, split into blocks, each block's
    error correction, interleaved."""
    bits = [0, 1, 0, 0]  # byte mode
    bits += [(len(data) >> i) & 1 for i in reversed(range(count_bits(version)))]
    for b in data:
        bits += [(b >> i) & 1 for i in reversed(range(8))]
    room = data_codewords(version) * 8
    bits += [0] * min(4, room - len(bits))  # the terminator
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    words += [(0xEC, 0x11)[i % 2] for i in range(room // 8 - len(words))]
    ec, groups = M_BLOCKS[version - 1]
    blocks, at = [], 0
    for n, k in groups:
        for _ in range(n):
            blocks.append(words[at:at + k])
            at += k
    eccs = [rs_remainder(b, ec) for b in blocks]
    out = [b[i] for i in range(max(len(b) for b in blocks)) for b in blocks if i < len(b)]
    return out + [e[i] for i in range(ec) for e in eccs]


# ---- the symbol ----
def format_bits(mask: int) -> int:
    """Level M and ``mask``'s 15 format bits: BCH(15,5), then XOR 101010000010010."""
    data = LEVEL_M << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


def version_bits(version: int) -> int:
    """``version``'s 18 version bits (7 and up): BCH(18,6)."""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return version << 12 | rem


MASKS = (
    lambda y, x: (x + y) % 2 == 0,
    lambda y, x: y % 2 == 0,
    lambda y, x: x % 3 == 0,
    lambda y, x: (x + y) % 3 == 0,
    lambda y, x: (x // 3 + y // 2) % 2 == 0,
    lambda y, x: x * y % 2 + x * y % 3 == 0,
    lambda y, x: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda y, x: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class _Symbol:
    def __init__(self, version: int):
        self.version, self.size = version, version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]  # function patterns
        self.functions()

    def put(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x], self.fixed[y][x] = dark, True

    def functions(self) -> None:
        size = self.size
        for i in range(size):  # the timing patterns
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # the finders and their separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if 0 <= cx + dx < size and 0 <= cy + dy < size:
                        self.put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
        pos = alignment_positions(self.version)
        for i, cy in enumerate(pos):
            for j, cx in enumerate(pos):
                if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                    continue  # under a finder
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.format(0)  # reserved now, drawn with the chosen mask
        if self.version >= 7:
            bits = version_bits(self.version)
            for i in range(18):
                a, b = size - 11 + i % 3, i // 3
                self.put(a, b, bits >> i & 1 == 1)
                self.put(b, a, bits >> i & 1 == 1)

    def format(self, mask: int) -> None:
        bits, size = format_bits(mask), self.size
        bit = [bits >> i & 1 == 1 for i in range(15)]
        for i in range(6):
            self.put(8, i, bit[i])
        self.put(8, 7, bit[6])
        self.put(8, 8, bit[7])
        self.put(7, 8, bit[8])
        for i in range(9, 15):
            self.put(14 - i, 8, bit[i])
        for i in range(8):
            self.put(size - 1 - i, 8, bit[i])
        for i in range(8, 15):
            self.put(8, size - 15 + i, bit[i])
        self.put(8, size - 8, True)  # the dark module

    def data(self, words: list[int]) -> None:
        """The codewords in the zigzag, two columns at a time from the bottom right, skipping
        the vertical timing column; remainder bits stay light."""
        bits = [w >> (7 - i) & 1 == 1 for w in words for i in range(8)]
        at, size = 0, self.size
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            up = (right + 1) & 2 == 0
            for v in range(size):
                y = size - 1 - v if up else v
                for x in (right, right - 1):
                    if not self.fixed[y][x] and at < len(bits):
                        self.dark[y][x] = bits[at]
                        at += 1
            right -= 2

    def masked(self, mask: int) -> list[list[bool]]:
        test = MASKS[mask]
        return [[d != (not f and test(y, x)) for x, (d, f) in enumerate(zip(row, fixed))]
                for y, (row, fixed) in enumerate(zip(self.dark, self.fixed))]


def penalty(m: list[list[bool]]) -> int:
    """The standard's four rules: runs of five or more, 2x2 blocks, finder-like 1:1:3:1:1 runs
    with four light modules beside them (the quiet zone counts as light), and the dark share's
    distance from half."""
    size, score = len(m), 0
    lines = ["".join("1" if d else "0" for d in row) for row in m]
    lines += ["".join(row[x] for row in lines[:size]) for x in range(size)]
    for line in lines:
        run, prev = 0, ""
        for c in line + "x":
            if c == prev:
                run += 1
                continue
            if run >= 5:
                score += run - 2
            run, prev = 1, c
        padded = "0000" + line + "0000"
        for pat in ("10111010000", "00001011101"):
            at = padded.find(pat)
            while at != -1:
                score += 40
                at = padded.find(pat, at + 1)
    for y in range(size - 1):
        a, b = m[y], m[y + 1]
        for x in range(size - 1):
            if a[x] == a[x + 1] == b[x] == b[x + 1]:
                score += 3
    dark = sum(map(sum, m))
    total = size * size
    score += (abs(dark * 20 - total * 10) + total - 1) // total * 10 - 10 if dark * 2 != total else 0
    return score


def encode(data: bytes | str, mask: int | None = None) -> list[list[bool]]:
    """``data``'s QR code at level M in the smallest version that holds it: rows of modules,
    True dark, no quiet zone. ``mask`` forces one (0-7); else the lowest penalty's.
    ValueError past version 40's 2,331 bytes."""
    data = data.encode() if isinstance(data, str) else bytes(data)
    version = next((v for v in range(1, 41) if len(data) <= capacity(v)), None)
    if version is None:
        raise ValueError(f"{len(data)} bytes don't fit a QR code (at most {capacity(40)} at level M)")
    sym = _Symbol(version)
    sym.data(codewords(data, version))
    best = None
    for k in (range(8) if mask is None else (mask,)):
        sym.format(k)
        m = sym.masked(k)
        score = penalty(m) if mask is None else 0
        if best is None or score < best[0]:
            best = (score, m)
    return best[1]


def svg(data: bytes | str, border: int = 4, module: int = 8) -> str:
    """The code as an SVG image: one path of dark squares on a white square, ``border`` modules
    of quiet zone (the standard's 4), ``module`` px each at its natural size."""
    m = encode(data)
    n = len(m) + 2 * border
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(m)
                   for x, d in enumerate(row) if d)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * module}" '
            f'height="{n * module}" shape-rendering="crispEdges"><rect width="{n}" height="{n}" '
            f'fill="#fff"/><path d="{path}" fill="#000"/></svg>\n')


def half_blocks(data: bytes | str, border: int = 2, ansi: bool = True) -> str:
    """The code in text, two module rows per line (the upper and lower half blocks). With
    ``ansi``, each line is black on bright white, whatever the terminal's own colours: a dark
    theme otherwise drew it inverted, which many phone cameras don't read."""
    m = encode(data)
    n = len(m) + 2 * border
    blank = [False] * n
    rows = [blank] * border + [[False] * border + r + [False] * border for r in m] + [blank] * border
    if len(rows) % 2:
        rows.append(blank)
    out = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        line = "".join(" ▄▀█"[t * 2 + b] for t, b in zip(top, bottom))
        out.append(f"\x1b[30;107m{line}\x1b[0m" if ansi else line)
    return "\n".join(out)
