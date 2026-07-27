# Palette quality design — per-mode toning, gamut-aware chroma, perceptual distinctness

**Problem:** the dark-mode rendering of extracted palettes is consistently too
bright and washed out. Measured over the real cover corpus, dark mode produces a
median screen luminance of **0.215** against the **0.075** of the `witch_hour`
palette the user actually configured — ~2.9× too bright at the median and ~4.9×
at p90.

**Workstream:** extraction + presentation quality. This is *not* part of the
numbered async-pipeline audit sequence (4a–4d); the SEC-018 state-machine work is
merged and out of scope. Spec lives outside the root `<phase>-design.md` naming
to keep the two apart.

**Branch:** `quality/palette-toning`, cut from `main` at `4ac796d`.
**Baseline:** suite 224 (221 default + 3 opt-in skipped), green.

Design approved in conversation on the points below; this is the TDD contract.

---

## 1. Where the problem actually is

Verified before designing:

- **The bands are entirely daemon-side**, `mpris_chroma/colors.py` `BANDS`.
  `~/wlchroma` has no dark/light awareness whatsoever — it accepts three hexes
  and expands them in `src/render/palette.zig:buildPalette` into 12 cells that
  are alpha blends between consecutive pairs (c1→c2, c2→c3, c3→c1 at alpha
  1.00/0.72/0.50/0.28). The colormix fragment shader indexes that ramp by a warp
  field.
- **There is therefore no "background" slot.** All three colors are roughly
  equally present on screen, so perceived brightness is the mean over the 12-cell
  ramp — which is the metric this spec uses throughout, not a naive mean of three
  colors.
- The current `BANDS["dark"] = (0.45, 0.85)` is a **uniform clamp applied
  identically to all three slots**. Consequences, measured:
  - Nothing can ever be dark. `#120C14` (the anchor of the user's own configured
    palette) is unreachable by construction.
  - All three slots land in one narrow band, so palettes lack depth. Some are
    dead flat: cover `1141bbef` → `#733b5c #407466 #3f4873`, all at V = 0.45.
  - 34 of 150 slots sit pinned exactly at the V floor.
- **HSV `V` is not lightness.** `#d9d9d9` and `#0284c4` both sit at V ≈ 0.8 and
  read completely differently, so a "value band" does not control apparent
  brightness. Likewise `COLOR_MIN_DIST` in RGB does not measure perceptual
  distinctness: 108 of 165 palettes already contain a pair closer than Oklab
  dE 0.10 while passing the existing 0.12 RGB check.

### Corpus

165 decodable covers: ~50 real ones in `~/.local/share/jellyfin-tui/covers` plus
the Spotify covers already cached under `~/.cache/mpris-chroma/covers`. No
network needed. Source mean luminance spans the full range — p10 0.044,
median 0.191, p90 0.490; 19 covers below 0.05 and 8 above 0.6 — so both the
dark-cover and bright-cover cases are genuinely represented.

---

## 2. Decisions taken (and their rationale)

| # | Decision | Chosen |
|---|---|---|
| D1 | Within-cover lightness | **Preserve the source's own relationships**, re-tone the whole spread. A flat cover is allowed to stay flat. |
| D2 | Repo scope | **Daemon-first.** `~/wlchroma` opened only if the lab proves the 12-cell ramp is the limiting factor. |
| D3 | Iteration loop | **Interactive A/B walker** driving the real `wlchroma-ctl`. Contact sheet deferred. |
| D4 | Cross-cover lightness | **Compressive.** Ordering preserved, absolute level compressed — dark covers stay dark, bright covers pulled toward the dark envelope. |
| D5 | Gamut policy | **Preserve hue and lightness, reduce chroma to the sRGB boundary.** |

D1 and D4 are orthogonal axes — within-cover fidelity and cross-cover
compression — and §5 keeps them as two independent parameters rather than one
entangled curve.

---

## 3. Color space

**Oklab / OkLCh**, implemented inline in `colors.py`. No new dependency: the
forward and inverse transforms are ~20 lines of arithmetic, and the sRGB gamut
boundary is found by bisection on chroma at fixed (L, h). Pillow and `colorsys`
remain the only imports.

Rationale: lightness targets and distinctness thresholds are both meaningless in
HSV. Oklab gives a perceptually uniform L for the envelope, a hue angle that can
be held genuinely fixed, and a chroma axis whose sRGB ceiling can be computed —
which decision D5 requires.

