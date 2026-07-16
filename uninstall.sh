#!/usr/bin/env bash
set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CTL="${WLCHROMA_CTL:-$(command -v wlchroma-ctl || echo "$HOME/wlchroma/zig-out/bin/wlchroma-ctl")}"

systemctl --user disable --now mpris-chroma.service || true
rm -f "$UNIT_DIR/mpris-chroma.service"
systemctl --user daemon-reload

# Restore the live wlchroma default palette.
"$CTL" set-palette witch_hour || true

echo "Uninstalled and default palette restored."
