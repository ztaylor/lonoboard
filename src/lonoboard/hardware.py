"""The machine: host basics (Linux and macOS) and the accelerator (NVIDIA, AMD, Apple, none).

gpu dict: vendor, name, count, util_pct, temp_c, power_w, unified (GPU memory is system RAM),
mem_total_gib / mem_used_gib (discrete GPUs). Values the hardware doesn't report are None.
"""
import getpass
import json
import os
import platform
import re
import shutil
import socket
import time
from pathlib import Path

from lonoboard.util import sh

DARWIN = platform.system() == "Darwin"


def _num(s):
    try:
        return float(str(s).strip().rstrip("%").split()[0])
    except (ValueError, IndexError):
        return None


class Hardware:
    def __init__(self):
        if shutil.which("nvidia-smi"):
            self.vendor = "nvidia"
        elif shutil.which("rocm-smi"):
            self.vendor = "amd"
        elif DARWIN and platform.machine() == "arm64":
            self.vendor = "apple"
        else:
            self.vendor = None
        self.cpu_model = self._cpu_model()

    # --- host ---
    def host(self):
        total, available = self.memory_gib()
        return {
            "hostname": socket.gethostname().split(".")[0],
            "user": getpass.getuser(),
            "os": "macos" if DARWIN else platform.system().lower(),
            "uptime_s": self._uptime(),
            "load": [round(x, 2) for x in os.getloadavg()],
            "cpus": os.cpu_count(),
            "cpu_model": self.cpu_model,
            "memory_gib": {"total": total, "available": available},
        }

    def memory_gib(self):
        if DARWIN:
            total = int(sh("sysctl", "-n", "hw.memsize") or 0)
            page = int(sh("sysctl", "-n", "hw.pagesize") or 4096)
            vm = {k.strip(): int(v.strip().rstrip(".")) for k, v in
                  (line.split(":", 1) for line in sh("vm_stat").splitlines()[1:] if ":" in line)
                  if v.strip().rstrip(".").isdigit()}
            free = sum(vm.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
            return round(total / 2**30, 1), round(free * page / 2**30, 1)
        mem = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0]) * 1024
        except OSError:
            return 0.0, 0.0
        return round(mem.get("MemTotal", 0) / 2**30, 1), round(mem.get("MemAvailable", 0) / 2**30, 1)

    def _uptime(self):
        if DARWIN:
            m = re.search(r"sec = (\d+)", sh("sysctl", "-n", "kern.boottime"))
            return int(time.time() - int(m.group(1))) if m else None
        try:
            return int(float(Path("/proc/uptime").read_text().split()[0]))
        except OSError:
            return None

    def _cpu_model(self):
        if DARWIN:
            return sh("sysctl", "-n", "machdep.cpu.brand_string").strip() or None
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith(("model name", "hardware")):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
        out = sh("lscpu")
        m = re.search(r"Model name:\s*(.+)", out)
        return m.group(1).strip() if m else None

    # --- accelerator ---
    def gpu(self):
        """(gpu dict or None, {pid: gpu MiB})."""
        fn = {"nvidia": self._nvidia, "amd": self._amd, "apple": self._apple}.get(self.vendor)
        return fn() if fn else (None, {})

    def _nvidia(self):
        out = sh("nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw,memory.used,memory.total",
                 "--format=csv,noheader,nounits")
        rows = [[x.strip() for x in line.split(",")] for line in out.splitlines() if line.count(",") == 5]
        if not rows:
            return None, {}
        nums = lambda i: [v for v in (_num(r[i]) for r in rows) if v is not None]
        avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
        used, total = nums(4), nums(5)
        gpu = {
            "vendor": "nvidia", "name": rows[0][0], "count": len(rows),
            "util_pct": avg(nums(1)), "temp_c": max(nums(2)) if nums(2) else None,
            "power_w": round(sum(nums(3)), 1) if nums(3) else None,
            "unified": not total,                 # GB10-class chips report no separate memory
            "mem_used_gib": round(sum(used) / 1024, 1) if total else None,
            "mem_total_gib": round(sum(total) / 1024, 1) if total else None,
        }
        per_pid = {}
        for line in sh("nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits").splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                per_pid[int(parts[0])] = per_pid.get(int(parts[0]), 0) + int(parts[1])
        return gpu, per_pid

    def _amd(self):
        try:
            data = json.loads(sh("rocm-smi", "--showproductname", "--showuse", "--showtemp", "--showpower",
                                 "--showmeminfo", "vram", "--json") or "{}")
        except ValueError:
            return None, {}
        cards = [v for k, v in data.items() if k.startswith("card") and isinstance(v, dict)]
        if not cards:
            return None, {}

        def field(card, *needles):
            for k, v in card.items():
                if all(n in k.lower() for n in needles):
                    return v
            return None

        util = [_num(field(c, "gpu use")) for c in cards]
        temp = [_num(field(c, "temperature")) for c in cards]
        power = [_num(field(c, "power")) for c in cards]
        total = [_num(field(c, "vram total memory")) for c in cards]
        used = [_num(field(c, "vram total used")) for c in cards]
        ok = lambda xs: [x for x in xs if x is not None]
        return {
            "vendor": "amd", "name": field(cards[0], "card series") or field(cards[0], "card model") or "amd gpu",
            "count": len(cards),
            "util_pct": round(sum(ok(util)) / len(ok(util)), 1) if ok(util) else None,
            "temp_c": max(ok(temp)) if ok(temp) else None,
            "power_w": round(sum(ok(power)), 1) if ok(power) else None,
            "unified": False,
            "mem_used_gib": round(sum(ok(used)) / 2**30, 1) if ok(used) else None,
            "mem_total_gib": round(sum(ok(total)) / 2**30, 1) if ok(total) else None,
        }, {}

    def _apple(self):
        util = None
        m = re.search(r'"Device Utilization %"\s*=\s*(\d+)', sh("ioreg", "-r", "-d", "1", "-c", "IOAccelerator"))
        if m:
            util = float(m.group(1))
        name = (self.cpu_model or "apple silicon").replace("Apple ", "apple ")
        return {"vendor": "apple", "name": f"{name} gpu", "count": 1, "util_pct": util, "temp_c": None,
                "power_w": None, "unified": True, "mem_used_gib": None, "mem_total_gib": None}, {}
