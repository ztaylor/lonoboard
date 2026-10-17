import unittest

import helpers  # noqa: F401
from lonoboard.board import logo, render
from lonoboard.board.render import Screen, encode


class FakeSession:
    def __init__(self, kind="telnet", utf8=True, colors="theme", ansi_bbs=False):
        self.kind, self.utf8, self.colors, self.ansi_bbs = kind, utf8, colors, ansi_bbs
        self.prefs = {"pal": "vga", "ice": True}
        self.prev, self.attr, self.pending, self.title = None, None, [], None


class TestScreen(unittest.TestCase):
    def test_put_clips_and_returns_next_x(self):
        s = Screen(5)
        self.assertEqual(s.put(3, 0, "abcdef"), 9)
        self.assertEqual("".join(c[0] for c in s.rows[0]), "   ab")

    def test_blit_and_link(self):
        a, b = Screen(10), Screen(3)
        b.put(0, 0, "xyz", 14)
        a.blit(b, 2, 1)
        a.link(1, 2, 5, "k:key:s")
        self.assertEqual(a.rows[1][2], ("x", 14, 0, "k:key:s"))

    def test_fit_and_pad(self):
        self.assertEqual(render.fit("abcdef", 4), "abc»")
        self.assertEqual(render.padl("7", 3), "  7")
        self.assertEqual(render.dur(3725), "1h 02m")
        self.assertEqual(render.dur(None), "-")


class TestEncode(unittest.TestCase):
    def frame(self, cells, w=10, h=2):
        s = Screen(w)
        for y in range(h):
            s.row(y)
        for x, y, ch, f, b in cells:
            s.put(x, y, ch, f, b)
        return s

    def test_cga_blue_is_ansi_44(self):
        sess = FakeSession(colors="theme")
        out = encode(sess, self.frame([(0, 0, "A", 15, 1)]), None)
        self.assertIn("\x1b[0;97;44m", out)          # CGA 1 (blue) must be ANSI 4, not 1 (red)

    def test_exact_colours_are_24_bit(self):
        out = encode(FakeSession(colors="exact"), self.frame([(0, 0, "A", 15, 1)]), None)
        self.assertIn("38;2;255;255;255;48;2;0;0;170", out)

    def test_256_colours(self):
        out = encode(FakeSession(colors="256"), self.frame([(0, 0, "A", 11, 0)]), None)
        self.assertIn("38;5;87", out)

    def test_ansi_bbs_uses_bold_and_ice(self):
        out = encode(FakeSession(utf8=False, ansi_bbs=True), self.frame([(0, 0, "A", 14, 9)]), None)
        self.assertIn("\x1b[0;1;33;5;44m", out)      # bright yellow fg, bright blue bg via iCE blink bit

    def test_second_frame_sends_only_changes(self):
        sess = FakeSession()
        encode(sess, self.frame([(0, 0, "A", 7, 0)]), None)
        out = encode(sess, self.frame([(0, 0, "A", 7, 0), (5, 1, "Z", 7, 0)]), None)
        self.assertIn("\x1b[2;6H", out)
        self.assertNotIn("\x1b[2J", out)
        self.assertEqual(encode(sess, self.frame([(0, 0, "A", 7, 0), (5, 1, "Z", 7, 0)]), None), "")

    def test_bottom_right_cell_is_never_written(self):
        out = encode(FakeSession(), self.frame([(9, 1, "X", 7, 0)]), None)
        self.assertNotIn("X", out)

    def test_links_become_osc8_for_web(self):
        s = self.frame([])
        s.put(0, 0, "go", 7, 0, "k:svc:2")
        out = encode(FakeSession(kind="web"), s, None)
        self.assertIn("\x1b]8;;lonoboard:svc:2\x1b\\", out)


class TestLogo(unittest.TestCase):
    def test_every_glyph_is_10_rows_of_equal_width(self):
        for ch, g in logo.G.items():
            self.assertEqual(len(g), 10, ch)
            self.assertEqual(len({len(r) for r in g}), 1, ch)

    def test_counters_are_not_shaded(self):
        w, h, P = logo.logo([("O", "A")], 2)
        # inside the O (row 4, columns 2-5) stays empty: the shadow only falls outside
        self.assertTrue(all(P[4][i] is None for i in range(2, 6)))
        self.assertTrue(any(P[y][x] == ("S",) for y in range(h) for x in range(w)))

    def test_hostname_characters_are_covered(self):
        for ch in "abcdefghijklmnopqrstuvwxyz0123456789-._":
            self.assertIn(ch.upper(), logo.G)


if __name__ == "__main__":
    unittest.main()
