# Palette memo design — skip re-decoding an unchanged cover on a theme flip

**Problem:** a theme flip on an unchanged cover re-runs the full extraction
pipeline. Measured over 43 real jellyfin-tui covers (min-of-5, on `eeabf4e`):
`_histogram` 6.21 ms median / 9.59 ms max, `_select` 0.02 ms, `tone` + `separate`
0.01 ms, full `extract_colors` 6.15 ms. End to end a flip costs ~0.01 ms resolve
(disk cache-hit, no network) + 6.2 ms extract + 0.64 ms `wlchroma-ctl` round trip
≈ **6.85 ms**, of which the mode-independent prefix is ~91%.

**Workstream:** ordinary performance work. This is **not** an audit remediation.
PERF-002 was closed 2026-07-28 as dissolved by architecture, on pins that already
existed; its security content (externally-triggerable ImageMagick spawns) died in
Phase 2, and 4b moved the residual off the GLib main loop. The optimization below
is worth doing on its own merits, but it fixes nothing security-relevant and must
not be recorded as closing a finding. See `SECURITY_AUDIT.md` § PERF-002.

**Baseline:** `main` at `eeabf4e`, suite 304 (300 default + 4 opt-in skipped),
green. `python -m compileall -q mpris_chroma` clean.

**Precondition:** this design is only cheap because the palette-toning work
landed `_select` as a pure, mode-free seam returning `(picks, n_distinct)`. It
could not have been written against pre-toning `main`.

Design approved in conversation on the points below; this is the TDD contract.

---

## 1. What was verified before designing

Not assumed — checked against the code at `eeabf4e`:

- **Every `Ready` carries a `content_id`.** All four construction sites
  (`cover.py:330, 357, 376, 385`) call `_content_id()`, including the dir-scan
  fallback at 385. There is no resolved-cover path with a missing identity, so
  the memo key always exists and needs no None case.
- **`content_id` is `(st_size, st_mtime_ns)`** (`cover.py:74-78, 100-105`),
  chosen by SEC-018 so an in-place overwrite reads as a new cover. Keying the
  memo on it inherits that property exactly.
- **The worker call site already has it in scope.** `resolution.content_id` is
  bound at `worker.py:191`, five lines above `self._extract(resolution.path,
  desired.mode)` at `worker.py:196`.
- **A theme flip does reach extract.** The worker's dedup key is
  `(content_id, mode)` (`worker.py:191-193`), so a flip on an unchanged cover
  misses dedup and proceeds — which is the whole opportunity.
- **A theme flip does *not* re-download.** `cover.py:356-357` returns `Ready`
  from the disk cache before any network call. `resolve` on a flip is two stats.
- **`colors.py` has zero module-level mutable state today** — only constants and
  a `NullHandler` logger. Preserving that is a design constraint, not a nicety.
- **`sync.py:218` is the only production injection of `extract=`.**
- **Ten test lambdas inject `extract=`**: eight in `test_worker.py` (lines 22,
  50, 84, 122, 147, 161, 178, 268), plus `test_heartbeat_integration.py:42` and
  `test_worker_integration.py:24`. All positional two-arg.
- **`CorpusTest` is presence-gated, not env-gated** —
  `@skipUnless(len(_corpus()) >= 20, ...)` over `~/.local/share/jellyfin-tui/covers`
  and `~/.cache/mpris-chroma/covers`. New corpus-backed tests follow that
  convention, not `MPRIS_CHROMA_INTEGRATION`.

## 2. Why one slot

The access pattern a theme flip produces is exactly `(cover X, dark) →
(cover X, light)`. One slot covers 100% of flips.

A small LRU would additionally cover "switch tracks, then flip theme", but that
case is partly absorbed by the worker's own `(content_id, mode)` dedup, and it
buys an eviction policy, a size bound, and tests for both. Rejected as YAGNI —
and a bounded cache is precisely the kind of construct that later earns its own
audit finding. One slot has no eviction policy, no bound, and no TTL to get
wrong.

Memory cost: three LCh tuples and an int.

## 3. Architecture

`colors.py` splits at the existing seam and gains one stateful class.

| name | signature | owns |
| --- | --- | --- |
| `select_palette` | `(image_path) -> (picks, n_distinct)` | `_histogram → _select`; the "no extractable colors" warning |
| `render_palette` | `(picks, n_distinct, mode) -> (c1, c2, c3)` | `tone → separate → to_hex`; the unseparable-palette debug log; maps `([], 0)` to the default accent |
| `PaletteMemo` | `__call__(path, mode, content_id) -> (c1, c2, c3)` | the one slot |
| `extract_colors` | `(path, mode="dark") -> (c1, c2, c3)` | **unchanged**; composes the two, uncached |

