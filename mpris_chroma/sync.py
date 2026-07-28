import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .apply import CtlError, apply_wlchroma, revert_wlchroma
from .colors import PaletteMemo
from .coordinator import Coordinator, mode_from_color_scheme
from .cover import resolve_cover
from .framing import READ_CHUNK, LineFramer
from .worker import Mailbox, Worker

_log = logging.getLogger("mpris_chroma.sync")
_log.addHandler(logging.NullHandler())

PLAYERS = "jellyfin-tui,spotify"
# Players with a local on-disk cover cache; others (spotify) resolve via http.
COVERS_DIRS = {"jellyfin-tui": Path.home() / ".local/share/jellyfin-tui/covers"}

# Seconds to wait for the playerctl child to exit on SIGTERM before escalating
# to SIGKILL, so shutdown is bounded well under systemd's stop timeout (SEC-013).
CHILD_STOP_TIMEOUT = 5

# Bound on joining the palette worker at shutdown (SEC-001 §5). Worst case is an
# abortable download stage (~one socket op once should_stop fires) plus one ctl
# call — far under systemd's 90 s TimeoutStopSec.
WORKER_STOP_TIMEOUT = 10

# freedesktop settings portal: color-scheme lives in this namespace and is
# 0 = no preference, 1 = prefer dark, 2 = prefer light.
APPEARANCE_NS = "org.freedesktop.appearance"
SCHEME_KEY = "color-scheme"


# Subprocess policy (PY-002). The daemon has exactly two kinds of subprocess:
# short wlchroma-ctl calls (apply._run_ctl, now driven only from the worker) and
# the one long-lived playerctl watcher spawned here and reaped by
# _terminate_child. Both use argv lists; nothing uses shell=True.


def _spawn_follow(*, popen=subprocess.Popen):
    """Spawn the long-lived playerctl watcher (PY-002). Lifecycle policy: an
    argv list (never shell=True), stdout piped for the GLib IO watch, stderr
    inherited to the journal. It runs for the daemon's lifetime and is reaped by
    _terminate_child within CHILD_STOP_TIMEOUT."""
    return popen(_follow_cmd(), stdout=subprocess.PIPE)


def _terminate_child(proc, *, timeout: int = CHILD_STOP_TIMEOUT) -> None:
    """Terminate the playerctl child and guarantee it is reaped within `timeout`:
    SIGTERM, wait up to `timeout`, then SIGKILL and an unbounded reap if it
    ignored the term. Tolerates a child that has already exited. Bounds shutdown
    so a playerctl that ignores SIGTERM cannot hang the service (SEC-013)."""
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


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


def _make_io_reader(fd, framer, on_line, *, on_hangup, hup_err_mask):
    """Build the GLib IO callback for playerctl's stdout (SEC-011 §3).

    Module-level (and GLib-free — the caller supplies the condition mask) so the
    flood test can drive the real reader instead of a copy of it.

    Exactly ONE bounded read per dispatch: a read-until-EAGAIN loop would
    reintroduce the unbounded drain the framer exists to prevent. GLib
    re-dispatches this source while data remains, so a flood costs at most one
    chunk per loop iteration and every equal-priority source (retry timers,
    D-Bus) gets its turn in between — bounded latency, not merely eventual
    progress. EOF is treated exactly like HUP: playerctl is gone, so hang up.
    """
    def _on_io(_fd, condition):
        if condition & hup_err_mask:
            on_hangup()
            return False
        try:
            data = os.read(fd, READ_CHUNK)
        except BlockingIOError:
            return True          # spurious wakeup; nothing readable yet
        if not data:
            on_hangup()          # EOF: same contract as HUP
            return False
        for line in framer.feed(data):
            on_line(line)
        return True

    return _on_io


def _submit_guarded(item, worker, mailbox, on_worker_dead) -> None:
    """Put `item` on the worker's mailbox, unless the worker thread has died — in
    which case invoke on_worker_dead() and drop it. Defense in depth (SEC-001
    §2.3): a BaseException (e.g. MemoryError) can kill the worker past _serve's
    Exception backstop; a daemon that kept accepting jobs nothing runs would
    degrade silently behind a healthy-looking PID, so instead we exit non-zero
    for systemd Restart=on-failure to recover."""
    if not worker.is_alive():
        on_worker_dead()
        return
    mailbox.put(item)


def _bounded_revert() -> None:
    """Revert to the config preset directly, containing a ctl failure. Used at
    startup (loop not running yet) and once more at shutdown after the worker is
    stopped, where a bounded ctl call on the main thread is fine (SEC-001 §5)."""
    try:
        revert_wlchroma()
    except CtlError as e:
        _log.warning("revert failed: %s", e)


