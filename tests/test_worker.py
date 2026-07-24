import os
import tempfile
import unittest
from pathlib import Path

from mpris_chroma.apply import CtlError
from mpris_chroma.cover import Ready, Rejected, Retryable, resolve_cover
from mpris_chroma.worker import CoverTarget, Desired, Mailbox, Worker


def _ready(path="/covers/a.jpg", content_id=(10, 100)):
    return Ready(Path(path), content_id)


def _worker(**overrides):
    """A Worker wired with inert fakes; individual tests override the stages
    they exercise. `post` runs the callback synchronously so adopt-style
    reporting is observable without a real GLib loop."""
    calls = overrides.pop("_calls", None)
    kw = dict(
        resolve=lambda art, covers_dir: _ready(),
        extract=lambda path, mode: ("#aa0000", "#00bb00", "#0000cc"),
        apply=lambda c1, c2, c3: None,
        revert=lambda: None,
        report=lambda result: None,
    )
    kw.update(overrides)
    return Worker(Mailbox(), **kw), calls


class WorkerRunOnceApplyTest(unittest.TestCase):
    def test_apply_job_resolves_extracts_applies_and_commits(self):
        applied = []
        w, _ = _worker(apply=lambda c1, c2, c3: applied.append((c1, c2, c3)))
        target = CoverTarget(art_url="http://x", covers_dir=None)
        result = w._run_once((5, Desired(target=target, mode="dark")))
        self.assertEqual(result.gen, 5)
        self.assertEqual(result.outcome, "committed")
        self.assertEqual(applied, [("#aa0000", "#00bb00", "#0000cc")])


class WorkerRunOnceFailureTest(unittest.TestCase):
    def _apply_target(self):
        return Desired(target=CoverTarget("http://x", None), mode="dark")

    def test_retryable_resolution_is_failed_retryable_and_skips_work(self):
        extracted, applied = [], []
        w, _ = _worker(
            resolve=lambda a, c: Retryable("network"),
            extract=lambda p, m: extracted.append((p, m)) or ("#1", "#2", "#3"),
            apply=lambda *c: applied.append(c),
        )
        result = w._run_once((5, self._apply_target()))
        self.assertEqual(result.outcome, "failed_retryable")
        self.assertIsNone(result.cover_id)
        self.assertEqual(extracted, [])
        self.assertEqual(applied, [])

    def test_rejected_resolution_is_rejected_and_skips_work(self):
        applied = []
        w, _ = _worker(
            resolve=lambda a, c: Rejected("ssrf"),
            apply=lambda *c: applied.append(c),
        )
        result = w._run_once((5, self._apply_target()))
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(applied, [])

    def test_ctl_error_on_apply_is_failed_retryable(self):
        # ctl momentarily unreachable is transient — the timer should retry it.
        w, _ = _worker(apply=lambda *c: (_ for _ in ()).throw(CtlError("ctl down")))
        result = w._run_once((5, self._apply_target()))
        self.assertEqual(result.outcome, "failed_retryable")


class WorkerSupersededTest(unittest.TestCase):
    """Guarantee (b): a job that finds a strictly-newer item waiting drops itself
    before ctl and returns None — the newer job produces the authoritative
    result, and no stale palette is pushed."""

    def _worker_with_mailbox(self, mb, **overrides):
        kw = dict(
            resolve=lambda a, c: _ready(),
            extract=lambda p, m: ("#1", "#2", "#3"),
            apply=lambda *c: None,
            revert=lambda: None,
            report=lambda result: None,
        )
        kw.update(overrides)
        return Worker(mb, **kw)

    def test_superseded_before_ctl_aborts_apply(self):
        applied = []
        mb = Mailbox()
        w = self._worker_with_mailbox(mb, apply=lambda *c: applied.append(c))
        mb.put((9, Desired(CoverTarget("http://y", None), "dark")))  # newer waiting
        result = w._run_once((5, Desired(CoverTarget("http://x", None), "dark")))
        self.assertIsNone(result)
        self.assertEqual(applied, [])

    def test_superseded_before_revert_aborts_revert(self):
        reverted = []
        mb = Mailbox()
        w = self._worker_with_mailbox(mb, revert=lambda: reverted.append(True))
        mb.put((9, Desired(target=None, mode="dark")))
        result = w._run_once((5, Desired(target=None, mode="dark")))
        self.assertIsNone(result)
        self.assertEqual(reverted, [])

    def test_superseded_during_extract_aborts_before_ctl(self):
        # A newer desire that arrives WHILE extract runs (a ~100-300ms decode)
        # must still be caught immediately before ctl — guarantee (b)'s window is
        # extract-scale, not just resolve-scale, without the pre-ctl re-check.
        applied = []
        mb = Mailbox()

        def extract_then_supersede(path, mode):
            mb.put((9, Desired(CoverTarget("http://z", None), "dark")))  # newer arrives
            return ("#1", "#2", "#3")

        w = self._worker_with_mailbox(
            mb, extract=extract_then_supersede, apply=lambda *c: applied.append(c))
        result = w._run_once((5, Desired(CoverTarget("http://x", None), "dark")))
        self.assertIsNone(result)
        self.assertEqual(applied, [])


