import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from .apply import apply_wlchroma, revert_wlchroma, CtlError
from .colors import extract_colors
from .cover import resolve_cover
from .select import select

_log = logging.getLogger("mpris_chroma.sync")
_log.addHandler(logging.NullHandler())

PLAYERS = "jellyfin-tui,spotify"
# Players with a local on-disk cover cache; others (spotify) resolve via http.
COVERS_DIRS = {"jellyfin-tui": Path.home() / ".local/share/jellyfin-tui/covers"}

MPRIS_PREFIX = "org.mpris.MediaPlayer2."

# freedesktop settings portal: color-scheme lives in this namespace and is
# 0 = no preference, 1 = prefer dark, 2 = prefer light.
APPEARANCE_NS = "org.freedesktop.appearance"
SCHEME_KEY = "color-scheme"


def mode_from_color_scheme(value: int) -> str:
    """Map the portal's color-scheme value to a palette mode. Anything that
    isn't an explicit light preference (incl. 0 = no preference and future
    values) falls back to dark — the historical band."""
    return "light" if value == 2 else "dark"


def player_name_from_bus(bus_name: str) -> str | None:
    """Map a D-Bus name to playerctl's {{playerName}}, or None if not a player.

    playerctl keys players by the bus name minus the MPRIS prefix (instance
    suffix included), so this is exactly the key used in the `players` dict —
    letting a NameOwnerChanged vanish evict the matching entry.
    """
    if bus_name.startswith(MPRIS_PREFIX):
        return bus_name[len(MPRIS_PREFIX):]
    return None


def _revert_all() -> bool:
    """Revert to the config preset. Returns True only if wlchroma-ctl confirmed
    the change; a bounded ctl failure is logged and returns False so the caller
    leaves `applied` set and a later event retries (SEC-007)."""
    try:
        revert_wlchroma()
        return True
    except CtlError as e:
        _log.warning("revert failed: %s", e)
        return False


def _apply_all(cover: Path, mode: str = "dark") -> bool:
    """Extract and apply the cover's palette. Returns True only if wlchroma-ctl
    confirmed the change; a bounded ctl failure is logged and returns False so
    the caller does not mark the cover applied and a later event retries."""
    c1, c2, c3 = extract_colors(cover, mode=mode)
    try:
        apply_wlchroma(c1, c2, c3)
        return True
    except CtlError as e:
        _log.warning("apply failed: %s", e)
        return False


def _reconcile(players: dict, applied, mode: str = "dark"):
    """Run the selection decision over the current player states and apply,
    hold, or revert. Returns the new `applied` cover id. Shared by the stdout
    line handler and the player-vanished handler so the decision lives once.

    `applied` advances only on a confirmed wlchroma change: a failed apply or
    revert leaves it unchanged so the same work is retried on the next event
    rather than being silently recorded as done (SEC-007)."""
    action, chosen = select(players)
    if action == "apply" and chosen != applied:
        if _apply_all(Path(chosen), mode):
            applied = chosen
    elif action == "revert" and applied is not None:
        if _revert_all():
            applied = None
    return applied


def _handle_line(line: str, players: dict, seq: int, applied, mode: str = "dark"):
    """Parse one 'playerName\tstatus\tartUrl' line, update per-player state,
    then apply/hold/revert. Mutates `players` in place; returns (seq, applied)."""
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 2:
        return seq, applied
    name, status = parts[0], parts[1]
    art_url = parts[2] if len(parts) > 2 else ""
    cover = resolve_cover(art_url, COVERS_DIRS.get(name))
    cover_id = str(cover) if cover else None
    seq += 1
    players[name] = (status, cover_id, seq)
    return seq, _reconcile(players, applied, mode)


def _handle_vanish(bus_name: str, players: dict, applied, mode: str = "dark"):
    """A D-Bus name was lost. If it was a tracked player, evict it and
    re-decide — this is what reverts to the config preset when the last
    player closes (playerctl --follow emits no line for a vanished player)."""
    name = player_name_from_bus(bus_name)
    if name is None or players.pop(name, None) is None:
        return applied
    return _reconcile(players, applied, mode)


