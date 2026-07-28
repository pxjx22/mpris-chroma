# Palette Memo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a theme flip on an unchanged cover skip the ~6.2 ms decode by memoizing the mode-free half of extraction in a single slot.

**Architecture:** `colors.py` splits at the existing `_select` seam into `select_palette` (content-derived, mode-free) and `render_palette` (mode-derived). A `PaletteMemo` callable holds one slot keyed on the `content_id` that `cover.py` already derives for SEC-018, and is injected into the `Worker` in place of `extract_colors`. `extract_colors` is retained as the uncached composition so its ~30 existing call sites are untouched and it stays the reference the memo is checked against.

**Tech Stack:** Python 3.13, stdlib `unittest`, Pillow. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-28-palette-memo-design.md` (committed as `72fd447`).

## Global Constraints

- CI baseline, both must pass: `python -m unittest discover -s tests` and `python -m compileall -q mpris_chroma`.
- The opt-in suites must also pass, because two of them inject `extract=`: `MPRIS_CHROMA_INTEGRATION=1 python -m unittest discover -s tests`.
- Baseline at `eeabf4e`: 304 tests, OK, 4 skipped.
- Commit convention: conventional, **no scope**. Trailer `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Never `git add`** the untracked working files in the repo root: `SECURITY_AUDIT.md`, `EFFICIENCY_AUDIT.md`, `4b-design.md`, `4b-kimi-k3-verdict.md`, `4c-design.md`, `4d-design.md`, `4d-runbook.md`, `4d-harness-*.py`. Stage explicit paths, never `git add -A`.
- Do not edit `SECURITY_AUDIT.md`. PERF-002 is closed; this is not an audit remediation.
- `colors.py` must keep **zero module-level mutable state**. The memo's state is instance state.
- No timing assertions in the suite — assert call counts instead.

## Two deviations from the spec, decided while writing this plan

1. **`render_palette` takes a `label` keyword.** The existing unseparable-palette debug log at `colors.py:170-171` interpolates `image_path.name`, which `render_palette` does not have. Dropping the filename would silently degrade a diagnostic. Signature is therefore `render_palette(picks, n_distinct, mode, *, label="?")`; both `extract_colors` and `PaletteMemo` pass `image_path.name`.
2. **The corpus equivalence test lives in `tests/test_colors.py`, not `tests/test_palette_memo.py`.** The `_corpus()` helper and the presence-gate live in `test_colors.py`, and `unittest discover -s tests` imports test modules as top-level names, so a cross-import from the new file would break under `python -m unittest tests.test_palette_memo`. Reusing the helper in place is cheaper than duplicating it.

---

### Task 1: Split `colors.py` into `select_palette` and `render_palette`

**Files:**
- Modify: `mpris_chroma/colors.py:146-173`
- Test: `tests/test_colors.py`

