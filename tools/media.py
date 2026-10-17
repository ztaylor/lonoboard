#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "pillow>=11"]
# ///
"""lonoboard's media, made with lonoboard.

The board runs on its demo data under a virtual clock. Every frame is drawn by the real renderer
and rasterized with the bundled IBM VGA font, and the promo's soundtrack is synthesized to the cuts.

    uv run tools/media.py promo [OUT.mp4]        # the promo: 1920x1080, 60 fps, 29.5 s, with sound,
                                                 # plus OUT-silent.mp4 (default build/promo/)
    uv run tools/media.py promo --still 12.5 x.png   # one frame of it, for working on the timeline
    uv run tools/media.py shots [DIR]            # README media: screenshots, hero.gif, social.png
                                                 # (default docs/media/)

Needs ffmpeg. numpy and Pillow come from the header above, so `uv run` fetches them; the package
itself stays standard-library only.
"""
import argparse
import collections
import contextlib
import copy
import math
import subprocess
import sys
import time as _time
from datetime import datetime as _datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ---- A virtual clock: the board and the demo run on video time ----------------------------

class Clock:
    t = _datetime(2026, 10, 17, 21, 40, 0).timestamp()      # an evening: the demo machine is busy


CLOCK = Clock()
START = CLOCK.t
_time.time = lambda: CLOCK.t
_time.sleep = lambda s: None                                 # the demo's chat paces itself with sleeps