class WorkerServeTest(unittest.TestCase):
    """_serve is one pump iteration's post-get processing: run the job, report a
    non-None result, and backstop an unexpected exception as failed so the
    thread loop survives (design §2.3)."""

    def _apply(self, gen):
        return (gen, Desired(target=CoverTarget("http://x", None), mode="dark"))

    def test_serve_reports_the_result(self):
        reported = []
        w, _ = _worker(report=reported.append)
        w._serve(self._apply(5))
        self.assertEqual([r.outcome for r in reported], ["committed"])

    def test_serve_does_not_report_when_superseded(self):
        reported = []
        mb = Mailbox()
        w = Worker(
            mb, resolve=lambda a, c: _ready(),
            extract=lambda p, m: ("#1", "#2", "#3"),
            apply=lambda *c: None, revert=lambda: None, report=reported.append,
        )
        mb.put((9, Desired(CoverTarget("http://y", None), "dark")))
        w._serve(self._apply(5))
        self.assertEqual(reported, [])

    def test_serve_backstops_unexpected_exception_as_failed_retryable(self):
        # A non-Cover/Ctl exception (a bug) is contained and reported; the loop is
        # not torn down. A bug retries (bounded by the attempt cap) then goes
        # terminal, rather than crashing the worker.
        reported = []
        w, _ = _worker(
            report=reported.append,
            extract=lambda p, m: (_ for _ in ()).throw(RuntimeError("bug")),
        )
        w._serve(self._apply(5))
        self.assertEqual([r.outcome for r in reported], ["failed_retryable"])


class RealResolveIdentityTest(unittest.TestCase):
    """Unit 5 (SEC-018): content identity end-to-end through the REAL
    resolve_cover with real files — no fakes on the resolve side. These are
    integration guards for the cover.py <-> worker seam (each half was built
    RED-first under fakes in units 1-2); they verify the ledger requirement
    'replacing a file at the same path triggers re-extraction' as shipped."""

    def _worker(self, extracted):
        return Worker(
            Mailbox(),
            resolve=resolve_cover,   # the real resolver (dir-scan path)
            extract=lambda p, m: extracted.append(p) or ("#1", "#2", "#3"),
            apply=lambda *c: None,
            revert=lambda: None,
            report=lambda r: None,
        )

    def test_file_overwritten_in_place_reextracts(self):
        with tempfile.TemporaryDirectory() as d:
            covers = Path(d)
            img = covers / "cover.jpg"
            img.write_bytes(b"ONE")
            extracted = []
            w = self._worker(extracted)
            desired = Desired(CoverTarget("", covers), "dark")
            r1 = w._run_once((1, desired))
            img.write_bytes(b"TWO-LONGER")     # same path, new size+mtime
            os.utime(img, ns=(1, 1))           # force a distinct mtime_ns too
            r2 = w._run_once((2, desired))
        self.assertEqual(r1.outcome, "committed")
        self.assertEqual(r2.outcome, "committed")   # NOT skipped_duplicate
        self.assertEqual(len(extracted), 2)         # re-extracted

    def test_unchanged_file_is_skipped_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            covers = Path(d)
            (covers / "cover.jpg").write_bytes(b"ONE")
            extracted = []
            w = self._worker(extracted)
            desired = Desired(CoverTarget("", covers), "dark")
            r1 = w._run_once((1, desired))
            r2 = w._run_once((2, desired))      # nothing changed on disk
        self.assertEqual(r1.outcome, "committed")
        self.assertEqual(r2.outcome, "skipped_duplicate")
        self.assertEqual(len(extracted), 1)


class _FakeMailbox:
    """Returns queued items then None, to drive the pump loop without a thread."""

    def __init__(self, items):
        self._items = list(items)

    def get(self, stop):
        return self._items.pop(0) if self._items else None

    def superseded(self, gen):
        return False


