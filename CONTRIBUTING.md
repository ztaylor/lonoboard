# Contributing

Issues and pull requests are welcome. Reports about servers, GPUs and terminals that lonoboard gets
wrong are especially useful. Include the output of `lonoboard check` and, where it helps, `lonoboard json`.

## Ground rules

- **Standard library only.** lonoboard has no dependencies and should stay that way. It runs on
  Python 3.11 and newer.
- **Plain words on screen.** The BBS is the look, not the vocabulary. Write "requests", not
  "callers"; "machine", not "node".
- **Subtle ANSI.** The art should frame the data, not compete with it.
- **Safe by default.** Nothing listens beyond localhost without the user asking, and nothing
  unprotected listens beyond the tailnet.

## Working on it

    git clone https://github.com/ztaylor/lonoboard && cd lonoboard
    PYTHONPATH=src python3 -m lonoboard demo          # the board on simulated data
    PYTHONPATH=src python3 -m lonoboard check         # what it finds on your machine
    python3 -m unittest discover -s tests -t tests

Every screen is tested at several window sizes. If you change a screen, run the tests and look
at it at 80x25 too.

## Layout

| path | what it does |
|------|--------------|
| `src/lonoboard/cli.py` | the `lonoboard` command |
| `src/lonoboard/config.py` | defaults, loading, the listen-address safety check |
| `src/lonoboard/collector.py` | builds the snapshot, samples every 2 s, feeds usage every 10 s |
| `src/lonoboard/sources.py` | identifying model servers and describing each kind |
| `src/lonoboard/control.py` | starting and stopping services: who may, what can be done, the memory check |
| `src/lonoboard/runtime.py` | listeners, processes, systemd, Docker |
| `src/lonoboard/hardware.py` | CPU, memory, GPUs |
| `src/lonoboard/network.py` | Tailscale |
| `src/lonoboard/usage.py` | request history on disk |
| `src/lonoboard/demo.py` | the simulated machine |
| `src/lonoboard/web.py` | HTTP: the web terminal, JSON API, fonts |
| `src/lonoboard/board/` | the board itself: `render` (cell grid → ANSI), `logo`, `screens`, `session` (keys, modals, chat), `hub` (shared data, events, remote boards), `telnet` |
| `src/lonoboard/static/terminal.html` | the browser terminal |
| `tools/media.py` | the promo video, README screenshots, `hero.gif` and the social card, made by running the board on demo data (`uv run tools/media.py promo` / `shots`) |

## Adding a model server kind

1. Add a describe function to `sources.py`, next to `_ollama` and `_vllm`, and register it in
   `describe()`.
2. Teach `identify()` to recognise the server, and add its process name to `PROCESS_KINDS`.
3. If the server logs requests, add a parser to `collector.py` and a feed in `Collector.feed()`.
4. Add a fake server to `tests/helpers.py` and tests to `tests/test_sources.py`.

## Releases

Bump `__version__` in `src/lonoboard/__init__.py`, add a `CHANGELOG.md` entry, and tag `vX.Y.Z`.
