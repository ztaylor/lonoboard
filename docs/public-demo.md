# Hosting a public demo

`lonoboard demo` runs the board on a simulated machine:
- 2x RTX 3090
- six model servers
- live traffic
- a fake chat that never contacts a real model

It reads nothing from the host, which makes it safe to put on the internet.

When `lonoboard demo` listens on a public address, it switches to public defaults by itself:

| setting | value | why |
|---------|-------|-----|
| `allow_anonymous` | `true` | there's nothing to protect, so no password |
| `idle_timeout` | `15` | idle sessions are disconnected after 15 minutes |
| `max_per_ip` | `3` | at most 3 sessions per address |
| `max_sessions` | `24` | the usual cap on sessions overall |

Settings in a config file take precedence.

## With Docker

```bash
docker build -t lonoboard .
docker run -d --name lonoboard-demo --restart unless-stopped \
  -p 80:7777 -p 23:2323 lonoboard
```

The image runs `lonoboard demo --listen 0.0.0.0` as an unprivileged user.

- `http://your-host/` serves the web board.
- `telnet your-host` reaches it over telnet.

## Without Docker

    lonoboard demo --listen 0.0.0.0 --port 7777 --telnet-port 2323

Port 23 is privileged. Map it in your firewall, or put a TCP proxy in front.

## Behind a reverse proxy

Proxy HTTP to port 7777. The web terminal uses server-sent events at `/term/stream`, so turn
off response buffering for that path. With nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:7777;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

Telnet is plain TCP and needs a TCP proxy, or a direct port.
