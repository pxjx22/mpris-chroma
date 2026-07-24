#!/usr/bin/env python3
"""Fake jellyfin-tui MPRIS player — live SEC-018 runbook harness.

Owns org.mpris.MediaPlayer2.jellyfin-tui on the session bus and emits minimal
but well-formed Player properties so `playerctl --follow` sees it. Driven over
stdin (one command per line), so a FIFO gives interactive control:

    play          PlaybackStatus -> Playing (+PropertiesChanged)
    pause         PlaybackStatus -> Paused  (+PropertiesChanged)
    track <title> new Metadata: xesam:title=<title>, mpris:artUrl="" (+changed)
    quit          exit cleanly (releases the bus name -> vanish)

With an empty artUrl and jellyfin-tui's configured covers_dir, the daemon
treats this as a dir-scan player — an absent/empty covers dir resolves
Retryable (write-lag), and dropping an image into the dir heals it.

Preserved from the SEC-018 (phase 4c) live runbook: the only way to live-test
retry/fallback/vanish behavior without jellyfin-tui installed. Not part of the
unit suite; run it manually alongside `journalctl --user -u mpris-chroma -f`.

    python tools/fake_mpris.py [ctl-fifo-path] &
    echo play > /tmp/fake-mpris.ctl
"""

import os
import sys

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS_NAME = "org.mpris.MediaPlayer2.jellyfin-tui"
OBJ_PATH = "/org/mpris/MediaPlayer2"

NODE_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Play"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Seek"><arg name="Offset" type="x" direction="in"/></method>
    <method name="SetPosition"><arg name="TrackId" type="o" direction="in"/><arg name="Position" type="x" direction="in"/></method>
    <method name="OpenUri"><arg name="Uri" type="s" direction="in"/></method>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="read"/>
    <property name="Shuffle" type="b" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="read"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class FakePlayer:
    def __init__(self):
        self.playback_status = "Paused"
        self.track_no = 0
        self.metadata: dict = {}
        self.conn = None
        self._set_track("fake track 0")

    def _set_track(self, title, art_url=""):
        self.metadata = {
            "mpris:trackid": GLib.Variant(
                "o", f"/org/mpris/MediaPlayer2/Track/{self.track_no}"
            ),
            "xesam:title": GLib.Variant("s", title),
            "xesam:artist": GLib.Variant("as", ["fake"]),
            "mpris:artUrl": GLib.Variant("s", art_url),
        }

    # --- bus plumbing -------------------------------------------------------

    def on_method(self, conn, sender, path, iface, method, params, invocation):
        invocation.return_value(None)

    _STATIC = {
        ("org.mpris.MediaPlayer2", "CanQuit"): ("b", False),
        ("org.mpris.MediaPlayer2", "CanRaise"): ("b", False),
        ("org.mpris.MediaPlayer2", "HasTrackList"): ("b", False),
        ("org.mpris.MediaPlayer2", "Identity"): ("s", "fake-jellyfin-tui"),
        ("org.mpris.MediaPlayer2", "SupportedUriSchemes"): ("as", []),
        ("org.mpris.MediaPlayer2", "SupportedMimeTypes"): ("as", []),
        ("org.mpris.MediaPlayer2.Player", "LoopStatus"): ("s", "None"),
        ("org.mpris.MediaPlayer2.Player", "Rate"): ("d", 1.0),
        ("org.mpris.MediaPlayer2.Player", "Shuffle"): ("b", False),
        ("org.mpris.MediaPlayer2.Player", "Volume"): ("d", 1.0),
        ("org.mpris.MediaPlayer2.Player", "Position"): ("x", 0),
        ("org.mpris.MediaPlayer2.Player", "MinimumRate"): ("d", 1.0),
        ("org.mpris.MediaPlayer2.Player", "MaximumRate"): ("d", 1.0),
        ("org.mpris.MediaPlayer2.Player", "CanGoNext"): ("b", False),
        ("org.mpris.MediaPlayer2.Player", "CanGoPrevious"): ("b", False),
        ("org.mpris.MediaPlayer2.Player", "CanPlay"): ("b", True),
        ("org.mpris.MediaPlayer2.Player", "CanPause"): ("b", True),
        ("org.mpris.MediaPlayer2.Player", "CanSeek"): ("b", False),
        ("org.mpris.MediaPlayer2.Player", "CanControl"): ("b", True),
    }

    def on_get(self, conn, sender, path, iface, prop):
        if (iface, prop) in self._STATIC:
            sig, val = self._STATIC[(iface, prop)]
            return GLib.Variant(sig, val)
        if prop == "PlaybackStatus":
            return GLib.Variant("s", self.playback_status)
        if prop == "Metadata":
            return GLib.Variant("a{sv}", self.metadata)
        return None

    def _emit(self, iface, changed):
        self.conn.emit_signal(
            None,
            OBJ_PATH,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (iface, changed, [])),
        )

    # --- commands -----------------------------------------------------------

    def cmd(self, line):
        parts = line.strip().split(maxsplit=1)
        if not parts:
            return True
        print(f"cmd: {line.strip()!r}", flush=True)
        try:
            return self._cmd(parts)
        except Exception:
            import traceback

            traceback.print_exc()
            return True

    def _cmd(self, parts):
        """Returns False only for 'quit' — on_stdin quits the loop on False."""
        cmd = parts[0]
        if cmd == "play":
            self.playback_status = "Playing"
            self._emit(
                "org.mpris.MediaPlayer2.Player",
                {"PlaybackStatus": GLib.Variant("s", "Playing")},
            )
        elif cmd == "pause":
            self.playback_status = "Paused"
            self._emit(
                "org.mpris.MediaPlayer2.Player",
                {"PlaybackStatus": GLib.Variant("s", "Paused")},
            )
        elif cmd == "track":
            args = parts[1].split(maxsplit=1) if len(parts) > 1 else []
            title = args[0] if args else f"fake track {self.track_no}"
            art_url = args[1] if len(args) > 1 else ""
            self.track_no += 1
            self._set_track(title, art_url)
            self._emit(
                "org.mpris.MediaPlayer2.Player",
                {"Metadata": GLib.Variant("a{sv}", self.metadata)},
            )
        elif cmd == "quit":
            return False
        else:
            print(f"unknown command: {cmd}", flush=True)
        return True


