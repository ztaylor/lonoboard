"""HTTP: the web terminal (page, fonts, session stream) and the JSON API.

    GET  /                 the board in a browser; JSON (same as /api) for anything else (curl)
    GET  /api?since=T      snapshot: host, services, other served ports, fleet (+ samples newer than T)
    GET  /api/summary      a short summary, for other boards' fleet screens
    GET  /api/callers      recent requests
    GET  /api/usage        requests per hour (24 h) and the top 10 by tokens today
    GET  /api/events       the board's event log
    GET  /api/logs/NAME    recent log lines of a service (systemd or docker)
    GET  /health           {"status": "ok", "app": "lonoboard", "version": ...}
    POST /term/session     web terminal: start a session -> {"id"}
    GET  /term/stream?id=   web terminal: the session's ANSI output (server-sent events)
    POST /term/input?id=    web terminal: keystrokes
    POST /term/resize?id=   web terminal: window size

With a password set, the JSON API wants it as a Bearer token (or Basic auth, any user name);
the web terminal asks for it on screen. Without one, the board only answers to host names a web
page elsewhere can't point at it (config.host_allowed), so other sites can't read it.
"""
import base64
import hmac
import json
import re
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lonoboard import __version__
from lonoboard.config import host_allowed

STATIC = Path(__file__).resolve().parent / "static"
GUESSES, GUESS_WINDOW = 10, 600       # wrong API passwords allowed per address per window (seconds)


class Guesses:
    """Wrong passwords per address: each costs a second, and too many lock the address out a while."""

    def __init__(self):
        self.lock = threading.Lock()
        self.failed = {}

    def locked(self, ip):
        with self.lock:
            recent = [t for t in self.failed.get(ip, []) if time.time() - t < GUESS_WINDOW]
            self.failed[ip] = recent
            return len(recent) >= GUESSES

    def wrong(self, ip):
        with self.lock:
            self.failed.setdefault(ip, []).append(time.time())
        time.sleep(1.0)


class Handler(BaseHTTPRequestHandler):
    server_version = f"lonoboard/{__version__}"
    protocol_version = "HTTP/1.0"
    timeout = 60                      # a client that stops reading or sending is dropped

    @property
    def app(self):
        return self.server.app

    def authorized(self):
        pw = self.app.board.password
        if not pw:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            given = auth[7:]
        elif auth.startswith("Basic "):
            try:
                given = base64.b64decode(auth[6:]).decode().split(":", 1)[-1]
            except Exception:
                return False
        else:
            return False
        return hmac.compare_digest(given.encode(), pw.encode())

    def host_ok(self):
        if host_allowed(self.headers.get("Host"), self.app.board.cfg):
            return True
        self.send(403, b"lonoboard: this host name isn't allowed without a password; add it to board.hosts\n",
                  "text/plain")
        return False

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(url.query)
        if path == "/health":
            return self.json({"status": "ok", "app": "lonoboard", "version": __version__})
        if not path.startswith("/fonts/") and not self.host_ok():
            return
        if path == "/" and "text/html" in self.headers.get("Accept", ""):
            return self.send(200, (STATIC / "terminal.html").read_bytes(), "text/html; charset=utf-8")
        if m := re.fullmatch(r"/fonts/([\w.-]+\.ttf)", path):
            font = STATIC / "fonts" / m.group(1)
            if not font.is_file():
                return self.send_error(404)
            return self.send(200, font.read_bytes(), "font/ttf", cache="public, max-age=604800")
        if path == "/term/stream":
            sess = self.app.board.web.get(query.get("id", [""])[0])
            if not sess or getattr(sess, "attached", False):
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            return self.app.board.web_stream(sess, self)
        if not path.startswith("/api") and path != "/":
            return self.send_error(404)
        guesses, ip = self.server.guesses, self.client_address[0]
        if guesses.locked(ip):
            return self.send(429, b"too many wrong passwords; try again later\n", "text/plain")
        if not self.authorized():
            if self.headers.get("Authorization"):
                guesses.wrong(ip)
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="lonoboard"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        c = self.app.collector
        if path in ("/", "/api"):
            since = query.get("since", [None])[0]
            body = c.snapshot(float(since) if since and re.fullmatch(r"[\d.]+", since) else None)
        elif path == "/api/summary":
            body = c.summary()
        elif path == "/api/callers":
            body = {"callers": c.callers(30)}
        elif path == "/api/usage":
            body = c.usage_summary(24)
        elif path == "/api/events":
            body = {"events": self.app.board.hub.events_json()}
        elif m := re.fullmatch(r"/api/logs/([\w.@:-]+)", path):
            if not any(t["name"] == m.group(1) for t in c.targets()):
                return self.send_error(404)
            n = query.get("n", ["80"])[0]
            body = {"name": m.group(1), "lines": c.logs(m.group(1), min(int(n) if n.isdigit() else 80, 500))}
        else:
            return self.send_error(404)
        self.json(body)

    def do_POST(self):
        if not self.host_ok():
            return
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        n = self.headers.get("Content-Length") or "0"
        if not n.isdigit():
            return self.send_error(400)
        n = min(int(n), 65536)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        board = self.app.board
        if url.path == "/term/session":
            try:
                req = json.loads(body or "{}")
                sess = board.web_create(int(req.get("cols", 80)), int(req.get("rows", 25)),
                                        req.get("prefs") or {}, bool(req.get("intro", True)), self.client_address[0])
            except (ValueError, TypeError, AttributeError):
                return self.send_error(400)
            if not sess:
                return self.send_error(503, "too many sessions")
            return self.json({"id": sess.id})
        sess = board.web.get(query.get("id", [""])[0])
        if not sess:
            return self.send_error(404)
        if url.path == "/term/input":
            sess.feed(body)
        elif url.path == "/term/resize":
            try:
                req = json.loads(body)
                sess.resize(int(req["cols"]), int(req["rows"]))
            except (ValueError, KeyError, TypeError):
                return self.send_error(400)
        else:
            return self.send_error(404)
        self.send(204, b"", "text/plain")

    def json(self, body):
        self.send(200, (json.dumps(body, indent=2) + "\n").encode(), "application/json")

    def send(self, code, data, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away mid-response (closed tab, aborted fetch)

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        self.guesses = Guesses()
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, Handler)


def serve(app, host, port, background=False):
    srv = Server((host, port), app)
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True, name="lonoboard-http").start()
    return srv
