import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

# A palette entry is exactly '#' plus six hex digits. fullmatch (not $) is
# deliberate: '$' would accept a trailing newline, and wlchroma-ctl joins argv
# into one whitespace-delimited IPC line, so a newline-bearing value could
# inject a second protocol line (SEC-008).
_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")

# wlchroma-ctl is a build output (zig-out/bin), not usually system-installed, so
# WLCHROMA_CTL lets the service point at it; falls back to PATH then bare name.
CTL = os.environ.get("WLCHROMA_CTL") or shutil.which("wlchroma-ctl") or "wlchroma-ctl"
# wlchroma's live config; its [effect.settings] palette is the "config preset"
# a closed/stopped player reverts to.
CONFIG_PATH = Path.home() / ".config/wlchroma/config.toml"
# Smoothstep glide to the new palette (0 = instant). Long on purpose: wlchroma
# only redraws the background on Wayland frame callbacks, which niri throttles for
# an occluded layer surface, so a short fade settles before enough frames render
# and snaps. A longer duration spans enough sparse callbacks to read as a glide.
FADE_MS = 2000


def apply_wlchroma(c1: str, c2: str, c3: str, *, fade_ms: int = FADE_MS,
                   ctl: str = CTL, run=subprocess.run) -> None:
    cmd = [ctl, "set-colors", c1, c2, c3]
    if fade_ms > 0:
        cmd.append(str(fade_ms))
    run(cmd)


def _valid_hex(value: object) -> str | None:
    """Return value unchanged iff it is a well-formed '#rrggbb' string, else
    None. Casing is preserved (wlchroma parses hex, and existing configs' IPC
    output must not silently change); only the format is enforced, which also
    strips whitespace/control/newline injection vectors."""
    if not isinstance(value, str):
        return None
    return value if _HEX_RE.fullmatch(value) else None


def _config_palette(config_path: Path) -> tuple[str, str, str] | None:
    """The 3 [effect.settings] palette colors, or None if unreadable/malformed.

    Every element must be a well-formed '#rrggbb' hex string; anything else
    (wrong type, wrong length, non-hex charset, or a whitespace/newline-bearing
    value) yields None so the caller reverts to a safe default rather than
    forwarding an unvalidated value to wlchroma-ctl (SEC-008)."""
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        palette = data["effect"]["settings"]["palette"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return None
    if not isinstance(palette, list) or len(palette) != 3:
        return None
    validated = tuple(_valid_hex(c) for c in palette)
    if any(v is None for v in validated):
        return None
    return validated  # type: ignore[return-value]


def revert_wlchroma(*, ctl: str = CTL, run=subprocess.run,
                    config_path: Path = CONFIG_PATH, fade_ms: int = FADE_MS) -> None:
    colors = _config_palette(config_path)
    if colors is None:
        # Config gone or malformed: fall back to the named default so the
        # daemon still reverts instead of dying mid-revert.
        run([ctl, "set-palette", "witch_hour"])
        return
    apply_wlchroma(*colors, fade_ms=fade_ms, ctl=ctl, run=run)