### The gamut constraint is real and hue-dependent

Maximum in-gamut Oklab chroma, measured:

| L | red | orange | yellow | green | cyan | blue | purple |
|---|---|---|---|---|---|---|---|
| 0.15 | .076 | .054 | **.048** | .068 | **.032** | .114 | .086 |
| 0.25 | .106 | .064 | **.058** | .090 | **.044** | .176 | .120 |
| 0.35 | .146 | .084 | .078 | .120 | .060 | .244 | .164 |
| 0.45 | .186 | .106 | .098 | .152 | .076 | .312 | .208 |

Median chroma of current picks is 0.083, so darkening at constant chroma leaves
sRGB on **29%** of slots. Under the final design, policy D5 engages on
**130/495 slots (26%)** — overwhelmingly yellows and cyans. Left implicit this
would default to naive RGB clipping, which shifts hue unevenly and turns dark
yellows to mud; D5 makes it explicit and hue-preserving.

---

## 4. Pipeline

Four stages in `colors.py`. Mode enters only at stage 3.

```
1. histogram        unchanged — Pillow; SEC-005/006 guards untouched
2. select           vibrancy ranking → 3 source colors; MODE-INDEPENDENT
3. tone(mode)       Oklab: compress across covers, preserve within cover
4. separate(mode)   perceptual distinctness repair
```

Stages 3 and 4 are pure functions `(list[OkLCh], mode) -> list[OkLCh]` with no
image I/O, so they are directly unit-testable. Stage 2 keeps today's
`_vibrancy_score` ranking unchanged — the ranking is not what is broken, and the
existing vibrancy tests must continue to pass as written.

---

## 5. Stage 3 — toning

```
A     = mean(L_i)                            anchor: the cover's overall tone
A'    = lo + (hi - lo) · A^γ                 cross-cover compression   (D4)
L_i'  = clamp(A' + k · (L_i - A), lo, hi)    within-cover offsets kept (D1)
C_i'  = max(C_i, cfrac · ceil_C(L_i', h_i))  chroma relative to its ceiling
h_i'  = h_i                                  hue is never modified
```

then D5: `C_i' = min(C_i', ceil_C(L_i', h_i))`.

- `γ` alone controls cross-cover compression; `k` alone controls within-cover
  fidelity. `k = 1.0` is the default, and the only spread loss is envelope
  clamping.
- `cfrac` is a **fraction of the achievable ceiling**, not an absolute chroma.
  An absolute target is incoherent because the ceiling varies ~3× across hue at
  fixed L — simultaneously unreachable for cyan and unambitious for blue. This
  is what keeps dark colors saturated rather than muddy, and it replaces `S_MIN`.
- Colors with `C < NEUTRAL_C` are exempt from the `cfrac` lift, which is how
  grayscale covers stay grayscale. This replaces `NEUTRAL_S`.

### Starting parameters (lab-tunable)

| Constant | Dark | Light | Meaning |
|---|---|---|---|
| `ENVELOPES[mode]` | `(0.15, 0.55)` | `(0.55, 0.92)` | Oklab L envelope |
| `GAMMA[mode]` | `0.85` | `1.18` | compression exponent |
| `SPREAD_GAIN` | `1.0` | `1.0` | `k` — within-cover fidelity |
| `CHROMA_FRAC` | `0.85` | `0.85` | `cfrac` — target fraction of ceiling |
| `NEUTRAL_C` | `0.02` | `0.02` | below this, stay neutral |
| `MIN_DE` | `0.10` | `0.10` | §6 separation threshold |
| `SEPARATION_STEP` | `0.01` | `0.01` | §6 per-pass L nudge |
| `MAX_SEPARATION_SHIFT` | `0.04` | `0.04` | §6 per-slot total displacement cap |
| `MAX_SEPARATION_PASSES` | `8` | `8` | §6 loop safety net |

`γ < 1` compresses the top of the range harder than the bottom (slope 0.88 at
A = 0.8 vs 1.08 at A = 0.2), which is exactly D4. Light mode mirrors this
*structurally* — `γ_light = 1/γ_dark`, so the bottom compresses instead, lifting
dark covers while bright ones keep their relative position, against an envelope
shifted up. The two envelopes are tuned independently in the lab and are not
required to be exact reflections of each other.

