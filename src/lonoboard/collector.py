"""The collector: everything the board shows, gathered from model servers, how they run,
the hardware and the network. One per process; the board's sessions and the JSON API share it."""
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from lonoboard import __version__, runtime, sources
from lonoboard.config import board_name, exposure, peer_url, state_dir
from lonoboard.hardware import Hardware
from lonoboard.network import Network
from lonoboard.usage import Usage
from lonoboard.util import get_json, get_text, stream_chat

SAMPLE_EVERY = 2          # seconds between telemetry samples
HISTORY_LEN = 450         # 15 minutes of samples
TIMING_RE = re.compile(r"task (\d+) \| +(prompt )?eval time = +[\d.]+ ms / +(\d+) tokens .*?([\d.]+) tokens per second")
GIN_RE = re.compile(r'\[GIN\] (\d{4}/\d\d/\d\d - \d\d:\d\d:\d\d) \| (\d{3}) \|\s*([\d.]+)(µs|ms|s|m)\s*\|[^|]*\|\s*POST\s+"([^"]+)"')
OLLAMA_WORK = ("/api/chat", "/api/generate", "/api/embed", "/api/embeddings", "/v1/chat/completions",
               "/v1/completions", "/v1/embeddings")


def parse_timing(lines):
    """llama-server print_timing lines (journal short-unix output) -> finished requests.
    A request logs "prompt eval time" and "eval time" with the same task id."""
    groups = []
    for line in lines:
        m = TIMING_RE.search(line)
        if not m:
            continue
        try:
            at = float(line.split()[0])
        except (ValueError, IndexError):
            continue
        if not groups or groups[-1]["task"] != m.group(1):
            groups.append({"task": m.group(1), "at": at})
        groups[-1].setdefault("prompt" if m.group(2) else "gen", (int(m.group(3)), float(m.group(4))))
    out = []
    for g in groups:
        gen, prompt = g.get("gen", (None, None)), g.get("prompt", (None, None))
        out.append({"at": int(g["at"]), "tokens": gen[0], "gen_tps": gen[1], "prompt_tokens": prompt[0],
                    "prompt_tps": prompt[1]})
    return out


def parse_gin(lines):
    """Ollama's request log lines -> [(unix time, path, seconds)] for model work (POSTs)."""
    out = []
    for line in lines:
        m = GIN_RE.search(line)
        if not m or not m.group(5).startswith(OLLAMA_WORK):
            continue
        try:
            at = datetime.strptime(m.group(1), "%Y/%m/%d - %H:%M:%S").timestamp()
        except ValueError:
            continue
        secs = float(m.group(3)) * {"µs": 1e-6, "ms": 1e-3, "s": 1, "m": 60}[m.group(4)]
        out.append((int(at), m.group(5), secs))
    return out


def platform_label(gpu, cpu_model):
    if gpu:
        name = re.sub(r"^(nvidia )?(geforce )?", "", gpu["name"].lower()) if gpu["vendor"] == "nvidia" else gpu["name"].lower()
        name = f"nvidia {name}" if gpu["vendor"] == "nvidia" and not name.startswith(("rtx", "gtx")) else name
        return f"{gpu['count']}x {name}" if gpu.get("count", 1) > 1 else name
    return (cpu_model or "cpu").lower().replace("(r)", "").replace("(tm)", "").replace("  ", " ")[:32]


