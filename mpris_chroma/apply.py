import logging
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

_log = logging.getLogger("mpris_chroma.apply")
_log.addHandler(logging.NullHandler())

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

# wlchroma-ctl talks to a local Unix socket, so a healthy call returns almost
# instantly. Bound it so a hung ctl (wlchroma wedged, socket unreadable) can
# never stall a GLib callback or block shutdown until systemd's stop timeout
# (SEC-007).
CTL_TIMEOUT = 5   # seconds
_MAX_DIAG = 200   # bytes of ctl stderr preserved in a CtlError message


class CtlError(Exception):
    """A wlchroma-ctl call timed out, failed to start, or exited non-zero."""


def _run_ctl(cmd: list[str], *, run=subprocess.run) -> None:
    """Run a wlchroma-ctl command bounded by CTL_TIMEOUT with its exit status
    checked. Raises CtlError on timeout, spawn failure, or non-zero exit — with
    a bounded stderr excerpt — so a ctl call can neither block indefinitely nor
    have its failure silently ignored (SEC-007)."""
    try:
        result = run(cmd, timeout=CTL_TIMEOUT, capture_output=True, text=True)
    except subprocess.TimeoutExpired as e:
        raise CtlError(f"wlchroma-ctl timed out after {CTL_TIMEOUT}s") from e
    except OSError as e:
        raise CtlError(f"wlchroma-ctl could not run: {e}") from e
    if result.returncode != 0:
        err = (result.stderr or "").strip()[:_MAX_DIAG]
        raise CtlError(f"wlchroma-ctl exited {result.returncode}: {err}")


def apply_wlchroma(c1: str, c2: str, c3: str, *, fade_ms: int = FADE_MS,
                   ctl: str = CTL, run=subprocess.run) -> None:
    # Defense in depth at the subprocess-construction boundary (SEC-016): every
    # color must be a well-formed '#rrggbb' regardless of caller. wlchroma-ctl
    # joins argv into one whitespace-delimited IPC line, so a value bearing
    # whitespace or a newline could otherwise smuggle an extra IPC token or a
    # whole extra protocol line. Callers already validate (config via SEC-008,
    # extracted colors are formatted hex), so this only ever rejects a bug.
    if any(_valid_hex(c) is None for c in (c1, c2, c3)):
        raise CtlError(f"refusing to apply malformed palette: {(c1, c2, c3)!r}")
    cmd = [ctl, "set-colors", c1, c2, c3]
    if fade_ms > 0:
        cmd.append(str(fade_ms))
    _run_ctl(cmd, run=run)


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
        # daemon still reverts instead of dying mid-revert. Bounded/checked like
        # every other ctl call.
        _run_ctl([ctl, "set-palette", "witch_hour"], run=run)
        return
    apply_wlchroma(*colors, fade_ms=fade_ms, ctl=ctl, run=run)