def _open_fifo():
    """Open the control FIFO O_RDWR: we hold both ends, so it never blocks and
    never HUPs when an external writer (echo cmd > fifo) closes. This decouples
    the harness from the launching shell's fd lifetime."""
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake-mpris.ctl"
    if not os.path.exists(path):
        os.mkfifo(path, 0o600)
    return os.open(path, os.O_RDWR | os.O_NONBLOCK)


def main():
    player = FakePlayer()
    loop = GLib.MainLoop()
    node = Gio.DBusNodeInfo.new_for_xml(NODE_XML)

    def on_bus_acquired(conn, name):
        player.conn = conn
        for iface in node.interfaces:
            conn.register_object(OBJ_PATH, iface, player.on_method, player.on_get, None)

    def on_stdin(channel, condition):
        if condition & GLib.IO_HUP:
            print("HUP on control FIFO -> quit", flush=True)
            loop.quit()
            return False
        status, line, _length, _term = channel.read_line()
        if status == GLib.IOStatus.AGAIN:
            return True  # spurious wakeup on the non-blocking FIFO; no data yet
        if status != GLib.IOStatus.NORMAL or line is None:
            print(f"read failed ({status}) -> quit", flush=True)
            loop.quit()
            return False
        if not player.cmd(line):
            loop.quit()
            return False
        return True

    channel = GLib.IOChannel.unix_new(_open_fifo())
    GLib.io_add_watch(
        channel, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP, on_stdin
    )
    Gio.bus_own_name(
        Gio.BusType.SESSION,
        BUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired,
        lambda c, n: print(f"acquired {n}", flush=True),
        lambda c, n: (print("bus name lost -> quit", flush=True), loop.quit()),
    )
    print("fake jellyfin-tui ready; commands: play|pause|track <t>|quit", flush=True)
    loop.run()


if __name__ == "__main__":
    main()
