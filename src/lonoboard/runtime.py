"""How a model server is running: its process, the systemd unit or Docker container around it,
and where its logs are. Everything is best-effort and read-only."""
import json
import os
import re
import shutil
import time
from pathlib import Path

from lonoboard.util import sh

DARWIN = os.uname().sysname == "Darwin"


# ---------- processes ----------

def listeners():
    """{port: {'pid': int|None, 'process': str|None, 'public': bool}} for listening TCP sockets.
    public = bound to all interfaces (reachable from the network), not just loopback.
    Other users' processes show up without a pid/name (that needs root)."""
    out = {}
    if DARWIN:
        for line in sh("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn").splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("c"):
                name = line[1:]
            elif line.startswith("n"):
                m = re.search(r":(\d+)$", line)
                if m:
                    out.setdefault(int(m.group(1)), {"pid": pid, "process": name, "public": line[1:].startswith("*")})
        return out
    for line in sh("ss", "-tlnpH").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        m = re.search(r":(\d+)$", parts[3])
        if not m:
            continue
        p = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        port = int(m.group(1))
        public = parts[3].rsplit(":", 1)[0] in ("0.0.0.0", "*", "[::]")
        prev = out.get(port)
        out[port] = {"pid": (int(p.group(2)) if p else None) or (prev or {}).get("pid"),
                     "process": (p.group(1) if p else None) or (prev or {}).get("process"),
                     "public": public or bool((prev or {}).get("public"))}
    return out


_BTIME = None


def proc_info(pid):
    """{'since', 'rss_mib', 'cmdline', 'unit', 'unit_user', 'container'} for a pid (Linux; partial on macOS)."""
    global _BTIME
    info = {"since": None, "rss_mib": None, "cmdline": [], "unit": None, "unit_user": None, "container": None}
    if not pid:
        return info
    if DARWIN:
        out = sh("ps", "-o", "etimes=,rss=,command=", "-p", str(pid)).strip().split(None, 2)
        if len(out) >= 2 and out[0].isdigit():
            info["since"] = int(time.time() - int(out[0]))
            info["rss_mib"] = int(out[1]) // 1024
            info["cmdline"] = out[2].split() if len(out) > 2 else []
        return info
    base = Path(f"/proc/{pid}")
    try:
        info["cmdline"] = [a for a in (base / "cmdline").read_bytes().decode("utf-8", "replace").split("\0") if a]
        stat = (base / "stat").read_text()
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        if _BTIME is None:
            _BTIME = next(int(l.split()[1]) for l in Path("/proc/stat").read_text().splitlines() if l.startswith("btime"))
        info["since"] = int(_BTIME + start_ticks / os.sysconf("SC_CLK_TCK"))
        m = re.search(r"VmRSS:\s+(\d+)", (base / "status").read_text())
        info["rss_mib"] = int(m.group(1)) // 1024 if m else None
        cgroup = (base / "cgroup").read_text()
    except (OSError, StopIteration, IndexError, ValueError):
        return info
    info["unit"], info["unit_user"], info["container"] = parse_cgroup(cgroup)
    return info


def parse_cgroup(text):
    """/proc/PID/cgroup -> (systemd unit or None, is it a --user unit, docker container id or None)."""
    unit = user = container = None
    for line in text.splitlines():
        path = line.split(":", 2)[-1]
        c = re.search(r"docker[-/]([0-9a-f]{12,64})", path)
        if c:
            container = c.group(1)[:12]
        units = [u for u in re.findall(r"([\w@.\\-]+\.service)", path) if not u.startswith("user@")]
        if units:                                  # the innermost service, e.g. llama@qwen.service
            unit, user = units[-1], "/user@" in path
    return unit, user, container


# ---------- systemd ----------

def systemd_show(unit, user):
    if not unit or not shutil.which("systemctl"):
        return None
    props = "Id,LoadState,ActiveState,SubState,UnitFileState,MainPID,ActiveEnterTimestamp,NRestarts,MemoryCurrent"
    out = sh("systemctl", *(["--user"] if user else []), "show", "--timestamp=unix", "-p", props, unit)
    d = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if not d or d.get("LoadState") == "not-found":
        return None
    since = d.get("ActiveEnterTimestamp", "").lstrip("@")
    mem = d.get("MemoryCurrent", "")
    return {
        "manager": "systemd", "unit": unit, "user": user,
        "state": d.get("ActiveState"), "sub_state": d.get("SubState"),
        "enabled": d.get("UnitFileState") == "enabled",
        "pid": int(d.get("MainPID") or 0) or None,
        "since": int(since) if d.get("ActiveState") == "active" and since.isdigit() else None,
        "restarts": int(d.get("NRestarts") or 0),
        "memory_mib": int(mem) // 2**20 if mem.isdigit() else None,
    }


def journal(unit, user, n=80, grep=None, since=None, output="short-iso", timeout=10):
    cmd = ["journalctl", *(["--user"] if user else []), "-u", unit, "--no-pager", "-o", output]
    if grep:
        cmd += ["--grep", grep]
    cmd += ["--since", since] if since else ["-n", str(n)]
    return sh(*cmd, timeout=timeout).splitlines()


# ---------- docker ----------

_docker_ok = None


def docker_available():
    global _docker_ok
    if _docker_ok is None:
        _docker_ok = bool(shutil.which("docker")) and bool(sh("docker", "version", "--format", "{{.Server.Version}}", timeout=3).strip())
    return _docker_ok


def docker_containers():
    """{published host port: container info} for running containers."""
    if not docker_available():
        return {}
    by_port = {}
    for line in sh("docker", "ps", "--format", "{{json .}}", timeout=5).splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        for m in re.finditer(r":(\d+)->", c.get("Ports", "")):
            by_port[int(m.group(1))] = {"id": c.get("ID"), "name": c.get("Names"), "image": c.get("Image")}
    return by_port


def docker_show(container):
    if not container or not docker_available():
        return None
    try:
        d = json.loads(sh("docker", "inspect", container, timeout=5) or "[]")[0]
    except (ValueError, IndexError):
        return None
    st = d.get("State", {})
    started = st.get("StartedAt", "")
    since = None
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", started)
    if m and st.get("Running"):
        since = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) - time.timezone)
    policy = d.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") or "no"
    return {
        "manager": "docker", "unit": d.get("Name", "").lstrip("/") or container, "container": container,
        "image": d.get("Config", {}).get("Image"),
        "state": "active" if st.get("Running") else ("failed" if st.get("ExitCode") else "inactive"),
        "sub_state": st.get("Status"), "enabled": policy not in ("no", ""),
        "pid": st.get("Pid") or None, "since": since, "restarts": d.get("RestartCount", 0), "memory_mib": None,
    }


def docker_logs(container, n=80):
    return sh("docker", "logs", "--timestamps", "--tail", str(n), container, timeout=5, merge=True).splitlines()
