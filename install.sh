#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DIR="$HOME/.local/bin"

# Build and install the binary. The unit runs the installed copy, so a
# `cargo clean` or a branch switch in the repo never breaks the service.
cargo build --release --locked --manifest-path "$REPO/Cargo.toml"
install -Dm755 "$REPO/target/release/mpris-chroma" "$BIN_DIR/mpris-chroma"

# The unit's sandbox needs its one writable path to exist before start.
mkdir -p "$HOME/.cache/mpris-chroma"

# Install + enable the user service; restart so a reinstall runs the new
# binary.
mkdir -p "$UNIT_DIR"
ln -sf "$REPO/systemd/mpris-chroma.service" "$UNIT_DIR/mpris-chroma.service"
systemctl --user daemon-reload
systemctl --user enable mpris-chroma.service
systemctl --user restart mpris-chroma.service

echo "Installed. Status: systemctl --user status mpris-chroma.service"
