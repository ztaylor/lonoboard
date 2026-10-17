"""Reachability: Tailscale (optional) and the URLs other machines can use."""
import json
import re
import shutil
import socket
import time

from lonoboard.util import sh


class Network:
    def __init__(self):
        self.tailscale = bool(shutil.which("tailscale"))
        self._cache, self._at = None, 0

    def info(self, ttl=10):
        """{'tailnet': {dns_name, ips} | None, 'served': {local port: [urls]}, 'peers': [...]}"""
        if self._cache and time.time() - self._at < ttl:
            return self._cache
        info = {"tailnet": None, "served": {}, "peers": []}
        if self.tailscale:
            try:
                st = json.loads(sh("tailscale", "status", "--json") or "{}")
            except ValueError:
                st = {}
            me = st.get("Self") or {}
            if me and st.get("BackendState", "Running") == "Running":
                info["tailnet"] = {"dns_name": (me.get("DNSName") or "").rstrip(".") or None,
                                   "ips": me.get("TailscaleIPs") or []}
                info["peers"] = sorted(
                    ({"name": p.get("HostName"), "dns_name": (p.get("DNSName") or "").rstrip("."),
                      "online": bool(p.get("Online")), "os": p.get("OS")}
                     for p in (st.get("Peer") or {}).values() if p.get("HostName")),
                    key=lambda p: p["name"])
                info["served"] = self._served()
        self._cache, self._at = info, time.time()
        return info

    def _served(self):
        try:
            serve = json.loads(sh("tailscale", "serve", "status", "--json") or "{}")
        except ValueError:
            return {}
        return parse_serve(serve, socket.gethostname().split(".")[0])

    def urls_for(self, port, public):
        """Where other machines can reach a local port: tailscale serve routes, else the LAN name."""
        served = self.info()["served"].get(port)
        if served:
            return list(served)
        if public:
            return [f"http://{socket.gethostname().split('.')[0]}:{port}"]
        return []


def parse_serve(serve, short):
    """`tailscale serve status --json` -> {local port: [urls other machines can use]}."""
    tcp, served = serve.get("TCP") or {}, {}
    for hostport, web in (serve.get("Web") or {}).items():
        fqdn, port = hostport.rsplit(":", 1)
        https = (tcp.get(port) or {}).get("HTTPS", False)
        scheme = "https" if https else "http"
        suffix = "" if port == ("443" if https else "80") else f":{port}"
        for path, h in (web.get("Handlers") or {}).items():
            m = re.match(r"https?://(?:127\.0\.0\.1|localhost):(\d+)", h.get("Proxy", ""))
            if not m:
                continue
            p = path if path != "/" else ""
            urls = [f"{scheme}://{fqdn}{suffix}{p}"]
            if not https:      # the MagicDNS short name only works for plain http (no cert)
                urls.insert(0, f"http://{short}{suffix}{p}")
            served.setdefault(int(m.group(1)), []).extend(urls)
    # raw TCP forwards (e.g. telnet on :23) are listed too, as host:port
    for port, t in tcp.items():
        fwd = (t or {}).get("TCPForward", "")
        m = re.match(r"(?:127\.0\.0\.1|localhost):(\d+)", fwd)
        if m:
            served.setdefault(int(m.group(1)), []).append(f"{short}:{port}")
    return served
