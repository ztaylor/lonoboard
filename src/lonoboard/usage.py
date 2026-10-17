"""Usage history: requests per hour, the top 10 and recent requests, kept on disk.

Fed from whatever each server offers (see collector.Feeds): per-request log lines (exact),
Prometheus counters (aggregates), or slot activity (request counts only).
"""
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

KEEP_HOURS = 48


def hour_start(t):
    return int(datetime.fromtimestamp(t).replace(minute=0, second=0, microsecond=0).timestamp())


class Usage:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.events = deque(maxlen=2000)     # {at, name, tokens, gen_tps, prompt_tokens, prompt_tps, source}
        self.keys = set()
        self.hours = {}                      # {hour start: {name: {requests, tokens, prompt_tokens, gen_s}}}
        self.dirty = False
        self.load()

    # --- writing ---
    def add_event(self, e):
        """One finished request. Returns False if we've already seen it."""
        key = (e["name"], int(e["at"]), e.get("tokens"), e.get("prompt_tokens"), e.get("source"))
        with self.lock:
            if key in self.keys:
                return False
            self.keys.add(key)
            self.events.append(e)
            gen_s = e["tokens"] / e["gen_tps"] if e.get("tokens") and e.get("gen_tps") else 0.0
            self._bump(e["name"], e["at"], 1, e.get("tokens") or 0, e.get("prompt_tokens") or 0, gen_s)
            self.dirty = True
        return True

    def add_counts(self, name, at, requests=0, tokens=0, prompt_tokens=0, gen_s=0.0):
        """Aggregate counts with no per-request detail (from counters)."""
        if not (requests or tokens or prompt_tokens):
            return
        with self.lock:
            self._bump(name, at, requests, tokens, prompt_tokens, gen_s)
            self.dirty = True

    def _bump(self, name, at, requests, tokens, prompt_tokens, gen_s):
        h = self.hours.setdefault(hour_start(at), {}).setdefault(
            name, {"requests": 0, "tokens": 0, "prompt_tokens": 0, "gen_s": 0.0})
        h["requests"] += requests
        h["tokens"] += tokens
        h["prompt_tokens"] += prompt_tokens
        h["gen_s"] += gen_s

    # --- reading ---
    def recent(self, n=30, name=None):
        with self.lock:
            evs = [e for e in self.events if (name is None or e["name"] == name) and e.get("source") != "slots"]
        return sorted(evs, key=lambda e: -e["at"])[:n]

    def last(self, name):
        r = self.recent(1, name)
        return r[0] if r else None

    def today(self, name=None):
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self.lock:
            return sum(v["requests"] for start, per in self.hours.items() if start >= midnight
                       for n, v in per.items() if name is None or n == name)

    def summary(self, hours=24):
        """Requests per hour (oldest first) and services ranked by tokens generated today."""
        now = datetime.now()
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        out, top = [], {}
        with self.lock:
            for i in range(hours):
                hs = int((start + timedelta(hours=i)).timestamp())
                per = self.hours.get(hs, {})
                out.append({"hour": (start + timedelta(hours=i)).hour, "start": hs,
                            "requests": sum(v["requests"] for v in per.values()),
                            "tokens": sum(v["tokens"] for v in per.values())})
            for hs, per in self.hours.items():
                if hs < midnight:
                    continue
                for name, v in per.items():
                    t = top.setdefault(name, {"name": name, "requests": 0, "tokens": 0, "gen_s": 0.0})
                    t["requests"] += v["requests"]
                    t["tokens"] += v["tokens"]
                    t["gen_s"] += v["gen_s"]
        ranked = sorted(top.values(), key=lambda t: (-t["tokens"], -t["requests"]))[:10]
        for t in ranked:
            t["avg_tps"] = round(t["tokens"] / t["gen_s"], 1) if t["gen_s"] else None
            del t["gen_s"]
        return {"hours": out, "top": ranked}

    # --- persistence ---
    def prune(self):
        cut = time.time() - KEEP_HOURS * 3600
        with self.lock:
            self.hours = {h: v for h, v in self.hours.items() if h >= hour_start(cut)}
            while self.events and self.events[0]["at"] < cut:
                self.events.popleft()
            self.keys = {k for k in self.keys if k[1] >= cut}

    def load(self):
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.hours = {int(h): v for h, v in (data.get("hours") or {}).items()}
        for e in data.get("events") or []:
            self.events.append(e)
            self.keys.add((e["name"], int(e["at"]), e.get("tokens"), e.get("prompt_tokens"), e.get("source")))
        self.prune()

    def save(self):
        if not self.path or not self.dirty:
            return
        self.prune()
        with self.lock:
            data = {"version": 1, "hours": self.hours, "events": list(self.events)}
            self.dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            pass
