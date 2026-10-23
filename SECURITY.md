# Security

lonoboard runs servers: a web board, a JSON API and telnet. Anyone who can reach them sees what
your machine is doing, so here is what it shows and how it's protected.

## What the board shows

- The machine's name, hardware, uptime, and the model servers it finds: their models, ports,
  URLs, systemd units or containers, and launch flags.
- Recent requests (sizes and speeds, never prompts or replies), and the last lines of each
  service's log. Logs can contain whatever your model servers write to them.
- A chat window that sends viewers' messages to your models. Turn it off with `chat = false` if
  viewers shouldn't use your models.

## Who can reach it

- **By default, only this machine.** lonoboard listens on `127.0.0.1` until you change `listen`.
- **Beyond localhost or your tailnet, a password is required.** lonoboard refuses to start
  otherwise, unless you set `allow_anonymous`, which is meant for a demo board.
- **The password** is asked for at a login prompt in telnet and web sessions: three tries, with
  a delay after each. The API takes it as a bearer token or basic auth. Wrong guesses cost a
  second each, and ten in ten minutes lock that address out for a while.
- **Telnet and plain HTTP aren't encrypted.** Over anything but a network you trust, reach the
  board through Tailscale, an SSH tunnel, or a reverse proxy with TLS.
- **Other web sites can't read a board.** There are no cross-origin headers, and without a
  password the board only answers to host names a stranger's page can't point at it: IP
  addresses, single-label names, local and tailnet suffixes, this machine's name, and anything
  in `board.hosts`. This defeats DNS rebinding.

## Controlling services

Off by default. With `[control] users` set, the logins listed can start, stop and restart
services and change whether they start at boot, from the board or the API.

- **Identity comes from Tailscale**: the `Tailscale-User-Login` header `tailscale serve` adds
  (on connections from localhost), or `tailscale whois` for a board on a tailnet address. Tagged
  devices and telnet sessions have no login, so they can't control anything.
- **Anything on the machine itself can claim a login**, by connecting to the board on localhost
  with that header. Don't turn controls on where other people have accounts, or behind a reverse
  proxy that passes visitors' headers through. lonoboard won't start with controls on and
  `listen` beyond localhost or the tailnet.
- **Other web pages can't send controls in your name**: a control must be a JSON POST, and a
  browser's `Origin` must be the board's own. A plain form or a cross-site `fetch` fails.
- **A web session is tied to the login that opened it**; input from anyone else is refused.
- Only systemd **user** units can be controlled, so the board can't do anything its own user
  couldn't do with `systemctl --user`.

## Data from elsewhere

Model names, log lines, chat replies and other boards' data come from programs lonoboard doesn't
control. Control characters and escape sequences are removed before anything reaches a terminal,
and links only open `http` and `https` URLs.

## The fleet

- Boards find each other on your tailnet by asking `/health`, which is always open and says only
  that this is lonoboard, and which version.
- **Your board's password is sent only to peers you list under `[fleet] peers`**, never to
  machines found on the tailnet. List a password-protected peer there, with the same password.

## Not covered

lonoboard has no defence against denial of service beyond session limits (`max_sessions`,
`max_per_ip`, `idle_timeout`). Don't put an anonymous board on the internet without a reverse
proxy in front of it.

## Reporting a vulnerability

Please report security problems privately, through GitHub's
[private vulnerability reporting](https://github.com/ztaylor/lonoboard/security/advisories/new),
rather than in a public issue. You'll get a reply within a few days.
