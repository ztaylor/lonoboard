"""Model servers: finding them, telling the kinds apart, and describing each one.

A *target* is what we know before probing: {name, kind, url, description, unit, unit_user,
api_key_env, endpoints, args, command, origin}. describe() turns it into the service dict the
board and the API show.
"""
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

from lonoboard.util import get_json, get_text, timed_json

ENDPOINT_RE = re.compile(r"(/v1/[\w/.-]+)")
CHAT = "/v1/chat/completions"
# process names (or command lines) that give a server away
PROCESS_KINDS = [("llama-server", "llama.cpp"), ("ollama", "ollama"), ("vllm", "vllm"), ("lms", "openai"),
                 ("LM Studio", "openai"), ("tabbyAPI", "openai"), ("koboldcpp", "openai"), ("sglang", "openai")]


def headers_for(target):
    env = target.get("api_key_env")
    key = os.environ.get(env) if env else None
    return {"Authorization": f"Bearer {key}"} if key else {}


def port_of(url):
    u = urllib.parse.urlsplit(url)
    return u.port or (443 if u.scheme == "https" else 80)


def flag(args, *names):
    for i, a in enumerate(args or []):
        if a in names:
            return args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else True
    return None


# ---------- identifying a server ----------

def identify(url, timeout=1.0):
    """What kind of server answers at url? 'ollama' | 'llama.cpp' | 'vllm' | 'openai' | None."""
    v = get_json(f"{url}/api/version", timeout)
    if isinstance(v, dict) and "version" in v and get_json(f"{url}/api/tags", timeout) is not None:
        return "ollama"
    props = get_json(f"{url}/props", timeout)
    if isinstance(props, dict) and ("default_generation_settings" in props or "total_slots" in props):
        return "llama.cpp"
    if get_json(f"{url}/v1/models", timeout) is None:
        return None
    metrics = get_text(f"{url}/metrics", timeout) or ""
    if "vllm:" in metrics:
        return "vllm"
    return "openai"


def kind_from_process(name, cmdline):
    text = " ".join([name or ""] + list(cmdline or []))
    for needle, kind in PROCESS_KINDS:
        if needle in text:
            return kind
    return None


# ---------- service records ----------

def base_service(t):
    return {
        "name": t["name"], "kind": t.get("kind") or "openai", "origin": t.get("origin", "config"),
        "description": t.get("description") or "", "url": t["url"], "port": port_of(t["url"]),
        "urls": {"local": t["url"], "tailnet": []}, "endpoints": list(t.get("endpoints") or []),
        "run": {"manager": None, "unit": t.get("unit"), "state": None, "sub_state": None, "enabled": None,
                "pid": None, "since": None, "restarts": 0},
        "memory_mib": {"gpu": None, "process": None},
        "health": None, "latency_ms": None, "version": None,
        "model": None, "models": [], "chat": None,
    }


def describe(t, timeout=2.0):
    s = base_service(t)
    fn = {"llama.cpp": _llamacpp, "ollama": _ollama, "vllm": _vllm, "openai": _openai, "custom": _custom}
    fn.get(s["kind"], _openai)(s, t, timeout)
    return s


def _model_record(**kw):
    m = {"id": None, "alias": None, "params_b": None, "size_gib": None, "quant": None, "context": None,
         "context_train": None, "input_modalities": None, "file": None, "args": None,
         "slots": None, "slots_busy": None, "loaded": True}
    m.update({k: v for k, v in kw.items() if v is not None})
    return m


def _llamacpp(s, t, timeout):
    url = s["url"]
    status, h, s["latency_ms"] = timed_json(f"{url}/health", timeout)
    s["health"] = "ok" if status == 200 else "loading" if status == 503 else "unreachable" if not status else "error"
    args = t.get("args") or []
    if not s["endpoints"]:
        if flag(args, "--embeddings", "--embedding"):
            s["endpoints"] = ["/v1/embeddings"]
        elif flag(args, "--reranking", "--rerank"):
            s["endpoints"] = ["/v1/rerank"]
        else:
            s["endpoints"] = [CHAT, "/v1/completions"]
    s["endpoints"] = list(dict.fromkeys(s["endpoints"] + ["/v1/models", "/health"]))
    m = _model_record(alias=flag(args, "--alias", "-a"), file=flag(args, "-m", "--model"), args=args or None)
    if s["health"] == "ok":
        info = get_json(f"{url}/v1/models", timeout)
        if info and info.get("data"):
            d = info["data"][0]
            meta, arch = d.get("meta") or {}, d.get("architecture") or {}
            m.update(id=d.get("id"), context=meta.get("n_ctx"), context_train=meta.get("n_ctx_train"),
                     params_b=round(meta["n_params"] / 1e9, 2) if meta.get("n_params") else None,
                     size_gib=round(meta["size"] / 2**30, 2) if meta.get("size") else None,
                     quant=meta.get("ftype"), input_modalities=arch.get("input_modalities"))
        props = get_json(f"{url}/props", timeout) or {}
        m["slots"] = props.get("total_slots")
        m["file"] = m["file"] or props.get("model_path")
        m["alias"] = m["alias"] or props.get("model_alias")
        if props.get("build_info"):
            s["version"] = "llama.cpp " + str(props["build_info"]).split("-", 1)[-1]
        m["slots_busy"], s["_slots"] = llamacpp_slots(url, timeout)
        if not s["description"] and m.get("id"):
            s["description"] = f"llama.cpp serving {m['id']}"
    s["model"] = m
    s["models"] = [m] if m.get("id") else []
    if CHAT in s["endpoints"] and s["health"] == "ok":
        s["chat"] = {"url": url, "models": [m.get("id") or m.get("alias") or ""]}