class VDatetime(_datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(CLOCK.t, tz)


from lonoboard import demo, usage  # noqa: E402
from lonoboard.board import Board, Session  # noqa: E402
from lonoboard.board import hub as hubmod  # noqa: E402
from lonoboard.board import screens as screensmod  # noqa: E402
from lonoboard.board.logo import draw_logo, logo  # noqa: E402
from lonoboard.board.render import PAL_RGB, Screen, center_segs  # noqa: E402
from lonoboard.config import DEFAULTS  # noqa: E402

for _m in (demo, usage, screensmod):
    _m.datetime = VDatetime
hubmod.BaseHub.ensure_running = lambda self: None            # no hub threads: the rig feeds the hubs


# ---- The machines ---------------------------------------------------------------------------

SPARK_SERVICES = [
    dict(name="qwen", kind="llama.cpp", model="qwen3-next-80b-a3b", params=79.7, quant="Q4_K - Medium", size=45.1,
         ctx=131072, slots=4, tps=48, gpu=49800, mgr=("systemd", "llama@qwen.service"), think=True, port=8080,
         endpoints=["/v1/chat/completions", "/v1/completions"], modalities=["text"],
         desc="Qwen3-Next 80B with long context, in the Spark's unified memory."),
    dict(name="vllm", kind="vllm", model="openai/gpt-oss-120b", ctx=131072, tps=41, gpu=64000, mgr=("docker", "vllm"),
         port=8000, endpoints=["/v1/chat/completions", "/v1/completions"], desc="gpt-oss-120b in vLLM."),
    dict(demo.SERVICES[3]),
]
SPARK_HOST = {"platform": "dgx spark", "memory_label": "128 gib unified", "cpu_model": "NVIDIA Grace", "cpus": 20,
              "memory_gib": {"total": 119.6, "available": 4.2}, "software": ["llama.cpp b6510", "vllm 0.10.2"],
              "gpu": {"name": "NVIDIA GB10", "count": 1, "unified": True, "mem_total_gib": 119.6}}
PEERS = {
    "spark-1": {"name": "spark-1", "hostname": "spark-1", "platform": "dgx spark", "version": "0.1.0", "services_up": 6,
                "services_total": 6, "busy": 2, "gpu_util": 71, "mem_used_gib": 98.2, "mem_total_gib": 119.6,
                "requests_today": 1284, "ok": True},
    "mac-studio": {"name": "mac-studio", "hostname": "mac-studio", "platform": "apple m3 ultra", "version": "0.1.0",
                   "services_up": 2, "services_total": 2, "busy": 0, "gpu_util": 4, "mem_used_gib": 41.0,
                   "mem_total_gib": 192.0, "requests_today": 96, "ok": True},
}


class Machine(demo.DemoCollector):
    """The demo collector with its own name, services, hardware and fleet."""

    def __init__(self, cfg, seed, name, services=None, host=None, fleet=()):
        self.services, self.host_over, self.fleet_list = services or demo.SERVICES, host or {}, list(fleet)
        cfg = copy.deepcopy(cfg)
        cfg["board"]["name"] = name
        with self.mine():
            super().__init__(cfg, seed)

    @contextlib.contextmanager
    def mine(self):
        saved, demo.SERVICES = demo.SERVICES, self.services
        try:
            yield
        finally:
            demo.SERVICES = saved

    def _step(self, t):
        if not isinstance(self.restarts, collections.defaultdict):      # restarts pick from the demo's names
            self.restarts = collections.defaultdict(int, self.restarts)
        with self.mine():
            super()._step(t)

    def snapshot(self, since=None, max_age=None):
        with self.mine():
            d = super().snapshot(since, max_age)
        for k, v in self.host_over.items():
            if isinstance(v, dict) and isinstance(d["host"].get(k), dict):
                d["host"][k].update(v)
            else:
                d["host"][k] = v
        d["fleet"] = [dict(f) for f in self.fleet_list]
        return d

    def summary(self):
        with self.mine():
            r = super().summary()
        g = self.snapshot()["host"]["gpu"]
        return dict(r, mem_used_gib=g["mem_used_gib"], mem_total_gib=g["mem_total_gib"])

    def logs(self, name, n=30):
        with self.mine():
            return super().logs(name, n)


class Rig:
    """A board on demo data, driven by hand: the simulation, the hubs, one session, the chat stream."""

    STEP = 1.0              # the demo samples every 2 s; the video steps it every second, so it's busier

    def __init__(self, seed=7, cols=160, rows=45):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["_path"] = None
        cfg["board"]["intro"] = False
        cfg["fleet"]["tailscale"] = False
        fleet = [dict(name=n, url=f"http://{n}:7777", online=True, source="tailscale") for n in ("spark-1", "spark-2", "mac-studio")]
        CLOCK.t = START
        self.local = Machine(cfg, seed, "lono", fleet=fleet)
        self.spark = Machine(cfg, seed + 1, "spark-2", services=SPARK_SERVICES, host=SPARK_HOST)
        self.board = Board(self.local, cfg)
        self.remote = hubmod.Hub(self.spark)
        self.remote.remote, self.remote.label = True, "spark-2"
        self.board.remote = lambda name, url: self.remote
        self.next_step = START
        self.stream = None
        self.usage_grow = None              # (start time, seconds): the usage bars grow in
        self.advance(START)
        self.sess = Session(self.board, "telnet", cols, rows, utf8=True, intro=False)
        self.sess.resize(cols, rows)
        self.board.hub.join(self.sess)
        self.pal = "vga"
        rig = self

        def send(chat, text, sess):          # the chat, streamed on the video's clock rather than a thread
            chat.msgs.append({"role": "user", "content": text})
            reply = {"role": "assistant", "content": "", "reasoning": "", "stats": None, "error": None,
                     "t0": CLOCK.t, "think_s": None}
            history = [{"role": m["role"], "content": m["content"]} for m in chat.msgs if m["content"]]
            chat.msgs.append(reply)
            chat.busy, chat.cancel, chat.scroll = True, False, 0
            items, t = [], CLOCK.t + 0.15
            for kind, value in sess.hub.chat_stream(chat.service, chat.model, history, lambda: False):
                t += {"reasoning": 1 / 60, "content": 1 / 12, "stats": 0.05}.get(kind, 0)
                items.append((t, kind, value))
            rig.stream = {"chat": chat, "reply": reply, "items": items}

        hubmod.Chat.send = send

    def advance(self, now):
        while self.next_step <= now:
            CLOCK.t = self.next_step
            for m in (self.local, self.spark):
                m._step(CLOCK.t)
            for h in (self.board.hub, self.remote):
                h.ingest(h.collector.snapshot(since=h.last_t), extend_history=False)
                h.load_callers()
                h.load_usage()
            self.board.hub.fleet = dict(PEERS, **{"spark-2": dict(self.spark.summary(), ok=True)})
            self.next_step += self.STEP
        CLOCK.t = now
        if self.stream and self.stream["chat"].cancel:          # esc stops a reply, as on the real board
            self.stream["reply"]["error"] = "stopped"
            self.stream["chat"].busy, self.stream = False, None
        if self.stream:
            s, reply = self.stream, self.stream["reply"]
            while s["items"] and s["items"][0][0] <= now:
                _, kind, value = s["items"].pop(0)
                if kind == "reasoning":
                    reply["reasoning"] += value
                elif kind == "content":
                    if reply["think_s"] is None and reply["reasoning"]:
                        reply["think_s"] = now - reply["t0"]
                    reply["content"] += value
                elif kind == "stats":
                    reply["stats"] = value
            if not s["items"]:
                s["chat"].busy, self.stream = False, None

    def keys(self, *keys):
        for k in keys:
            self.sess.handle(k)

    def cells(self, t):
        """The session's screen as cell rows, and its cursor."""
        self.sess.tick = int(t * 7)
        hub = self.sess.hub
        saved = hub.usage
        if self.usage_grow and saved:
            t0, length = self.usage_grow
            p = min(1.0, max(0.0, (CLOCK.t - t0) / length))
            p = 1 - (1 - p) ** 3
            hub.usage = dict(saved, hours=[dict(h, requests=int(h["requests"] * p), tokens=int(h["tokens"] * p))
                                           for h in saved["hours"]])
        try:
            scr, cursor = self.sess.render()
        finally:
            hub.usage = saved
        return scr.rows, cursor


# ---- Cells to pixels ----------------------------------------------------------------------------

FONT_FILE = ROOT / "src/lonoboard/static/fonts/PxPlus_IBM_VGA_8x16.ttf"
CW, CH = 8, 16
BLINK_PERIOD = 1.06                  # the web terminal's blink and cursor rate


class Raster:
    def __init__(self):
        self.font = ImageFont.truetype(str(FONT_FILE), 16)
        self.ids, self.masks = {}, []

    def gid(self, ch):
        i = self.ids.get(ch)
        if i is None:
            im = Image.new("L", (CW, CH))
            ImageDraw.Draw(im).text((0, 0), ch, font=self.font, fill=255, anchor="la")
            i = self.ids[ch] = len(self.masks)
            self.masks.append(np.array(im) > 127)
            self.stack = np.stack(self.masks)
        return i

    def image(self, rows, pal="vga", t=0.0, cursor=None):
        """Cell rows [(ch, fg, bg, link)] -> an RGB array, 16 px per row and 8 per column."""
        h, w = len(rows), max(len(r) for r in rows)
        g = np.zeros((h, w), np.int32)
        f = np.zeros((h, w), np.int32)
        b = np.zeros((h, w), np.int32)
        blink_off = (t % BLINK_PERIOD) >= BLINK_PERIOD / 2
        space = self.gid(" ")
        for y, r in enumerate(rows):
            for x, c in enumerate(r):
                ch, fg, bg = c[0], c[1], c[2]
                g[y, x] = space if (fg & 16 and blink_off) else self.gid(ch)
                f[y, x], b[y, x] = fg & 15, bg & 15
        p = np.array(PAL_RGB[pal], np.uint8)
        m = self.stack[g]
        img = np.where(m[..., None], p[f][:, :, None, None, :], p[b][:, :, None, None, :])
        img = img.transpose(0, 2, 1, 3, 4).reshape(h * CH, w * CW, 3)
        if cursor is not None and not blink_off:
            cx, cy = cursor
            if 0 <= cx < w and 0 <= cy < h:
                img[cy * CH + 14:cy * CH + 16, cx * CW:(cx + 1) * CW] = p[7]
        return np.ascontiguousarray(img)


def text_screen(lines, w):
    """A Screen from [(x or 'c', y, segs)]: segs are (text, fg[, bg])."""
    s = Screen(w)
    for x, y, segs in lines:
        if x == "c":
            center_segs(s, y, segs)
        else:
            s.segs(x, y, segs)
    return s


# ---- The camera and the CRT ---------------------------------------------------------------------

OUT_W, OUT_H = 1920, 1080


def ease(p):
    p = min(1.0, max(0.0, p))
    return p * p * (3 - 2 * p)


def keyed(keys, t):
    """Camera keys [(t, cx, cy, zoom)]: hold before the first, ease between, hold after the last."""
    if t <= keys[0][0]:
        return keys[0][1:]
    for (t0, *a), (t1, *b) in zip(keys, keys[1:]):
        if t <= t1:
            p = ease((t - t0) / (t1 - t0)) if t1 > t0 else 1.0
            return tuple(x + (y - x) * p for x, y in zip(a, b))
    return keys[-1][1:]


def shoot(native, cx, cy, zoom, out=(OUT_W, OUT_H)):
    """Frame `native` (an RGB array) for the output: centre (cx, cy) in 0..1, zoom 1 = fit.
    Returns the image and the mapping (y0, scale) of output rows to native rows, for the scanlines."""
    ow, oh = out
    nh, nw = native.shape[:2]
    scale = min(ow / nw, oh / nh) * zoom
    cw, ch = ow / scale, oh / scale
    x0 = min(max(cx * nw - cw / 2, min(0, (nw - cw) / 2)), max(0, nw - cw) if cw <= nw else (nw - cw) / 2)
    y0 = min(max(cy * nh - ch / 2, min(0, (nh - ch) / 2)), max(0, nh - ch) if ch <= nh else (nh - ch) / 2)
    k = max(1, math.ceil(scale))                       # nearest up to a whole multiple, then smooth down
    pad = 2
    ix0, iy0 = max(0, int(math.floor(x0)) - pad), max(0, int(math.floor(y0)) - pad)
    ix1, iy1 = min(nw, int(math.ceil(x0 + cw)) + pad), min(nh, int(math.ceil(y0 + ch)) + pad)
    canvas = Image.new("RGB", (ow, oh))
    if ix1 > ix0 and iy1 > iy0:
        crop = Image.fromarray(native[iy0:iy1, ix0:ix1]).resize(((ix1 - ix0) * k, (iy1 - iy0) * k), Image.NEAREST)
        box = ((x0 - ix0) * k, (y0 - iy0) * k, (x0 - ix0 + cw) * k, (y0 - iy0 + ch) * k)
        bx0, by0 = max(0.0, box[0]), max(0.0, box[1])
        bx1, by1 = min(crop.width, box[2]), min(crop.height, box[3])
        dx0, dy0 = round((bx0 - box[0]) / k * scale), round((by0 - box[1]) / k * scale)
        dw, dh = round((bx1 - bx0) / k * scale), round((by1 - by0) / k * scale)
        if dw > 0 and dh > 0:
            part = crop.resize((dw, dh), Image.LANCZOS, box=(bx0, by0, bx1, by1))
            canvas.paste(part, (dx0, dy0))
    return canvas, (y0, scale)


class CRT:
    """The web terminal's CRT effect: phosphor bloom, a scanline per font pixel row, a vignette."""

    def __init__(self, size=(OUT_W, OUT_H)):
        w, h = size
        self.size = size
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / math.sqrt(2)
        self.vignette = (1 - 0.5 * np.clip((r - 0.62) / 0.38, 0, 1) ** 1.6)[..., None]

    def scanlines(self, y0, scale):
        """Per output row: the part of it that falls on the dark top 1/4 of a native pixel row."""
        h = self.size[1]
        top = y0 + np.arange(h, dtype=np.float64) / scale
        bot = top + 1 / scale
        dark = np.zeros(h)
        for k in range(int(math.floor(top[0])) - 1, int(math.ceil(bot[-1])) + 1):
            dark += np.clip(np.minimum(bot, k + 0.25) - np.maximum(top, k), 0, None)
        return (1 - 0.28 * dark * scale)[:, None, None].astype(np.float32)

    def __call__(self, img, mapping, strength=1.0):
        a = np.asarray(img, np.float32) / 255
        w, h = self.size
        small = img.resize((w // 2, h // 2), Image.BILINEAR).filter(ImageFilter.GaussianBlur(3))
        glow = np.asarray(small.resize((w, h), Image.BILINEAR), np.float32) / 255
        a = 1 - (1 - a) * (1 - 0.5 * strength * glow)
        if mapping:
            a = a * (1 - strength + strength * self.scanlines(*mapping))
        a = a * (1 - strength + strength * self.vignette)
        return np.clip(a * 255, 0, 255).astype(np.uint8)


# ---- The promo's timeline -----------------------------------------------------------------------

FPS = 60
BEAT = 0.5                           # 120 bpm: every cut lands on a beat
TOTAL = 29.5
PAINT, BOARD, SERVICES, CHAT, USAGE, FLEET, LOOKS, END, OFF = 2.2, 3.0, 7.0, 11.0, 16.0, 19.0, 23.0, 25.0, 28.75
SWEEP = 0.16                         # a new screen paints top to bottom, the way it would arrive over a line
INTRO_TYPE = (0.35, "telnet lono 2323", 15)
CHAT_PROMPT = (11.45, "write a haiku about my GPU", 24)

CAPTIONS = [
    (BOARD + 0.25, SERVICES - 0.1, "every model on your machine, live"),
    (SERVICES + 0.25, CHAT - 0.1, "llama.cpp · ollama · vllm · found on their own"),
    (CHAT + 0.25, USAGE - 0.1, "talk to them"),
    (USAGE + 0.25, FLEET - 0.1, "see what they did today"),
    (FLEET + 0.25, LOOKS - 0.1, "all your machines, one board"),
    (LOOKS + 0.25, END - 0.1, "any terminal · any browser · no dependencies"),
]

CAMERA = [                           # (t, cx, cy, zoom): eased between keys, cut at a key repeated in time
    (0.0, 0.0, 0.0, 2.6), (PAINT - 0.01, 0.0, 0.0, 2.6),
    (PAINT, 0.5, 0.5, 1.0), (BOARD, 0.5, 0.5, 1.0), (SERVICES - 0.05, 0.36, 0.55, 1.32),
    (SERVICES, 0.5, 0.5, 1.0), (SERVICES + 0.6, 0.5, 0.5, 1.0), (CHAT - 0.05, 0.72, 0.4, 1.3),
    (CHAT, 0.5, 0.45, 1.12), (USAGE - 0.05, 0.5, 0.42, 1.38),
    (USAGE, 0.5, 0.5, 1.0), (FLEET - 0.05, 0.3, 0.3, 1.45),
    (FLEET, 0.0, 0.22, 1.5), (FLEET + 1.95, 0.0, 0.22, 1.5),          # the machine names stay in shot
    (FLEET + 2.0, 0.5, 0.5, 1.0), (LOOKS - 0.05, 0.5, 0.9, 1.1),       # keep the magenta remote bar in shot
    (LOOKS, 0.5, 0.47, 0.84), (END - 0.01, 0.5, 0.47, 0.84),           # each look as a screen in the dark
    (END, 0.5, 0.42, 1.5), (TOTAL, 0.5, 0.42, 1.62),
]


def typing(t0, text, cps):
    return [(t0 + i / cps, ch) for i, ch in enumerate(text)]


def actions():
    """(time, keys) the session gets, in order."""
    a = [(BOARD - 0.001, ())]
    a += [(SERVICES, ("s",)), (SERVICES + 1.0, ("down",)), (SERVICES + 2.0, ("down",)), (SERVICES + 3.0, ("up",)),
          (SERVICES + 3.5, ("up",)), (CHAT, ("c",))]
    a += [(t, (ch,)) for t, ch in typing(*CHAT_PROMPT)]
    a += [(CHAT_PROMPT[0] + len(CHAT_PROMPT[1]) / CHAT_PROMPT[2] + 0.15, ("enter",))]
    a += [(USAGE - 0.02, ("esc",)), (USAGE, ("u",)), (FLEET, ("f",)), (FLEET + 0.75, ("down",)), (FLEET + 1.25, ("down",)),
          (FLEET + 2.0, ("enter",)), (LOOKS, ("esc",))]
    return a


LOOK_CUTS = [(LOOKS, "amber", 160), (LOOKS + 0.5, "green", 160), (LOOKS + 1.0, "vga", 80), (LOOKS + 1.5, "amber", 80)]


def intro_screen(t):
    t0, text, cps = INTRO_TYPE
    n = max(0, min(len(text), int((t - t0) * cps)))
    lines = [(1, 1, [("$ ", 10), (text[:n], 15)])]
    done = t0 + len(text) / cps + 0.12
    out = [(done + 0.10, "Trying 100.96.12.7..."), (done + 0.35, "Connected to lono."),
           (done + 0.45, "Escape character is '^]'."), (done + 0.75, "connecting to lono...")]
    y, cursor = 2, (3 + n, 1)
    for at, line in out:
        if t >= at:
            lines.append((1, y, [(line, 15 if line.startswith("connecting") else 7)]))
            cursor = (1 + len(line), y) if line.startswith("connecting") else (1, y + 1)
            y += 1 + line.startswith("Escape")
    s = text_screen(lines, 160)
    s.row(44)
    return s.rows, cursor


def end_screen(t):
    s = Screen(160)
    s.row(44)
    rng = np.random.default_rng(5)
    for _ in range(46):
        x, y = int(rng.integers(0, 160)), int(rng.integers(0, 45))
        if not 8 <= y <= 31:
            s.put(x, y, "·∙"[int(rng.integers(0, 2))], int(rng.choice([1, 8, 9])))
    px = logo([("LONO", "A"), ("BOARD", "B")], 2)
    x0 = (160 - px[0]) // 2
    draw_logo(s, x0, 11, px)
    lines = [(END + 0.5, 20, [("a live dashboard for the machines running your AI models", 15)]),
             (END + 0.85, 22, [("drawn like a 1990s ANSI BBS", 7)]),
             (END + 1.3, 25, [("telnet or browser", 3), ("  ∙  ", 9), ("llama.cpp · ollama · vllm", 3), ("  ∙  ", 9),
                              ("pure python", 3)]),
             (END + 1.75, 28, [("github.com/ztaylor/lonoboard", 11)])]
    cursor = None
    for at, y, segs in lines:
        full = "".join(x[0] for x in segs)
        n = max(0, min(len(full), int((t - at) * 70)))
        if n == 0:
            continue
        shown, left = [], n
        for seg in segs:
            shown.append((seg[0][:left], seg[1]))
            left -= len(seg[0][:left])
        x = (160 - len(full)) // 2
        s.segs(x, y, shown)
        cursor = (x + n, y)
    return s.rows, cursor


def caption_image(raster, text, t, t0):
    n = max(0, min(len(text), int((t - t0) * 90)))
    segs = [("░▒▓", 1), (" " + text[:n] + " " * (len(text) - n) + " ", 15, 1), ("▓▒░", 1)]
    s = text_screen([(0, 0, segs)], sum(len(x[0]) for x in segs))
    cur = (4 + n, 0) if n < len(text) or ((t - t0) % BLINK_PERIOD) < BLINK_PERIOD / 2 else None
    img = raster.image(s.rows, "vga", 0.0, cur)
    return Image.fromarray(img).resize((img.shape[1] * 3, img.shape[0] * 3), Image.NEAREST)


class Promo:
    def __init__(self):
        self.rig = Rig()
        self.raster = Raster()
        self.crt = CRT()
        self.acts = actions()
        self.prev_img, self.prev_id, self.sweep_at, self.sweep_from = None, None, -9, None

    def native(self, t):
        rig = self.rig
        while self.acts and self.acts[0][0] <= t:
            at, keys = self.acts.pop(0)
            rig.advance(START + at)
            if keys == ("u",):
                rig.usage_grow = (START + at, 0.9)
            rig.keys(*keys)
        rig.advance(START + t)
        if t < PAINT:
            rows, cursor = intro_screen(t)
            return self.raster.image(rows, "vga", t, cursor), ("intro",)
        if t >= END:
            rows, cursor = end_screen(t)
            return self.raster.image(rows, "vga", t, cursor), ("end",)
        pal, cols = "vga", 160
        for at, p, c in LOOK_CUTS:
            if t >= at:
                pal, cols = p, c
        if (rig.sess.cols, rig.sess.rows) != ((cols, 45) if cols == 160 else (80, 25)):
            rig.sess.resize(*((cols, 45) if cols == 160 else (80, 25)))
        rows, cursor = rig.cells(t)
        s = rig.sess
        ident = (s.screen, id(s.hub), s.modal[0] if s.modal else None, s.cols)
        return self.raster.image(rows, pal, t, cursor), ident

    def step(self, t):
        """The native picture at t, with any paint sweep in progress. Call for every frame, in order."""
        img, ident = self.native(t)
        if ident != self.prev_id and self.prev_img is not None:
            self.sweep_at, self.sweep_from = t, self.prev_img
        self.prev_id = ident
        if t - self.sweep_at < SWEEP and self.sweep_from is not None and self.sweep_from.shape == img.shape and ident[0] != "end":
            r = int((t - self.sweep_at) / SWEEP * img.shape[0])
            img = img.copy()
            img[r:] = self.sweep_from[r:]
        elif ident == ("end",) and t - END < 0.45:            # the end card paints in at line speed too
            r = int((t - END) / 0.45 * img.shape[0])
            img = img.copy()
            img[r:] = 0
        elif t < PAINT + 0.7 and ident[0] != "intro":         # the board's first paint
            r = int((t - PAINT) / 0.7 * img.shape[0])
            img = img.copy()
            img[r:] = 0
        self.prev_img = img
        return img

    def frame(self, t):
        img = self.step(t)
        cx, cy, zoom = keyed(CAMERA, t)
        out, mapping = shoot(img, cx, cy, zoom)
        for t0, t1, text in CAPTIONS:
            if t0 <= t < t1:
                cap = caption_image(self.raster, text, t, t0)
                x, y = (OUT_W - cap.width) // 2, 1006 if t0 >= LOOKS else 868
                shade = Image.new("RGBA", (cap.width + 36, cap.height + 28), (0, 0, 0, 235))
                out.paste(shade, (x - 18, y - 14), shade)
                out.paste(cap, (x, y))
        a = self.crt(out, mapping)
        return power_off(a, t)


def power_off(a, t):
    """The tube going off: the picture folds to a bright line, the line to a dot, the dot fades."""
    if t < OFF:
        return a
    p = t - OFF
    h, w = a.shape[:2]
    out = np.zeros_like(a)
    if p < 0.16:
        lines = max(2, int(h * (1 - ease(p / 0.16)) ** 2))
        img = Image.fromarray(a).resize((w, lines), Image.BILINEAR)
        band = np.asarray(img, np.float32) * (1 + 2.5 * ease(p / 0.16))
        out[(h - lines) // 2:(h - lines) // 2 + lines] = np.clip(band, 0, 255).astype(np.uint8)
    elif p < 0.36:
        q = ease((p - 0.16) / 0.2)
        half = max(2, int(w / 2 * (1 - q) ** 2))
        out[h // 2 - 1:h // 2 + 2, w // 2 - half:w // 2 + half] = (235, 245, 255)
    elif p < 0.7:
        v = int(255 * (1 - (p - 0.36) / 0.34))
        out[h // 2 - 2:h // 2 + 3, w // 2 - 3:w // 2 + 4] = (v, v, v)
    return out


# ---- The soundtrack: a Suicide drum box and fuzz organ, minimalist Aphex in the middle --------

RATE = 48_000


class Mix:
    def __init__(self, seconds):
        self.l = np.zeros(int(seconds * RATE) + RATE, np.float32)
        self.r = np.zeros_like(self.l)

    def add(self, at, sig, gain=1.0, pan=0.0):
        i = int(at * RATE)
        if i >= len(self.l):
            return
        sig = sig[:len(self.l) - i] * gain
        self.l[i:i + len(sig)] += sig * math.cos((1 + pan) * math.pi / 4)
        self.r[i:i + len(sig)] += sig * math.sin((1 + pan) * math.pi / 4)


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def tt(seconds):
    return np.arange(int(seconds * RATE), dtype=np.float32) / RATE


def env_ad(t, attack, decay):
    return np.minimum(1, t / max(attack, 1e-4)) * np.exp(-t / decay)


def comb(x, d, g):
    y = x.copy()
    for s in range(d, len(y), d):
        e = min(s + d, len(y))
        y[s:e] += g * y[s - d:e - d]
    return y


def allpass(x, d, g):
    y = -g * x
    y[d:] += x[:-d]
    for s in range(d, len(y), d):
        e = min(s + d, len(y))
        y[s:e] += g * y[s - d:e - d]
    return y


def reverb(x, size=1.0, damp=0.0):
    out = sum(comb(x, int(d * size), g) for d, g in ((1557, .84), (1617, .83), (1491, .85), (1422, .86))) / 4
    for d in (225, 556):
        out = allpass(out, d, 0.5)
    if damp:
        out = np.convolve(out, np.ones(int(damp)) / int(damp), "same").astype(np.float32)
    return out


def noise(n, seed):
    return np.random.default_rng(seed).uniform(-1, 1, n).astype(np.float32)


def kick(seed=0):
    t = tt(0.32)
    ph = 2 * np.pi * np.cumsum(48 + 110 * np.exp(-t * 38)) / RATE
    return np.tanh(2.2 * np.sin(ph) * np.exp(-t * 9)) * 0.9 + noise(len(t), seed) * 0.06 * np.exp(-t * 60)


def snare(seed):
    t = tt(0.22)
    n = noise(len(t), seed)
    hp = np.diff(n, prepend=0)
    return hp * np.exp(-t * 16) * 0.7 + np.sin(2 * np.pi * 185 * t) * np.exp(-t * 30) * 0.4


def hat(seed, length=0.05):
    t = tt(length)
    metal = sum(np.sign(np.sin(2 * np.pi * f * t)) for f in (5210, 6770, 8130, 9580)) / 4
    return (metal * 0.5 + np.diff(noise(len(t), seed), prepend=0) * 0.6) * np.exp(-t * 70)


def organ(f, length, bright=1.0, fuzz=2.6):
    """A cheap combo organ through a fuzz box: additive saw, brightness in 0..1, then tanh."""
    t = tt(length)
    ph = 2 * np.pi * f * t
    x = np.zeros_like(t)
    for k in range(1, 18):
        if f * k > 7000:
            break
        x += np.sin(k * ph) / k * math.exp(-k * (1.6 - 1.3 * bright) * 0.35)
    gate = np.minimum(1, t / 0.004) * np.minimum(1, (length - t) / 0.012)
    return np.tanh(fuzz * x * 0.5) * gate * (1 + 0.12 * np.sin(2 * np.pi * 6.2 * t))


def pad(notes, length, attack=0.9, release=1.2):
    """Detuned soft saws, a few harmonics only: the Selected Ambient Works hum."""
    t = tt(length)
    x = np.zeros_like(t)
    for n in notes:
        for det in (-0.08, 0.08):
            f = midi(n + det)
            ph = 2 * np.pi * np.cumsum(f * (1 + 0.003 * np.sin(2 * np.pi * 0.31 * t))) / RATE
            for k in range(1, 5):
                x += np.sin(k * ph) / (k * k)
    e = np.minimum(1, t / attack) * np.minimum(1, np.maximum(0, (length - t) / release))
    return x * e / (len(notes) * 2)


def bell(n, length=2.2):
    t = tt(length)
    f = midi(n) * (1 + 0.004 * np.sin(2 * np.pi * 0.8 * t))       # a little wow, like a worn tape
    ph = 2 * np.pi * np.cumsum(f) / RATE
    return (np.sin(ph + 0.9 * np.sin(2 * ph) * np.exp(-t * 3)) * np.exp(-t * 1.9)
            + 0.25 * np.sin(3.01 * ph) * np.exp(-t * 4.5)) * np.minimum(1, t / 0.003)


def soundtrack():
    total = TOTAL
    drums, music, send, fx = Mix(total), Mix(total), Mix(total), Mix(total)
    seed = iter(range(1, 100000))

    # Typing on the intro and in the chat: quiet key clicks.
    keys = typing(*INTRO_TYPE) + typing(*CHAT_PROMPT)
    for at, _ in keys:
        t = tt(0.03)
        fx.add(at, np.diff(noise(len(t), next(seed)), prepend=0) * np.exp(-t * 220), 0.12, 0.2)
    # Connected: a nod to the modem - two FSK tones, very quiet - then a riser into the first paint.
    t_conn = INTRO_TYPE[0] + len(INTRO_TYPE[1]) / INTRO_TYPE[2] + 0.5
    t = tt(0.32)
    fsk = np.sin(2 * np.pi * np.cumsum(np.where((t * 40).astype(int) % 2, 1200, 2200)) / RATE) * np.minimum(1, (0.32 - t) / 0.05)
    fx.add(t_conn, fsk, 0.05, -0.2)
    t = tt(PAINT + 0.7 - t_conn)
    riser = np.convolve(noise(len(t), 9), np.ones(6) / 6, "same") * (t / t[-1]) ** 2
    fx.add(t_conn, riser.astype(np.float32), 0.18)

    # The drum box: from the top, quiet and dark until the board paints.
    bar_notes = [45, 45, 43, 45]                     # A, A, G, A: a bar (2 s) each, round and round
    riff = [0, 0, 12, 0, 7, 0, 10, 12]               # eighths over the root
    beat = 0
    while beat * BEAT < OFF:
        t0 = beat * BEAT
        pos = beat % 4
        section = ("intro" if t0 < BOARD else "break" if CHAT <= t0 < USAGE - BEAT else
                   "looks" if LOOKS <= t0 < END else "end" if t0 >= END else "groove")
        soft = 0.45 if section == "intro" else 1.0
        if section in ("intro", "groove"):
            drums.add(t0, kick(), 0.9 * soft)
            if pos == 1:
                drums.add(t0 + BEAT * 0.5, kick(), 0.5 * soft)
            if pos in (1, 3) and section == "groove":
                drums.add(t0, snare(next(seed)), 0.42, 0.08)
        if section in ("intro", "groove", "break"):
            for e in range(2):
                drums.add(t0 + e * BEAT / 2, hat(next(seed)), (0.16 if e else 0.1) * {"break": 0.6, "intro": 0.25}.get(section, 1.0), -0.25)
        if section == "groove" and t0 >= BOARD:
            root = bar_notes[int((t0 - BOARD) / (4 * BEAT)) % 4]
            for e in range(2):
                n = root + riff[(pos * 2 + e) % 8] - 12
                sweep = min(1.0, 0.35 + (t0 - BOARD) / 4)
                note = organ(midi(n), BEAT / 2 * 0.92, sweep)
                music.add(t0 + e * BEAT / 2, note, 0.2, -0.1)
                send.add(t0 + e * BEAT / 2, note, 0.05)
                music.add(t0 + e * BEAT / 2, organ(midi(root - 24), BEAT / 2 * 0.95, 0.2, 1.6), 0.22)
        beat += 1
    # A snare roll into the second half.
    for i in range(8):
        drums.add(USAGE - BEAT * 2 + i * BEAT / 4, snare(next(seed)), 0.18 + i * 0.035, 0.1)

    # The break: a pad, and a few bell notes over the chat.
    music.add(CHAT, pad([57, 64, 67, 71, 72], USAGE - CHAT + 0.6, 0.8, 0.8), 0.5)
    send.add(CHAT, pad([57, 64, 67, 71, 72], USAGE - CHAT + 0.6, 0.8, 0.8), 0.25)
    for k, (dt, n) in enumerate([(0.5, 76), (1.5, 72), (2.0, 69), (3.0, 71), (3.5, 76), (4.25, 74)]):
        b = bell(n)
        music.add(CHAT + dt, b, 0.16, (-0.4, 0.3, -0.1, 0.4, -0.3, 0.2)[k])
        send.add(CHAT + dt, b, 0.18)

    # A whoosh as the board flips to the other machine.
    t = tt(0.6)
    whoosh = np.convolve(noise(len(t), 77), np.ones(12) / 12, "same") * np.sin(np.pi * t / 0.6) ** 2
    fx.add(FLEET + 2.0 - 0.45, whoosh.astype(np.float32), 0.35, 0.3)

    # The looks: a hit on every cut.
    for i, (at, _, _) in enumerate(LOOK_CUTS):
        drums.add(at, kick(), 1.0)
        drums.add(at, snare(next(seed)), 0.35)
        n = [45, 48, 43, 40][i]
        music.add(at, organ(midi(n), BEAT * 0.8, 0.9), 0.24)
        music.add(at, organ(midi(n + 12), BEAT * 0.8, 0.9), 0.12)
        send.add(at, organ(midi(n + 12), BEAT * 0.8, 0.9), 0.08)

    # The end card: a held chord, the bell, then the tube going off.
    chord = pad([45, 52, 57, 60, 64, 71], OFF - END + 0.3, 0.05, 0.6)
    music.add(END, chord, 0.75)
    send.add(END, chord, 0.4)
    music.add(END, organ(midi(33), 1.2, 0.3, 1.8) * np.exp(-tt(1.2) * 2.2), 0.4)
    drums.add(END, kick(), 1.0)
    for dt, n in ((0.0, 76), (0.75, 72), (1.5, 81), (2.6, 79)):
        music.add(END + dt, bell(n, 2.6), 0.15)
        send.add(END + dt, bell(n, 2.6), 0.22)
    t = tt(0.6)
    ph = 2 * np.pi * np.cumsum(30 + 700 * np.exp(-t * 7)) / RATE
    fx.add(OFF, np.sin(ph) * np.exp(-t * 4), 0.3)
    t = tt(0.05)
    fx.add(OFF, np.diff(noise(len(t), 5), prepend=0) * np.exp(-t * 90), 0.3)

    # A tick on every cut.
    for at in (PAINT, BOARD, SERVICES, CHAT, USAGE, FLEET, FLEET + 2.0, LOOKS, END):
        t = tt(0.04)
        fx.add(at, np.sin(2 * np.pi * 2400 * t) * np.exp(-t * 120), 0.06)

    # Spring-ish reverb on the send; the drums stay dry and a little crushed, like a cheap rhythm box.
    wet_l, wet_r = reverb(send.l, 1.0, 3), reverb(send.r, 1.13, 3)
    crush = lambda x: np.round(x * 160) / 160
    dl, dr = crush(drums.l), crush(drums.r)
    n = int(total * RATE)
    left = dl[:n] + music.l[:n] + wet_l[:n] * 0.9 + fx.l[:n]
    right = dr[:n] + music.r[:n] + wet_r[:n] * 0.9 + fx.r[:n]
    t = np.arange(n) / RATE
    fade = np.minimum(1, t / 0.01) * np.clip((total - t) / 0.25, 0, 1)
    left, right = np.tanh(left * 1.1) * fade, np.tanh(right * 1.1) * fade
    peak = max(np.abs(left).max(), np.abs(right).max(), 1e-6)
    return left / peak * 0.89, right / peak * 0.89


def write_wav(path, left, right):
    import wave
    data = np.stack([left, right], 1)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


# ---- promo ----------------------------------------------------------------------------------------

def promo(out, still=None):
    p = Promo()
    if still is not None:
        n = int(round(still * FPS))
        for i in range(n):
            p.step(i / FPS)
        Image.fromarray(p.frame(n / FPS)).save(out)
        print(f"wrote {out}")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    video, audio = out.with_suffix(".video.mp4"), out.with_suffix(".wav")
    silent = out.with_name(out.stem + "-silent.mp4")
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{OUT_W}x{OUT_H}", "-framerate", str(FPS), "-i", "-",
                           "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-maxrate", "24M", "-bufsize", "48M",
                           "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart", str(video)],
                          stdin=subprocess.PIPE)
    frames = int(round(TOTAL * FPS))
    t_start = _time.monotonic()
    for i in range(frames):
        ff.stdin.write(p.frame(i / FPS).tobytes())
        if i % (FPS * 5) == 0:
            print(f"{i / FPS:5.1f} s  ({_time.monotonic() - t_start:.0f} s)", file=sys.stderr)
    ff.stdin.close()
    if ff.wait():
        sys.exit("ffmpeg could not encode the video")
    left, right = soundtrack()
    write_wav(audio, left, right)
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio), "-c:v", "copy",
                        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
                        "-shortest", "-movflags", "+faststart", str(out)])
    if r.returncode:
        sys.exit("ffmpeg could not add the sound")
    video.replace(silent)
    audio.unlink()
    print(f"wrote {out} and {silent}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("promo", help="the promo video")
    sp.add_argument("out", nargs="?", type=Path, default=ROOT / "build/promo/lonoboard-promo.mp4")
    sp.add_argument("--still", type=float, help="write one frame at this time instead, as a PNG")
    sp.add_argument("--audio", action="store_true", help="write just the soundtrack, as a WAV")
    sh = sub.add_parser("shots", help="README screenshots, hero.gif and social.png")
    sh.add_argument("dir", nargs="?", type=Path, default=ROOT / "docs/media")
    a = ap.parse_args()
    if a.cmd == "promo":
        if a.audio:
            write_wav(a.out, *soundtrack())
            print(f"wrote {a.out}")
        else:
            promo(a.out, a.still)
    else:
        shots(a.dir)


# ---- shots: screenshots, hero.gif, social.png -------------------------------------------------

BACKDROP = (11, 13, 26)


def window(img, title, scale=2):
    """A board picture in a plain window - title bar in the VGA font - on a dark backdrop with a soft
    shadow: for the README and for posting."""
    raster = Raster()
    board = Image.fromarray(img).resize((img.shape[1] * scale, img.shape[0] * scale), Image.NEAREST)
    bar_rows = text_screen([(1, 0, [("■", 12), (" ", 7), ("■", 14), (" ", 7), ("■", 10)])], 8).rows
    bar = raster.image(bar_rows, "vga")
    label = raster.image(text_screen([(0, 0, [(title, 7)])], len(title)).rows, "vga")
    bar_h, margin, border = 22 * scale, 40 * scale, scale
    w, h = board.width + 2 * border, board.height + bar_h + 2 * border
    canvas = Image.new("RGB", (w + 2 * margin, h + 2 * margin), BACKDROP)
    shadow = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(shadow).rectangle((margin, margin + 10 * scale, margin + w, margin + h + 10 * scale), fill=170)
    canvas.paste((0, 0, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(14 * scale)))
    d = ImageDraw.Draw(canvas)
    d.rectangle((margin, margin, margin + w - 1, margin + h - 1), fill=(64, 72, 112))
    d.rectangle((margin + border, margin + border, margin + w - 1 - border, margin + border + bar_h - 1), fill=(22, 26, 48))
    lights = Image.fromarray(bar).resize((bar.shape[1] * scale, bar.shape[0] * scale), Image.NEAREST)
    canvas.paste(lights, (margin + border + 4 * scale, margin + border + (bar_h - lights.height) // 2))
    text = Image.fromarray(label).resize((label.shape[1] * scale, label.shape[0] * scale), Image.NEAREST)
    mask = Image.fromarray((label.max(axis=2) > 0).astype(np.uint8) * 255).resize(text.size, Image.NEAREST)
    canvas.paste(text, (margin + (w - text.width) // 2, margin + border + (bar_h - text.height) // 2), mask)
    canvas.paste(board, (margin + border, margin + border + bar_h))
    return canvas


def shots(out):
    out.mkdir(parents=True, exist_ok=True)
    raster = Raster()
    rig = Rig()
    t = 0.0

    def run_to(at):
        nonlocal t
        while t < at:                        # in small steps, so a chat reply streams as it would
            t = min(at, t + 0.05)
            rig.advance(START + t)

    def grab(pal="vga"):
        rows, cursor = rig.cells(t)
        return raster.image(rows, pal, 0.0, None)

    def save(name, img, title):
        window(img, title).save(out / name, optimize=True)
        print(f"wrote {out / name}")

    run_to(4.0)
    save("main.png", grab(), "telnet lono 2323")
    save("amber.png", grab("amber"), "telnet lono 2323")
    rig.keys("s", "down", "down")
    run_to(5.0)
    save("services.png", grab(), "telnet lono 2323")
    rig.keys("up", "up", "c")
    for ch in CHAT_PROMPT[1]:
        rig.keys(ch)
    rig.keys("enter")
    run_to(9.0)
    save("chat.png", grab(), "telnet lono 2323")
    rig.keys("esc", "u")
    run_to(9.5)
    save("usage.png", grab(), "telnet lono 2323")
    rig.keys("f", "down", "down", "enter")
    run_to(11.0)
    save("fleet.png", grab(), "telnet lono 2323  ·  viewing spark-2")
    rig.keys("esc")
    rig.sess.resize(80, 25)
    run_to(11.5)
    save("terminal.png", grab(), "telnet lono 2323  ·  80x25")
    hero_gif(out / "hero.gif", raster)
    social(out / "social.png", raster)


def hero_gif(path, raster, fps=12.5):
    """The README's opening loop: the main screen, the services lightbar, then a chat."""
    rig = Rig()
    script = [(2.4, ("s",)), (3.2, ("down",)), (4.0, ("down",)), (4.8, ("up",)), (5.2, ("up",)), (5.8, ("c",))]
    script += [(6.2 + i / 22, (ch,)) for i, ch in enumerate(CHAT_PROMPT[1])]
    script += [(6.2 + len(CHAT_PROMPT[1]) / 22 + 0.2, ("enter",)), (11.6, ("esc",))]
    length = 12.0
    frames = []
    for i in range(int(length * fps)):
        at = i / fps
        while script and script[0][0] <= at:
            rig.advance(START + script[0][0])
            rig.keys(*script.pop(0)[1])
        rig.advance(START + at)
        rows, cursor = rig.cells(at)
        frames.append(raster.image(rows, "vga", at, cursor))
    tmp = path.with_suffix(".frames")
    tmp.mkdir(exist_ok=True)
    for i, f in enumerate(frames):
        Image.fromarray(f).save(tmp / f"{i:04d}.png")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(tmp / "%04d.png"),
                    "-filter_complex", "split[a][b];[a]palettegen=max_colors=32:stats_mode=full[p];[b][p]paletteuse=dither=none",
                    "-loop", "0", str(path)], check=True)
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    print(f"wrote {path} ({path.stat().st_size // 1024} KiB)")


def social(path, raster):
    """GitHub's 1280x640 preview card: the end card's logo and words over a strip of the board."""
    rig = Rig()
    rig.advance(START + 4.0)
    rows, _ = rig.cells(4.0)
    board = raster.image(rows, "vga")
    card = Image.new("RGB", (1280, 640), (0, 0, 0))
    strip = Image.fromarray(board[176:176 + 300])               # the stats, memory and menu
    fade = np.linspace(0, 1, 120)[:, None, None]
    s = np.asarray(strip, np.float32)
    s[:120] *= fade
    s *= 0.62
    card.paste(Image.fromarray(s.astype(np.uint8)), (0, 340))
    scr = Screen(160)
    scr.row(24)
    px = logo([("LONO", "A"), ("BOARD", "B")], 2)
    draw_logo(scr, (160 - px[0]) // 2, 3, px)
    center_segs(scr, 11, [("a live dashboard for the machines running your AI models", 15)])
    center_segs(scr, 13, [("drawn like a 1990s ANSI BBS", 7)])
    center_segs(scr, 16, [("telnet or browser", 3), ("  ∙  ", 9), ("llama.cpp · ollama · vllm", 3), ("  ∙  ", 9),
                          ("pure python", 3)])
    top = raster.image(scr.rows[:18], "vga")
    mask = np.asarray(Image.fromarray(top).convert("L")) > 0
    c = np.asarray(card).copy()
    c[:top.shape[0]][mask] = top[mask]
    crt = CRT((1280, 640))
    Image.fromarray(crt(Image.fromarray(c), None, 0.7)).save(path, optimize=True)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
