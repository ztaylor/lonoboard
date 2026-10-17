"""Shared data for sessions.

A hub holds what every session on a board renders from: the latest snapshot, telemetry history,
recent requests, usage, the event log and fleet summaries. Hub is fed by this machine's
collector; RemoteHub by another machine's lonoboard API, so a session can view any board in the fleet
just by switching hubs.
"""
import threading
import time
from collections import deque

from lonoboard.board.model import expected, healthy, status
from lonoboard.board.render import SVC_COLORS, dur
from lonoboard.util import get_json, stream_chat

HISTORY_LEN = 450


class BaseHub:
    remote = False
    label = None

    def __init__(self):
        self.cond = threading.Condition()
        self.sessions = set()
        self.data = None
        self.history = deque(maxlen=HISTORY_LEN)
        self.callers, self.usage, self.fleet = [], None, {}
        self.events = deque(maxlen=300)
        self.colors = {}
        self.busy_prev, self.busy_start = {}, {}
        self.last_t = 0
        self.wake = threading.Event()
        self.force = False
        self.started = time.time()
        self.thread = None
        self.error = None
        self.auth = {}                      # Authorization header for password-protected peers

    @property
    def hist(self):
        return list(self.history)

    def color(self, name):
        if name not in self.colors:
            self.colors[name] = SVC_COLORS[len(self.colors) % len(SVC_COLORS)]
        return self.colors[name]

    def join(self, sess):
        with self.cond:
            self.sessions.add(sess)
        self.force = True
        self.wake.set()
        self.ensure_running()

    def leave(self, sess):
        with self.cond:
            self.sessions.discard(sess)

    def notify(self):
        with self.cond:
            for s in self.sessions:
                s.dirty.set()

    def ensure_running(self):
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.run, daemon=True, name=f"lonoboard-{type(self).__name__}")
            self.thread.start()

    def log(self, segs, t=None):
        self.events.append((t or time.time(), segs))

    def events_json(self):
        return [[t, [list(s) for s in segs]] for t, segs in self.events]

    # --- turning snapshots into events ---
    def ingest(self, d, extend_history=True):
        for s in d["services"]:
            self.color(s["name"])
        pts = d.pop("history", [])
        if extend_history:
            self.history.extend(pts)
        for p in pts:
            names = set(p.get("busy", {})) | set(self.busy_prev)
            for n in sorted(names):
                b, was = p.get("busy", {}).get(n, 0), self.busy_prev.get(n, 0)
                if not was and b:
                    self.busy_start[n] = p["t"]
                    self.log([(n, self.color(n)), (f" started generating · {b} slot{'s' if b > 1 else ''} busy", 7)], p["t"])
                elif was and not b:
                    self.log([(n, self.color(n)), (f' finished after {dur(p["t"] - self.busy_start.get(n, p["t"]))}', 7)], p["t"])
                elif b > was:
                    self.log([(n, self.color(n)), (f" now {b} slots busy", 7)], p["t"])
                self.busy_prev[n] = b
        if pts:
            self.last_t = pts[-1]["t"]
        if self.data is None:
            self.log([("monitoring started", 10), (f' on {d["host"]["board"]["name"]} · {len(healthy(d))} of {len(expected(d))} services healthy', 7)])
        else:
            self.diff(self.data, d)
        self.data = d

    def diff(self, a, b):
        hist = self.hist
        prev = {s["name"]: s for s in a["services"]}
        bucket = lambda k: "ok" if k == "busy" else k
        for s in b["services"]:
            p = prev.get(s["name"])
            n = (s["name"], self.color(s["name"]))
            if not p:
                self.log([("new service ", 7), n, (f' on :{s["port"]}', 7)])
                continue
            k0, k1 = status(p, hist), status(s, hist)
            if bucket(k0[0]) != bucket(k1[0]):
                if k1[0] in ("ok", "busy"):
                    self.log([n, (" is back online", 10)])
                else:
                    self.log([n, (f' is {k1[1] or "down"}', 12)])
            if (s["run"].get("restarts") or 0) > (p["run"].get("restarts") or 0):
                self.log([n, (f' restarted (#{s["run"]["restarts"]})', 14)])
        for name in set(prev) - {s["name"] for s in b["services"]}:
            self.log([("service ", 7), (name, self.color(name)), (" went away", 12)])


class Hub(BaseHub):
    """This machine's board, fed by the collector (or the demo)."""

    def __init__(self, collector):
        super().__init__()
        self.collector = collector

    @property
    def hist(self):
        return list(self.collector.history)

    def run(self):
        due = {"snap": 0, "callers": 0, "usage": 0, "fleet": 0}
        every = {"callers": 10, "usage": 60, "fleet": 15}
        while True:
            now = time.time()
            watching = bool(self.sessions)
            if self.force:
                due = dict.fromkeys(due, 0)
                self.force = False
            snap_every = 2.5 if watching else 15
            try:
                if now >= due["snap"]:
                    self.ingest(self.collector.snapshot(since=self.last_t, max_age=1.0), extend_history=False)
                    due["snap"] = now + snap_every
                if watching:
                    for k, fn in (("callers", self.load_callers), ("usage", self.load_usage), ("fleet", self.load_fleet)):
                        if now >= due[k]:
                            fn()
                            due[k] = now + every[k]
            except Exception as e:  # noqa: BLE001 - keep the board alive whatever a source does
                self.log([("collector error: ", 12), (str(e)[:80], 7)])
            self.notify()
            self.wake.wait(max(0.2, min(due.values()) - time.time()) if watching else snap_every)
            self.wake.clear()

    def load_callers(self):
        self.callers = self.collector.callers(30)

    def load_usage(self):
        self.usage = self.collector.usage_summary(24)

    def load_fleet(self):
        for p in (self.data or {}).get("fleet", []):
            before = self.fleet.get(p["name"])
            auth = self.auth if p.get("source") == "config" else {}      # the password only goes to listed peers
            summary = get_json(f"{p['url']}/api/summary", 3, auth)
            self.fleet[p["name"]] = dict(summary, ok=True) if isinstance(summary, dict) else {"ok": False}
            after = self.fleet[p["name"]]
            if before is not None and before.get("ok") != after.get("ok"):
                self.log([(p["name"], 15), (" is reachable", 10) if after.get("ok") else (" is unreachable", 12)])

    def logs(self, name, n=30):
        return self.collector.logs(name, n)

    def chat_stream(self, service, model, messages, cancelled):
        return self.collector.chat_stream(service, model, messages, cancelled)


