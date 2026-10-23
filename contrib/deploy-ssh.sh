#!/bin/bash
# Install or update lonoboard on other machines over ssh, from this source checkout.
#
#   contrib/deploy-ssh.sh [--config FILE] HOST...
#
# On each host: copies src/lonoboard to ~/.local/share/lonoboard, optionally installs FILE as
# ~/.config/lonoboard/config.toml, installs and (re)starts the systemd user service (lonoboard install --now),
# and if Tailscale is there, publishes the board on the tailnet (:7777 web, :23 telnet).
# Needs python3 >= 3.11 and systemd on the host; nothing else.
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --config) CONFIG=$2; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) break ;;
  esac
done
[[ $# -gt 0 ]] || { echo "usage: $0 [--config FILE] HOST..." >&2; exit 1; }

for h in "$@"; do
  echo "== $h"
  ssh "$h" 'mkdir -p ~/.local/share/lonoboard ~/.config/lonoboard && rm -rf ~/.local/share/lonoboard/lonoboard'
  tar -C src -cf - --exclude __pycache__ lonoboard | ssh "$h" 'tar -C ~/.local/share/lonoboard -xf -'
  if [[ -n $CONFIG ]]; then
    scp -q "$CONFIG" "$h":.config/lonoboard/config.toml
  fi
  ssh "$h" 'set -e
    PYTHONPATH=~/.local/share/lonoboard python3 -m lonoboard install --config ~/.config/lonoboard/config.toml >/dev/null
    systemctl --user daemon-reload
    systemctl --user enable lonoboard >/dev/null 2>&1
    systemctl --user restart lonoboard
    if command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1; then
      tailscale serve --bg --http=7777 http://127.0.0.1:7777 >/dev/null
      tailscale serve --bg --tcp=23 --proxy-protocol=1 tcp://127.0.0.1:2323 >/dev/null
    fi
    sleep 2
    curl -sf -m 5 localhost:7777/health'
  echo
done
