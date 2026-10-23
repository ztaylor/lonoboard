"""Configuration: an optional TOML file, environment variables and command-line flags.

Search order for the file: --config, $LONOBOARD_CONFIG, $XDG_CONFIG_HOME/lonoboard/config.toml
(~/.config/lonoboard/config.toml). Every setting has a default, so no file is needed.
"""
import copy
import functools
import ipaddress
import os
import socket
import sys
import tomllib
from pathlib import Path

DEFAULTS = {
    "board": {
        "name": "",            # logo text; defaults to the hostname
        "tagline": "",         # an extra label for the header, e.g. "dgx spark"
        "listen": "127.0.0.1",  # where the web board, API and telnet listen
        "port": 7777,
        "telnet_port": 2323,   # 0 turns telnet off
        "password": "",        # also $LONOBOARD_PASSWORD; required to listen beyond localhost/tailnet
        "allow_anonymous": False,  # listen beyond localhost/tailnet without a password (demo boards)
        "chat": True,          # let viewers chat with the models
        "max_sessions": 24,
        "max_per_ip": 0,       # connections allowed from one address (0 = no limit)
        "idle_timeout": 0,     # disconnect viewers idle this many minutes (0 = never)
        "intro": True,         # the modem-style connect sequence
        "hosts": [],           # more host names the web board answers to without a password (see host_allowed)
    },
    "detect": {
        "enabled": True,       # find model servers by running process and well-known ports
        "ports": [8080, 8081, 8000, 11434, 1234, 5000, 5001],
    },
    "server": [],              # [[server]] tables: name, url, kind, description, unit, api_key_env...
    "llama_models": {
        "dir": "~/llama-models",           # NAME.conf files with PORT=/ARGS=/CMD= (if the dir exists)
        "unit": "llama@{name}.service",    # the systemd user unit that runs each one
    },
    "fleet": {
        "peers": [],           # other machines running lonoboard: "host", "host:port" or a URL
        "tailscale": True,     # also look for lonoboard on online tailnet peers
    },
    "labels": {},              # names for other tailscale-served ports: {"11000" = "DGX Dashboard"}
    "control": {
        "users": [],           # Tailscale logins that may start, stop and enable services; empty = nobody
    },
    "usage": {
        "state_dir": "",       # where usage history is kept; defaults to $XDG_STATE_HOME/lonoboard
    },
}

KINDS = ("llama.cpp", "ollama", "vllm", "openai", "custom")


class ConfigError(Exception):
    pass


def default_path():
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "lonoboard" / "config.toml"


def _merge(base, extra, where=""):
    for k, v in extra.items():
        if k not in base:
            print(f"lonoboard: ignoring unknown config key {where}{k}", file=sys.stderr)
            continue
        if isinstance(base[k], dict) and k != "labels":
            if not isinstance(v, dict):
                raise ConfigError(f"{where}{k} should be a table")
            _merge(base[k], v, f"{where}{k}.")
        else:
            base[k] = v


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    path = Path(path or os.environ.get("LONOBOARD_CONFIG") or default_path()).expanduser()
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from None
        _merge(cfg, data)
        cfg["_path"] = str(path)
    else:
        cfg["_path"] = None
    if os.environ.get("LONOBOARD_PASSWORD"):
        cfg["board"]["password"] = os.environ["LONOBOARD_PASSWORD"]
    validate(cfg)
    return cfg


def validate(cfg):
    for i, s in enumerate(cfg["server"]):
        if not isinstance(s, dict) or not s.get("url"):
            raise ConfigError(f"[[server]] #{i + 1} needs a url")
        if s.get("kind") and s["kind"] not in KINDS:
            raise ConfigError(f"[[server]] {s.get('name') or s['url']}: kind must be one of {', '.join(KINDS)}")
    cfg["labels"] = {int(k): str(v) for k, v in cfg["labels"].items() if str(k).isdigit()}
    b = cfg["board"]
    for k in ("port", "telnet_port", "max_sessions", "max_per_ip", "idle_timeout"):
        if not isinstance(b[k], int) or b[k] < 0:
            raise ConfigError(f"board.{k} should be a non-negative integer")
    if not isinstance(b["hosts"], list) or not all(isinstance(h, str) for h in b["hosts"]):
        raise ConfigError('board.hosts should be a list of host names, e.g. ["board.example.com"]')
    users = cfg["control"]["users"]
    if not isinstance(users, list) or not all(isinstance(u, str) and u.strip() for u in users):
        raise ConfigError('control.users should be a list of Tailscale logins, e.g. ["alice@github"]')


def state_dir(cfg):
    d = cfg["usage"]["state_dir"] or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "lonoboard"
    return Path(d).expanduser()


def board_name(cfg):
    return cfg["board"]["name"] or socket.gethostname().split(".")[0]


TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def exposure(listen):
    """'local', 'tailnet' or 'network' for a listen address."""
    try:
        ip = ipaddress.ip_address(listen)
    except ValueError:
        return "network" if listen not in ("localhost",) else "local"
    if ip.is_loopback:
        return "local"
    if ip in TAILNET_V4 or ip in TAILNET_V6:
        return "tailnet"
    return "network"


def peer_url(peer, port):
    """A [fleet] peers entry ("host", "host:port" or a URL) -> its base URL."""
    url = peer if "://" in peer else f"http://{peer}" if ":" in peer else f"http://{peer}:{port}"
    return url.rstrip("/")


LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home.arpa", ".internal", ".ts.net")


@functools.cache
def my_names():
    """This machine's names (not getfqdn: that can wait seconds on DNS)."""
    return {socket.gethostname().lower(), "localhost"}


def host_allowed(host, cfg):
    """Whether a request's Host header is one this board answers to. Without a password, the board
    only answers to names a stranger's web page can't point at it (DNS rebinding): IP addresses,
    single-label names (spark-1), local and tailnet suffixes, this machine's names and board.hosts."""
    b = cfg["board"]
    if b["password"] or b["allow_anonymous"]:
        return True
    name = (host or "").strip().lower()
    if name.startswith("["):                                    # [::1]:7777
        name = name[1:].split("]", 1)[0]
    elif name.count(":") == 1:
        name = name.split(":", 1)[0]
    name = name.rstrip(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    me = my_names()
    extra = {h.lower().rstrip(".") for h in b.get("hosts") or []}
    return "." not in name or name in me | extra or name.endswith(LOCAL_SUFFIXES)


def check_exposure(cfg):
    """Refuse to serve an unprotected board beyond localhost / the tailnet, or controls beyond it at all."""
    b = cfg["board"]
    if cfg["control"]["users"] and exposure(b["listen"]) == "network":
        raise ConfigError(
            f"control.users is set, and the board would listen on {b['listen']}, beyond your tailnet.\n"
            "service controls trust Tailscale to say who's asking, so they only work on localhost (behind\n"
            "tailscale serve) or a tailnet address. listen on one of those, or empty control.users.")
    if exposure(b["listen"]) == "network" and not b["password"] and not b["allow_anonymous"]:
        raise ConfigError(
            f"listening on {b['listen']} would put the board on your network without a password.\n"
            "set board.password (or $LONOBOARD_PASSWORD), listen on localhost/your tailnet address, or set\n"
            "board.allow_anonymous = true if that's really what you want (e.g. a public demo).")
