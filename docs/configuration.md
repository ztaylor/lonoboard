# Configuration

lonoboard runs without a config file. When it wants one, it looks in this order:

1. `--config FILE`
2. `$LONOBOARD_CONFIG`
3. `$XDG_CONFIG_HOME/lonoboard/config.toml`, which is usually `~/.config/lonoboard/config.toml`

The file is TOML, and every setting is optional. [examples/config.toml](../examples/config.toml)
lists them all with their defaults. Command-line flags (`--listen`, `--port`, `--telnet-port`,
`--name`, `--no-chat`) override the file. Run `lonoboard check` to see what a config finds.

## `[board]`

| setting | default | |
|---------|---------|---|
| `name` | hostname | the name drawn as the logo. Letters, digits, `-`, `.` and `_` |
| `tagline` | `""` | an extra label for the header, e.g. `"dgx spark"` |
| `listen` | `"127.0.0.1"` | the address the web board, API and telnet listen on |
| `port` | `7777` | web board and JSON API |
| `telnet_port` | `2323` | `0` turns telnet off |
| `password` | `""` | required to listen beyond localhost or the tailnet. `$LONOBOARD_PASSWORD` overrides it |
| `allow_anonymous` | `false` | listen on the network without a password |
| `chat` | `true` | let viewers chat with the models |
| `max_sessions` | `24` | concurrent sessions, telnet and web together |
| `max_per_ip` | `0` | concurrent sessions per address; `0` is unlimited |
| `idle_timeout` | `0` | minutes before an idle session is disconnected; `0` never |
| `intro` | `true` | the "connecting to…" sequence when a session starts |
| `hosts` | `[]` | more host names the web board answers to without a password, e.g. a reverse proxy's domain |

### Who can reach the board

lonoboard sorts the `listen` address into one of three cases:

- **localhost** (`127.0.0.1`, `::1`): only this machine. Use this with `tailscale serve` or an
  SSH tunnel.
- **tailnet** (an address in `100.64.0.0/10` or `fd7a:115c:a1e0::/48`): only your Tailscale
  network.
- **network** (anything else, including `0.0.0.0`): lonoboard refuses to start this way unless
  `password` is set or `allow_anonymous = true`.

With a password set:
- Telnet and web sessions open on a login prompt. A session gets three tries, and each failure
  adds a short delay.
- The API needs `Authorization: Bearer <password>`, or HTTP basic auth with any user name.
- `/health` stays open, so the fleet and uptime checks keep working.

Without a password, the web board and API only answer to host names that a web page elsewhere
can't point at your machine. That rules out DNS rebinding. Allowed are:
- IP addresses
- single-label names like `spark-1`
- names ending in `.local`, `.lan`, `.internal`, `.home.arpa`, `.localhost` or `.ts.net`
- this machine's hostname
- anything listed in `hosts`

Other names get a 403 that says so. See [SECURITY.md](../SECURITY.md).

## `[detect]`

| setting | default | |
|---------|---------|---|
| `enabled` | `true` | find model servers on this machine |
| `ports` | `[8080, 8081, 8000, 11434, 1234, 5000, 5001]` | well-known ports to try |

Detection looks at every listening TCP port. A port is tried when the process behind it looks
like a model server, or when it's in `ports`. Ports already covered by `[[server]]` or
`[llama_models]` are skipped. See [sources.md](sources.md) for how servers are identified.

## `[[server]]`

List a server when detection can't find it. That includes a server on another port, on
another machine, or one that needs an API key. Use it also to give a server a better name or a
description.

```toml
[[server]]
name = "qwen"                       # defaults to something derived from the server
url = "http://127.0.0.1:30000"      # required
kind = "llama.cpp"                  # llama.cpp | ollama | vllm | openai | custom; guessed if left out
description = "the everyday model"  # shown in details. /paths in it are listed as endpoints
unit = "llama-qwen.service"         # its systemd unit, if lonoboard can't work it out
user_unit = true                    # a --user unit (default) or a system one
api_key_env = "QWEN_API_KEY"        # send Authorization: Bearer $QWEN_API_KEY
endpoints = ["/v1/chat/completions"]
```

`kind = "custom"` is for servers that aren't model APIs, like a TTS server. lonoboard only checks
their `/health`.

## `[llama_models]`

This adapter is optional and is used when `dir` exists. It suits setups where each model is a
`NAME.conf` file, run by a templated systemd user unit:

```bash
# ~/llama-models/qwen.conf - comments at the top become the description
PORT=8080
ARGS=(-m ~/models/qwen3-30b-a3b-q4_k_m.gguf -c 65536 --jinja)
# or, for something that isn't llama-server:  CMD=(python3 tts_server.py)
```

| setting | default |
|---------|---------|
| `dir` | `"~/llama-models"` |
| `unit` | `"llama@{name}.service"` |

## `[fleet]`

| setting | default | |
|---------|---------|---|
| `peers` | `[]` | other machines running lonoboard: `"host"`, `"host:port"` or a URL |
| `tailscale` | `true` | also look for lonoboard on online tailnet peers, at this board's port |

Each peer is checked once a minute at `/health`. Machines that answer as lonoboard show up on the
fleet screen. This board's password is sent only to peers listed in `peers`, never to ones found
on the tailnet, so list a password-protected peer here. It needs the same password as this board.

## `[labels]`

Some ports are published with `tailscale serve` but lonoboard can't identify them, usually because
they run as another user. `[labels]` gives them names:

```toml
[labels]
11000 = "DGX Dashboard"
```

## `[control]`

| setting | default | |
|---------|---------|---|
| `users` | `[]` | Tailscale logins that may start, stop and enable services from the board, e.g. `["alice@github"]` |

With `users` empty, which is the default, nobody can control anything. With a login listed, that
person sees a **control** row in a service's details window: `s` starts or stops it, `r`
restarts it, and `e` turns starting at boot on or off. Each asks first. The same actions are in
the API.

- **What can be controlled:** services that run as a systemd user unit, such as `[llama_models]`
  ones or a `[[server]]` with `unit`. The board runs as the same user, so it needs no extra
  privileges. System units and containers are shown but not controlled.
- **Who is asking** comes from Tailscale. Through `tailscale serve`, it's the
  `Tailscale-User-Login` header that serve adds to each request; serve replaces any the client
  sends. On a board listening on a tailnet address, lonoboard asks `tailscale whois`. Requests
  from tagged devices have no login, and telnet sessions can't be identified, so neither can
  control anything.
- **The header is trusted on connections from localhost**, because that's how `tailscale serve`
  delivers them. Anything else running on the machine can send it too. On a machine other people
  log in to, or behind another reverse proxy that forwards visitors' headers, don't set `users`.
  lonoboard refuses to start with `users` set and `listen` beyond localhost or the tailnet.
- **Memory:** before starting a model, lonoboard adds up its weight files (`-m`, `--mmproj`,
  `-md`, including split GGUFs) and compares them with free memory: system memory on
  unified-memory machines like the DGX Spark, free VRAM on discrete GPUs. If they don't fit,
  it asks again before going ahead. The KV cache comes on top, so this is a lower bound.
- **Other machines:** from the fleet screen you can control another board's services if you're
  allowed on this board and the other board allows this machine. Through `tailscale serve`, that
  means this machine's owner is in the other board's `users`.

## `[usage]`

| setting | default | |
|---------|---------|---|
| `state_dir` | `$XDG_STATE_HOME/lonoboard`, usually `~/.local/state/lonoboard` | where `usage.json` is kept. It holds 48 hours of history |
