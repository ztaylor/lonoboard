"""Demo mode: a simulated machine with lively fake model servers, so lonoboard can be tried anywhere.

Same interface as the Collector. Everything here is invented and labelled as such on the board.
"""
import copy
import random
import threading
import time
from collections import deque
from datetime import datetime

from lonoboard import __version__
from lonoboard.config import board_name, exposure
from lonoboard.sources import base_service
from lonoboard.usage import Usage

SAMPLE_EVERY = 2


def exposure_label(cfg):
    return {"local": "localhost only", "tailnet": "tailnet only", "network": "lan"}[exposure(cfg["board"]["listen"])]

REPLIES = [
    "This is a demo board, so I'm a simulated model. On a real board you'd be talking to a model "
    "running on that machine, streamed token by token.\n\nTry the other screens: S shows each model "
    "server with its live slots, H the GPU, U requests per hour and the top 10.",
    "Lono is Hawaiian for news, a report - and to hear. lonoboard reports on the machines running "
    "your models: what's loaded, what's busy, how fast. It's also a nod to Hunter S. Thompson's "
    "The Curse of Lono.",
    "Here's a haiku about a GPU at night:\n\n  fans spin up, then hush\n  a thousand tokens per breath\n"
    "  the lightbar moves on",
    "Everything on this board is simulated: the services, the requests, the GPU. Point lonoboard at a real "
    "llama.cpp, Ollama or vLLM server and it fills in by itself - see `lonoboard check`.",
]
THOUGHT = ("The user is on the demo board. I should answer briefly and point them at the real thing: "
           "lonoboard finds llama.cpp, Ollama, vLLM and OpenAI-compatible servers on its own.")

SERVICES = [
    # name, kind, model id, params, quant, size, ctx, slots, tok/s, gpu MiB, manager, endpoints, description
    dict(name="qwen", kind="llama.cpp", model="qwen3-30b-a3b", params=30.5, quant="Q4_K - Medium", size=17.3, ctx=65536,
         slots=4, tps=96, gpu=18600, mgr=("systemd", "llama-server@qwen.service"), think=True, port=8080,
         endpoints=["/v1/chat/completions", "/v1/completions"], modalities=["text"],
         desc="Qwen3-30B-A3B, the everyday model: chat, code and tools. ~95 tok/s on one 3090."),
    dict(name="ollama", kind="ollama", port=11434, gpu=9800, mgr=("systemd", "ollama.service"),
         desc="Ollama for quick experiments; models load on demand."),
    dict(name="vllm", kind="vllm", model="meta-llama/Llama-3.1-8B-Instruct", ctx=32768, tps=142, gpu=12300,
         mgr=("docker", "vllm"), port=8000, endpoints=["/v1/chat/completions", "/v1/completions"],
         desc="vLLM in Docker for batch jobs; continuous batching keeps the second GPU busy."),
    dict(name="embed", kind="llama.cpp", model="bge-m3", params=0.57, quant="Q8_0", size=0.6, ctx=8192, slots=4,
         tps=None, gpu=1500, mgr=("systemd", "llama-server@embed.service"), port=8081,
         endpoints=["/v1/embeddings"], modalities=["text"], desc="BGE-M3 embeddings for the notes search."),
    dict(name="rerank", kind="llama.cpp", model="bge-reranker-v2-m3", params=0.57, quant="Q8_0", size=0.6, ctx=8192,
         slots=4, tps=None, gpu=1300, mgr=("systemd", "llama-server@rerank.service"), port=8082,
         endpoints=["/v1/rerank"], modalities=["text"], desc="Reranks search results before they reach the model."),
    dict(name="whisper", kind="custom", port=8090, gpu=1600, mgr=("process", None), endpoints=["/inference"],
         desc="whisper.cpp server: speech to text for voice notes."),
]
OLLAMA_MODELS = [("llama3.1:8b", 8.0, "Q4_K_M", 4.9, True), ("gemma3:12b", 12.2, "Q4_K_M", 8.1, True),
                 ("mistral-small3.2:24b", 24.0, "Q4_K_M", 15.0, False), ("qwen2.5-coder:7b", 7.6, "Q4_K_M", 4.7, False),
                 ("nomic-embed-text", 0.14, "F16", 0.3, False)]


