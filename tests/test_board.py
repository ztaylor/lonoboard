"""The board end to end on demo data: every screen renders at several sizes, and the keys work."""
import time
import unittest

import helpers
from lonoboard.board import Board, Session
from lonoboard.board.screens import SCREENS
from lonoboard.demo import DemoCollector


def text(scr):
    return "\n".join("".join(c[0] for c in row) for row in scr.rows)


def scr_status(scr):
    return "".join(c[0] for c in scr.rows[-1])


class BoardCase(unittest.TestCase):
    password = ""

    @classmethod
    def setUpClass(cls):
        cls.cfg = helpers.config(password=cls.password)
        cls.collector = DemoCollector(cls.cfg, seed=1)
        cls.board = Board(cls.collector, cls.cfg)
        deadline = time.time() + 5
        while cls.board.hub.data is None and time.time() < deadline:
            time.sleep(0.05)
        cls.board.hub.load_callers()
        cls.board.hub.load_usage()

    def session(self, cols=80, rows=25, kind="telnet"):
        s = Session(self.board, kind, cols, rows, utf8=True, intro=False)
        s.resize(cols, rows)
        return s

    def press(self, s, *keys):
        for k in keys:
            s.handle(k)
        return s.render()[0]


class TestScreens(BoardCase):
    def test_every_screen_at_every_size(self):
        for cols, rows in ((80, 25), (160, 50), (46, 30), (120, 40)):
            s = self.session(cols, rows)
            for name, key, _ in SCREENS:
                scr = self.press(s, key)
                self.assertEqual(len(scr.rows), rows, (name, cols))
                self.assertEqual(s.screen, name)
                body = text(scr)
                self.assertIn("SCREEN", body)

    def test_main_has_the_logo_menu_and_status(self):
        body = text(self.session(160, 50).render()[0])
        self.assertIn("main menu", body)
        self.assertIn("2x rtx 3090", body)
        self.assertIn("6/6 UP", body)

    def test_every_command_shows_at_every_size(self):
        for cols, rows in ((80, 25), (46, 30), (120, 40), (160, 50)):
            s = self.session(cols, rows)
            for name, key, _ in SCREENS:
                scr = self.press(s, key)
                footer = "\n".join(text(scr).splitlines()[s.vh:])
                want = ["quit", "screens", "help", "display", "C chat"]
                want += ["select"] if name in ("main", "services", "fleet") else []
                want += ["page"] if s.pages > 1 else []
                for w in want:
                    self.assertIn(w, footer, (name, cols, rows))
                self.assertIn("SCREEN", scr_status(scr), (name, cols, rows))

    def test_chat_is_on_the_screen_bar(self):
        for cols in (80, 160):
            s = self.session(cols, 25)
            scr = s.render()[0]
            bar = next(y for y, r in enumerate(scr.rows) if any(c[3] == "k:key:m" for c in r))
            self.assertIn("C chat", text(scr).splitlines()[bar], cols)
            self.assertFalse(any(c[3] == "k:key:m" for c in scr.rows[bar + 1]), cols)   # still one row at 80 columns
            s.prev = scr.rows
            x = next(x for x, c in enumerate(scr.rows[bar]) if c[3] == "k:key:c")
            s.handle(("mouse", (0, x + 1, bar + 1, True)))
            self.assertEqual(s.modal[0], "chat")

    def test_flipping_and_paging(self):
        s = self.session()
        self.press(s, "right", "right")
        self.assertEqual(s.screen, "host")
        self.press(s, "left")
        self.assertEqual(s.screen, "services")
        self.press(s, "esc")
        self.assertEqual(s.screen, "main")
        self.press(s, "b")                       # back from the first page of main: the last screen
        self.assertEqual(s.screen, "fleet")

    def test_services_lightbar_and_details(self):
        s = self.session()
        self.press(s, "s", "down", "down", "enter")
        self.assertEqual(s.modal, ("svc", "vllm"))
        self.assertIn("vllm", text(s.render()[0]))
        self.press(s, "esc")
        self.assertIsNone(s.modal)

    def test_main_menu_enter_opens_the_entry(self):
        s = self.session()
        self.press(s, "down", "down", "enter")   # third entry: usage
        self.assertEqual(s.screen, "usage")

    def test_chat_streams_a_reply(self):
        s = self.session()
        self.press(s, "s", "enter", "c")         # chat from a service's details
        self.assertEqual(s.modal[0], "chat")
        for ch in "hello":
            s.handle(ch)
        s.handle("enter")
        chat = s.current_chat()
        deadline = time.time() + 15
        while chat.busy and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(chat.busy)
        self.assertTrue(chat.msgs[-1]["content"])
        self.press(s, "tab")
        self.assertNotEqual(s.current_chat(), chat)

    def test_quit_asks_first(self):
        s = self.session()
        self.press(s, "q")
        self.assertEqual(s.modal, ("quit", None))
        self.press(s, "n")
        self.assertTrue(s.alive)
        self.press(s, "q", "enter")
        self.assertFalse(s.alive)

    def test_web_sessions_cant_quit(self):
        s = self.session(kind="web")
        self.press(s, "q")
        self.assertIsNone(s.modal)

    def test_mouse_click_on_a_menu_entry(self):
        s = self.session(80, 25)
        self.press(s, "s")
        scr = s.render()[0]
        s.prev = scr.rows
        row = next(y for y, r in enumerate(scr.rows) if any(c[3] == "k:svc:2" for c in r))
        s.handle(("mouse", (0, 5, row + 1, True)))
        self.assertEqual(s.modal, ("svc", "vllm"))


class TestLogin(BoardCase):
    password = "letmein"

    def test_nothing_shows_before_login(self):
        s = self.session()
        body = text(s.render()[0])
        self.assertIn("password", body)
        self.assertNotIn("2x rtx 3090", body)
        self.assertNotIn("qwen", body)

    def test_wrong_then_right(self):
        s = self.session()
        for ch in "nope":
            s.handle(ch)
        s.handle("enter")
        self.assertTrue(s.locked)
        self.assertIn("2 tries left", s.login_error)
        for ch in "letmein":
            s.handle(ch)
        s.handle("enter")
        self.assertFalse(s.locked)
        self.assertIn("main menu", text(s.render()[0]))

    def test_three_strikes(self):
        s = self.session()
        for _ in range(3):
            s.handle("x")
            s.handle("enter")
        self.assertFalse(s.alive)


if __name__ == "__main__":
    unittest.main()