class Collector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.hw, self.net = Hardware(), Network()
        self.usage = Usage(state_dir(cfg) / "usage.json")
        self.history = deque(maxlen=HISTORY_LEN)
        self.pool = ThreadPoolExecutor(16)
        self.started = time.time()
        b = cfg["board"]
        self.own_ports = {p for p in (b["port"], b["telnet_port"]) if p}
        self._lock = threading.Lock()
        self._targets, self._targets_at, self._kinds = [], 0, {}
        self._snap, self._snap_at = None, 0
        self._fleet, self._fleet_at = [], 0
        self._logsrc = {}         # service name -> ("journal", unit, user) | ("docker", container)
        self._slot_tasks = {}     # (service, slot id) -> last task counted
        self._cursors = {}        # journal unit -> last timestamp read
        self._metrics_prev = {}   # service -> previous counter values
        self._no_metrics = {}     # url -> time /metrics was found missing

    def start(self):
        threading.Thread(target=self._loop, args=(SAMPLE_EVERY, self.sample), daemon=True, name="lonoboard-sampler").start()
        threading.Thread(target=self._loop, args=(10, self.feed), daemon=True, name="lonoboard-feeds").start()
        return self

    @staticmethod
    def _loop(every, fn):
        while True:
            t = time.time()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - a broken source must not stop the board
                print(f"lonoboard: {fn.__name__}: {e}")
            time.sleep(max(0.2, every - (time.time() - t)))

    # ---------- what to watch ----------
    def targets(self, ttl=30):
        with self._lock:
            if self._targets_at and time.time() - self._targets_at < ttl:
                return self._targets
        cfg = self.cfg
        listening = runtime.listeners()
        configured = [self._from_config(s) for s in cfg["server"]]
        adapters = sources.read_llama_models(cfg["llama_models"]["dir"], cfg["llama_models"]["unit"])
        known = {sources.port_of(t["url"]) for t in configured + adapters}
        detected = sources.detect(listening, cfg["detect"]["ports"], known, self.own_ports) if cfg["detect"]["enabled"] else []
        merged = {}
        for t in detected + adapters + configured:            # later wins: config > adapters > detected
            port = sources.port_of(t["url"])
            merged[port] = {**merged.get(port, {}), **{k: v for k, v in t.items() if v not in (None, "", [])}}
        order = {"config": 0, "llama-models": 1, "detected": 2}
        targets = sorted(merged.values(), key=lambda t: (order.get(t.get("origin"), 3), t["name"]))
        seen = set()
        for t in targets:                                     # names must be unique
            if t["name"] in seen:
                t["name"] = f"{t['name']}-{sources.port_of(t['url'])}"
            seen.add(t["name"])
        with self._lock:
            self._targets, self._targets_at = targets, time.time()
        return targets

    def _from_config(self, s):
        url = s["url"].rstrip("/")
        kind = s.get("kind")
        if not kind:
            if url not in self._kinds:
                self._kinds[url] = sources.identify(url) or None
            kind = self._kinds[url] or "openai"
        t = dict(s, url=url, kind=kind, origin="config")
        t.setdefault("name", f"{kind.split('.')[0]}-{sources.port_of(url)}")
        if "unit" in s and "unit_user" not in s:
            t["unit_user"] = s.get("user_unit", True)
        return t

    def target(self, name):
        return next((t for t in self.targets() if t["name"] == name), None)

    # ---------- the snapshot ----------
    def snapshot(self, since=None, max_age=1.0):
        with self._lock:
            fresh = self._snap if self._snap and time.time() - self._snap_at < max_age else None
        if fresh is None:
            fresh = self._build()
            with self._lock:
                self._snap, self._snap_at = fresh, time.time()
        body = dict(fresh)
        if since is not None:
            body["history"] = [h for h in list(self.history) if h["t"] > since]
        return body

    def _build(self):
        t0 = time.time()
        targets = self.targets()
        f_host, f_gpu = self.pool.submit(self.hw.host), self.pool.submit(self.hw.gpu)
        f_net, f_docker = self.pool.submit(self.net.info), self.pool.submit(runtime.docker_containers)
        listening = runtime.listeners()
        gpu, gpu_mem = f_gpu.result()
        docker = f_docker.result()
        services = list(self.pool.map(lambda t: self._service(t, listening, docker, gpu_mem), targets))
        host, net = f_host.result(), f_net.result()
        tot = host["memory_gib"]["total"]
        host.update({
            "gpu": gpu, "platform": platform_label(gpu, host.get("cpu_model")),
            "memory_label": (f"{round(tot)} gib unified" if gpu and gpu.get("unified") else
                             f"{round(gpu['mem_total_gib'])} gib vram · {round(tot)} gib ram"
                             if gpu and gpu.get("mem_total_gib") else f"{round(tot)} gib ram"),
            "software": sorted({s["version"] for s in services if s.get("version")}),
            "tailnet": net["tailnet"],
            "board": {"name": board_name(self.cfg), "tagline": self.cfg["board"]["tagline"],
                      "exposure": self.exposure(net), "version": __version__,
                      "chat": bool(self.cfg["board"]["chat"])},
        })
        svc_ports = {s["port"] for s in services}
        other = []
        for port, urls in sorted(net["served"].items()):
            if port in svc_ports:
                continue
            l = listening.get(port) or {}
            name = ("lonoboard" if port == self.cfg["board"]["port"] else "lonoboard telnet" if port == self.cfg["board"]["telnet_port"]
                    else self.cfg["labels"].get(port) or l.get("process"))
            other.append({"name": name, "process": l.get("process"), "port": port, "listening": port in listening,
                          "urls": {"local": f"http://127.0.0.1:{port}", "remote": urls}})
        return {"host": host, "services": services, "other": other, "fleet": self.fleet(),
                "generated_at": int(t0), "took_ms": int((time.time() - t0) * 1000)}

    def exposure(self, net):
        listen = self.cfg["board"]["listen"]
        e = exposure(listen)
        if e == "network":
            return "lan"
        if e == "tailnet" or self.cfg["board"]["port"] in net["served"]:
            return "tailnet only"
        return "localhost only"

    def _service(self, t, listening, docker, gpu_mem):
        s = sources.describe(t)
        s.pop("_slots", None)
        port, l = s["port"], listening.get(s["port"]) or {}
        pid = l.get("pid")
        info = runtime.proc_info(pid) if pid else {}
        unit = t.get("unit") or info.get("unit")
        user = t.get("unit_user") if t.get("unit") else info.get("unit_user")
        run = None
        if unit:
            run = runtime.systemd_show(unit, True if user is None else user)
            if run is None and user is None:
                run = runtime.systemd_show(unit, False)
        if run is None and s["kind"] == "ollama" and not pid:     # the usual ollama install: a system unit
            run = runtime.systemd_show("ollama.service", False)
        container = info.get("container") or (docker.get(port) or {}).get("id")
        if run is None and container:
            run = runtime.docker_show(container)
        proc_mem = None
        if run:
            proc_mem = run.pop("memory_mib", None)
            s["run"].update(run)
        else:
            s["run"].update(manager="process" if pid else None, pid=pid, since=info.get("since"),
                            state="active" if pid or s["health"] in ("ok", "loading") else None)
        gpid = s["run"].get("pid") or pid
        if s["memory_mib"]["gpu"] is None:
            s["memory_mib"]["gpu"] = gpu_mem.get(gpid) or gpu_mem.get(pid)
        s["memory_mib"]["process"] = proc_mem or info.get("rss_mib")
        s["urls"] = {"local": s["url"], "remote": self.net.urls_for(port, l.get("public", False))}
        if s["run"].get("manager") == "systemd":
            self._logsrc[s["name"]] = ("journal", s["run"]["unit"], s["run"].get("user", True))
        elif s["run"].get("manager") == "docker":
            self._logsrc[s["name"]] = ("docker", s["run"].get("container"))
        if s["model"] is not None:
            s["model"]["last_request"] = self.usage.last(s["name"])
            s["model"]["requests_today"] = self.usage.today(s["name"])
        if s["chat"] and not self.cfg["board"]["chat"]:
            s["chat"] = None
        return s

    # ---------- background work ----------
    def sample(self):
        """Every couple of seconds: GPU, memory and how busy each model server is."""
        targets = self.targets()
        f_gpu = self.pool.submit(self.hw.gpu)
        busy = dict(zip([t["name"] for t in targets], self.pool.map(self._busy, targets)))
        gpu, _ = f_gpu.result()
        total, available = self.hw.memory_gib()
        gpu = gpu or {}
        mem = gpu.get("mem_used_gib") if gpu.get("mem_total_gib") else round(total - available, 1)
        self.history.append({"t": round(time.time(), 1), "gpu": gpu.get("util_pct"), "temp": gpu.get("temp_c"),
                             "power": gpu.get("power_w"), "mem": mem,
                             "busy": {n: b for n, b in busy.items() if b is not None}})

    def _busy(self, t):
        if t["kind"] == "llama.cpp":
            n, slots = sources.llamacpp_slots(t["url"], 1.0)
            if slots and not self._exact(t["name"]):            # count requests from slot activity
                for sl in slots:
                    key = (t["name"], sl["id"])
                    if sl["processing"] and sl["task"] is not None and self._slot_tasks.get(key) != sl["task"]:
                        self._slot_tasks[key] = sl["task"]
                        self.usage.add_event({"at": int(time.time()), "name": t["name"], "tokens": None,
                                              "gen_tps": None, "prompt_tokens": None, "prompt_tps": None,
                                              "source": "slots"})
            return n
        if t["kind"] == "vllm" and not self._metrics_missing(t["url"]):
            m = sources.parse_metrics(get_text(f"{t['url']}/metrics", 1.0) or "")
            return int(m["vllm:num_requests_running"]) if "vllm:num_requests_running" in m else None
        return None

    def _exact(self, name):
        src = self._logsrc.get(name)
        return bool(src and src[0] == "journal")

    def _metrics_missing(self, url):
        return time.time() - self._no_metrics.get(url, 0) < 300

    def feed(self):
        """Every ten seconds: usage from logs and counters, the fleet, and saving to disk."""
        self.snapshot(max_age=30)          # learns each service's log source, even with nobody watching
        for t in self.targets():
            src = self._logsrc.get(t["name"])
            try:
                if t["kind"] == "llama.cpp" and src and src[0] == "journal":
                    self._feed_journal(t["name"], src[1], src[2])
                elif t["kind"] == "ollama":
                    self._feed_ollama(t, src)
                if t["kind"] in ("llama.cpp", "vllm") and not self._metrics_missing(t["url"]):
                    self._feed_metrics(t)
            except Exception as e:  # noqa: BLE001
                print(f"lonoboard: usage feed for {t['name']}: {e}")
        if time.time() - self._fleet_at > 60:
            self._probe_fleet()
        self.usage.save()

    def _feed_journal(self, name, unit, user):
        cur = self._cursors.get(unit)
        lines = runtime.journal(unit, user, grep="eval time", since=f"@{int(cur)}" if cur else "-24h", output="short-unix")
        for r in parse_timing(lines):
            self.usage.add_event({**r, "name": name, "source": "log"})
            self._cursors[unit] = max(self._cursors.get(unit, 0), r["at"])
        self._cursors.setdefault(unit, time.time() - 1)

    def _feed_ollama(self, t, src):
        unit, user = (src[1], src[2]) if src and src[0] == "journal" else ("ollama.service", False)
        cur = self._cursors.get(unit)
        lines = runtime.journal(unit, user, grep="GIN", since=f"@{int(cur)}" if cur else "-24h", output="short")
        for at, path, secs in parse_gin(lines):
            self.usage.add_event({"at": at, "name": t["name"], "tokens": None, "gen_tps": None, "prompt_tokens": None,
                                  "prompt_tps": None, "duration_s": round(secs, 2), "path": path, "source": "log"})
            self._cursors[unit] = max(self._cursors.get(unit, 0), at)
        self._cursors.setdefault(unit, time.time() - 1)

    def _feed_metrics(self, t):
        text = get_text(f"{t['url']}/metrics", 2)
        if not text:
            self._no_metrics[t["url"]] = time.time()
            return
        m, prev = sources.parse_metrics(text), self._metrics_prev.get(t["name"])
        self._metrics_prev[t["name"]] = m
        if not prev:
            return
        d = lambda k: max(0.0, m.get(k, 0) - prev.get(k, 0)) if k in m and k in prev else 0.0
        now = time.time()
        if t["kind"] == "llama.cpp" and not self._exact(t["name"]):
            self.usage.add_counts(t["name"], now, tokens=int(d("llamacpp:tokens_predicted_total")),
                                  prompt_tokens=int(d("llamacpp:prompt_tokens_total")),
                                  gen_s=d("llamacpp:tokens_predicted_seconds_total"))
        elif t["kind"] == "vllm":
            self.usage.add_counts(t["name"], now, requests=int(d("vllm:request_success_total")),
                                  tokens=int(d("vllm:generation_tokens_total")),
                                  prompt_tokens=int(d("vllm:prompt_tokens_total")),
                                  gen_s=d("vllm:request_decode_time_seconds_sum"))

    # ---------- the fleet ----------
    def fleet(self):
        return list(self._fleet)

    def _probe_fleet(self):
        port, cands = self.cfg["board"]["port"], {}
        for p in self.cfg["fleet"]["peers"]:
            url = peer_url(p, port)
            cands[url] = {"name": re.sub(r"^\w+://|:\d+$|/.*$", "", url).split(".")[0], "url": url,
                          "source": "config", "online": None}
        if self.cfg["fleet"]["tailscale"]:
            for peer in self.net.info(ttl=60)["peers"]:
                if peer["online"] and (peer.get("os") or "").lower() in ("linux", "macos", ""):
                    url = f"http://{peer['name']}:{port}"
                    cands.setdefault(url, {"name": peer["name"], "url": url, "source": "tailscale", "online": None})

        def probe(c):
            h = get_json(f"{c['url']}/health", 1.5)
            c["online"] = bool(isinstance(h, dict) and h.get("app") == "lonoboard")
            return c

        found = [c for c in self.pool.map(probe, cands.values()) if c["online"] or c["source"] == "config"]
        self._fleet, self._fleet_at = sorted(found, key=lambda c: c["name"]), time.time()

    # ---------- for the board and the API ----------
    def callers(self, n=30):
        return [dict(e) for e in self.usage.recent(n)]

    def usage_summary(self, hours=24):
        return self.usage.summary(hours)

    def logs(self, name, n=80):
        src = self._logsrc.get(name)
        if not src:
            return []
        if src[0] == "journal":
            lines = runtime.journal(src[1], src[2], n=n, output="short-iso", timeout=5)
            return [re.sub(r"^(\S+) \S+ [^:]+: ", r"\1 ", line) for line in lines]
        return runtime.docker_logs(src[1], n)

    def chat_stream(self, name, model, messages, cancelled=lambda: False):
        t = self.target(name)
        if not t or not self.cfg["board"]["chat"]:
            return iter([("error", "chat is turned off" if t else f"no service {name}")])
        return stream_chat(t["url"], model, messages, sources.headers_for(t), cancelled=cancelled)

    def summary(self):
        """A small summary for other boards' fleet screens."""
        snap = self.snapshot(max_age=5)
        svcs = snap["services"]
        up = [s for s in svcs if s["health"] == "ok"]
        last = self.history[-1] if self.history else {}
        g, m = snap["host"].get("gpu") or {}, snap["host"]["memory_gib"]
        return {"name": snap["host"]["board"]["name"], "hostname": snap["host"]["hostname"],
                "platform": snap["host"]["platform"], "version": __version__,
                "services_up": len(up), "services_total": len(svcs),
                "busy": sum(1 for b in (last.get("busy") or {}).values() if b),
                "gpu_util": g.get("util_pct"), "mem_used_gib": round(m["total"] - m["available"], 1),
                "mem_total_gib": m["total"], "requests_today": self.usage.today()}
