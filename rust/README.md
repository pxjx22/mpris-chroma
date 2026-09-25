# mpris-chroma (Rust port)

A module-by-module port of the Python daemon in `../mpris_chroma`. The Python
package stays the reference implementation, and keeps running as the daemon,
until this crate reaches parity. At that point the crate moves to the repo root
and the Python is removed in one commit.

```bash
cd rust
cargo test
```

## Parity

Unit tests port the Python tests case for case. Two golden suites also check
exact values against fixtures recorded from the Python code:

```bash
# from the repo root, after changing any ported Python module
python tools/dump_golden.py > rust/tests/fixtures/color_golden.json
python tools/dump_image_golden.py   # needs Pillow; writes images/, samples/, JSON
```

- `tests/color_golden.rs`: the color math over a seeded corpus. Hex output,
  separation reasons and flags must match exactly; floats to 1e-10, which
  absorbs ulp-level libm differences between CPython and Rust.
- `tests/image_golden.rs`: 18 synthetic covers (no album art in the repo),
  each built to reach a branch: gradients, near-collision tans, a small vivid
  accent, noise with thousands of colours, grey/palette/RGBA PNGs, upscaling,
  the very-tall two-step resize, JPEG draft scaling, lossy and lossless WebP,
  an animated WebP, a CMYK JPEG. On Pillow's own 100x100 sample, quantize,
  pick and render match exactly for all 18. For PNG and lossless WebP, decode
  and resize also reproduce Pillow's sample byte for byte, so the whole
  pipeline matches. JPEG and lossy WebP use different decoders; their
  palettes must stay within dE 0.03 of Pillow's (measured worst: 0.012).

Two rounding rules are easy to get wrong:

- `oklab::lch_to_hex` rounds ties to even, like Python's `round()`.
- `ramp::expand` rounds ties away from zero, like Zig's `@round` in wlchroma.

`color::quantize` and `color::resize` are ports of Pillow 12.3's C
(`libImaging/Quant.c`, `QuantHeap.c`, `Resample.c`), because no Rust crate
reproduces Pillow's median cut or fixed-point bicubic, and the picked colours
depend on every sample byte.

## Status

| Step | Python | Rust | State |
|---|---|---|---|
| 1 | `oklab.py`, `tone.py`, `ramp.py`, `colors.render_palette` | `color::{oklab, tone, ramp}`, `color::render_palette` | done, golden-checked |
| 2 | `colors.py` decode / quantize / select, `PaletteMemo` | `color::{decode, resize, quantize, pick}`, `color::{select_palette, extract_colors, PaletteMemo}` | done, golden-checked |
| 3 | `framing.py`, `state.py`, `select.py` | `framing`, `state`, `select` | done (`test_decide`'s `_follow_cmd` cases move with step 8) |
| 4 | `coordinator.py` | `coordinator` | done (all `test_coordinator` cases) |
| 5 | `worker.py` | `worker` | job/result types only (`Desired`, `JobResult`, `Outcome`) |
| 6 | `apply.py` | `apply` | |
| 7 | `cover.py` | `cover::{fetch, cache, local}` | |
| 8 | `sync.py` | `sources::{playerctl, dbus, signals}`, `runtime`, `main.rs` | |
| 9 | integration tests | `tests/*.rs` against `tools/fake_mpris.py` | |

## Deliberate differences from the Python

- `oklab::max_chroma` has no memo. The `lru_cache` exists for CPython's speed;
  compiled bisection costs well under a microsecond.
- String states become enums (`Mode`, `SeparationReason`, `PlaybackStatus`,
  and later the cover and job outcomes). `PlayerState.status` is a
  `PlaybackStatus`, so the MPRIS domain check (SEC-011 §2.3) happens at parse
  time rather than at the coordinator.
- `select::decide` returns a `Selection` enum (`Apply`/`Revert`/`Hold`)
  instead of the Python's tuple-or-`None`.
- The coordinator's injected callables (submit, schedule, cancel, jitter,
  clock) are one `coordinator::Host` trait. Warnings go through `Host::warn`,
  so the drop-log tests count them where the Python uses `assertLogs`. The
  retry timer calls `Coordinator::fire_retry()`, like the Python's bound
  `_fire_retry`. `gen` is a reserved word in Rust 2024, so it is `generation`.
- `fire_retry`'s "guard 2" (desire moved on between arm and fire) is not
  reachable through the public API, in either language: every change of
  desired value bumps the generation, which cancels the retry first. It is
  kept as defence in depth; no test covers it.
- `tone::separate` panics on `n_distinct > slots.len()`, where Python raises
  `ValueError`. Either way it is a caller bug.
- JPEG draft (libjpeg's scaled IDCT) is emulated by decoding at full size and
  block-averaging to the same draft size. 16-bit PNGs follow Pillow's raw
  modes (high byte for colour; 16-bit grey clips at 255, as Pillow's `I;16`
  conversion does).
- The quantizer does not port Pillow's reduced-precision rehash, which only
  triggers past 65536 distinct colours; the 100x100 sample has at most 10000.
- `PaletteMemo` is `get(path, mode, content_id)` over injectable halves; a
  panicking select leaves the slot untouched, as a raising one does in Python.
  The "logged once" part of the Python memo test is covered by counting
  select calls instead of capturing logs.
