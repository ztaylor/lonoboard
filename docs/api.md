# JSON API

The board serves JSON on the same port as the web board, `7777` by default. Any request that
doesn't ask for HTML gets JSON, so `curl` on `/` works.

When the board has a password, every endpoint except `/health` needs it. Send it either way:

    curl -H "Authorization: Bearer $LONOBOARD_PASSWORD" localhost:7777/api
    curl -u any:$LONOBOARD_PASSWORD localhost:7777/api

## `GET /` and `GET /api`

A snapshot of the machine. Add `?since=0` to include 15 minutes of telemetry samples, taken
every 2 seconds, under `history`. Add `?since=<unix time>` for only the newer samples.

```jsonc
{
  "host": {
    "hostname": "gpu-box", "user": "ai", "os": "linux", "uptime_s": 280000,
    "load": [1.1, 1.1, 0.9], "cpus": 32, "cpu_model": "...", "memory_gib": {"total": 125.7, "available": 71.2},
    "gpu": {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 3090", "count": 2, "util_pct": 29,
            "temp_c": 42, "power_w": 220.4, "unified": false, "mem_used_gib": 44.8, "mem_total_gib": 48.0},
    "platform": "2x rtx 3090", "memory_label": "48 gib vram · 126 gib ram",
    "software": ["llama.cpp b6510", "ollama 0.12.6"],
    "tailnet": {"dns_name": "gpu-box.tail1234.ts.net", "ips": ["100.64.0.7"]},
    "board": {"name": "gpu-box", "tagline": "", "exposure": "local", "version": "0.1.0", "chat": true}
  },
  "services": [
    {
      "name": "qwen", "kind": "llama.cpp", "origin": "detected",
      "description": "", "url": "http://127.0.0.1:8080", "port": 8080,
      "urls": {"local": "http://127.0.0.1:8080", "remote": ["http://gpu-box:8080"]},
      "endpoints": ["/v1/chat/completions", "/v1/completions", "/health", "/slots"],
      "run": {"manager": "systemd", "unit": "llama-server@qwen.service", "user": true,
              "state": "active", "sub_state": "running", "enabled": true,
              "pid": 48080, "since": 1792107890, "restarts": 0, "container": null, "image": null},
      "memory_mib": {"gpu": 18700, "process": 1200},
      "health": "ok", "latency_ms": 3.2, "version": "llama.cpp b6510",
      "model": {"id": "qwen3-30b-a3b", "params_b": 30.5, "quant": "Q4_K - Medium", "size_gib": 17.3,
                "context": 65536, "slots": 4, "slots_busy": 1,
                "last_request": {"at": 1792216873, "tokens": 720, "gen_tps": 92.4,
                                 "prompt_tokens": 2010, "prompt_tps": 1072.8},
                "requests_today": 45},
      "models": [],
      "chat": {"url": "http://127.0.0.1:8080", "models": ["qwen3-30b-a3b"]}
    }
  ],
  "other": [{"name": "DGX Dashboard", "process": null, "port": 11000, "listening": true,
             "urls": {"local": "http://127.0.0.1:11000", "remote": ["https://gpu-box.tail1234.ts.net:11000"]}}],
  "fleet": [{"name": "gpu-box-2", "url": "http://gpu-box-2:7777", "online": true, "source": "tailscale"}],
  "generated_at": 1792216900.1,
  "took_ms": 41
}
```

Notes on the fields:
- `run.manager` is `systemd`, `docker` or `null`, the last meaning a plain process or unknown.
- `health` is `ok`, `loading`, `error` or `unreachable`.
- `other` lists ports published with `tailscale serve` that aren't model servers.
- `tailnet` is `null` without Tailscale.
- `chat` is `null` for servers that can't chat, such as embeddings and rerankers.

## Other endpoints

| endpoint | returns |
|----------|---------|
| `GET /health` | `{"status": "ok", "app": "lonoboard", "version": "0.1.0"}`. Always open; the fleet uses it to find boards |
| `GET /api/summary` | one line per machine: services up and total, busy, GPU utilisation, memory, requests today. The fleet screen uses it |
| `GET /api/callers` | the most recent requests across all services: time, service, tokens, speeds |
| `GET /api/usage` | `{"hours": [{"hour", "start", "requests", "tokens"} × 24], "top": [{"name", "requests", "tokens", "avg_tps"}]}` |
| `GET /api/events` | the event log as `[time, [[text, colour], ...]]`, where colour is a CGA palette index |
| `GET /api/logs/NAME?n=100` | the last `n` lines of a service's journal or container log |
| `GET /api/control` | `{"enabled", "you", "allowed"}`: whether service controls are on, who the board thinks you are, and whether you may use them |

## Controlling services

    POST /api/services/NAME/ACTION        ACTION: start | stop | restart | enable | disable

`enable` starts the service now and at boot, and `disable` stops it now and at boot. The body is
a JSON object, `{}` or `{"force": true}`. Only logins in `[control] users` may do this; see
[configuration](configuration.md#control).

    curl -X POST -H 'Content-Type: application/json' -d '{}' http://gpu-box:7777/api/services/qwen/stop

| status | meaning |
|--------|---------|
| `200` | `{"ok": true, "service", "action"}`. The board asked systemd; the snapshot shows the result in a moment |
| `400` | an unknown action, or a body that isn't a JSON object |
| `403` | controls are off, you aren't allowed, or the request came from another web page |
| `404` | no such service |
| `409` | it isn't a systemd user unit, or `{"fits": false, "need_gib", "free_gib"}`: its weights are bigger than free memory. Send `{"force": true}` to start it anyway |
| `500` | `systemctl` failed; `error` says why |

The body must be sent as `Content-Type: application/json`, and a browser's `Origin`, if any, must
be the board itself. Together these keep other web pages from sending controls in your name.

The endpoints under `/term/` (`session`, `stream`, `input`, `resize`) belong to the web
terminal. They're not a stable API.
