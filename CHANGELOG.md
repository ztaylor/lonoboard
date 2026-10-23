# Changelog

## Unreleased

**Controlling services** (opt-in, `[control] users`)
- Start, stop and restart services that run as systemd user units, and choose whether they
  start at boot: `s`, `r`, `e` in a service's details window, after a confirmation, or
  `POST /api/services/NAME/ACTION`.
- Only the Tailscale logins listed may, identified by `tailscale serve`'s headers or
  `tailscale whois`. Tagged devices can't.
- Telnet sessions are identified too, when `tailscale serve` forwards them with
  `--proxy-protocol=1` (or 2): the board reads the PROXY header on connections from localhost.
  `lonoboard install` and the docs now print the serve command with it.
- Before starting a model, its weight files are compared with free memory; if they don't fit,
  the board asks again.
- Works on other machines from the fleet screen, when their board allows this machine.
- `GET /api/control` says whether you may.

## 0.1.0 - 2026-10-17

The first public version.

**The board**
- Telnet and web terminal: CP437 or UTF-8, 24-bit, 256 or 16 colours, iCE colours, mouse,
  hyperlinks.
- Screens: main, services, host, usage, requests, events, network, fleet. They have paging,
  lightbars, a details window with a live log, and a chat window.
- Display settings: VGA 8x16 or 9x16 font, VGA, amber or green palette, CRT effect.

**Model servers**
- llama.cpp, Ollama, vLLM, OpenAI-compatible, custom. Found automatically or listed in config.
- Runtime from systemd units and Docker containers.
- Request history from logs and Prometheus metrics, kept for 48 hours.

**Hardware**
- NVIDIA (including unified memory), AMD, Apple Silicon, CPU-only.

**Fleet**
- Other machines running lonoboard, from config or the tailnet. You can view their boards in your
  own session.

**Safety**
- Listens on localhost by default.
- A password with an in-terminal login is required beyond localhost or the tailnet.
- Session limits and idle timeouts.
- Other web sites can't read a board: no cross-origin access, and a DNS-rebinding guard for boards
  without a password.
- Escape sequences in model output, logs and other boards' data never reach a terminal.
- The board's password only goes to fleet peers you list.

**Commands**
- `lonoboard serve`, `demo`, `check`, `json`, `install`.
- A TOML config, a Dockerfile for a public demo board, and `contrib/deploy-ssh.sh`.
