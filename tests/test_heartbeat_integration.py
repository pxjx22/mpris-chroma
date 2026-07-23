"""Opt-in GLib integration test (design §6): prove a blocked worker does not
freeze the main loop — the one acceptance criterion the thread-free unit suite
cannot express. Real worker thread, Event-gated fake fetch, a real GLib timeout
heartbeat asserted to keep firing while the fetch is blocked.

Enable with:  MPRIS_CHROMA_INTEGRATION=1 python -m unittest tests.test_heartbeat_integration
"""

import os
import threading
import unittest

from mpris_chroma.worker import CoverTarget, Desired, Mailbox, Worker

_ENABLED = os.environ.get("MPRIS_CHROMA_INTEGRATION")


@unittest.skipUnless(_ENABLED, "GLib integration; set MPRIS_CHROMA_INTEGRATION=1")
class HeartbeatTest(unittest.TestCase):
    def test_blocked_worker_does_not_freeze_the_glib_loop(self):
        try:
            from gi.repository import GLib
        except ImportError as e:
            self.skipTest(f"gi/GLib unavailable: {e}")

        beats = {"n": 0}
        gate = threading.Event()      # holds the fake fetch "in flight"
        reported = threading.Event()  # set when the worker finishes the job

        def slow_resolve(art, covers_dir):
            gate.wait(2.0)            # block like a slow download (capped so no hang)
            return "/covers/a.jpg"

        mb = Mailbox()
        worker = Worker(
            mb,
            resolve=slow_resolve,
            extract=lambda p, m: ("#1", "#2", "#3"),
            apply=lambda *c: None,
            revert=lambda: None,
            report=lambda r: reported.set(),
        )
        worker.start()
        try:
            mb.put((1, Desired(CoverTarget("http://x", None), "dark")))  # worker blocks

            def _heartbeat():
                beats["n"] += 1
                return True           # keep firing

            loop = GLib.MainLoop()
            GLib.timeout_add(10, _heartbeat)                       # 10 ms heartbeat
            GLib.timeout_add(200, lambda: bool(loop.quit()))       # run ~200 ms

            loop.run()

            # The fetch was still blocked the whole window (gate never released),
            # yet the loop kept dispatching its heartbeat — proving the worker's
            # block never reached the main thread.
            self.assertFalse(reported.is_set(), "worker should still be mid-fetch")
            self.assertGreaterEqual(beats["n"], 5, "GLib loop was starved")
        finally:
            gate.set()                # release the fetch
            self.assertTrue(worker.stop_and_join(2), "worker did not stop")


if __name__ == "__main__":
    unittest.main()
