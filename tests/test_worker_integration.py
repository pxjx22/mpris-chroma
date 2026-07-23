"""Opt-in integration tests that use REAL threads (design §6).

Kept out of the default thread-free unit run; enable with:

    MPRIS_CHROMA_INTEGRATION=1 python -m unittest tests.test_worker_integration
"""

import os
import threading
import unittest

from mpris_chroma.worker import CoverTarget, Desired, Mailbox, Worker

_ENABLED = os.environ.get("MPRIS_CHROMA_INTEGRATION")


def _worker(mb, report):
    return Worker(
        mb,
        resolve=lambda a, c: "/covers/a.jpg",
        extract=lambda p, m: ("#1", "#2", "#3"),
        apply=lambda *c: None,
        revert=lambda: None,
        report=report,
    )


@unittest.skipUnless(_ENABLED, "real-thread integration; set MPRIS_CHROMA_INTEGRATION=1")
class WorkerThreadLifecycleTest(unittest.TestCase):
    def test_started_worker_serves_a_submitted_job(self):
        reported, done = [], threading.Event()
        mb = Mailbox()
        w = _worker(mb, lambda r: (reported.append(r), done.set()))
        w.start()
        try:
            mb.put((1, Desired(CoverTarget("http://x", None), "dark")))
            self.assertTrue(done.wait(2), "worker did not report within 2s")
            self.assertEqual(reported[0].outcome, "committed")
        finally:
            self.assertTrue(w.stop_and_join(2), "worker did not stop")
        self.assertFalse(w.is_alive())

    def test_stop_wakes_a_worker_blocked_on_an_empty_mailbox(self):
        # The worker is blocked in get() with nothing queued; stop_and_join must
        # wake it (Mailbox.wake) so shutdown does not hang. Without wake(), the
        # join times out and this returns False.
        mb = Mailbox()
        w = _worker(mb, lambda r: None)
        w.start()
        self.assertTrue(w.stop_and_join(2), "blocked worker was not woken/stopped")
        self.assertFalse(w.is_alive())


if __name__ == "__main__":
    unittest.main()
