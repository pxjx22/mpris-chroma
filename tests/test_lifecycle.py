import subprocess
import unittest

from mpris_chroma import sync


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


if __name__ == "__main__":
    unittest.main()
