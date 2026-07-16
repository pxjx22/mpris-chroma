#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

cd "$REPO"  # so `python -m mpris_chroma...` resolves the package

# Install + enable the user service.
mkdir -p "$UNIT_DIR"
ln -sf "$REPO/systemd/mpris-chroma.service" "$UNIT_DIR/mpris-chroma.service"
systemctl --user daemon-reload
systemctl --user enable --now mpris-chroma.service

echo "Installed. Status: systemctl --user status mpris-chroma.service"