`extract_colors` is retained deliberately. `palette_lab.py` and ~30 call sites
across `test_colors.py` use it; keeping it as the uncached composition means none
of them change, and it remains the honest reference the memo is checked against
in § 6.

Both the warning and the `#a48ec7` fallback currently live inside
`extract_colors`, so relocating them as above leaves its observable behaviour
byte-identical. `DEFAULT_ACCENT` stays a literal in `colors.py` (it is defined as
a named constant only in the test file today); moving it is out of scope.

## 4. Data flow

`sync.main()` injects `extract=PaletteMemo()` in place of `extract_colors`
(`sync.py:218`). `worker.py:196` gains a third argument:

```python
c1, c2, c3 = self._extract(resolution.path, desired.mode,
                           resolution.content_id)
```

Nothing else moves. On a flip the worker's dedup misses, the job proceeds, the
memo hits, and only `render_palette` runs: **~6.85 ms → ~0.65 ms**, essentially
just the ctl round trip.

The post-extract `superseded()` re-check at `worker.py:197-199` is unaffected. Its
comment ("extract is not free, so a newer desire may have arrived during it")
becomes conservative rather than wrong on a memo hit, and the check must stay —
it still guards the miss path, which is unchanged at ~6 ms.

## 5. Invariants

1. **Assignment order.** `self._value = select_palette(path)` executes *before*
   `self._key = content_id`. If `select_palette` raises — `MemoryError`, a Pillow
   bug, anything `_histogram` does not already catch — the old key and old value
   remain consistent with each other. Reversing the two lines leaves `_key`
   pointing at a value that was never computed, and every later flip on that
   cover silently serves the *previous* cover's palette. This is the failure mode
   this design most needs a test for.
2. **Single-threaded by construction.** Only the worker thread calls `extract`
   (`worker.py` `_serve`). No lock, with a comment stating why.
3. **Identity, never path.** The key is `content_id`. The same `Path` with new
   bytes must miss.
4. **Mode never reaches the slot.** It is applied strictly after the hit, which
   is what makes a one-slot cache correct at all.
5. **A "no extractable colors" result is cached.** `select_palette` returns
   `([], 0)` and that occupies the slot. Safe because a rewritten file changes
   `content_id` and misses. Accepted consequence: the warning fires once per
   cover instead of once per theme flip — a reduction in journal volume,
   consistent with the drop-log rate limiting elsewhere.

## 6. Testing

Strict TDD, each RED first. New `tests/test_palette_memo.py`:

- **hit** — same `content_id`, two modes → `select_palette` called **once**, two
  different triples returned.
- **miss** — different `content_id` → called **twice**.
- **identity not path** — same `Path`, different `content_id` → miss. Pins the
  SEC-018 property.
- **failure is cached** — empty histogram → default accent triple,
  `select_palette` called once across two flips, warning logged once.
- **exception safety** — `select_palette` raises → the exception propagates, the
  slot is unchanged, and a subsequent call on the old key still returns the old
  palette. This is the RED for invariant 1.
- **equivalence** — `PaletteMemo()(p, m, cid) == extract_colors(p, m)` across the
  real corpus, both modes. Pins that the split changed no output. Presence-gated
  via the existing `_corpus()` helper, per § 1.

`test_worker.py` gains one test that the worker passes `content_id` through. The
ten existing `extract=` lambdas gain a third parameter.

## 7. Verification

- `python -m unittest discover -s tests` — expected 304 + new, all green.
- `MPRIS_CHROMA_INTEGRATION=1 python -m unittest discover -s tests` — the two
  opt-in files inject `extract=`, so they must be run, not just the default set.
- `python -m compileall -q mpris_chroma` clean.
- Re-run the stage benchmark and confirm a flip drops to ctl-only cost.

Live verification is **not** required: this changes no Wayland, IPC, or renderer
path, and the observable palette output is pinned identical by the § 6
equivalence test.

## 8. Out of scope

- `_histogram` itself — still ~6.2 ms on a miss.
- The covers-dir scan (`cover.py:384`) — that is PERF-003's territory.
- Any change to `extract_colors`' signature or behaviour.
- Any edit to `SECURITY_AUDIT.md`. PERF-002 is closed; this work does not reopen
  or amend it.