**The dark and light numbers do not have equal standing.** Dark mode's envelope
is fitted against a measured reference — `witch_hour`, the palette the user
actually configured — and validated against the corpus in §5. Light mode has no
such anchor: `(0.55, 0.92)` and `γ = 1.18` are a **provisional lab seed**,
reasoned from symmetry rather than fitted to anything. §12 gives light mode its
own acceptance criteria for that reason; it must not be read as carrying dark
mode's evidence.

### Measured result

Mean screen luminance through wlchroma's real 12-cell ramp, 165 covers:

| | p10 | median | p90 |
|---|---|---|---|
| current dark mode | 0.098 | **0.215** | 0.367 |
| this design (dark) | 0.040 | **0.067** | 0.096 |
| `witch_hour` reference | — | **0.075** | — |

Median within 10% of the configured reference. p10→p90 spans 2.4×, so a bright
cover still reads brighter than a dark one — compression, not normalization.
Median within-cover spread survives at **92%** of source.

---

## 6. Stage 4 — perceptual distinctness

Toning alone does not fix distinctness and slightly worsens it, because
compressing lightness pulls apart-in-L colors together:

| | p10 dE | median dE | palettes with a pair under dE 0.10 |
|---|---|---|---|
| current | 0.042 | 0.081 | 108 / 165 |
| toned only | 0.003 | 0.082 | 102 / 165 |

The repair must run **after** toning, because that is where the collision
appears — but it must not re-select, or it breaks mode-independence (§7).

**Algorithm.** Selection is fixed; separation adjusts only L (and the chroma
that follows from it), never hue:

1. Fix a **rank order** once, from the toned `L'`, tie-broken by original index.
   This order is an invariant for the rest of the algorithm; slot identity is
   carried alongside so the output can be restored to selection order.
2. While some pair has `dE < MIN_DE` and `passes < MAX_SEPARATION_PASSES`:
   take the closest pair, move the higher-ranked slot up by `SEPARATION_STEP` and
   the lower-ranked one down by the same. Each move is clamped by **three**
   bounds — the envelope, the slot's remaining displacement budget (below), and
   **its adjacent slots' current L**, so a slot can never cross a neighbour.
   Recompute chroma at the new L (the ceiling moved) and re-evaluate.
3. Stop on any of: all pairs clear; no slot can move; every slot has exhausted
   its displacement budget; or the pass cap is hit.

The neighbour clamp in step 2 is what makes monotonicity hold **by
construction** rather than incidentally. Moving a pair apart preserves that
pair's mutual order, but without the clamp a slot could still be pushed past a
*third* slot and invert the source ordering that §8 asserts.

**Displacement budget — the bound on the D1 exception.** Each slot may move at
most `MAX_SEPARATION_SHIFT` in total from its toned L, so a pair's separation can
grow by at most `2 × MAX_SEPARATION_SHIFT`. At 0.04 that is 0.08 against a dark
envelope 0.40 wide — 20% of the envelope, not the 0.16 that an uncapped 8-pass
run would have allowed. The budget binds before the pass cap does
(0.04 ÷ 0.01 = 4 passes of movement per slot); `MAX_SEPARATION_PASSES` is a loop
safety net, not the operative limit.

**Unresolved collisions are reported, not hidden.** When separation terminates
with a pair still under `MIN_DE`, `colors.py` logs it at DEBUG with the cover
name and the residual dE, and the lab surfaces it in the walker line. A palette
that cannot be separated within budget is an accepted outcome — for a genuinely
monochrome cover it is the *correct* outcome — but it must be observable, in
keeping with the SEC-019 precedent that a degraded palette is never silent.

**Carve-out — duplicate slots stay duplicated.** When a cover yields fewer than
three distinct colors, `extract_colors` repeats the last real one rather than
fabricating a hue. Separation must not fire on slots that are duplicates *of the
same source color*, or `test_solid_cover_repeats_not_invents` breaks and a solid
cover starts inventing contrast. Separation applies only between distinct source
picks.

**Accepted trade-off against D1.** Separation adds contrast the source did not
have. It is bounded (envelope-limited), fires only on collisions, and never
touches hue — but it is not pure faithfulness. It is in scope because the brief
explicitly asked for good distinctness across the three slots. A genuinely
monochrome cover cannot be separated and must not be; the envelope bound is the
mechanism that stops it.

---

## 7. Invariants that must not regress

- **SEC-005** (format allowlist by signature), **SEC-006** (decode byte/pixel
  bounds), **SEC-019** (bounded color data, observable fallback): the existing
  `FormatAllowlistTest`, `DecodeBoundsTest` and `ColorDataBoundsTest` classes
  stay exactly as written and must still pass. The undecodable-cover fallback
  triple `#a48ec7 ×3` is unchanged, and still logged at WARNING.