class WorkerRunLoopTest(unittest.TestCase):
    def test_run_serves_each_item_until_mailbox_drains(self):
        import threading
        reported = []
        w, _ = _worker(report=reported.append)
        w._mailbox = _FakeMailbox([
            (1, Desired(CoverTarget("http://a", None), "dark")),
            (2, Desired(CoverTarget("http://b", None), "dark")),
        ])
        w._run(threading.Event())
        self.assertEqual([r.gen for r in reported], [1, 2])


class WorkerRunOnceRevertTest(unittest.TestCase):
    def test_revert_job_calls_revert_and_commits_with_none_cover(self):
        reverted, applied = [], []
        w, _ = _worker(
            revert=lambda: reverted.append(True),
            apply=lambda *c: applied.append(c),
        )
        result = w._run_once((5, Desired(target=None, mode="dark")))
        self.assertEqual(result.outcome, "committed")
        self.assertIsNone(result.cover_id)
        self.assertEqual(reverted, [True])
        self.assertEqual(applied, [])

    def test_revert_ctl_error_is_failed_retryable(self):
        w, _ = _worker(revert=lambda: (_ for _ in ()).throw(CtlError("ctl down")))
        result = w._run_once((5, Desired(target=None, mode="dark")))
        self.assertEqual(result.outcome, "failed_retryable")


class WorkerCommitDedupTest(unittest.TestCase):
    def _apply(self, gen):
        return (gen, Desired(target=CoverTarget("http://x", None), mode="dark"))

    def test_identical_apply_second_time_is_skipped_duplicate(self):
        # Layer-2 guard: once (resolved_path, mode) is committed, a resubmit
        # resolving to the same pair skips extraction and ctl.
        extracted, applied = [], []
        w, _ = _worker(
            extract=lambda p, m: extracted.append((p, m)) or ("#1", "#2", "#3"),
            apply=lambda *c: applied.append(c),
        )
        r1 = w._run_once(self._apply(5))
        r2 = w._run_once(self._apply(6))
        self.assertEqual(r1.outcome, "committed")
        self.assertEqual(r2.outcome, "skipped_duplicate")
        self.assertEqual(len(extracted), 1)
        self.assertEqual(len(applied), 1)

    def test_different_mode_reextracts(self):
        # A theme flip on the same cover is NOT a duplicate: (content_id, mode) differs.
        applied = []
        w, _ = _worker(apply=lambda *c: applied.append(c))
        w._run_once((5, Desired(CoverTarget("http://x", None), "dark")))
        r2 = w._run_once((6, Desired(CoverTarget("http://x", None), "light")))
        self.assertEqual(r2.outcome, "committed")
        self.assertEqual(len(applied), 2)

    def test_same_path_new_content_id_reextracts(self):
        # SEC-018 identity: a file overwritten in place (same path, new mtime →
        # new content_id) is NOT a duplicate — it re-extracts.
        applied = []
        ids = iter([(10, 100), (10, 200)])  # same size, new mtime_ns
        w, _ = _worker(
            resolve=lambda a, c: _ready("/covers/a.jpg", next(ids)),
            apply=lambda *c: applied.append(c),
        )
        r1 = w._run_once(self._apply(5))
        r2 = w._run_once(self._apply(6))
        self.assertEqual(r1.outcome, "committed")
        self.assertEqual(r2.outcome, "committed")  # not skipped_duplicate
        self.assertEqual(len(applied), 2)

    def test_repeat_revert_is_skipped_duplicate(self):
        # last_committed covers the None/revert case: a second revert to the
        # same preset skips ctl (design §2.3), so a failure-reset revert storm
        # cannot re-fire ctl.
        reverted = []
        w, _ = _worker(revert=lambda: reverted.append(True))
        r1 = w._run_once((5, Desired(target=None, mode="dark")))
        r2 = w._run_once((6, Desired(target=None, mode="dark")))
        self.assertEqual(r1.outcome, "committed")
        self.assertEqual(r2.outcome, "skipped_duplicate")
        self.assertEqual(len(reverted), 1)

    def test_initial_committed_seed_skips_matching_first_revert(self):
        # Production seeds (None, startup_mode) to mirror the inline startup
        # revert (§5.2): the first matching revert job is a no-op skip.
        reverted = []
        w, _ = _worker(revert=lambda: reverted.append(True),
                       initial_committed=(None, "dark"))
        result = w._run_once((5, Desired(target=None, mode="dark")))
        self.assertEqual(result.outcome, "skipped_duplicate")
        self.assertEqual(reverted, [])


if __name__ == "__main__":
    unittest.main()
