"""Telnet, HTTP and the command line, against a demo board on free ports."""
import base64
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

import helpers
from lonoboard import web
from lonoboard.board import Board
from lonoboard.board.telnet import serve as telnet_serve
from lonoboard.cli import App
from lonoboard.demo import DemoCollector

IAC, DO, WILL, SB, SE = 255, 253, 251, 250, 240


def start(password=""):
    cfg = helpers.config(password=password, intro=False, port=0, telnet_port=0)
    collector = DemoCollector(cfg, seed=2)
    board = Board(collector, cfg)
    app = App(collector, board)
    http = web.serve(app, "127.0.0.1", 0, background=True)
    tel = telnet_serve(board, "127.0.0.1", 0)
    return http, tel, board


class TestTelnet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http, cls.tel, cls.board = start()

    def connect(self, ttype=b"xterm-256color", cols=80, rows=25):
        s = socket.create_connection(("127.0.0.1", self.tel.server_address[1]), timeout=5)
        buf, t0 = b"", time.time()
        while time.time() - t0 < 3:
            try:
                d = s.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            if bytes([IAC, DO, 31]) in d:
                s.sendall(bytes([IAC, WILL, 31, IAC, SB, 31, 0, cols, 0, rows, IAC, SE]))
            if bytes([IAC, DO, 24]) in d:
                s.sendall(bytes([IAC, WILL, 24]))
            if bytes([IAC, SB, 24, 1, IAC, SE]) in d:
                s.sendall(bytes([IAC, SB, 24, 0]) + ttype + bytes([IAC, SE]))
            buf += d
            if b"SCREEN 1/" in buf:
                break
            s.settimeout(1)
        return s, buf

    def test_negotiates_and_draws(self):
        s, buf = self.connect()
        text = buf.decode("utf-8", "replace")
        self.assertIn("\x1b[?1049h", text)              # alternate screen for modern terminals
        self.assertIn("SCREEN 1/", text)
        self.assertIn("38;5;", text)                    # *-256color gets the 256-colour palette
        s.sendall(b"q\r")
        rest = b""
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                rest += d
        except socket.timeout:
            pass
        s.close()
        self.assertIn(b"\x1b[?1049l", rest)             # terminal restored on the way out
        self.assertIn(b"disconnected from", rest)

    def test_unknown_terminal_gets_cp437(self):
        s, buf = self.connect(ttype=b"ansi")
        self.assertNotIn(b"\x1b[?1049h", buf)
        self.assertIn(b"\xdb", buf)                     # CP437 full block, not UTF-8
        s.close()


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http, cls.tel, cls.board = start()
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def test_health_identifies_lonoboard(self):
        _, _, body = self.get("/health")
        self.assertEqual(json.loads(body)["app"], "lonoboard")

    def test_browser_gets_the_board_curl_gets_json(self):
        _, ctype, body = self.get("/", {"Accept": "text/html"})
        self.assertIn("text/html", ctype)
        self.assertIn(b"<title>", body)
        _, ctype, body = self.get("/")
        self.assertEqual(json.loads(body)["host"]["platform"], "2x rtx 3090")

    def test_api_endpoints(self):
        snap = json.loads(self.get("/api?since=0")[2])
        self.assertTrue(snap["history"])
        self.assertEqual(len(snap["services"]), 6)
        self.assertIn("hours", json.loads(self.get("/api/usage")[2]))
        self.assertIn("callers", json.loads(self.get("/api/callers")[2]))
        self.assertIn("events", json.loads(self.get("/api/events")[2]))
        self.assertTrue(json.loads(self.get("/api/logs/qwen")[2])["lines"])
        self.assertEqual(json.loads(self.get("/api/summary")[2])["services_total"], 6)

    def test_web_terminal_session(self):
        req = urllib.request.Request(self.base + "/term/session", data=json.dumps({"cols": 80, "rows": 25, "intro": False}).encode())
        sid = json.loads(urllib.request.urlopen(req, timeout=5).read())["id"]
        with urllib.request.urlopen(f"{self.base}/term/stream?id={sid}", timeout=5) as r:
            first = b""
            while b"SCREEN" not in first:
                first += r.readline()
        self.assertIn(b"data:", first)
        urllib.request.urlopen(urllib.request.Request(f"{self.base}/term/input?id={sid}", data=b"s"), timeout=5)

    def test_fonts_are_served(self):
        status, ctype, body = self.get("/fonts/PxPlus_IBM_VGA_8x16.ttf")
        self.assertEqual((status, ctype), (200, "font/ttf"))


class TestPasswordHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http, cls.tel, cls.board = start(password="pw123")
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"

    def test_api_needs_the_password(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.base + "/api", timeout=5)
        self.assertEqual(e.exception.code, 401)
        e.exception.close()
        ok = urllib.request.urlopen(urllib.request.Request(self.base + "/api", headers={"Authorization": "Bearer pw123"}), timeout=5)
        self.assertEqual(ok.status, 200)
        basic = base64.b64encode(b"any:pw123").decode()
        ok = urllib.request.urlopen(urllib.request.Request(self.base + "/api/usage", headers={"Authorization": f"Basic {basic}"}), timeout=5)
        self.assertEqual(ok.status, 200)

    def test_health_stays_open(self):
        self.assertEqual(urllib.request.urlopen(self.base + "/health", timeout=5).status, 200)


class TestCLI(unittest.TestCase):
    def run_lonoboard(self, *args):
        env = dict(os.environ, PYTHONPATH=str(helpers.SRC), LONOBOARD_CONFIG="/nonexistent/lonoboard.toml")
        return subprocess.run([sys.executable, "-m", "lonoboard", *args], capture_output=True, text=True, env=env, timeout=60)

    def test_version(self):
        self.assertIn("lonoboard ", self.run_lonoboard("--version").stdout)

    def test_json_demo(self):
        r = self.run_lonoboard("json", "--demo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(json.loads(r.stdout)["services"]), 6)

    def test_install_print(self):
        r = self.run_lonoboard("install", "--print")
        self.assertIn("ExecStart=", r.stdout)
        self.assertIn("-m lonoboard serve", r.stdout)

    def test_refuses_an_open_network_board(self):
        r = self.run_lonoboard("serve", "--listen", "0.0.0.0", "--port", "0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("password", r.stderr)


if __name__ == "__main__":
    unittest.main()