def main():
    # Event-driven under a GLib loop. All blocking work (download, decode, ctl)
    # runs on a single worker thread; GLib callbacks only parse, update state,
    # and schedule (SEC-001). playerctl stdout drives status/cover changes, and
    # D-Bus NameOwnerChanged drives player-vanished reverts.
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib, GLibUnix

    # Surface contained worker/cover failures (SEC-015) in the journal.
    logging.basicConfig(level=logging.WARNING)

    DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()
    bus = dbus.SessionBus()
    exit_code = 0

    # Palette mode: MPRIS_CHROMA_MODE=light|dark forces it; otherwise follow the
    # desktop's color-scheme via the freedesktop settings portal, live.
    forced_mode = os.environ.get("MPRIS_CHROMA_MODE", "").lower()
    follow_scheme = forced_mode not in ("light", "dark")
    if not follow_scheme:
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

    # Start from a known-good default, inline (the loop is not running yet, so
    # there is nothing to starve). The worker is seeded with (None, mode) so this
    # established state is not redundantly re-applied by the first revert job.
    _bounded_revert()

    mailbox = Mailbox()

    # Shutdown flips this; the worker's resolve stage polls it so an in-flight
    # download aborts promptly instead of waiting out the transfer deadline.
    stopping = threading.Event()

    def _on_worker_dead():
        # Same exit path as an unexpected playerctl death (HUP): exit non-zero so
        # systemd Restart=on-failure recovers instead of the daemon degrading.
        nonlocal exit_code
        _log.critical("palette worker thread died; exiting for restart")
        exit_code = 1
        loop.quit()

    # `worker` is referenced late-bound (defined below); submit is only called
    # from event handlers, which fire after loop.run(), so it always exists then.
    def _submit(item):
        _submit_guarded(item, worker, mailbox, _on_worker_dead)

    def _schedule_retry(delay_ms, fn):
        # One-shot: the wrapper returns False so GLib removes the source after
        # it fires; the coordinator treats its handle as consumed on fire and
        # only cancels handles that have not fired, so source_remove never sees
        # a dead source.
        def _cb():
            fn()
            return False
        return GLib.timeout_add(int(delay_ms), _cb)

    coordinator = Coordinator(submit=_submit, covers_dir_for=COVERS_DIRS.get,
                              mode=mode, schedule=_schedule_retry,
                              cancel=GLib.source_remove,
                              # Same value as `--player=` in _follow_cmd, so the
                              # follow set and the accept set cannot drift.
                              allowed_players=PLAYERS)

    def _post(result):
        # Marshal a worker result to the main thread; return False so the idle
        # source is one-shot (design §2.3 "report without removing GLib sources").
        def _cb():
            coordinator.adopt(result)
            return False
        GLib.idle_add(_cb)

    worker = Worker(
        mailbox,
        resolve=lambda art, cd: resolve_cover(art, cd, should_stop=stopping.is_set),
        extract=PaletteMemo(),
        apply=apply_wlchroma,
        revert=revert_wlchroma,
        report=_post,
        initial_committed=(None, mode),
    )
    worker.start()

    # Enter the guaranteed-cleanup scope immediately after a successful spawn: an
    # exception while wiring the IO watch or receivers below would otherwise leak
    # the playerctl child (SEC-013).
    proc = _spawn_follow()
    stdout = proc.stdout        # never None: _spawn_follow always pipes stdout
    try:
        fd = stdout.fileno()
        os.set_blocking(fd, False)
        framer = LineFramer(on_drop=lambda detail:
                            coordinator.log_drop("oversize", detail))

        def _on_hangup():
            # playerctl died unexpectedly (HUP/ERR or EOF); exit non-zero so
            # systemd's Restart=on-failure recovers us.
            nonlocal exit_code
            exit_code = 1
            loop.quit()

        _on_io = _make_io_reader(
            fd, framer, coordinator.on_line, on_hangup=_on_hangup,
            hup_err_mask=GLib.IOCondition.HUP | GLib.IOCondition.ERR)

        GLibUnix.fd_add_full(
            GLib.PRIORITY_DEFAULT, fd,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            _on_io)

        def _on_name_owner_changed(name, _old_owner, new_owner):
            if new_owner == "":  # a bus name was lost
                coordinator.on_vanish(str(name))

        bus.add_signal_receiver(
            _on_name_owner_changed,
            signal_name="NameOwnerChanged",
            dbus_interface="org.freedesktop.DBus",
            bus_name="org.freedesktop.DBus")

        if follow_scheme:
            def _on_setting_changed(namespace, key, value):
                if str(namespace) == APPEARANCE_NS and str(key) == SCHEME_KEY:
                    coordinator.on_scheme(int(value))

            bus.add_signal_receiver(
                _on_setting_changed,
                signal_name="SettingChanged",
                dbus_interface="org.freedesktop.portal.Settings")

        def _on_term():
            # Sequenced shutdown (design §5): stop scheduling and invalidate
            # in-flight results, drain the mailbox, abort an in-flight download,
            # join the worker, then do the final revert with the worker stopped
            # so no in-flight ctl can commit after it.
            coordinator.begin_shutdown()
            mailbox.clear()
            stopping.set()
            if not worker.stop_and_join(WORKER_STOP_TIMEOUT):
                _log.warning("worker did not stop within %ss", WORKER_STOP_TIMEOUT)
            _bounded_revert()
            loop.quit()
            return False

        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _on_term)
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _on_term)

        loop.run()
    finally:
        # Reached by both the graceful path (above, worker already stopped) and
        # the HUP path (exit_code=1, no deliberate revert — we exit non-zero for
        # systemd to restart us). stop_and_join is idempotent.
        stopping.set()
        worker.stop_and_join(WORKER_STOP_TIMEOUT)
        _terminate_child(proc)
        stdout.close()   # release the pipe read end (was channel.shutdown)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
