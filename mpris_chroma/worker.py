"""Off-main-thread palette worker (SEC-001, phase 4b).

GLib-free by construction: this module never imports gi/GLib. The worker reports
results through an injected ``post`` callable (sync.py wires ``GLib.idle_add``),
so the unit suite can drive it synchronously and provably without initializing
GLib.
"""

import threading


class Mailbox:
    """A one-slot, replace-on-put handoff between the coordinator (producer) and
    the single worker (consumer). Holding only one slot is the coalescing
    mechanism: a newer desired state overwrites an unconsumed older one."""

    def __init__(self):
        self._cond = threading.Condition()
        self._item = None

    def put(self, item):
        with self._cond:
            self._item = item
            self._cond.notify()

    def get(self, stop):
        """Block until an item is available or ``stop`` is set. ``stop`` wins:
        it returns ``None`` and drains any pending item so nothing lingers past
        shutdown. (The blocking wake is covered by the opt-in integration test.)"""
        with self._cond:
            while self._item is None and not stop.is_set():
                self._cond.wait()
            if stop.is_set():
                self._item = None
                return None
            item, self._item = self._item, None
            return item

    def clear(self):
        """Drop any pending item without blocking (shutdown step 3)."""
        with self._cond:
            self._item = None

    def superseded(self, gen):
        """True iff a strictly-newer item is already waiting. Strict ``>`` so a
        same-gen resubmit (a dir-scan re-run, design §3) does not preempt the
        running job."""
        with self._cond:
            return self._item is not None and self._item[0] > gen