def llamacpp_slots(url, timeout=1.0):
    """(busy count, [{id, task, processing, decoded}]) from /slots; (None, None) if it's off."""
    slots = get_json(f"{url}/slots", timeout)
    if not isinstance(slots, list):
        return None, None
    info = []
    for sl in slots:
        nt = (sl.get("next_token") or [{}])
        nt = nt[0] if isinstance(nt, list) and nt else (nt if isinstance(nt, dict) else {})
        info.append({"id": sl.get("id"), "task": sl.get("id_task"), "processing": bool(sl.get("is_processing")),
                     "decoded": nt.get("n_decoded")})
    return sum(1 for i in info if i["processing"]), info


def _ollama(s, t, timeout):
    url = s["url"]
    status, v, s["latency_ms"] = timed_json(f"{url}/api/version", timeout)
    s["health"] = "ok" if status == 200 else "unreachable" if not status else "error"
    s["endpoints"] = s["endpoints"] or [CHAT, "/api/chat", "/api/generate", "/api/embed"]
    if s["health"] != "ok":
        return
    s["version"] = f"ollama {(v or {}).get('version', '')}".strip()
    tags = (get_json(f"{url}/api/tags", timeout) or {}).get("models") or []
    ps = {p.get("name"): p for p in (get_json(f"{url}/api/ps", timeout) or {}).get("models") or []}
    models = []
    for tg in tags:
        det = tg.get("details") or {}
        loaded = ps.get(tg.get("name"))
        models.append(_model_record(
            id=tg.get("name"), size_gib=round(tg["size"] / 2**30, 2) if tg.get("size") else None,
            quant=det.get("quantization_level"), params_b=_params_b(det.get("parameter_size")),
            context=(loaded or {}).get("context_length"), loaded=bool(loaded)))
    models.sort(key=lambda m: (not m["loaded"], m["id"] or ""))
    s["models"] = models
    loaded = [m for m in models if m["loaded"]]
    s["memory_mib"]["gpu"] = sum(int(p.get("size_vram") or 0) for p in ps.values()) // 2**20 or None
    primary = dict(loaded[0]) if loaded else _model_record(id=None, loaded=False)
    primary["loaded_count"], primary["installed_count"] = len(loaded), len(models)
    s["model"] = primary
    if not s["description"]:
        s["description"] = f"ollama · {len(models)} models installed, {len(loaded)} loaded"
    if models:
        s["chat"] = {"url": url, "models": [m["id"] for m in models]}


def _params_b(text):
    m = re.match(r"([\d.]+)\s*([BbMm])", str(text or ""))
    if not m:
        return None
    n = float(m.group(1))
    return round(n / 1000, 2) if m.group(2).lower() == "m" else n


def _openai(s, t, timeout):
    url = s["url"]
    status, info, s["latency_ms"] = timed_json(f"{url}/v1/models", timeout, headers_for(t))
    s["health"] = "ok" if status == 200 else "unreachable" if not status else "error"
    s["endpoints"] = s["endpoints"] or [CHAT, "/v1/completions", "/v1/models"]
    ids = [d.get("id") for d in ((info or {}).get("data") or []) if d.get("id")]
    s["models"] = [_model_record(id=i, context=d.get("max_model_len")) for i, d in
                   zip(ids, (info or {}).get("data") or [])]
    s["model"] = dict(s["models"][0]) if s["models"] else _model_record()
    if ids and CHAT in s["endpoints"]:
        s["chat"] = {"url": url, "models": ids, "auth": bool(headers_for(t))}
    if not s["description"] and ids:
        s["description"] = f"openai-compatible · {', '.join(ids[:3])}" + (" ..." if len(ids) > 3 else "")


def _vllm(s, t, timeout):
    _openai(s, t, timeout)
    url = s["url"]
    if s["health"] == "ok":
        v = get_json(f"{url}/version", timeout)
        if v and v.get("version"):
            s["version"] = f"vllm {v['version']}"
        metrics = parse_metrics(get_text(f"{url}/metrics", timeout) or "")
        running = metrics.get("vllm:num_requests_running")
        if s["model"] is not None and running is not None:
            s["model"]["slots_busy"] = int(running)
        if not t.get("description") and s["models"]:
            s["description"] = f"vllm serving {s['models'][0]['id']}"


