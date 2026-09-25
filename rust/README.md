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
| 5 | `worker.py` | `worker` | done (`test_worker`, `test_mailbox`, `test_worker_integration`; the real-resolver identity cases move with step 7) |
| 6 | `apply.py` | `apply` | done (all `test_apply` cases, plus real-process runner tests) |
| 7 | `cover.py` | `cover::{policy, fetch, cache, local}` | done (all `test_cover` cases bar one, plus the two real-resolver worker cases) |
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
- The worker's four injected stages are a `worker::Stages` trait. Python's
  catch-all around a job becomes `catch_unwind`: a panic in a stage is
  reported as a retryable failure and the loop survives. `WorkerHandle`
  replaces the daemon thread: `stop_and_join` waits up to its timeout and
  then leaves a wedged thread detached, as the Python's daemon thread is.
  The real-thread lifecycle tests are opt-in in Python but run by default
  here (they take milliseconds).
- `apply` runs ctl through an injectable `Runner` (Python injects
  `subprocess.run`). `WLCHROMA_CTL` or the bare `wlchroma-ctl` (resolved on
  `PATH` at spawn) replaces `shutil.which`. On timeout the child is killed
  and reaped without waiting on its pipes, as `subprocess.run` does. One
  deliberate difference: if ctl exits but leaves a descendant holding
  stderr, Python's `communicate()` waits out the timeout and reports a
  timeout error, while the port reports ctl's real exit status (stderr is
  awaited only until the same deadline).

### Cover resolution

Security-relevant differences, each deliberate:

- **DNS pinning.** The Python checks a host's resolved addresses, then lets
  urllib resolve it again to connect, so a DNS-rebinding server can answer
  the check with a public address and the connect with 127.0.0.1. Here
  `policy::check_destination` returns the addresses it checked and the
  transport connects only to those (a custom ureq resolver), on every hop,
  redirects included. TLS is still verified against the URL's hostname.
- **Bounded DNS.** `getaddrinfo` has no timeout; the lookup runs on a helper
  thread and is abandoned after 5 s. The Python lookup is unbounded.
- **Stricter "global address".** A superset of Python's `ipaddress`
  refusals: every IANA special-purpose IPv4 block, and for IPv6 only global
  unicast 2000::/3 minus protocol-assignment, documentation and 6to4 blocks.
  IPv4-mapped and NAT64 addresses are refused outright.
- **No proxies.** ureq reads `*_proxy` from the environment by default,
  which would bypass pinning; it is disabled. (urllib honours those
  variables; the systemd unit sets none.)
- **Trust roots** are bundled Mozilla roots (webpki-roots) rather than the
  system store.
- **Stalled reads.** The body is still read in 64 KiB chunks under the byte
  cap and the 20 s total deadline, with the stop flag polled per chunk, but a
  single stalled read is bounded by the 20 s body timeout rather than
  urllib's 5 s per-socket-operation timeout.
- **URLs** are parsed per WHATWG (`url` crate), not `urlparse`: hosts are
  lowercased, `file://localhost/` normalizes to an empty host, dot segments
  are resolved before confinement (which still checks the canonical path).

Test-harness differences: the Python mocks `os.scandir`/`os.replace`; here
an unreadable covers dir is a covers "dir" that is a file, a failed publish
renames onto a non-empty directory, and the scan memo is proven by putting
the directory's mtime back after adding a newer cover. The Python's
"candidate vanishes mid-scan" case is not reproduced (it needs a mocked
`stat`); per-candidate errors are skipped structurally (`local::candidate`
returns `None` on any error). The Python module globals (`CACHE_DIR`, the
scan memo, the log limiter) are fields of `CoverResolver`.
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
