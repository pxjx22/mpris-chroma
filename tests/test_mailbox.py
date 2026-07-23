import threading
import unittest

from mpris_chroma.worker import Mailbox


class MailboxTest(unittest.TestCase):
    def test_put_then_get_returns_item(self):
        # A single put is handed straight back by get when nothing signals stop.
        mb = Mailbox()
        mb.put((1, "desired-a"))
        self.assertEqual(mb.get(threading.Event()), (1, "desired-a"))

    def test_superseded_true_when_strictly_newer_item_pending(self):
        # The worker's pre-commit check: a newer desired state is waiting.
        mb = Mailbox()
        mb.put((5, "d"))
        self.assertTrue(mb.superseded(3))

    def test_superseded_false_when_pending_gen_not_strictly_greater(self):
        # Equal gen must NOT count as superseded: a same-gen dir-scan resubmit
        # (design §3) reuses the running job's gen and must not preempt it.
        mb = Mailbox()
        mb.put((5, "d"))
        self.assertFalse(mb.superseded(5))

    def test_superseded_false_when_slot_empty(self):
        mb = Mailbox()
        self.assertFalse(mb.superseded(0))

    def test_get_with_stop_set_returns_none_and_discards_pending(self):
        # Shutdown: stop wins over a pending item — get returns None and the
        # slot is drained so nothing lingers to be picked up.
        mb = Mailbox()
        mb.put((1, "d"))
        stop = threading.Event()
        stop.set()
        self.assertIsNone(mb.get(stop))
        self.assertFalse(mb.superseded(0))

    def test_clear_drops_pending_item(self):
        # Shutdown step 3: clear the mailbox so no queued work survives.
        mb = Mailbox()
        mb.put((7, "d"))
        mb.clear()
        self.assertFalse(mb.superseded(0))

    def test_put_replaces_unconsumed_item(self):
        # Coalescing guard: a newer put overwrites an unconsumed older one, so a
        # burst collapses to only the newest desired state.
        mb = Mailbox()
        mb.put((1, "old"))
        mb.put((2, "new"))
        self.assertEqual(mb.get(threading.Event()), (2, "new"))


if __name__ == "__main__":
    unittest.main()
