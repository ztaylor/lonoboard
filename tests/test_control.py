"""Starting and stopping services: who may, what's allowed, the memory check, the API and the board."""
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import helpers
from lonoboard import config, control, web
from lonoboard.board import Board, Session
from lonoboard.cli import App
from lonoboard.demo import DemoCollector

ME = "alice@github"


def controlled_config(**board):
    cfg = helpers.config(intro=False, port=0, telnet_port=0, **board)
    cfg["control"]["users"] = [ME]
    return cfg


def wait_for(fn, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return False


def service(snap, name):
    return next(s for s in snap["services"] if s["name"] == name)


class TestRules(unittest.TestCase):
    def test_off_until_someone_is_listed(self):
        cfg = helpers.config()
        self.assertFalse(control.enabled(cfg))
        self.assertFalse(control.allowed(ME, cfg))
        cfg["control"]["users"] = [ME]
        self.assertTrue(control.allowed("Alice@GitHub", cfg))
        self.assertFalse(control.allowed("bob@github", cfg))
        self.assertFalse(control.allowed(None, cfg))

    def test_users_must_be_logins(self):
        cfg = helpers.config()
        for bad in ("alice@github", [""], [3]):
            cfg["control"]["users"] = bad
            with self.assertRaises(config.ConfigError):
                config.validate(cfg)

    def test_no_controls_beyond_the_tailnet(self):
        cfg = controlled_config(listen="0.0.0.0", password="secret")
        with self.assertRaises(config.ConfigError):
            config.check_exposure(cfg)
        for listen in ("127.0.0.1", "100.101.102.103"):
            config.check_exposure(controlled_config(listen=listen))

    def test_the_header_counts_only_from_localhost(self):
        h = {"Tailscale-User-Login": ME}
        self.assertEqual(control.identity("127.0.0.1", h), ME)
        self.assertEqual(control.identity("::1", h), ME)
        self.assertIsNone(control.identity("192.168.1.20", h))
        self.assertIsNone(control.identity("127.0.0.1", {}))
        with mock.patch.object(control, "whois", return_value="bob@github") as who:
            self.assertEqual(control.identity("100.101.102.103", h), "bob@github")    # asked, not told
            who.assert_called_once_with("100.101.102.103")

    def test_tagged_devices_are_nobody(self):
        out = json.dumps({"Node": {"Tags": ["tag:server"]}, "UserProfile": {"LoginName": "tagged-devices"}})
        control._whois.clear()
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=out)):
            self.assertIsNone(control.whois("100.64.0.9"))
        out = json.dumps({"Node": {}, "UserProfile": {"LoginName": ME}})
        control._whois.clear()
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(control.whois("100.64.0.10"), ME)

    def test_only_systemd_user_units(self):
        unit = {"manager": "systemd", "unit": "llama@qwen.service", "user": True, "state": "active", "enabled": True}
        self.assertEqual(control.actions_for({"run": unit}), ["stop", "restart", "disable"])
        self.assertEqual(control.actions_for({"run": dict(unit, state="inactive", enabled=False)}), ["start", "enable"])
        for run in (dict(unit, user=False), dict(unit, manager="docker"), {"manager": "process"}, {}):
            self.assertEqual(control.actions_for({"run": run}), [])


class TestMemory(unittest.TestCase):
    def test_weights_from_launch_flags_including_split_files(self):
        with tempfile.TemporaryDirectory() as d:
            for i in (1, 2, 3):
                Path(d, f"big-0000{i}-of-00003.gguf").write_bytes(b"\0" * 2**20)
            Path(d, "mmproj.gguf").write_bytes(b"\0" * 2**20)
            args = ["-m", f"{d}/big-00001-of-00003.gguf", "--mmproj", f"{d}/mmproj.gguf", "-c", "8192"]
            self.assertEqual(control.weights_gib({"model": {"args": args}}), round(4 / 1024, 1))

    def test_falls_back_to_what_the_server_said(self):
        self.assertEqual(control.weights_gib({"model": {"size_gib": 17.3}}), 17.3)
        self.assertIsNone(control.weights_gib({"model": None}))

    def test_fits(self):
        stopped = {"run": {"state": "inactive"}, "model": {"size_gib": 40}}
        unified = {"gpu": {"unified": True}, "memory_gib": {"total": 120, "available": 30}}
        discrete = {"gpu": {"unified": False, "mem_total_gib": 48, "mem_used_gib": 4}, "memory_gib": {"available": 10}}
        self.assertEqual(control.fits(stopped, unified, "start"), {"need_gib": 40, "free_gib": 30})
        self.assertIsNone(control.fits(stopped, discrete, "start"))                  # 44 gib of vram free
        self.assertIsNone(control.fits(stopped, unified, "stop"))
        self.assertIsNone(control.fits(dict(stopped, run={"state": "active"}), unified, "restart"))
        self.assertIsNone(control.fits({"run": {}, "model": None}, unified, "start"))   # unknown: let it try


