"""Rules over a snapshot: what state a service is in and which ones should be up."""


def busy_of(sv, hist):
    b = (sv.get("model") or {}).get("slots_busy")
    if b is None and hist:
        b = hist[-1].get("busy", {}).get(sv["name"], 0)
    return b or 0


def status(sv, hist):
    """(kind, text): kind is busy | ok | warn | starting | bad | off."""
    run, health = sv.get("run") or {}, sv.get("health")
    st = run.get("state")
    if health == "ok":
        return ("busy", None) if busy_of(sv, hist) > 0 else ("ok", None)
    if st in ("activating", "reloading") or health == "loading":
        return "starting", None
    if st == "failed":
        return "bad", "failed"
    if st == "active":
        return "warn", "not responding" if health in (None, "unreachable") else str(health).lower()
    if not run.get("manager"):            # something we only know by its URL
        return "bad", "unreachable"
    return ("bad", "down") if run.get("enabled") else ("off", None)


def expected(d):
    """Services that should be running: enabled, active, or simply listed (no manager to ask)."""
    return [s for s in d["services"]
            if s["run"].get("enabled") or s["run"].get("state") == "active" or not s["run"].get("manager")]


def healthy(d):
    return [s for s in expected(d) if s.get("health") == "ok"]


def chat_capable(sv):
    return bool((sv.get("chat") or {}).get("models"))


def chat_targets(d):
    """[(service, model)] that can be chatted with."""
    return [(s["name"], m) for s in d["services"] for m in (s.get("chat") or {}).get("models") or []]


def requests_today(d):
    return sum((s.get("model") or {}).get("requests_today") or 0 for s in d["services"])
