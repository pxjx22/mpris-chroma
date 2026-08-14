import subprocess
import types
import unittest

from mpris_chroma import sync


class SubmitGuardTest(unittest.TestCase):
    """A live worker's job is put on the mailbox; a dead worker (killed by a
    BaseException past _serve's backstop) instead triggers the exit path so the
    daemon does not silently accept jobs nothing runs (SEC-001 §2.3)."""

    def _guard(self, alive):
        put, dead = [], []
        worker = types.SimpleNamespace(is_alive=lambda: alive)
        mailbox = types.SimpleNamespace(put=put.append)
        sync._submit_guarded((1, "d"), worker, mailbox, lambda: dead.append(1))
        return put, dead

    def test_live_worker_receives_the_job(self):
        put, dead = self._guard(alive=True)
        self.assertEqual(put, [(1, "d")])
        self.assertEqual(dead, [])

    def test_dead_worker_triggers_exit_and_drops_the_job(self):
        put, dead = self._guard(alive=False)
        self.assertEqual(put, [])
        self.assertEqual(dead, [1])


class _FakeProc:
    """Records terminate/kill/wait so child-reaping can be tested without a
    real subprocess."""

    def __init__(self, *, hangs=False):
        self.terminated = False
        self.killed = False
        self.waits = []          # timeout arg of each wait() call
        self._hangs = hangs

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        # A hanging child times out on the bounded wait, but is reaped by the
        # unbounded wait that follows kill().
        if self._hangs and timeout is not None:
            raise subprocess.TimeoutExpired("playerctl", timeout)
        return 0


class TerminateChildTest(unittest.TestCase):
    """SEC-013: the playerctl child is always reaped within a bounded time — a
    SIGTERM it ignores is escalated to SIGKILL — so shutdown cannot hang until
    systemd's stop timeout."""

    def test_cooperative_child_is_terminated_and_reaped_without_kill(self):
        p = _FakeProc()
        sync._terminate_child(p, timeout=1)
        self.assertTrue(p.terminated)
        self.assertFalse(p.killed)
        self.assertEqual(p.waits, [1])  # one bounded wait, reaped

    def test_child_ignoring_sigterm_is_killed_and_reaped(self):
        p = _FakeProc(hangs=True)
        sync._terminate_child(p, timeout=1)
        self.assertTrue(p.terminated)
        self.assertTrue(p.killed)
        self.assertEqual(p.waits, [1, None])  # bounded wait, then unbounded reap

    def test_already_exited_child_is_tolerated(self):
        # wait() returns immediately; terminate on a finished child is harmless.
        p = _FakeProc()
        sync._terminate_child(p, timeout=1)  # must not raise


class SchemeHandlerTest(unittest.TestCase):
    """SEC-010: the portal SettingChanged callback validates namespace, key,
    and value type before calling on_scheme, and never raises or disables
    itself on malformed input — sender/path pinning is delivered separately by
    _register_scheme_receiver's add_signal_receiver kwargs."""

    def _handler(self):
        calls, drops = [], []
        handler = sync._make_scheme_handler(
            on_scheme=calls.append,
            log_drop=lambda category, detail: drops.append(category))
        return handler, calls, drops

    def test_accepts_a_valid_portal_signal(self):
        handler, calls, drops = self._handler()
        handler(sync.APPEARANCE_NS, sync.SCHEME_KEY, 2)
        self.assertEqual(calls, [2])
        self.assertEqual(drops, [])

    def test_ignores_wrong_namespace_or_key(self):
        handler, calls, drops = self._handler()
        handler("org.gnome.desktop.interface", sync.SCHEME_KEY, 2)
        handler(sync.APPEARANCE_NS, "accent-color", 2)
        self.assertEqual(calls, [])

    def test_ignores_malformed_value_without_raising(self):
        handler, calls, drops = self._handler()
        handler(sync.APPEARANCE_NS, sync.SCHEME_KEY, "not-an-int")  # must not raise
        self.assertEqual(calls, [])
        self.assertEqual(drops, ["scheme"])

    def test_second_call_still_works_after_a_malformed_one(self):
        # A malformed value must not disable the callback for subsequent signals.
        handler, calls, drops = self._handler()
        handler(sync.APPEARANCE_NS, sync.SCHEME_KEY, "bad")
        handler(sync.APPEARANCE_NS, sync.SCHEME_KEY, 1)
        self.assertEqual(calls, [1])


class SchemeReceiverRegistrationTest(unittest.TestCase):
    """SEC-010: the theme-change receiver is pinned to the portal's bus name
    and object path, so a signal from any other session-bus peer is filtered
    by the bus daemon before it ever reaches the handler."""

    def test_registers_pinned_to_the_portal_bus_name_and_path(self):
        seen = {}

        def add_signal_receiver(handler, **kw):
            seen["handler"] = handler
            seen["kw"] = kw

        bus = types.SimpleNamespace(add_signal_receiver=add_signal_receiver)
        sentinel = object()
        sync._register_scheme_receiver(bus, sentinel)

        self.assertIs(seen["handler"], sentinel)
        self.assertEqual(seen["kw"]["signal_name"], "SettingChanged")
        self.assertEqual(seen["kw"]["dbus_interface"],
                          "org.freedesktop.portal.Settings")
        self.assertEqual(seen["kw"]["bus_name"], sync.PORTAL_BUS_NAME)
        self.assertEqual(seen["kw"]["path"], sync.PORTAL_OBJECT_PATH)


class SpawnFollowTest(unittest.TestCase):
    """PY-002: the playerctl watcher is spawned through one documented policy
    helper — an argv list (never shell=True) with stdout piped for the GLib
    watch — the daemon's only long-lived subprocess, reaped by _terminate_child.
    Together with apply._run_ctl (short ctl calls), no direct subprocess call
    remains without a documented lifecycle policy."""

    def test_spawns_follow_cmd_with_piped_stdout_and_no_shell(self):
        seen = {}

        def popen(cmd, **kw):
            seen["cmd"] = list(cmd)
            seen["kw"] = kw
            return "PROC"

        proc = sync._spawn_follow(popen=popen)
        self.assertEqual(proc, "PROC")
        self.assertEqual(seen["cmd"], sync._follow_cmd())      # argv list
        self.assertEqual(seen["kw"].get("stdout"), subprocess.PIPE)
        self.assertNotIn("shell", seen["kw"])                  # never shell=True


if __name__ == "__main__":
    unittest.main()