- **Selection stays mode-independent.**
  `test_mode_switch_never_changes_which_colors_are_picked` must pass unmodified.
  Mode enters at stage 3 only; stages 1–2 never see it.
- **Hue is never modified** by toning, gamut mapping, or separation, in either
  mode.
- **Grayscale covers stay grayscale** (`NEUTRAL_C` exemption).
- **Solid covers repeat rather than invent** (§6 carve-out).
- Vibrancy ranking behavior (`_vibrancy_score` tests) is untouched.

---

## 8. Test plan — `tests/test_colors.py`, unittest, thread-free

Properties over the pure stages, not golden-hex snapshots, so tuning constants
does not churn the suite.

### Which existing tests change

Removing `clamp_hsv`, `BANDS`, `S_MIN`, `V_MIN`/`V_MAX`, `NEUTRAL_S` and
`COLOR_MIN_DIST` necessarily invalidates the tests written against them. Stated
explicitly so the diff is not a surprise:

- **Replaced** — `ClampTest` and `ModeBandTest` in full: they assert on
  `clamp_hsv` and the `BANDS` value bands, both of which cease to exist. Their
  intent survives as the envelope, hue-invariance and neutral-preservation
  properties below. (`test_hex_of_roundtrips_format` carries over unchanged.)
- **Restated in Oklab terms**, same intent, three tests in `ExtractTest`:
  `test_dark_colored_cover_is_lifted_to_readable` (S_MIN/V_MIN → chroma fraction
  and envelope), `test_grayscale_cover_stays_neutral` (S_MIN → `NEUTRAL_C`),
  `test_light_mode_same_hues_brighter_values` (`BANDS` → `ENVELOPES`).
- **Frozen — the test methods themselves must not be edited**, and must still
  pass as written: `VibrancyScoreTest`, `FormatAllowlistTest`,
  `DecodeBoundsTest`, `ColorDataBoundsTest`, and the remaining six `ExtractTest`
  cases, notably `test_mode_switch_never_changes_which_colors_are_picked` and
  `test_solid_cover_repeats_not_invents`. This constrains the *source of the
  tests*, not the palettes they exercise — toning changes almost every extracted
  hex, which is the point of the work; these tests are frozen precisely because
  they assert properties that survive it.

**New properties**

- Every output lands inside its mode's `ENVELOPES[mode]`.
- Every output is in sRGB gamut — no channel clipping after the chroma cut.
- **Monotonicity:** if source `L_i > L_j` then toned `L_i' >= L_j'`. This is the
  property that makes mode-independence safe.
- **Cross-cover compression:** a bright synthetic cover tones darker than it does
  today, and still tones brighter than a dark synthetic cover.
- **Within-cover fidelity:** a contrasty synthetic cover retains a substantial
  fraction of its source spread; a flat one stays flat.
- **Distinctness:** every pair clears `MIN_DE`, *or* one of §6's accepted
  terminal conditions holds — envelope exhausted, displacement budget spent, pass
  cap reached, or the slots are duplicates of one source color. A test asserts
  each terminal condition is reachable and correctly reported.
- **Separation is bounded:** no slot moves more than `MAX_SEPARATION_SHIFT` from
  its toned L, and no slot crosses a neighbour (the §6 rank order is preserved
  end to end).
- **Light-mode symmetry:** the same properties hold against the light envelope,
  and hues match dark mode's for the same cover.
- `NEUTRAL_C` exemption: a near-neutral input is not chroma-lifted.

**Corpus test — opt-in, not in the default run.** One test running the full
pipeline over the real cover directories, asserting median ramp luminance sits
within a band. Guarded by `unittest.skipUnless` on directory existence, matching
the existing 3 opt-in tests, so CI and a fresh clone stay green.

---

## 9. The offline lab — `tools/palette_lab.py`

Thread-free, no daemon, no D-Bus. Reads covers from disk, drives `wlchroma-ctl`
through `apply.apply_wlchroma()` so what is judged is exactly what the daemon
will produce. Style matches the existing `tools/fake_mpris.py`; stdlib + Pillow
only (raw-mode key reads via `termios`, no curses).

