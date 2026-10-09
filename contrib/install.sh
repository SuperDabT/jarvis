#!/usr/bin/env bash
# Install Jarvis for daily use: CLI + window commands, login service, launcher entry.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"

uv tool install -e "$here" --reinstall -q

mkdir -p ~/.config/jarvis ~/.config/systemd/user ~/.local/share/applications
if [[ ! -f ~/.config/jarvis/env ]]; then
  install -m 600 "$here/contrib/env.example" ~/.config/jarvis/env
fi
chmod 600 ~/.config/jarvis/env
if [[ ! -f ~/.config/jarvis/hardware.yaml ]]; then
  cp "$here/docs/hardware.example.yaml" ~/.config/jarvis/hardware.yaml
fi

install -m 644 "$here/contrib/jarvis.service" ~/.config/systemd/user/jarvis.service
install -m 644 "$here/contrib/jarvis.desktop" ~/.local/share/applications/jarvis.desktop

systemctl --user daemon-reload
systemctl --user enable --now jarvis.service
systemctl --user --no-pager status jarvis.service | head -5