class DemoCollector:
    def __init__(self, cfg, seed=None):
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.history = deque(maxlen=450)
        self.usage = Usage(None)
        self.started = time.time()
        self.name = cfg["board"]["name"] or "lono"
        self.lock = threading.Lock()
        self.busy = {s["name"]: [] for s in SERVICES}            # per service: [episode end times]
        self.restarting = {}                                     # name -> time it comes back
        self.restarts = {s["name"]: 0 for s in SERVICES}
        self.restart_at = {}
        self.gpu = {"util": 4.0, "temp": 38.0, "power": 62.0}
        now = time.time()
        self.boot = now - 3 * 86400 - 5 * 3600
        self._backfill_usage(now)
        for t in range(int(now - 900), int(now), SAMPLE_EVERY):   # 15 minutes of history
            self._step(t)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="lonoboard-demo").start()
        return self

    def _loop(self):
        while True:
            self._step(time.time())
            time.sleep(SAMPLE_EVERY)

    # ---------- the simulation ----------
    def _step(self, t):
        rng = self.rng
        with self.lock:
            busy_now = {}
            for s in SERVICES:
                n = s["name"]
                if n in self.restarting:
                    if t >= self.restarting[n]:
                        del self.restarting[n]
                    continue
                eps = [e for e in self.busy[n] if e > t]
                for _ in range(len(self.busy[n]) - len(eps)):
                    self._finished(s, t)
                self.busy[n] = eps
                rate = {"qwen": 0.03, "vllm": 0.045, "embed": 0.06, "rerank": 0.025}.get(n, 0)
                if rate and len(eps) < (s.get("slots") or 8) and rng.random() < rate:
                    self.busy[n].append(t + (rng.uniform(4, 28) if s.get("tps") else rng.uniform(0.5, 2.5)))
                if n == "ollama" and rng.random() < 0.02:
                    self._finished(s, t)
                if s["kind"] in ("llama.cpp", "vllm"):
                    busy_now[n] = len(self.busy[n])
            if rng.random() < 0.0015 and not self.restarting:
                who = rng.choice(["whisper", "rerank", "embed"])
                self.restarting[who] = t + rng.uniform(6, 14)
                self.restarts[who] += 1
                self.restart_at[who] = self.restarting[who]
                self.busy[who] = []
            load = busy_now.get("qwen", 0) * 26 + busy_now.get("vllm", 0) * 18 + busy_now.get("embed", 0) * 9 + busy_now.get("rerank", 0) * 6
            target = min(99, 3 + load + rng.uniform(-2, 2))
            g = self.gpu
            g["util"] += (target - g["util"]) * 0.6
            g["temp"] += ((36 + g["util"] * 0.42) - g["temp"]) * 0.15
            g["power"] = 58 + g["util"] * 5.6 + rng.uniform(-6, 6)
            used = sum(s["gpu"] for s in SERVICES if s["name"] not in self.restarting) / 1024
            self.history.append({"t": round(t, 1), "gpu": round(g["util"], 1), "temp": round(g["temp"], 1),
                                 "power": round(g["power"], 1), "mem": round(used + 0.8, 1), "busy": busy_now})

    def _finished(self, s, t):
        rng = self.rng
        if s["kind"] == "ollama":
            model = rng.choice([m for m in OLLAMA_MODELS if m[4]])
            self.usage.add_event({"at": int(t), "name": s["name"], "tokens": None, "gen_tps": None,
                                  "prompt_tokens": None, "prompt_tps": None, "duration_s": round(rng.uniform(0.8, 9), 1),
                                  "path": "/api/chat", "model": model[0], "source": "log"})
        elif s.get("tps"):
            tps = s["tps"] * rng.uniform(0.85, 1.08)
            toks = int(rng.uniform(80, 1800))
            prompt = int(rng.uniform(30, 4000))
            self.usage.add_event({"at": int(t), "name": s["name"], "tokens": toks, "gen_tps": round(tps, 1),
                                  "prompt_tokens": prompt, "prompt_tps": round(rng.uniform(900, 2400), 1), "source": "log"})
        elif s["kind"] == "llama.cpp":
            self.usage.add_event({"at": int(t), "name": s["name"], "tokens": None, "gen_tps": None,
                                  "prompt_tokens": int(rng.uniform(40, 900)), "prompt_tps": round(rng.uniform(2500, 6000), 1),
                                  "source": "log"})

    def _backfill_usage(self, now):
        rng = self.rng
        for h in range(24, 0, -1):
            hour_t = now - h * 3600
            local_h = datetime.fromtimestamp(hour_t).hour
            busy = 1.0 if 9 <= local_h <= 23 else 0.25
            for s in SERVICES:
                n = int(rng.uniform(0.5, 1.0) * {"qwen": 54, "vllm": 81, "embed": 108, "rerank": 45, "ollama": 36}.get(s["name"], 0) * busy)
                for _ in range(n):
                    self._finished(s, hour_t + rng.uniform(0, 3600))

    # ---------- the collector interface ----------
    def targets(self):
        return [{"name": s["name"], "kind": s["kind"], "url": f"http://127.0.0.1:{s['port']}"} for s in SERVICES]

    def snapshot(self, since=None, max_age=None):
        now = time.time()
        with self.lock:
            services = [self._service(s, now) for s in SERVICES]
            hist = [h for h in self.history if since is not None and h["t"] > since]
            g = dict(self.gpu)
        used = sum(s["memory_mib"]["gpu"] or 0 for s in services) / 1024 + 0.8
        host = {
            "hostname": self.name, "user": "ai", "os": "linux", "uptime_s": int(now - self.boot),
            "load": [round(0.4 + g["util"] / 40, 2), 1.1, 0.9], "cpus": 32, "cpu_model": "AMD Ryzen 9 7950X",
            "memory_gib": {"total": 125.7, "available": 71.2},
            "gpu": {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 3090", "count": 2, "util_pct": round(g["util"]),
                    "temp_c": round(g["temp"]), "power_w": round(g["power"], 1), "unified": False,
                    "mem_used_gib": round(used, 1), "mem_total_gib": 48.0},
            "platform": "2x rtx 3090", "memory_label": "48 gib vram · 126 gib ram",
            "software": ["llama.cpp b6510", "ollama 0.12.6", "vllm 0.10.2"],
            "tailnet": None,
            "board": {"name": board_name(dict(self.cfg, board=dict(self.cfg["board"], name=self.name))),
                      "tagline": self.cfg["board"]["tagline"] or "demo · simulated data",
                      "exposure": exposure_label(self.cfg), "version": __version__, "chat": bool(self.cfg["board"]["chat"])},
        }
        body = {"host": host, "services": services, "other": [], "fleet": [], "generated_at": int(now), "took_ms": 3}
        if since is not None:
            body["history"] = hist
        return body

    def _service(self, spec, now):
        t = {"name": spec["name"], "kind": spec["kind"], "url": f"http://127.0.0.1:{spec['port']}",
             "description": spec["desc"], "endpoints": spec.get("endpoints") or [], "origin": "demo"}
        s = base_service(t)
        restarting = spec["name"] in self.restarting
        mgr, unit = spec["mgr"]
        s["run"].update(manager=mgr, unit=unit, user=mgr == "systemd" and spec["kind"] != "ollama",
                        state="activating" if restarting else "active", sub_state="running", enabled=mgr != "process",
                        pid=40000 + spec["port"], since=None if restarting else (
                            self.started - (spec["port"] % 97) * 3511 - 7200 if self.restarts[spec["name"]] == 0
                            else self.restart_at.get(spec["name"], now)),
                        restarts=self.restarts[spec["name"]], container="5f2c1e9a7b3d" if mgr == "docker" else None,
                        image="vllm/vllm-openai:v0.10.2" if mgr == "docker" else None)
        s["health"] = "loading" if restarting else "ok"
        s["latency_ms"] = round(self.rng.uniform(0.6, 3.5), 1)
        s["memory_mib"] = {"gpu": None if restarting else spec["gpu"], "process": 900 + spec["gpu"] // 20}
        s["urls"] = {"local": t["url"], "remote": [f"http://{self.name}:{spec['port']}"]}
        s["version"] = {"llama.cpp": "llama.cpp b6510", "ollama": "ollama 0.12.6", "vllm": "vllm 0.10.2"}.get(spec["kind"])
        last, today = self.usage.last(spec["name"]), self.usage.today(spec["name"])
        if spec["kind"] == "ollama":
            s["endpoints"] = ["/v1/chat/completions", "/api/chat", "/api/generate", "/api/embed"]
            models = [dict(id=m[0], params_b=m[1], quant=m[2], size_gib=m[3], loaded=m[4], context=8192 if m[4] else None,
                           alias=None, context_train=None, input_modalities=None, file=None, args=None, slots=None,
                           slots_busy=None) for m in OLLAMA_MODELS]
            s["models"] = models
            primary = dict(models[0], loaded_count=sum(1 for m in models if m["loaded"]), installed_count=len(models))
            s["model"] = primary
            s["chat"] = {"url": t["url"], "models": [m["id"] for m in models if not m["id"].startswith("nomic")]}
        elif spec["kind"] == "custom":
            s["model"] = None
        else:
            busy = len(self.busy[spec["name"]])
            m = {"id": spec["model"], "alias": spec["model"], "params_b": spec.get("params"), "quant": spec.get("quant"),
                 "size_gib": spec.get("size"), "context": spec.get("ctx"), "context_train": spec.get("ctx"),
                 "input_modalities": spec.get("modalities"), "file": None, "args": None,
                 "slots": spec.get("slots"), "slots_busy": None if restarting else busy, "loaded": True}
            s["model"], s["models"] = m, [m]
            if "/v1/chat/completions" in s["endpoints"]:
                s["chat"] = {"url": t["url"], "models": [spec["model"]]}
        if s["model"] is not None:
            s["model"]["last_request"], s["model"]["requests_today"] = last, today
        if s["chat"] and not self.cfg["board"]["chat"]:
            s["chat"] = None
        return copy.deepcopy(s)

    def callers(self, n=30):
        return [dict(e) for e in self.usage.recent(n)]

    def usage_summary(self, hours=24):
        return self.usage.summary(hours)

    def logs(self, name, n=30):
        spec = next((s for s in SERVICES if s["name"] == name), None)
        if not spec:
            return []
        now = time.time()
        lines = []
        for i in range(min(n, 18)):
            t = datetime.fromtimestamp(now - (18 - i) * 37).strftime("%Y-%m-%dT%H:%M:%S")
            if spec["kind"] == "llama.cpp":
                msg = ["I srv  update_slots: all slots are idle", "I slot launch_slot_: id  0 | task 4182 | processing task",
                       "I slot print_timing: id  0 | task 4182 | eval time = 9182.11 ms /   873 tokens (95.07 tokens per second)",
                       "I slot      release: id  0 | task 4182 | stop processing: n_tokens = 1204"][i % 4]
            elif spec["kind"] == "ollama":
                msg = '[GIN] 200 |  2.481s |  127.0.0.1 | POST "/api/chat"'
            elif spec["kind"] == "vllm":
                msg = "INFO: Avg prompt throughput: 412.3 tokens/s, Avg generation throughput: 286.9 tokens/s, Running: 2 reqs"
            else:
                msg = "whisper_full: processed 14.2 s of audio in 0.61 s"
            lines.append(f"{t} {msg}")
        return lines + [f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} (demo: simulated log lines)"]

    def chat_stream(self, name, model, messages, cancelled=lambda: False):
        spec = next((s for s in SERVICES if s["name"] == name), {})
        tps = spec.get("tps") or 60
        text = REPLIES[sum(len(m["content"]) for m in messages) % len(REPLIES)]
        if spec.get("think"):
            for word in THOUGHT.split(" "):
                if cancelled():
                    yield ("error", "stopped")
                    return
                time.sleep(1.4 / tps * 2)
                yield ("reasoning", word + " ")
        n = 0
        for word in text.split(" "):
            if cancelled():
                yield ("error", "stopped")
                return
            time.sleep(1.3 / tps * 2)
            n += 2
            yield ("content", word + " ")
        yield ("stats", (n, tps * 0.97))

    def summary(self):
        snap = self.snapshot()
        g = snap["host"]["gpu"]
        return {"name": self.name, "hostname": self.name, "platform": snap["host"]["platform"], "version": __version__,
                "services_up": sum(1 for s in snap["services"] if s["health"] == "ok"), "services_total": len(snap["services"]),
                "busy": sum(1 for b in (self.history[-1]["busy"] if self.history else {}).values() if b),
                "gpu_util": g["util_pct"], "mem_used_gib": 54.5, "mem_total_gib": 125.7, "requests_today": self.usage.today()}