```
[ 17/165 ] 4f5c6622.jpeg                    verdicts: A 3  B 11  = 2
   A  current     #c8706e #8f504f #d9d9d9   L .62 .45 .85   lum .215
 ▶ B  oklab-v1    #2b1418 #7a4b52 #c98d84   L .18 .42 .68   lum .067
   [space] toggle   [a/b/=] verdict   [←/→] cover   [q] quit
```

- Cover displayed via `imv` (confirmed installed). Start with spawn-and-kill per
  cover; move to a reused window only if it flickers badly.
- Candidates are named parameter sets in a table at the top of the file
  (`lo`, `hi`, `γ`, `k`, `cfrac`, `min_de`), so a variant is one line.
- Verdicts append to `tools/verdicts.json`, keyed by
  `(cover, candidate_a, candidate_b)`, so a later constant change can be replayed
  against existing judgements to report which ones it would flip.
- `--corpus` defaults to both cover directories; `--holdout 25` reserves a
  deterministic slice by filename hash that tuning never touches, for a cold
  check at the end (guards against eyeball-overfitting 165 covers).
- Restores the configured palette on exit, so the lab cannot leave the desktop
  stuck on a candidate.

---

## 10. Documentation

- **Tuning table** in `README.md` replaced: `S_MIN`, `V_MIN`/`V_MAX`, `BANDS`,
  `NEUTRAL_S`, `COLOR_MIN_DIST` give way to the §5 constants, with the Oklab
  basis stated.
- **Ranking description** updated: selection is unchanged, but "colors are only
  lifted for visibility" becomes an accurate description of envelope toning.
- **New "Theme switching" section.** The daemon subscribes to `SettingChanged`
  on `org.freedesktop.portal.Settings` (`sync.py:236`), so any backend
  implementing the portal Settings interface drives it live. Verified on this
  machine: portal answers `color-scheme` = 1 (prefer-dark) via
  xdg-desktop-portal-gtk proxying `org.gnome.desktop.interface color-scheme`;
  darkman is not installed. Document both wirings —
  (a) register darkman as the Settings backend via
  `~/.config/xdg-desktop-portal/portals.conf`
  (`org.freedesktop.impl.portal.Settings=darkman`), or
  (b) a `~/.local/share/{dark,light}-mode.d/` script setting the gsettings key,
  which the gtk portal republishes. Neither needs code changes.

---

## 11. Risks and deferred knobs

- **Yellows are structurally disadvantaged.** At L 0.15 a yellow holds C 0.048
  against blue's 0.114, so yellow-dominant covers will read deader. The fix
  (letting target L float up for low-ceiling hues) trades away D1 fidelity, so it
  ships as a **lab parameter defaulting to off** and is judged visually on real
  yellow covers rather than decided in the abstract.
- **Eyeball overfitting.** Mitigated by the 25-cover holdout (§9).
- **The 12-cell ramp** spends 8 of 12 cells on midpoint blends, which are always
  less saturated than the endpoints. Per D2 this is treated as a fixed constraint;
  `~/wlchroma` is opened only if the lab shows it dominating after §5–§6 land.
- **Envelope clamping erodes spread** for palettes whose anchor lands near an
  envelope edge (measured: 8% median loss). Acceptable; `k` is the lever if not.

---

## 12. Acceptance

**Measurable, dark mode:** all of §7 and §8 pass; the frozen tests listed in §8
still pass with their source unedited; the suite grows from its 224 baseline
after the §8 replacements are accounted for; median ramp luminance lands near the
`witch_hour` reference (0.075) with p10→p90 still spanning >2×.

**Measurable, light mode.** There is no reference palette to fit against, so
light mode is accepted on *relational* criteria instead of a target number:

- **Strictly brighter than dark:** for every corpus cover, light-mode ramp
  luminance exceeds the same cover's dark-mode luminance.
- **Hue and order preserved across the flip:** same hues within tolerance, and
  the same slot rank order, for the same cover in both modes.
- **No upper-envelope clustering:** the p10→p90 spread of light-mode ramp
  luminance across the corpus stays above a floor, so light mode does not
  reproduce today's defect of crushing every cover into a narrow bright band
  (the current `BANDS["light"]` = 0.70–0.97 failure).
- **Within-cover spread survives** at a comparable fraction to dark mode's 92%.

**Visual: the user's call**, via the §9 walker, for both modes — and the light
seed in §5 should be expected to move as a result, where the dark constants are
expected to hold. Constants are then tuned from the recorded verdicts and
re-checked cold against the holdout.
