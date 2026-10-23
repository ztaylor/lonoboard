"""The lonoboard command."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from lonoboard import __version__
from lonoboard.config import ConfigError, board_name, check_exposure, default_path, exposure, load


class App:
    def __init__(self, collector, board):
        self.collector, self.board = collector, board


def build_parser():
    p = argparse.ArgumentParser(prog="lonoboard", description="lonoboard: a live dashboard for the machines running "
                                "your AI models, drawn like a 1990s ANSI BBS.")
    p.add_argument("--version", action="version", version=f"lonoboard {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--config", help=f"config file (default: $LONOBOARD_CONFIG or {default_path()})")
        sp.add_argument("--listen", help="address to listen on (default 127.0.0.1)")
        sp.add_argument("--port", type=int, help="web board and API port (default 7777)")
        sp.add_argument("--telnet-port", type=int, help="telnet port (default 2323; 0 turns telnet off)")
        sp.add_argument("--name", help="board name for the logo (default: the hostname)")
        sp.add_argument("--no-chat", action="store_true", help="don't let viewers chat with the models")

    for name, help_ in (("serve", "run the board (the default)"), ("demo", "run the board on simulated data")):
        common(sub.add_parser(name, help=help_))
    sp = sub.add_parser("check", help="show what lonoboard finds on this machine")
    sp.add_argument("--config")
    sp = sub.add_parser("json", help="print the board's data once, as JSON")
    sp.add_argument("--config")
    sp.add_argument("--demo", action="store_true")
    sp = sub.add_parser("install", help="install a systemd user service that runs the board")
    sp.add_argument("--config")
    sp.add_argument("--now", action="store_true", help="also enable and start it")
    sp.add_argument("--print", action="store_true", help="print the unit instead of writing it")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "serve"
    if args.cmd is None:
        args = build_parser().parse_args(["serve"] + (argv if argv is not None else sys.argv[1:]))
    try:
        cfg = load(getattr(args, "config", None))
    except ConfigError as e:
        sys.exit(f"lonoboard: {e}")
    if cmd in ("serve", "demo"):
        return serve(cfg, args, demo=cmd == "demo")
    if cmd == "check":
        return check(cfg)
    if cmd == "json":
        c = make_collector(cfg, args.demo)
        print(json.dumps(c.snapshot(), indent=2))
        return 0
    if cmd == "install":
        return install(args)
    return 0


def make_collector(cfg, demo):
    if demo:
        from lonoboard.demo import DemoCollector
        return DemoCollector(cfg)
    from lonoboard.collector import Collector
    return Collector(cfg)


def serve(cfg, args, demo=False):
    b = cfg["board"]
    for key, val in (("listen", args.listen), ("port", args.port), ("telnet_port", args.telnet_port), ("name", args.name)):
        if val is not None:
            b[key] = val
    if args.no_chat:
        b["chat"] = False
    if demo:
        b["name"] = b["name"] or "lono"              # the simulated machine, not the host it runs on
    if demo and exposure(b["listen"]) == "network":
        # a public demo board: nothing real to protect, so no password - but keep it polite
        b["allow_anonymous"] = True
        b["idle_timeout"] = b["idle_timeout"] or 15
        b["max_per_ip"] = b["max_per_ip"] or 3
    try:
        check_exposure(cfg)
    except ConfigError as e:
        sys.exit(f"lonoboard: {e}")
    from lonoboard import web
    from lonoboard.board import Board
    from lonoboard.board.telnet import serve as telnet_serve
    collector = make_collector(cfg, demo).start()
    board = Board(collector, cfg)
    app = App(collector, board)
    try:
        srv = web.serve(app, b["listen"], b["port"])
        if b["telnet_port"]:
            telnet_serve(board, b["listen"], b["telnet_port"])
    except OSError as e:
        sys.exit(f"lonoboard: can't listen on {b['listen']}: {e.strerror or e}")
    host = b["listen"] if exposure(b["listen"]) != "local" else "localhost"
    print(f"lonoboard {__version__}{' (demo)' if demo else ''} · board '{board_name(cfg)}'")
    print(f"  web     http://{host}:{b['port']}/")
    if b["telnet_port"]:
        print(f"  telnet  telnet {host} {b['telnet_port']}")
    print(f"  api     curl http://{host}:{b['port']}/api" + ("   (password protected)" if b["password"] else ""))
    if cfg.get("_path"):
        print(f"  config  {cfg['_path']}")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


# ---------- lonoboard check ----------

def check(cfg):
    from lonoboard import runtime, sources
    from lonoboard.collector import Collector
    c = Collector(cfg)
    print(f"lonoboard {__version__}")
    print(f"config      {cfg.get('_path') or f'none (defaults; create {default_path()} to change them)'}")
    t0 = time.time()
    snap = c.snapshot()
    h = snap["host"]
    g = h.get("gpu")
    print(f"machine     {h['hostname']} · {h['os']} · {h['cpus']} cpus · {h['memory_gib']['total']} gib ram")
    if g:
        mem = "unified memory" if g.get("unified") else f"{g.get('mem_total_gib')} gib vram"
        print(f"gpu         {h['platform']} · {mem} · via {g['vendor']}")
    else:
        print("gpu         none found (looked for nvidia-smi, rocm-smi, apple silicon)")
    ts = h.get("tailnet")
    print(f"tailscale   {ts['dns_name'] if ts else ('not running' if c.net.tailscale else 'not installed')} "
          f"{'(optional)' if not ts else ''}".rstrip())
    print(f"docker      {'available' if runtime.docker_available() else 'not available (optional)'}")
    users = cfg["control"]["users"]
    print(f"control     {'for ' + ', '.join(users) if users else 'off (list Tailscale logins in control.users to turn it on)'}")
    print()
    if not snap["services"]:
        print("model servers  none found.")
        print(textwrap.indent(textwrap.dedent("""\
            lonoboard looks for llama-server, ollama and vllm processes, and tries ports
            """ + ", ".join(map(str, cfg['detect']['ports'])) + """.
            to add one by hand, put this in the config file:

              [[server]]
              name = "my-model"
              url = "http://127.0.0.1:8080"
            """), "  "))
    else:
        print(f"model servers  {len(snap['services'])} found")
        for s in snap["services"]:
            run = s["run"]
            how = {"systemd": f"systemd unit {run.get('unit')}", "docker": f"docker container {run.get('unit')}",
                   "process": f"process {run.get('pid')}"}.get(run.get("manager"), "url only")
            models = ", ".join(m["id"] for m in s.get("models") or [] if m.get("id"))[:60] or "-"
            src = c._logsrc.get(s["name"])
            usage = ("per-request logs (journal)" if s["kind"] == "llama.cpp" and src and src[0] == "journal"
                     else "request log (journal)" if s["kind"] == "ollama" and src
                     else "slot activity" if s["kind"] == "llama.cpp"
                     else "metrics" if s["kind"] == "vllm" else "-")
            print(f"  {s['name']:<14} {s['kind']:<9} {s['url']:<24} {s['health'] or '-':<11} found by {s.get('origin')}")
            print(f"  {'':<14} models: {models}")
            print(f"  {'':<14} runs as: {how} · usage from: {usage}" + (" · chat" if s.get("chat") else ""))
    print()
    print(f"collected in {int((time.time() - t0) * 1000)} ms. run `lonoboard` to start the board, `lonoboard demo` to see it with fake data.")
    return 0


# ---------- lonoboard install ----------

def install(args):
    import lonoboard
    pkg_parent = Path(lonoboard.__file__).resolve().parent.parent
    in_site = "site-packages" in str(pkg_parent) or "dist-packages" in str(pkg_parent)
    exe = shutil.which("lonoboard") if in_site and shutil.which("lonoboard") else None
    cmd = f"{exe} serve" if exe else f"{sys.executable} -m lonoboard serve"
    if args.config:
        cmd += f" --config {Path(args.config).expanduser().resolve()}"
    lines = ["[Unit]", "Description=lonoboard: a live dashboard for your model servers",
             "After=network-online.target", "", "[Service]", f"ExecStart={cmd}"]
    if not in_site:                                   # running from a source checkout
        lines.append(f"Environment=PYTHONPATH={pkg_parent}")
    lines += ["Restart=on-failure", "RestartSec=5", "", "[Install]", "WantedBy=default.target"]
    unit = "\n".join(lines) + "\n"
    if args.print:
        print(unit, end="")
        return 0
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "lonoboard.service"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit)
    print(f"wrote {path}")
    if args.now:
        for c in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "lonoboard"]):
            r = subprocess.run(c, capture_output=True, text=True)
            if r.returncode:
                sys.exit(f"lonoboard: {' '.join(c)} failed: {r.stderr.strip()}")
        print("started: systemctl --user status lonoboard")
    else:
        print("start it with: systemctl --user daemon-reload && systemctl --user enable --now lonoboard")
    linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
                            capture_output=True, text=True).stdout.strip() if shutil.which("loginctl") else ""
    if linger and linger != "Linger=yes":
        print(f"to keep it running when you're logged out: sudo loginctl enable-linger {os.environ.get('USER')}")
    if shutil.which("tailscale"):
        print("to put it on your tailnet:")
        print("  tailscale serve --bg --http=7777 http://127.0.0.1:7777")
        print("  tailscale serve --bg --tcp=23 --proxy-protocol=1 tcp://127.0.0.1:2323      # then: telnet <this machine>")
    return 0
