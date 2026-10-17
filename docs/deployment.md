# Deployment

## As a systemd user service

    pipx install lonoboard
    lonoboard install --now

This writes `~/.config/systemd/user/lonoboard.service`, which runs `lonoboard serve`, then enables and
starts it. Add `--config FILE` to bake in a config file. Use `--print` to see the unit without
writing it.

- `systemctl --user status lonoboard` shows whether it's running.
- `journalctl --user -u lonoboard -f` follows its log.
- `sudo loginctl enable-linger $USER` keeps it running when you're logged out. `lonoboard install`
  reminds you if linger is off.

lonoboard runs as your user, so it sees your systemd units and your processes. For other users'
servers, it still probes the well-known ports.

## On a tailnet (recommended)

Keep lonoboard on localhost, and let Tailscale publish it to your tailnet only:

    tailscale serve --bg --http=7777 http://127.0.0.1:7777
    tailscale serve --bg --tcp=23   tcp://127.0.0.1:2323

Then `http://<machine>:7777/` and `telnet <machine>` work from any device on the tailnet.

The board's network screen shows the published URLs. The fleet screen finds every other
machine on the tailnet running lonoboard.

## On a LAN

Listen on the network with a password:

```toml
[board]
listen = "0.0.0.0"
password = "something long"      # or set LONOBOARD_PASSWORD in the unit's environment
```

Telnet sends the password in clear text, so only do this on a network you trust.

Putting a password-less board behind a reverse proxy with its own domain? Add that domain to
`board.hosts`, otherwise the board refuses it (see [SECURITY.md](../SECURITY.md)). Over the
internet, use Tailscale or an SSH tunnel instead:

    ssh -L 7777:localhost:7777 -L 2323:localhost:2323 gpu-box

## Several machines

`contrib/deploy-ssh.sh` copies the source to each host over SSH. It then installs the unit,
starts lonoboard, and runs the `tailscale serve` commands if Tailscale is installed:

    contrib/deploy-ssh.sh --config examples/dgx-spark.toml spark-1 spark-2

The hosts need only Python 3.11 or newer; nothing is installed with pip.

## Docker

The image is meant for the public demo board (see [public-demo.md](public-demo.md)). lonoboard can
run in a container on your own machine too, but it can only see what the container can see:

```bash
docker build -t lonoboard .
docker run --rm --network host --pid host --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  lonoboard lonoboard serve
```

| flag | gives lonoboard |
|------|-----------|
| `--network host` | your model servers' ports, and lets you reach the board |
| `--pid host` | the processes behind those ports |
| `--gpus all` | `nvidia-smi` |
| the Docker socket | containers |

systemd units and the journal aren't visible from inside a container. A native install sees
more.

## macOS

`lonoboard serve` works on macOS: hardware comes from `ioreg` and listeners from `lsof`. There's no
launchd installer yet, so run it in a terminal or write a LaunchAgent.
