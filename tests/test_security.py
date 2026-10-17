"""The board facing a network: other web sites, bad clients, hostile data."""
import socket
import time
import unittest
import urllib.error
import urllib.request

import helpers
from lonoboard import config, web
from lonoboard.board import Board, Session
from lonoboard.board.render import Screen, link_target
from lonoboard.board.session import clean_prefs
from lonoboard.board.telnet import serve as telnet_serve
from lonoboard.cli import App
from lonoboard.demo import DemoCollector


def start(**board):
    cfg = helpers.config(intro=False, port=0, telnet_port=0, **board)
    collector = DemoCollector(cfg, seed=3)
    b = Board(collector, cfg)
    http = web.serve(App(collector, b), "127.0.0.1", 0, background=True)
    return http, b


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, dict(e.headers), e.read()


class TestOtherSites(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http, cls.board = start()
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"

    def test_no_cross_origin_reads(self):
        _, headers, _ = get(self.base + "/api")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_a_rebound_domain_is_refused_without_a_password(self):
        status, _, body = get(self.base + "/api", {"Host": "evil.example.com"})
        self.assertEqual(status, 403)
        self.assertIn(b"board.hosts", body)
        status, _, _ = get(self.base + "/", {"Host": "evil.example.com", "Accept": "text/html"})
        self.assertEqual(status, 403)

    def test_local_names_are_fine(self):
        for host in ("127.0.0.1:7777", "[::1]:7777", "localhost", "spark-1:7777", "spark-1.tail1234.ts.net",
                     "gpu-box.local", socket.gethostname()):
            self.assertEqual(get(self.base + "/api", {"Host": host})[0], 200, host)

    def test_health_answers_any_host(self):
        self.assertEqual(get(self.base + "/health", {"Host": "evil.example.com"})[0], 200)


class TestHostRules(unittest.TestCase):
    def test_board_hosts_and_passwords(self):
        cfg = helpers.config(hosts=["board.example.com"])
        self.assertTrue(config.host_allowed("board.example.com:443", cfg))
        self.assertFalse(config.host_allowed("other.example.com", cfg))
        self.assertTrue(config.host_allowed("other.example.com", helpers.config(password="pw")))
        self.assertTrue(config.host_allowed("other.example.com", helpers.config(allow_anonymous=True)))
        self.assertFalse(config.host_allowed("", cfg))


class TestPasswordGuessing(unittest.TestCase):
    def test_too_many_wrong_passwords_lock_the_address_out(self):
        http, _ = start(password="right")
        base = f"http://127.0.0.1:{http.server_address[1]}"
        http.guesses.failed["127.0.0.1"] = [time.time()] * web.GUESSES
        self.assertEqual(get(base + "/api", {"Authorization": "Bearer right"})[0], 429)
        http.guesses.failed.clear()
        self.assertEqual(get(base + "/api", {"Authorization": "Bearer right"})[0], 200)
        t0 = time.time()
        self.assertEqual(get(base + "/api", {"Authorization": "Bearer wrong"})[0], 401)
        self.assertGreaterEqual(time.time() - t0, 0.9)                 # a wrong guess costs a second


class TestBadClients(unittest.TestCase):
    def test_telnet_window_sizes_are_clamped(self):
        _, board = start()
        for cols, rows in ((65535, 65535), (0, 0)):
            s = Session(board, "telnet", cols, rows, intro=False)
            self.assertTrue(40 <= s.cols <= 320 and 12 <= s.rows <= 200, (s.cols, s.rows))

    def test_endless_subnegotiation_is_capped(self):
        _, board = start()
        tel = telnet_serve(board, "127.0.0.1", 0)
        s = socket.create_connection(("127.0.0.1", tel.server_address[1]), timeout=3)
        s.sendall(bytes([255, 250, 24]) + b"x" * 100000)
        time.sleep(0.3)
        s.close()                                                      # the server kept at most SB_MAX bytes

    def test_negative_content_length(self):
        http, _ = start()
        s = socket.create_connection(("127.0.0.1", http.server_address[1]), timeout=3)
        s.sendall(b"POST /term/session HTTP/1.0\r\nHost: localhost\r\nContent-Length: -1\r\n\r\n")
        self.assertIn(b"400", s.recv(200))
        s.close()

    def test_browser_prefs_are_checked(self):
        self.assertEqual(clean_prefs({"pal": "amber", "font": "9x16", "crt": False, "ice": "yes", "x": 1}),
                         {"pal": "amber", "font": "9x16", "crt": False})
        self.assertEqual(clean_prefs({"pal": "<script>"}), {})
        self.assertEqual(clean_prefs("nonsense"), {})


class TestHostileData(unittest.TestCase):
    def test_escape_sequences_never_reach_the_screen(self):
        s = Screen(40)
        s.put(0, 0, "evil\x1b]52;c;cGF3bmVk\x07\u202ename\x9b")
        text = "".join(c[0] for c in s.rows[0]).rstrip()
        self.assertEqual(text, "evil]52;c;cGF3bmVkname")

    def test_only_web_links(self):
        class S:
            kind, utf8 = "web", True
        self.assertEqual(link_target(S, "u:http://spark-1:8080/v1"), "http://spark-1:8080/v1")
        self.assertIsNone(link_target(S, "u:javascript:alert(1)"))
        self.assertIsNone(link_target(S, "u:http://x\x1b]8;;"))

    def test_the_password_only_goes_to_listed_peers(self):
        cfg = helpers.config(password="pw")
        cfg["fleet"]["peers"] = ["spark-1"]
        board = Board(DemoCollector(cfg, seed=4), cfg)
        self.assertEqual(board.remote("spark-1", "http://spark-1:7777").auth, {"Authorization": "Bearer pw"})
        self.assertEqual(board.remote("stranger", "http://stranger:7777").auth, {})


if __name__ == "__main__":
    unittest.main()