class FakeCollector:
    """Just enough collector for control.perform."""

    def __init__(self, free=30):
        self.done = []
        self.snap = {"host": {"gpu": {"unified": True}, "memory_gib": {"total": 120, "available": free}},
                     "services": [{"name": "big", "run": {"manager": "systemd", "unit": "llama@big.service", "user": True,
                                                          "state": "inactive", "enabled": False},
                                   "model": {"size_gib": 40}}]}

    def snapshot(self, since=None, max_age=None):
        return self.snap

    def control(self, sv, action):
        self.done.append((sv["name"], action))
        return True, ""


class TestPerform(unittest.TestCase):
    def test_too_big_needs_force(self):
        c = FakeCollector(free=30)
        code, body = control.perform(c, "big", "start")
        self.assertEqual(code, 409)
        self.assertIs(body["fits"], False)
        self.assertEqual(c.done, [])
        code, _ = control.perform(c, "big", "start", force=True)
        self.assertEqual(code, 200)
        self.assertEqual(c.done, [("big", "start")])

    def test_bad_requests(self):
        c = FakeCollector()
        self.assertEqual(control.perform(c, "big", "explode")[0], 400)
        self.assertEqual(control.perform(c, "nope", "start")[0], 404)
        c.snap["services"][0]["run"]["manager"] = "docker"
        self.assertEqual(control.perform(c, "big", "start")[0], 409)

    def test_systemctl_runs_the_user_unit(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            self.assertEqual(control.systemctl("enable", "llama@qwen.service"), (True, ""))
        self.assertEqual(run.call_args[0][0], ["systemctl", "--user", "--no-block", "enable", "--now", "llama@qwen.service"])


def post(url, body=None, headers=None):
    data = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            raw = e.read()
            return e.code, json.loads(raw) if raw.startswith(b"{") else {}


class TestAPI(unittest.TestCase):
    def setUp(self):
        self.cfg = controlled_config()
        self.collector = DemoCollector(self.cfg, seed=3)
        self.board = Board(self.collector, self.cfg)
        self.http = web.serve(App(self.collector, self.board), "127.0.0.1", 0, background=True)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.me = {"Tailscale-User-Login": ME}

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()

    def test_stop_and_start(self):
        code, body = post(f"{self.base}/api/services/qwen/stop", headers=self.me)
        self.assertEqual((code, body.get("ok")), (200, True))
        self.assertEqual(service(self.collector.snapshot(), "qwen")["run"]["state"], "inactive")
        self.assertTrue(any("stopped" in "".join(s[0] for s in segs) for _, segs in self.board.hub.events))
        self.assertEqual(post(f"{self.base}/api/services/qwen/start", headers=self.me)[0], 200)
        self.assertEqual(service(self.collector.snapshot(), "qwen")["run"]["state"], "activating")

    def test_strangers_and_nobody(self):
        self.assertEqual(post(f"{self.base}/api/services/qwen/stop")[0], 403)
        self.assertEqual(post(f"{self.base}/api/services/qwen/stop", headers={"Tailscale-User-Login": "bob@github"})[0], 403)
        self.assertEqual(service(self.collector.snapshot(), "qwen")["run"]["state"], "active")

    def test_other_web_pages_cant_post(self):
        url = f"{self.base}/api/services/qwen/stop"
        self.assertEqual(post(url, headers={**self.me, "Origin": "http://evil.example.com"})[0], 403)
        self.assertEqual(post(url, headers={**self.me, "Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(post(url, headers={**self.me, "Content-Type": "text/plain"})[0], 403)    # a plain form
        host = self.base.split("//")[1]
        self.assertEqual(post(url, headers={**self.me, "Origin": self.base, "Host": host})[0], 200)

    def test_can_i(self):
        def ask(headers):
            req = urllib.request.Request(f"{self.base}/api/control", headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        self.assertEqual(ask(self.me), {"enabled": True, "you": ME, "allowed": True})
        self.assertEqual(ask({}), {"enabled": True, "you": None, "allowed": False})

    def test_off_by_default(self):
        self.cfg["control"]["users"] = []
        code, body = post(f"{self.base}/api/services/qwen/stop", headers=self.me)
        self.assertEqual(code, 403)
        self.assertIn("off", body["error"])

    def test_things_it_cant_control(self):
        self.assertEqual(post(f"{self.base}/api/services/vllm/stop", headers=self.me)[0], 409)      # docker
        self.assertEqual(post(f"{self.base}/api/services/nope/stop", headers=self.me)[0], 404)
        self.assertEqual(post(f"{self.base}/api/services/qwen/explode", headers=self.me)[0], 400)

    def test_web_sessions_belong_to_their_viewer(self):
        code, body = post(f"{self.base}/term/session", {"cols": 80, "rows": 25, "intro": False}, self.me)
        self.assertEqual(code, 200)
        sid = body["id"]
        self.assertEqual(self.board.web[sid].login, ME)
        req = urllib.request.Request(f"{self.base}/term/input?id={sid}", data=b"x", method="POST",
                                     headers={"Tailscale-User-Login": "bob@github"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()


class TestBoard(unittest.TestCase):
    def setUp(self):
        self.cfg = controlled_config()
        self.collector = DemoCollector(self.cfg, seed=4)
        self.board = Board(self.collector, self.cfg)
        self.assertTrue(wait_for(lambda: self.board.hub.data is not None))

    def session(self, login=None, kind="web"):
        s = Session(self.board, kind, 80, 25, utf8=True, intro=False)
        s.login = login
        s.resize(80, 25)
        return s

    def details(self, s, name):
        s.open("svc", name)
        return "\n".join("".join(c[0] for c in row) for row in s.render()[0].rows)

    def test_controls_show_only_for_allowed_viewers(self):
        self.assertIn("[s] stop", self.details(self.session(ME), "qwen"))
        self.assertNotIn("[s] stop", self.details(self.session("bob@github"), "qwen"))
        self.assertNotIn("[s] stop", self.details(self.session(None, "telnet"), "qwen"))
        self.assertNotIn("[s] stop", self.details(self.session(ME), "vllm"))                # docker

    def test_stop_asks_first_then_does_it(self):
        s = self.session(ME)
        self.details(s, "qwen")
        s.handle("s")
        self.assertEqual(s.modal[:3], ("confirm", "qwen", "stop"))
        s.handle("n")
        self.assertEqual(s.modal, ("svc", "qwen"))
        self.assertEqual(service(self.collector.snapshot(), "qwen")["run"]["state"], "active")
        s.handle("s")
        s.handle("y")
        self.assertTrue(wait_for(lambda: not s.control_busy()))
        self.assertEqual(service(self.collector.snapshot(), "qwen")["run"]["state"], "inactive")
        self.assertIn("stop qwen: done", self.details(s, "qwen"))

    def test_the_wrong_viewer_cant_press_the_keys(self):
        s = self.session("bob@github")
        self.details(s, "qwen")
        s.handle("s")
        self.assertEqual(s.modal, ("svc", "qwen"))

    def test_windows_fit_small_screens(self):
        tight = {"need_gib": 104.3, "free_gib": 12.0, "fits": False}
        for cols, rows in ((80, 25), (46, 30), (40, 12)):
            s = Session(self.board, "web", cols, rows, utf8=True, intro=False)
            s.login = ME
            s.resize(cols, rows)
            for modal in (("svc", "qwen"), ("confirm", "qwen", "disable", None), ("confirm", "qwen", "start", tight)):
                s.modal = modal
                scr = s.render()[0]
                self.assertEqual(len(scr.rows), rows, (cols, modal))
                self.assertTrue(all(len(r) == cols for r in scr.rows), (cols, modal))
            body = "\n".join("".join(c[0] for c in row) for row in scr.rows)
            if rows > 20:
                self.assertIn("start anyway", body)

    def test_help_mentions_the_keys_when_you_can(self):
        def help_text(login):
            s = self.session(login)
            s.open("help")
            return "\n".join("".join(c[0] for c in row) for row in s.render()[0].rows)
        self.assertIn("start or stop", help_text(ME))
        self.assertNotIn("start or stop", help_text(None))


class TestFleet(unittest.TestCase):
    """Controlling another machine's services through its board, from yours."""

    def test_through_the_other_board(self):
        far_cfg = controlled_config()
        far = DemoCollector(far_cfg, seed=5)
        far_board = Board(far, far_cfg)
        http = web.serve(App(far, far_board), "127.0.0.1", 0, background=True)
        try:
            url = f"http://127.0.0.1:{http.server_address[1]}"
            near_cfg = controlled_config()
            near = Board(DemoCollector(near_cfg, seed=6), near_cfg)
            s = Session(near, "web", 80, 25, utf8=True, intro=False)
            s.login = ME
            s.resize(80, 25)
            with mock.patch.object(control, "identity", return_value=ME):     # as tailscale serve would say
                s.view_hub(near.remote("far", url))
                self.assertTrue(wait_for(lambda: s.hub.data is not None and s.hub.control_info is not None))
                self.assertTrue(s.may_control())
                s.open("svc", "embed")
                s.handle("s")
                s.handle("y")
                self.assertTrue(wait_for(lambda: not s.control_busy()))
                self.assertEqual(service(far.snapshot(), "embed")["run"]["state"], "inactive")
            s.hub.control_info = {"enabled": True, "you": None, "allowed": False}      # the far board says no
            self.assertFalse(s.may_control())
        finally:
            http.shutdown()
            http.server_close()


if __name__ == "__main__":
    unittest.main()
