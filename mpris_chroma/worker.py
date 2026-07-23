"""Off-main-thread palette worker (SEC-001, phase 4b).

GLib-free by construction: this module never imports gi/GLib. The worker reports
results through an injected ``report`` callable (sync.py wires it to
``GLib.idle_add(coordinator.adopt, ...)``), so the unit suite can drive it
synchronously and provably without initializing GLib.
"""

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from .apply import CtlError
from .cover import CoverError
from .state import Mode

_log = logging.getLogger("mpris_chroma.worker")
_log.addHandler(logging.NullHandler())

# A job reports exactly one of these (design §2.3):
#   committed         - resolve succeeded and ctl confirmed the change
#   skipped_duplicate - layer-2 guard hit; no extraction, no ctl
#   failed            - resolve returned None/raised, OR ctl raised
COMMITTED = "committed"
SKIPPED_DUPLICATE = "skipped_duplicate"
FAILED = "failed"

# Sentinel for "no confirmed state yet" — never equals any real (path, mode).
_UNSET = object()


@dataclass(frozen=True, slots=True)
class CoverTarget:
    """The inputs resolve_cover needs to materialize one cover."""

    art_url: str
    covers_dir: Path | None


@dataclass(frozen=True, slots=True)
class Desired:
    """The end-state the worker converges wlchroma toward. target=None is a
    revert to the config preset; a CoverTarget is an apply."""

    target: CoverTarget | None
    mode: Mode


@dataclass(frozen=True, slots=True)
class Result:
    """What the worker hands back to the coordinator's adopt()."""

    gen: int
    outcome: str
    cover_id: str | None   # resolved cover id on commit, else None


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

    def wake(self):
        """Notify a blocked get() so it can re-check its stop event (shutdown)."""
        with self._cond:
            self._cond.notify()

    def superseded(self, gen):
        """True iff a strictly-newer item is already waiting. Strict ``>`` so a
        same-gen resubmit (a dir-scan re-run, design §3) does not preempt the
        running job."""
        with self._cond:
            return self._item is not None and self._item[0] > gen


class Worker:
    """Runs one job at a time off the main thread: resolve -> extract -> apply,
    or revert. GLib-free; results are handed to ``report`` (sync.py wires
    GLib.idle_add)."""

    def __init__(self, mailbox, *, resolve, extract, apply, revert, report,
                 initial_committed=_UNSET):
        self._mailbox = mailbox
        self._resolve = resolve
        self._extract = extract
        self._apply = apply
        self._revert = revert
        # Marshals a Result back to the main thread; sync.py wires this to
        # `lambda r: GLib.idle_add(coordinator.adopt, r)` (design §2.2/§2.3).
        self._report = report
        # (resolved_path, mode) of the last confirmed change; None-path is a
        # confirmed revert. Production seeds this with (None, startup_mode) to
        # mirror the inline startup revert (design §5.2); the default sentinel
        # never matches, so a fresh worker commits its first job.
        self._last_committed = initial_committed
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None

    def start(self) -> None:
        """Spawn the daemon worker thread. Daemon so a wedged worker can never
        block interpreter exit; stop_and_join is the graceful path."""
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(self._stop,), name="palette-worker", daemon=True)
        self._thread.start()

    def stop_and_join(self, timeout) -> bool:
        """Signal stop, wake a blocked get, and join within `timeout`. Returns
        True if the thread finished (design §5 shutdown step 4-5)."""
        self._stop.set()
        self._mailbox.wake()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self, stop) -> None:
        """Pump loop: serve items until get() returns None (stop signalled)."""
        while (item := self._mailbox.get(stop)) is not None:
            self._serve(item)

    def _serve(self, item) -> None:
        """One pump iteration after get(): run the job and report a non-None
        result. An unexpected exception (a bug, not a contained Cover/CtlError)
        is logged and reported as failed so the thread loop survives."""
        gen = item[0]
        try:
            result = self._run_once(item)
        except Exception:
            _log.exception("worker job (gen=%s) failed unexpectedly", gen)
            result = Result(gen, FAILED, None)
        if result is not None:
            self._report(result)

    def _run_once(self, item) -> Result | None:
        """Run one job. Returns a Result to report, or None when the job was
        superseded mid-flight (the newer job in the mailbox will report the
        authoritative outcome)."""
        gen, desired = item
        if desired.target is None:
            if (None, desired.mode) == self._last_committed:
                return Result(gen, SKIPPED_DUPLICATE, None)
            if self._mailbox.superseded(gen):
                return None  # a newer desire is waiting; drop this stale revert
            try:
                self._revert()
            except CtlError:
                return Result(gen, FAILED, None)
            self._last_committed = (None, desired.mode)
            return Result(gen, COMMITTED, None)
        try:
            cover_id = self._resolve(desired.target.art_url, desired.target.covers_dir)
        except CoverError:
            return Result(gen, FAILED, None)
        if cover_id is None:
            return Result(gen, FAILED, None)
        if (cover_id, desired.mode) == self._last_committed:
            return Result(gen, SKIPPED_DUPLICATE, cover_id)
        if self._mailbox.superseded(gen):
            return None  # a newer desire is waiting; drop before extract + ctl
        c1, c2, c3 = self._extract(cover_id, desired.mode)
        if self._mailbox.superseded(gen):
            return None  # re-check immediately before ctl: extract is not free,
            #              so a newer desire may have arrived during it (guarantee b)
        try:
            self._apply(c1, c2, c3)
        except CtlError:
            return Result(gen, FAILED, None)
        self._last_committed = (cover_id, desired.mode)
        return Result(gen, COMMITTED, cover_id)
