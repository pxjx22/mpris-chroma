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

Unit tests port the Python tests case for case. `tests/color_golden.rs` also
checks exact values against fixtures recorded from the Python code:

```bash
# from the repo root, after changing any ported Python module
python tools/dump_golden.py > rust/tests/fixtures/color_golden.json
```

Hex output, separation reasons and flags must match exactly. Floats must match
to 1e-10, which absorbs ulp-level libm differences between CPython and Rust.

Two rounding rules are easy to get wrong:

- `oklab::lch_to_hex` rounds ties to even, like Python's `round()`.
- `ramp::expand` rounds ties away from zero, like Zig's `@round` in wlchroma.

## Status

| Step | Python | Rust | State |
|---|---|---|---|
| 1 | `oklab.py`, `tone.py`, `ramp.py`, `colors.render_palette` | `color::{oklab, tone, ramp}`, `color::render_palette` | done, golden-checked |
| 2 | `colors.py` decode / quantize / select, `PaletteMemo` | `color::{decode, quantize}`, `color::select_palette` | next |
| 3 | `framing.py`, `state.py`, `select.py` | `framing`, `state`, `select` | `state::Mode` only |
| 4 | `coordinator.py` | `coordinator` | |
| 5 | `worker.py` | `worker` | |
| 6 | `apply.py` | `apply` | |
| 7 | `cover.py` | `cover::{fetch, cache, local}` | |
| 8 | `sync.py` | `sources::{playerctl, dbus, signals}`, `runtime`, `main.rs` | |
| 9 | integration tests | `tests/*.rs` against `tools/fake_mpris.py` | |

Step 2 is where parity gets hard. Pillow's `quantize()` (median cut), `draft()`
and resize filter have no bit-exact Rust equivalent, so the plan is to write a
median cut in `color/quantize.rs` and pin it with golden fixtures from real
covers.

## Deliberate differences from the Python

- `oklab::max_chroma` has no memo. The `lru_cache` exists for CPython's speed;
  compiled bisection costs well under a microsecond.
- String states become enums (`Mode`, `SeparationReason`, and later the cover
  and job outcomes).
- `tone::separate` panics on `n_distinct > slots.len()`, where Python raises
  `ValueError`. Either way it is a caller bug.