def _custom(s, t, timeout):
    """Something that isn't a model API (e.g. a TTS server): only /health is assumed."""
    status, h, s["latency_ms"] = timed_json(f"{s['url']}/health", timeout)
    s["health"] = (h or {}).get("status", "ok") if status == 200 else "unreachable" if not status else "error"
    found = list(dict.fromkeys(ENDPOINT_RE.findall(s["description"])))
    s["endpoints"] = s["endpoints"] or found + ["/health"]
    if t.get("command"):
        s["command"] = t["command"]


# ---------- prometheus text ----------

def parse_metrics(text):
    """Prometheus text -> {name: value}, summing series that differ only by labels."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+([-+0-9.eEnaNIf]+)", line)
        if m:
            try:
                out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(3))
            except ValueError:
                pass
    return out


# ---------- the llama-models convention ----------

def read_llama_models(directory, unit_fmt):
    """Targets from NAME.conf files (bash: PORT=, ARGS=(...) or CMD=(...)), run by llama@NAME units."""
    d = Path(directory).expanduser()
    if not d.is_dir():
        return []
    targets = []
    for path in sorted(d.glob("*.conf")):
        conf = _source_conf(path)
        if not conf["port"]:
            continue
        name = path.stem
        targets.append({
            "name": name, "kind": "custom" if conf["cmd"] else "llama.cpp",
            "url": f"http://127.0.0.1:{conf['port']}", "description": conf["description"],
            "unit": unit_fmt.format(name=name), "unit_user": True,
            "args": conf["args"], "command": conf["cmd"] or None, "origin": "llama-models",
            "endpoints": list(dict.fromkeys(ENDPOINT_RE.findall(conf["description"]))),
        })
    return targets


def _source_conf(path):
    """Source a .conf in bash (they use arrays and ~) and pull out the variables."""
    script = ('source "$1" >/dev/null 2>&1; printf "%s\\0" "${PORT:-}"; printf "\\1\\0"; '
              'printf "%s\\0" "${ARGS[@]}"; printf "\\1\\0"; [[ -n ${CMD+set} ]] && printf "%s\\0" "${CMD[@]}"')
    try:
        out = subprocess.run(["bash", "-c", script, "_", str(path)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    head, args, cmd = (out.split("\1\0") + ["", ""])[:3]
    port = head.split("\0")[0]
    comments = []
    for line in path.read_text().splitlines():
        if not line.startswith("#"):
            break
        comments.append(line.lstrip("# ").rstrip())
    return {"port": int(port) if port.isdigit() else None, "args": [a for a in args.split("\0") if a],
            "cmd": [a for a in cmd.split("\0") if a], "description": " ".join(comments)}


# ---------- auto-detection ----------

def detect(listening, ports, known_ports, own_ports):
    """Targets for model servers we can find on this machine.

    listening: {port: {pid, process}} from runtime.listeners(); known_ports: ports already
    covered by config/adapters; own_ports: lonoboard's own ports."""
    from lonoboard import runtime
    found, seen = [], set(known_ports) | set(own_ports)
    candidates = []
    for port, l in sorted(listening.items()):
        if port in seen:
            continue
        kind, cmdline = None, []
        if l.get("pid"):
            cmdline = runtime.proc_info(l["pid"])["cmdline"]
            kind = kind_from_process(l.get("process"), cmdline)
        if kind or port in ports:
            candidates.append((port, kind, cmdline))
    for port in ports:                      # well-known ports: also try ones we can't see (other users)
        if port not in seen and port not in {c[0] for c in candidates} and port in listening:
            candidates.append((port, None, []))
    for port, hint, cmdline in candidates:
        url = f"http://127.0.0.1:{port}"
        kind = identify(url) or (hint if hint == "llama.cpp" else None)
        if not kind:
            continue
        t = {"name": _detected_name(kind, url, port), "kind": kind, "url": url, "origin": "detected"}
        if kind == "llama.cpp" and len(cmdline) > 1:     # its flags tell us embeddings vs chat, and show in details
            t["args"] = cmdline[1:]
        found.append(t)
        seen.add(port)
    return found


def _detected_name(kind, url, port):
    if kind == "ollama":
        return "ollama"
    if kind == "llama.cpp":
        props = get_json(f"{url}/props", 1) or {}
        alias = props.get("model_alias") or Path(props.get("model_path") or "").stem
        return (alias or f"llama-{port}")[:24]
    if kind == "vllm":
        return "vllm" if port == 8000 else f"vllm-{port}"
    return {1234: "lmstudio"}.get(port, f"openai-{port}")