def _handle_scheme_change(value: int, applied, mode: str) -> str:
    """The portal's color-scheme changed. Returns the mode to use from now on.

    When a cover palette is currently applied, re-tone that same cover under
    the new band (same hues, shifted brightness). When we're reverted to
    wlchroma's config preset, that palette isn't ours to re-tone — just track
    the mode for the next apply."""
    new_mode = mode_from_color_scheme(value)
    if new_mode == mode:
        return mode
    if applied is not None:
        _apply_all(Path(applied), new_mode)
    return new_mode


def _follow_cmd():
    """argv for the streaming multi-player playerctl watcher.

    -a emits a named event for every whitelisted player on any change (not just
    the 'current' one); the `metadata` subcommand is REQUIRED or playerctl prints
    usage and exits.
    """
    return [
        "playerctl", f"--player={PLAYERS}", "-a", "--follow", "metadata",
        "--format", "{{playerName}}\t{{status}}\t{{mpris:artUrl}}",
    ]


def main():
    # Event-driven under a GLib loop: playerctl stdout drives status/cover
    # changes, and D-Bus NameOwnerChanged drives player-vanished reverts —
    # playerctl --follow emits no line when a player closes, so a stdout-only
    # loop can never notice the last player disappearing.
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib, GLibUnix

    # Surface contained cover failures (SEC-015) in the journal. Without an app
    # handler the library logger's NullHandler keeps them silent.
    logging.basicConfig(level=logging.WARNING)

    DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()

    players: dict = {}
    seq = 0
    applied = None
    exit_code = 0
    bus = dbus.SessionBus()

    # Palette mode: MPRIS_CHROMA_MODE=light|dark forces it; otherwise follow
    # the desktop's color-scheme via the freedesktop settings portal, live.
    forced_mode = os.environ.get("MPRIS_CHROMA_MODE", "").lower()
    if forced_mode in ("light", "dark"):
        mode = forced_mode
    else:
        mode = "dark"
        try:
            portal = bus.get_object("org.freedesktop.portal.Desktop",
                                    "/org/freedesktop/portal/desktop")
            value = portal.Read(APPEARANCE_NS, SCHEME_KEY,
                                dbus_interface="org.freedesktop.portal.Settings")
            mode = mode_from_color_scheme(int(value))
        except dbus.DBusException:
            pass  # no portal (headless/odd session): stay dark

        def _on_setting_changed(namespace, key, value):
            nonlocal mode, applied
            if str(namespace) == APPEARANCE_NS and str(key) == SCHEME_KEY:
                mode = _handle_scheme_change(int(value), applied, mode)

        bus.add_signal_receiver(
            _on_setting_changed,
            signal_name="SettingChanged",
            dbus_interface="org.freedesktop.portal.Settings")

    _revert_all()  # start from a known-good default

    proc = subprocess.Popen(_follow_cmd(), stdout=subprocess.PIPE)
    channel = GLib.IOChannel.unix_new(proc.stdout.fileno())
    channel.set_flags(GLib.IOFlags.NONBLOCK)

    def _on_io(chan, condition):
        nonlocal seq, applied, exit_code
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            exit_code = 1  # playerctl died unexpectedly; let systemd restart us
            loop.quit()
            return False
        while True:
            # read_line() -> (status, str_return, length, terminator_pos);
            # only the status has no attribute name, so unpack positionally.
            status, line, _length, _term = chan.read_line()
            if status != GLib.IOStatus.NORMAL or not line:
                break  # AGAIN (drained) or EOF
            seq, applied = _handle_line(line, players, seq, applied, mode)
        return True

    GLib.io_add_watch(
        channel, GLib.PRIORITY_DEFAULT,
        GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
        _on_io)

    def _on_name_owner_changed(name, _old_owner, new_owner):
        nonlocal applied
        if new_owner == "":  # a bus name was lost
            applied = _handle_vanish(str(name), players, applied, mode)

    bus.add_signal_receiver(
        _on_name_owner_changed,
        signal_name="NameOwnerChanged",
        dbus_interface="org.freedesktop.DBus",
        bus_name="org.freedesktop.DBus")

    def _on_term():
        _revert_all()
        loop.quit()
        return False

    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _on_term)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _on_term)

    try:
        loop.run()
    finally:
        proc.terminate()
        proc.wait()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