**Interfaces:**
- Consumes: existing `_histogram`, `_select`, `tone`, `separate`, `_log`.
- Produces:
  - `select_palette(image_path: Path) -> tuple[list[tuple[float, float, float]], int]` — returns `([], 0)` when the cover yields nothing.
  - `render_palette(picks, n_distinct: int, mode: str, *, label: str = "?") -> tuple[str, str, str]`
  - `extract_colors(image_path: Path, mode: str = "dark") -> tuple[str, str, str]` — unchanged signature and behaviour.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_colors.py`, after the `ModeIndependenceTest` class:

```python
class SplitPipelineTest(unittest.TestCase):
    """The two halves of extraction, addressable separately so a caller can
    cache the mode-free one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_select_palette_is_mode_free_and_returns_picks_with_count(self):
        img = self.tmp / "s.png"
        _thirds(img, "#d12b2b", "#2b7fd1", "#e0d020")
        picks, n = colors.select_palette(img)
        self.assertEqual(n, 3)
        self.assertEqual(len(picks), 3)
        self.assertNotIn("mode", inspect.signature(colors.select_palette).parameters)

    def test_select_palette_reports_empty_for_an_unreadable_cover(self):
        p = self.tmp / "not.png"
        p.write_bytes(b"this is not an image")
        self.assertEqual(colors.select_palette(p), ([], 0))

    def test_render_palette_maps_the_empty_selection_to_the_default(self):
        self.assertEqual(colors.render_palette([], 0, "dark"),
                         (DEFAULT_ACCENT,) * 3)

    def test_render_palette_composes_back_into_extract_colors(self):
        img = self.tmp / "c.png"
        _thirds(img, "#d12b2b", "#2b7fd1", "#e0d020")
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                picks, n = colors.select_palette(img)
                self.assertEqual(colors.render_palette(picks, n, mode),
                                 extract_colors(img, mode))
```

Add `import inspect` to the imports at the top of `tests/test_colors.py` (the file currently imports it locally inside `test_selection_takes_no_mode_at_all`; hoisting it is fine, leave that local import alone to keep the diff small — just add the top-level one).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_colors.SplitPipelineTest -v`
Expected: FAIL with `AttributeError: module 'mpris_chroma.colors' has no attribute 'select_palette'`

- [ ] **Step 3: Write the implementation**

In `mpris_chroma/colors.py`, replace the body of `extract_colors` (lines 146-173) with three functions:

```python
def select_palette(image_path: Path
                   ) -> tuple[list[tuple[float, float, float]], int]:
    """Content-derived half of extraction: decode, quantize, rank, pick.

    Mode-free by construction — the picks depend only on the cover's pixels, so
    a caller may reuse this result across theme changes (see PaletteMemo).
    Returns ([], 0) for a cover that yields no extractable colors (rejected
    format, undecodable data, or a genuine no-color image); render_palette maps
    that to the default accent.
    """
    hist = _histogram(image_path)
    if not hist:
        # Log here rather than at the caller so a memoized failure is reported
        # once per cover instead of once per theme flip.
        _log.warning("no extractable colors from %s; using default palette",
                     image_path.name)
        return [], 0
    return _select(hist)


def render_palette(picks: list[tuple[float, float, float]], n_distinct: int,
                   mode: str, *, label: str = "?") -> tuple[str, str, str]:
    """Mode-derived half: tone into the mode's Oklab envelope, then separate.

    `label` names the cover in the diagnostic below only; it has no effect on
    the palette. It exists because this half no longer holds the path.
    """
    if not picks:
        return "#a48ec7", "#a48ec7", "#a48ec7"
    toned = tone(picks, mode)
    slots, result = separate(toned, mode, n_distinct)
    if not result.resolved and result.reason != "duplicates":
        # An unseparable palette is an accepted outcome, but never a silent one.
        _log.debug("palette for %s left a pair at dE %.3f (%s)",
                   label, result.residual_de, result.reason)
    c1, c2, c3 = (slot.to_hex() for slot in slots)
    return c1, c2, c3


def extract_colors(image_path: Path, mode: str = "dark") -> tuple[str, str, str]:
    """Extract the three most prominent, visibly distinct colors from an image.

    Faithful to the cover: colors are ranked by coverage plus a vibrancy bonus,
    then toned into the mode's Oklab lightness envelope — compressed relative to
    other covers, but keeping this cover's own spread — and finally pushed apart
    if two of them collide perceptually. No hues are ever invented; the three
    slots are real cover colors. If the cover has fewer than three distinct
    colors, the last one is repeated rather than fabricated.

    The uncached composition of select_palette and render_palette. Kept as the
    reference implementation: PaletteMemo must agree with it exactly.
    """
    picks, n_distinct = select_palette(image_path)
    return render_palette(picks, n_distinct, mode, label=image_path.name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_colors -v 2>&1 | tail -5`
Expected: PASS, and the whole `test_colors` module still green (the corpus test runs here — the corpus is present on this machine).

- [ ] **Step 5: Run the full baseline**

Run: `python -m unittest discover -s tests 2>&1 | tail -4`
Expected: 308 tests, OK, 4 skipped. This is a pure refactor — no existing test may change.

- [ ] **Step 6: Commit**

```bash
git add mpris_chroma/colors.py tests/test_colors.py
git commit -m "refactor: split extract_colors into select and render halves

The mode-free half (decode, quantize, rank, pick) and the mode-derived half
(tone, separate) become separately callable, so a caller can reuse the former
across a theme flip. extract_colors is retained as the uncached composition:
its signature and output are unchanged, its ~30 call sites and palette_lab.py
are untouched, and it stays the reference any cache is checked against.

render_palette takes a label keyword because it no longer holds the path and
the unseparable-palette debug line names the cover.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `PaletteMemo` — hit, miss, and identity semantics

**Files:**
- Modify: `mpris_chroma/colors.py` (append after `extract_colors`)
- Create: `tests/test_palette_memo.py`

**Interfaces:**
- Consumes: `select_palette`, `render_palette` from Task 1.
- Produces: `PaletteMemo(select=select_palette, render=render_palette)` with `__call__(image_path: Path, mode: str, content_id: tuple[int, int]) -> tuple[str, str, str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_palette_memo.py`:

```python
import unittest
from pathlib import Path

from mpris_chroma.colors import PaletteMemo


def _memo(select=None, render=None):
    """A memo wired with inert fakes. Both halves are injected so these tests
    exercise the slot, not the extraction pipeline (Task 1 covers that)."""
    return PaletteMemo(
        select=select or (lambda p: ([(0.5, 0.1, 1.0)], 1)),
        render=render or (lambda picks, n, mode, label="?": (mode, mode, mode)),
    )


class PaletteMemoHitTest(unittest.TestCase):
    def test_same_content_id_selects_once_across_two_modes(self):
        # The whole point: a theme flip misses the worker's (content_id, mode)
        # dedup but must not re-run selection.
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([(0.5, 0.1, 1.0)], 1))
        cid = (10, 100)
        self.assertEqual(memo(Path("/c/a.jpg"), "dark", cid), ("dark",) * 3)
        self.assertEqual(memo(Path("/c/a.jpg"), "light", cid), ("light",) * 3)
        self.assertEqual(len(calls), 1)

    def test_mode_is_applied_after_the_hit_not_stored(self):
        # If mode leaked into the slot, the second call would return "dark".
        memo = _memo()
        cid = (10, 100)
        memo(Path("/c/a.jpg"), "dark", cid)
        self.assertEqual(memo(Path("/c/a.jpg"), "light", cid), ("light",) * 3)


class PaletteMemoMissTest(unittest.TestCase):
    def test_new_content_id_reselects(self):
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([(0.5, 0.1, 1.0)], 1))
        memo(Path("/c/a.jpg"), "dark", (10, 100))
        memo(Path("/c/b.jpg"), "dark", (20, 200))
        self.assertEqual(len(calls), 2)

    def test_same_path_with_new_content_misses(self):
        # SEC-018 identity: an in-place overwrite changes (size, mtime_ns), so
        # the same pathname must NOT serve a stale palette.
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([(0.5, 0.1, 1.0)], 1))
        memo(Path("/c/a.jpg"), "dark", (10, 100))
        memo(Path("/c/a.jpg"), "dark", (10, 999))
        self.assertEqual(len(calls), 2)

    def test_identity_is_the_key_not_the_path(self):
        # Documented consequence, not an accident: the key is content identity,
        # exactly as worker.py's dedup treats it. Two paths that stat identical
        # are the same cover to this memo. Inherited from SEC-018, not new.
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([(0.5, 0.1, 1.0)], 1))
        memo(Path("/c/a.jpg"), "dark", (10, 100))
        memo(Path("/c/b.jpg"), "dark", (10, 100))
        self.assertEqual(len(calls), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_palette_memo -v`
Expected: FAIL with `ImportError: cannot import name 'PaletteMemo' from 'mpris_chroma.colors'`

- [ ] **Step 3: Write the implementation**

Append to `mpris_chroma/colors.py`:

```python
class PaletteMemo:
    """One-slot memo over the mode-free half of extraction.

    A theme flip re-runs the pipeline for an unchanged cover: the worker's dedup
    key is (content_id, mode), so a mode change misses it and reaches extract.
    But only `mode` changed, and select_palette does not depend on mode, so its
    result is reusable — turning a ~6.2 ms decode into a ~0.01 ms retone.

    One slot, because the access pattern a flip produces is exactly
    (cover X, dark) -> (cover X, light). No eviction policy, no size bound, no
    TTL — none of which can then be got wrong.

    Keyed on the content_id cover.py derives for SEC-018, so an in-place
    overwrite of a cover changes the key and misses. Not keyed on the path.

    Not thread-safe, and does not need to be: `extract` is called only from the
    single worker thread (worker.py `_serve`).
    """

    def __init__(self, select=select_palette, render=render_palette):
        self._select = select
        self._render = render
        self._key: tuple[int, int] | None = None
        self._value: tuple[list[tuple[float, float, float]], int] | None = None

    def __call__(self, image_path: Path, mode: str,
                 content_id: tuple[int, int]) -> tuple[str, str, str]:
        if content_id != self._key:
            # Value first, key second, and it matters. If _select raises, the
            # old key and old value stay consistent with each other. Setting
            # the key first would leave it pointing at a value that was never
            # computed, and every later flip on this cover would silently serve
            # the *previous* cover's palette.
            self._value = self._select(image_path)
            self._key = content_id
        picks, n_distinct = self._value
        return self._render(picks, n_distinct, mode, label=image_path.name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_palette_memo -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add mpris_chroma/colors.py tests/test_palette_memo.py
git commit -m "feat: add a one-slot palette memo keyed on content identity

A theme flip misses the worker's (content_id, mode) dedup and reaches extract,
but only the mode changed and select_palette does not depend on it. One slot
covers the access pattern a flip actually produces -- (cover X, dark) then
(cover X, light) -- so there is no eviction policy or size bound to get wrong.

Keyed on the content_id cover.py already derives for SEC-018 rather than a
second identity of its own, so an in-place overwrite misses. Not keyed on the
path, matching how worker.py's dedup already treats identity.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `PaletteMemo` — cached failures and exception safety

**Files:**
- Modify: `tests/test_palette_memo.py`

**Interfaces:**
- Consumes: `PaletteMemo` from Task 2, `render_palette` from Task 1.
- Produces: nothing new. This task adds only tests — the implementation from Task 2 should already satisfy them. **If either test passes on the first run, stop and check why**; the exception test in particular is written to fail against the wrong assignment order.

- [ ] **Step 1: Write the failing tests**

Extend the existing import at the top of `tests/test_palette_memo.py` to
`from mpris_chroma.colors import PaletteMemo, render_palette` — do not add a
second import statement mid-file — then append:

```python
DEFAULT_ACCENT = "#a48ec7"


def _counting_select(calls, boom):
    """A select fake that records every call and can be armed to raise.

    Shared by both exception tests: they differ in what they assert after the
    failure, not in how the failure is produced.
    """
    def select(p):
        calls.append(p)
        if boom["on"]:
            raise MemoryError("decode blew up")
        return ([(0.5, 0.1, 1.0)], len(calls))
    return select


class PaletteMemoFailureTest(unittest.TestCase):
    def test_an_unextractable_cover_is_cached_as_the_default(self):
        # Caching the failure is deliberate: it turns the "no extractable
        # colors" warning from once-per-flip into once-per-cover. Safe because
        # a rewritten file changes content_id and misses.
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([], 0),
                     render=render_palette)
        cid = (10, 100)
        self.assertEqual(memo(Path("/c/bad.jpg"), "dark", cid),
                         (DEFAULT_ACCENT,) * 3)
        self.assertEqual(memo(Path("/c/bad.jpg"), "light", cid),
                         (DEFAULT_ACCENT,) * 3)
        self.assertEqual(len(calls), 1)


class PaletteMemoExceptionSafetyTest(unittest.TestCase):
    def test_a_raising_select_does_not_poison_the_slot(self):
        # The failure mode this memo most needs guarding: if the key were
        # assigned before the value, a select that raises would leave the slot
        # claiming the NEW key while holding the OLD picks, and the retry below
        # would be served the previous cover's palette without re-selecting.
        calls, boom = [], {"on": False}
        memo = _memo(select=_counting_select(calls, boom),
                     render=lambda picks, n, mode, label="?": (str(n),) * 3)
        memo(Path("/a.jpg"), "dark", (10, 100))           # slot holds A
        boom["on"] = True
        with self.assertRaises(MemoryError):
            memo(Path("/b.jpg"), "dark", (20, 200))       # raises mid-update
        boom["on"] = False
        memo(Path("/b.jpg"), "dark", (20, 200))           # must RE-select B

        # Three calls: A, the failed B, the successful B. With key-before-value
        # ordering this is 2 -- the last call hits a slot that wrongly claims
        # (20, 200) and returns A's palette.
        self.assertEqual(len(calls), 3)

    def test_the_slot_still_serves_the_old_cover_after_a_failure(self):
        calls, boom = [], {"on": False}
        memo = _memo(select=_counting_select(calls, boom),
                     render=lambda picks, n, mode, label="?": (str(n),) * 3)
        first = memo(Path("/a.jpg"), "dark", (10, 100))
        boom["on"] = True
        with self.assertRaises(MemoryError):
            memo(Path("/b.jpg"), "dark", (20, 200))
        # A's key was never overwritten, so this is still a hit: 2 calls, not 3.
        self.assertEqual(memo(Path("/a.jpg"), "light", (10, 100)), first)
        self.assertEqual(len(calls), 2)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m unittest tests.test_palette_memo -v`
Expected: PASS, 8 tests.

- [ ] **Step 3: Prove the exception tests are load-bearing**

Temporarily swap the two lines in `PaletteMemo.__call__` so the key is assigned first:

```python
            self._key = content_id
            self._value = self._select(image_path)
```

Run: `python -m unittest tests.test_palette_memo -v`
Expected: **FAIL** — `test_a_raising_select_does_not_poison_the_slot` (2 != 3) and `test_the_slot_still_serves_the_old_cover_after_a_failure`.

Then restore the correct order and re-run. Expected: PASS, 8 tests.

This step is mandatory. A test that guards an ordering constraint passes trivially against correct code and would otherwise prove nothing — the same mutation check PERF-001's pins required.

- [ ] **Step 4: Commit**

```bash
git add tests/test_palette_memo.py
git commit -m "test: pin the memo's cached-failure and exception-safety behaviour

An unextractable cover occupies the slot, so its warning fires once per cover
rather than once per theme flip; a rewritten file changes content_id and
misses, so the cached failure cannot outlive the file that caused it.

The exception tests guard the assignment order in __call__: value before key,
so a raising select leaves the pair consistent. Verified load-bearing by
swapping the two lines -- both fail with the key assigned first, where the
slot would claim the new key while holding the previous cover's picks.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Wire the memo into the worker and `sync.main()`

**Files:**
- Modify: `mpris_chroma/worker.py:196`
- Modify: `mpris_chroma/sync.py:10`, `mpris_chroma/sync.py:218`
- Modify: `tests/test_worker.py` (lines 22, 50, 84, 117-122, 147, 161, 178, 268)
- Modify: `tests/test_heartbeat_integration.py:42`
- Modify: `tests/test_worker_integration.py:24`

**Interfaces:**
- Consumes: `PaletteMemo` from Task 2.
- Produces: the injected `extract` callable's contract becomes `(path, mode, content_id) -> (c1, c2, c3)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_worker.py`, in `WorkerRunOnceApplyTest`:

```python
    def test_extract_receives_the_resolved_content_id(self):
        # The memo keys on content identity, so the worker must hand over the
        # identity it already resolved rather than let extract derive a second.
        seen = []
        w, _ = _worker(
            resolve=lambda a, c: _ready(content_id=(42, 4242)),
            extract=lambda p, m, cid: seen.append(cid) or ("#1", "#2", "#3"))
        target = CoverTarget(art_url="http://x", covers_dir=None)
        w._run_once((5, Desired(target=target, mode="dark")))
        self.assertEqual(seen, [(42, 4242)])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_worker.WorkerRunOnceApplyTest.test_extract_receives_the_resolved_content_id -v`
Expected: FAIL with `TypeError: <lambda>() missing 1 required positional argument: 'cid'`

- [ ] **Step 3: Widen the worker call site**

In `mpris_chroma/worker.py`, line 196:

```python
        c1, c2, c3 = self._extract(resolution.path, desired.mode,
                                   resolution.content_id)
```

- [ ] **Step 4: Update every injected lambda to the three-argument contract**

`tests/test_worker.py` — eight sites. The fixture default at line 22:

```python
        extract=lambda path, mode, content_id: ("#aa0000", "#00bb00", "#0000cc"),
```

Lines 50, 268 (they record what they were called with — keep recording only path and mode):

```python
            extract=lambda p, m, cid: extracted.append((p, m)) or ("#1", "#2", "#3"),
```

Lines 84, 147:

```python
            extract=lambda p, m, cid: ("#1", "#2", "#3"),
```

Line 161:

```python
            extract=lambda p, m, cid: (_ for _ in ()).throw(RuntimeError("bug")),
```

Line 178:

```python
            extract=lambda p, m, cid: extracted.append(p) or ("#1", "#2", "#3"),
```

The named function around line 117 (`extract_then_supersede`) gains a third parameter:

```python
        def extract_then_supersede(path, mode, content_id):
```

`tests/test_heartbeat_integration.py:42` and `tests/test_worker_integration.py:24`:

```python
            extract=lambda p, m, cid: ("#1", "#2", "#3"),
```

- [ ] **Step 5: Run the worker tests**

Run: `python -m unittest tests.test_worker -v 2>&1 | tail -4`
Expected: PASS, all green.

- [ ] **Step 6: Inject the memo in production**

In `mpris_chroma/sync.py`, line 10:

```python
from .colors import PaletteMemo
```

and line 218:

```python
        extract=PaletteMemo(),
```

Check whether `extract_colors` is still referenced anywhere in `sync.py`; if not, it must not remain in the import.

- [ ] **Step 7: Run the full baseline, both suites**

Run: `python -m unittest discover -s tests 2>&1 | tail -4`
Expected: 317 tests, OK, 4 skipped.

Run: `MPRIS_CHROMA_INTEGRATION=1 python -m unittest discover -s tests 2>&1 | tail -4`
Expected: OK, 0 skipped. This run is mandatory — two of the files edited in Step 4 are only exercised here.

Run: `python -m compileall -q mpris_chroma`
Expected: silent.

- [ ] **Step 8: Commit**

```bash
git add mpris_chroma/worker.py mpris_chroma/sync.py tests/test_worker.py \
        tests/test_heartbeat_integration.py tests/test_worker_integration.py
git commit -m "feat: memoize extraction across theme flips

The worker now hands extract the content_id it already resolved, and sync
injects PaletteMemo in place of extract_colors. A theme flip on an unchanged
cover drops from ~6.85 ms to ~0.65 ms -- essentially just the wlchroma-ctl
round trip -- because the ~6.2 ms decode is served from the slot.

Passing the resolved identity rather than letting extract stat the file again
keeps SEC-018's content identity the single source of truth.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Corpus equivalence and final verification

**Files:**
- Modify: `tests/test_colors.py` (inside the existing `CorpusTest`, or as a sibling presence-gated class)

**Interfaces:**
- Consumes: `PaletteMemo`, `extract_colors`, and the existing `_corpus()` helper and its `skipUnless` gate.
- Produces: nothing. Terminal verification task.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_colors.py`, after `CorpusTest`:

```python
@unittest.skipUnless(len(_corpus()) >= 20,
                     "real cover corpus not present (opt-in)")
class MemoEquivalenceTest(unittest.TestCase):
    """The memo must be a pure optimization: same output as the uncached
    composition, on every real cover, in both modes. This is what makes
    extract_colors worth keeping as the reference."""

    def test_memo_matches_uncached_extract_over_the_corpus(self):
        for p in _corpus():
            st = p.stat()
            cid = (st.st_size, st.st_mtime_ns)
            memo = colors.PaletteMemo()
            for mode in ("dark", "light"):
                # Same memo across both modes, so the second call is a hit --
                # a hit that returned different colors is the bug this catches.
                with self.subTest(cover=p.name, mode=mode):
                    self.assertEqual(memo(p, mode, cid),
                                     extract_colors(p, mode))
```

- [ ] **Step 2: Run it**

Run: `python -m unittest tests.test_colors.MemoEquivalenceTest -v`
Expected: PASS. If it fails, the split in Task 1 changed behaviour and must be fixed before proceeding — do not adjust this test to accommodate it.

- [ ] **Step 3: Re-run the stage benchmark**

Run:

```bash
python - <<'EOF'
import time
from pathlib import Path
from mpris_chroma.colors import PaletteMemo, extract_colors

covers = sorted(Path.home().joinpath(".local/share/jellyfin-tui/covers").iterdir())
p = [c for c in covers if c.is_file()][0]
st = p.stat(); cid = (st.st_size, st.st_mtime_ns)

memo = PaletteMemo()
memo(p, "dark", cid)                      # prime the slot
t = []
for _ in range(5):
    s = time.perf_counter(); memo(p, "light", cid); t.append((time.perf_counter()-s)*1000)
print("flip on a hit: %.3f ms" % min(t))
t = []
for _ in range(5):
    s = time.perf_counter(); extract_colors(p, "light"); t.append((time.perf_counter()-s)*1000)
print("flip uncached: %.3f ms" % min(t))
EOF
```

Expected: the hit is ~0.01 ms against ~6 ms uncached. Record both numbers in the commit message.

- [ ] **Step 4: Full verification**

Run: `python -m unittest discover -s tests 2>&1 | tail -4`
Expected: 318 tests, OK, 4 skipped.

Run: `MPRIS_CHROMA_INTEGRATION=1 python -m unittest discover -s tests 2>&1 | tail -4`
Expected: OK, 0 skipped.

Run: `python -m compileall -q mpris_chroma`
Expected: silent.

Run: `git status --short`
Expected: the untracked audit files listed in Global Constraints, and nothing else uncommitted.

- [ ] **Step 5: Commit**

```bash
git add tests/test_colors.py
git commit -m "test: pin memo/uncached equivalence over the real cover corpus

Asserts PaletteMemo returns exactly what extract_colors does for every cover
in the corpus, in both modes, reusing one memo per cover so the second call is
a hit. A hit that returns different colors than a fresh extract is the one
failure that would make this optimization user-visible, and nothing else in
the suite would catch it.

Measured on a hit: <FILL IN> ms against <FILL IN> ms uncached.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

Replace both `<FILL IN>` values with the numbers measured in Step 3.

---

## Not in scope

- `_histogram` itself — still ~6.2 ms on a miss.
- The covers-dir scan at `cover.py:384` — PERF-003's territory.
- Any change to `extract_colors`' signature or behaviour.
- Any edit to `SECURITY_AUDIT.md`.
- README parity: no user-visible config, CLI, or IPC behaviour changes, so no README update is required.
