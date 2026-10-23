"""Starting and stopping services from the board: who may, what can be done, and whether it fits.

Control is off unless [control] users lists the Tailscale logins allowed to use it. A viewer is
identified by Tailscale: through `tailscale serve` (the usual setup) by the Tailscale-User-Login
header it adds, and on a board listening on a tailnet address by asking tailscaled who the peer is.
Requests from tagged devices carry no person, so they can't control anything.

Services that run as systemd user units can be controlled: the board runs as the same user, so
it needs no extra privileges.
"""
import glob
import ipaddress
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from lonoboard.config import TAILNET_V4, TAILNET_V6

ACTIONS = ("start", "stop", "restart", "enable", "disable")
STARTS = ("start", "restart", "enable")        # actions that load a model, so need the memory
WHOIS_TTL = 60
WEIGHT_FLAGS = ("-m", "--model", "--mmproj", "-md", "--model-draft")

_whois, _whois_lock = {}, threading.Lock()


# ---------- who is asking ----------

def identity(peer, headers):
    """The Tailscale login of a request's sender, or None."""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return None
    if ip.is_loopback:                  # tailscale serve forwards from localhost and sets the header
        login = (headers.get("Tailscale-User-Login") or "").strip()
        return login or None
    if ip in TAILNET_V4 or ip in TAILNET_V6:
        return whois(str(ip))
    return None


def whois(ip):
    """Ask tailscaled who owns a tailnet address (cached); None for tagged devices or on error."""
    with _whois_lock:
        hit = _whois.get(ip)
        if hit and time.time() - hit[1] < WHOIS_TTL:
            return hit[0]
    try:
        out = subprocess.run(["tailscale", "whois", "--json", ip], capture_output=True, text=True, timeout=5).stdout
        d = json.loads(out)
        login = None if (d.get("Node") or {}).get("Tags") else (d.get("UserProfile") or {}).get("LoginName")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        login = None
    with _whois_lock:
        _whois[ip] = (login, time.time())
    return login


def enabled(cfg):
    return bool(cfg["control"]["users"])


def allowed(login, cfg):
    return bool(login) and login.lower() in {u.lower() for u in cfg["control"]["users"]}


# ---------- what can be done ----------

def controllable(sv):
    """Whether the board can start and stop this service: it runs as a systemd user unit."""
    run = sv.get("run") or {}
    return run.get("manager") == "systemd" and bool(run.get("unit")) and run.get("user") is not False


def running(sv):
    return (sv.get("run") or {}).get("state") in ("active", "activating", "reloading")


def actions_for(sv):
    """The actions that make sense for a service right now."""
    if not controllable(sv):
        return []
    acts = ["stop", "restart"] if running(sv) else ["start"]
    return acts + ["disable" if (sv.get("run") or {}).get("enabled") else "enable"]


def describe(action, name):
    return {"start": f"start {name}", "stop": f"stop {name}", "restart": f"restart {name}",
            "enable": f"start {name} now and at boot", "disable": f"stop {name} and don't start it at boot"}[action]


# ---------- will it fit ----------

def weights_gib(sv):
    """The size of a model's weight files, from its launch flags (or what the server reports)."""
    m = sv.get("model") or {}
    args, files = m.get("args") or [], []
    for i, a in enumerate(args[:-1]):
        if a in WEIGHT_FLAGS:
            files.append(Path(args[i + 1]).expanduser())
    total = 0
    for f in files:
        split = re.match(r"(.*)-\d{5}-of-(\d{5})\.gguf$", str(f))       # model-00001-of-00003.gguf
        parts = glob.glob(f"{glob.escape(split.group(1))}-*-of-{split.group(2)}.gguf") if split else [str(f)]
        total += sum(Path(p).stat().st_size for p in parts if Path(p).is_file())
    if total:
        return round(total / 2**30, 1)
    return m.get("size_gib")


def free_gib(host):
    """Memory a new model can use: system memory on unified-memory machines, else free GPU memory."""
    g = host.get("gpu") or {}
    if g.get("unified") or not g:
        return (host.get("memory_gib") or {}).get("available")
    if g.get("mem_total_gib") is not None and g.get("mem_used_gib") is not None:
        return round(g["mem_total_gib"] - g["mem_used_gib"], 1)
    return None


def fits(sv, host, action):
    """None if it's fine to go ahead, else {"need_gib", "free_gib"}: the weights alone won't fit."""
    if action not in STARTS or running(sv):
        return None
    need, free = weights_gib(sv), free_gib(host)
    if need is None or free is None or need <= free:
        return None
    return {"need_gib": need, "free_gib": free}


# ---------- doing it ----------

def perform(collector, name, action, force=False):
    """Check and run one action on a service. (HTTP status, body) for the API and the board."""
    if action not in ACTIONS:
        return 400, {"error": f"action must be one of {', '.join(ACTIONS)}"}
    snap = collector.snapshot(max_age=0)
    sv = next((s for s in snap["services"] if s["name"] == name), None)
    if sv is None:
        return 404, {"error": f"no service {name}"}
    if not controllable(sv):
        return 409, {"error": f"{name} doesn't run as a systemd user unit, so the board can't control it"}
    tight = fits(sv, snap["host"], action)
    if tight and not force:
        return 409, {"error": f"{name} needs about {tight['need_gib']} gib for its weights and "
                              f"{tight['free_gib']} gib is free", "fits": False, **tight}
    ok, msg = collector.control(sv, action)
    if not ok:
        return 500, {"error": msg or f"couldn't {action} {name}"}
    return 200, {"ok": True, "service": name, "action": action}


def systemctl(action, unit, timeout=30):
    """Run one action on a systemd user unit. (ok, message)."""
    verb = {"start": ["start"], "stop": ["stop"], "restart": ["restart"],
            "enable": ["enable", "--now"], "disable": ["disable", "--now"]}[action]
    try:
        r = subprocess.run(["systemctl", "--user", "--no-block", *verb, unit],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()