class RemoteHub(BaseHub):
    """Another machine's board, read through its lonoboard API."""
    remote = True

    def __init__(self, url, name):
        super().__init__()
        self.url, self.label = url.rstrip("/"), name

    def run(self):
        due = {"snap": 0, "callers": 0, "usage": 0, "events": 0}
        every = {"snap": 2.5, "callers": 10, "usage": 60, "events": 5}
        idle_since = None
        while True:
            if not self.sessions:                  # nobody's looking: stop after a while
                idle_since = idle_since or time.time()
                if time.time() - idle_since > 60:
                    return
            else:
                idle_since = None
            if self.force:
                due = dict.fromkeys(due, 0)
                self.force = False
            now = time.time()
            try:
                if now >= due["snap"]:
                    d = get_json(f"{self.url}/api?since={self.last_t}", 5, self.auth)
                    self.error = None if d is not None else f"can't reach {self.label}"
                    if d is not None:
                        for s in d["services"]:
                            self.color(s["name"])
                        pts = d.pop("history", [])
                        self.history.extend(pts)
                        self.last_t = pts[-1]["t"] if pts else self.last_t
                        self.data = d
                    due["snap"] = now + every["snap"]
                if now >= due["callers"]:
                    self.callers = (get_json(f"{self.url}/api/callers", 5, self.auth) or {}).get("callers") or self.callers
                    due["callers"] = now + every["callers"]
                if now >= due["usage"]:
                    self.usage = get_json(f"{self.url}/api/usage", 5, self.auth) or self.usage
                    due["usage"] = now + every["usage"]
                if now >= due["events"]:
                    ev = (get_json(f"{self.url}/api/events", 5, self.auth) or {}).get("events")
                    if ev is not None:
                        self.events = deque(((t, [tuple(s) for s in segs]) for t, segs in ev), maxlen=300)
                    due["events"] = now + every["events"]
            except Exception as e:  # noqa: BLE001
                self.error = str(e)[:80]
            self.notify()
            self.wake.wait(max(0.2, min(due.values()) - time.time()))
            self.wake.clear()

    def logs(self, name, n=30):
        return (get_json(f"{self.url}/api/logs/{name}?n={n}", 5, self.auth) or {}).get("lines") or []

    def chat_stream(self, service, model, messages, cancelled):
        sv = next((s for s in (self.data or {}).get("services", []) if s["name"] == service), None)
        urls = [u for u in ((sv or {}).get("urls") or {}).get("remote", []) if u.startswith("http")]
        if not sv or not sv.get("chat") or not urls:
            return iter([("error", f"{service} on {self.label} isn't reachable for chat from here")])
        return stream_chat(urls[0], model, messages, cancelled=cancelled)


class Chat:
    """One conversation with one model, streamed through a hub."""

    def __init__(self, service, model):
        self.service, self.model = service, model
        self.msgs = []          # {'role', 'content', 'reasoning', 'stats', 'error', 't0', 'think_s'}
        self.busy = False
        self.cancel = False
        self.scroll = 0         # lines up from the bottom

    @property
    def name(self):
        return self.service if not self.model or self.model == self.service else f"{self.service} · {self.model}"

    def send(self, text, sess):
        self.msgs.append({"role": "user", "content": text})
        reply = {"role": "assistant", "content": "", "reasoning": "", "stats": None, "error": None,
                 "t0": time.time(), "think_s": None}
        self.msgs.append(reply)
        self.busy, self.cancel, self.scroll = True, False, 0
        threading.Thread(target=self._stream, args=(reply, sess.hub, sess), daemon=True).start()

    def _stream(self, reply, hub, sess):
        history = [{"role": m["role"], "content": m["content"]} for m in self.msgs[:-1] if m["content"]]
        last_paint = 0
        for kind, value in hub.chat_stream(self.service, self.model, history, lambda: self.cancel or not sess.alive):
            if kind == "reasoning":
                reply["reasoning"] += value
            elif kind == "content":
                if reply["think_s"] is None and reply["reasoning"]:
                    reply["think_s"] = time.time() - reply["t0"]
                reply["content"] += value
            elif kind == "stats":
                reply["stats"] = value
            elif kind == "error":
                reply["error"] = value
                break
            if time.time() - last_paint > 0.06:
                sess.dirty.set()
                last_paint = time.time()
        if reply["think_s"] is None and reply["reasoning"]:
            reply["think_s"] = time.time() - reply["t0"]
        self.busy = False
        sess.dirty.set()
