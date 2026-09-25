//! Per-mode palette toning (port of `tone.py`, spec §5-6).
//!
//! Toning works in Oklab and splits the problem along two independent axes:
//! how bright this cover is relative to other covers (compressed), and how its
//! three slots relate to each other (preserved). Separation then repairs
//! perceptual collisions by moving lightness only, within a fixed budget.

use super::oklab::{self, Lab, Lch};
use crate::state::Mode;

/// Oklab lightness envelope per mode. Dark is fitted against the witch_hour
/// reference palette; light is a provisional seed reasoned from symmetry.
pub fn envelope(mode: Mode) -> (f64, f64) {
    match mode {
        Mode::Dark => (0.15, 0.55),
        Mode::Light => (0.55, 0.92),
    }
}

/// Compression exponent. Below 1 the top of the range compresses harder than
/// the bottom; light inverts it so the bottom compresses instead.
pub fn gamma(mode: Mode) -> f64 {
    match mode {
        Mode::Dark => 0.85,
        Mode::Light => 1.18,
    }
}

pub const SPREAD_GAIN: f64 = 1.0; // how much of the cover's own lightness spread survives
pub const CHROMA_FRAC: f64 = 0.85; // target chroma as a fraction of the in-gamut ceiling
pub const NEUTRAL_C: f64 = 0.02; // below this a slot is genuinely grey and is left alone

/// Chroma for a slot at lightness `l`, given the source color's chroma.
///
/// Expressed against the ceiling because the ceiling varies about threefold
/// across hue. A near-neutral source is exempt, so grayscale is never tinted.
pub fn chroma_for(l: f64, h: f64, c_src: f64) -> f64 {
    let ceiling = oklab::max_chroma(l, h);
    if c_src < NEUTRAL_C {
        return c_src.min(ceiling);
    }
    c_src.max(CHROMA_FRAC * ceiling).min(ceiling)
}

/// One toned palette slot.
///
/// Chroma is derived rather than stored: separation moves `l`, and the
/// in-gamut ceiling moves with it, so a stored chroma would fall out of gamut.
/// `c_src` is the *source* color's chroma and is mode-independent.
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct Toned {
    pub l: f64,
    pub h: f64,
    pub c_src: f64,
}

impl Toned {
    pub fn c(&self) -> f64 {
        chroma_for(self.l, self.h, self.c_src)
    }

    pub fn to_hex(&self) -> String {
        oklab::lch_to_hex(self.l, self.c(), self.h)
    }

    pub fn to_lab(&self) -> Lab {
        oklab::from_lch(self.l, self.c(), self.h)
    }
}

/// Map source OkLCh slots into the mode's envelope.
///
/// Cross-cover: the palette's mean lightness is pushed through a compressive
/// curve. Within-cover: each slot keeps its own offset from that anchor.
pub fn tone(source_lch: &[Lch], mode: Mode) -> Vec<Toned> {
    assert!(!source_lch.is_empty(), "tone() needs at least one slot");
    let (lo, hi) = envelope(mode);
    let anchor = source_lch.iter().map(|s| s.0).sum::<f64>() / source_lch.len() as f64;
    // max() guards negative float epsilon at pure black before the fractional
    // power (NaN in Rust, a complex number in Python).
    let toned_anchor = lo + (hi - lo) * anchor.max(0.0).powf(gamma(mode));
    source_lch
        .iter()
        .map(|&(l, c, h)| {
            let moved = toned_anchor + SPREAD_GAIN * (l - anchor);
            Toned {
                l: moved.max(lo).min(hi),
                h,
                c_src: c,
            }
        })
        .collect()
}

pub const MIN_DE: f64 = 0.10; // pairs closer than this in Oklab read as duplicates
pub const SEPARATION_STEP: f64 = 0.01; // per-pass lightness nudge
pub const MAX_SEPARATION_SHIFT: f64 = 0.04; // total displacement any one slot may accumulate
pub const MAX_SEPARATION_PASSES: usize = 8; // iteration cap (clamped partial steps can hit it)
const BUDGET_EPS: f64 = 1e-9; // repeated subtraction settles a few ulps above 0.0

/// Why separation stopped.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum SeparationReason {
    /// Every pair is at least `MIN_DE` apart.
    Clear,
    /// Fewer than two real picks; nothing to separate.
    Duplicates,
    /// Every slot spent its full displacement allowance.
    Budget,
    /// Budget remains, but the envelope or a neighbour pins the slots.
    Blocked,
    /// The pass cap was hit.
    Passes,
}

impl SeparationReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Clear => "clear",
            Self::Duplicates => "duplicates",
            Self::Budget => "budget",
            Self::Blocked => "blocked",
            Self::Passes => "passes",
        }
    }
}

/// An unseparable palette is an accepted outcome (a monochrome cover), but it
/// must be observable rather than silent.
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct SeparationResult {
    pub resolved: bool,
    pub reason: SeparationReason,
    /// Closest remaining pair; `MIN_DE` or more when resolved.
    pub residual_de: f64,
}

fn closest_pair(slots: &[Toned]) -> (usize, usize, f64) {
    let labs: Vec<Lab> = slots.iter().map(Toned::to_lab).collect();
    let mut best = (0, 1, f64::INFINITY);
    for i in 0..slots.len() {
        for j in i + 1..slots.len() {
            let d = oklab::delta_e(labs[i], labs[j]);
            if d < best.2 {
                best = (i, j, d);
            }
        }
    }
    best
}

/// Push colliding slots apart in lightness only, within bounds.
///
/// Only the first `n_distinct` slots (the *real* picks) participate; the rest
/// are repeats of the last real pick and are re-padded from it afterwards.
///
/// # Panics
/// If `n_distinct > slots.len()` — a caller bug, as in the Python version.
pub fn separate(slots: &[Toned], mode: Mode, n_distinct: usize) -> (Vec<Toned>, SeparationResult) {
    let (lo, hi) = envelope(mode);
    assert!(
        n_distinct <= slots.len(),
        "n_distinct ({n_distinct}) exceeds the number of slots ({})",
        slots.len()
    );
    if n_distinct < 2 {
        let result = SeparationResult {
            resolved: false,
            reason: SeparationReason::Duplicates,
            residual_de: 0.0,
        };
        return (slots.to_vec(), result);
    }

    let mut work: Vec<Toned> = slots[..n_distinct].to_vec();
    // Rank order is fixed once, here. Every move is clamped against
    // neighbours in this order, which makes monotonicity hold by construction.
    let mut order: Vec<usize> = (0..n_distinct).collect();
    order.sort_by(|&a, &b| work[a].l.total_cmp(&work[b].l).then(a.cmp(&b)));
    let mut rank = vec![0; n_distinct];
    for (pos, &slot) in order.iter().enumerate() {
        rank[slot] = pos;
    }
    let mut budget = vec![MAX_SEPARATION_SHIFT; n_distinct];
    let mut reason = SeparationReason::Passes;

    for _ in 0..MAX_SEPARATION_PASSES {
        let (i, j, dist) = closest_pair(&work);
        if dist >= MIN_DE {
            reason = SeparationReason::Clear;
            break;
        }
        // Move the higher-ranked slot of the pair up and the lower one down.
        let (up, down) = if rank[i] > rank[j] { (i, j) } else { (j, i) };
        let mut moved = false;
        for (idx, direction) in [(up, 1.0), (down, -1.0)] {
            let step = SEPARATION_STEP.min(budget[idx]);
            if step <= 0.0 {
                continue;
            }
            let current = work[idx].l;
            // Envelope first, then neighbours, so the neighbour clamp only
            // ever narrows toward `current`.
            let mut target = (current + direction * step).max(lo).min(hi);
            // Input already past the bound on the side this move heads into
            // gets clamped back *against* the requested direction; that is
            // not a separation move, so refuse it.
            if (target - current) * direction <= 0.0 {
                continue;
            }
            let pos = rank[idx];
            if direction > 0.0 && pos + 1 < n_distinct {
                target = target.min(work[order[pos + 1]].l);
            }
            if direction < 0.0 && pos >= 1 {
                target = target.max(work[order[pos - 1]].l);
            }
            let mut delta = target - current;
            if delta * direction <= 0.0 {
                continue;
            }
            // The budget wins over an envelope clamp that jumped further than
            // `step` for out-of-envelope input.
            if delta.abs() > budget[idx] {
                delta = if delta > 0.0 {
                    budget[idx]
                } else {
                    -budget[idx]
                };
            }
            budget[idx] -= delta.abs();
            work[idx] = Toned {
                l: current + delta,
                ..work[idx]
            };
            moved = true;
        }
        // Exhaustion before blockage, so both stay reachable.
        if budget.iter().all(|&b| b <= BUDGET_EPS) {
            reason = SeparationReason::Budget;
            break;
        }
        if !moved {
            reason = SeparationReason::Blocked;
            break;
        }
    }

    let residual = closest_pair(&work).2;
    let resolved = residual >= MIN_DE;
    if resolved {
        reason = SeparationReason::Clear;
    }
    // Re-pad: a cover with fewer than three real colors repeats its last one,
    // and that repeat must keep tracking the slot it mirrors.
    let last = work[n_distinct - 1];
    work.resize(slots.len(), last);
    (
        work,
        SeparationResult {
            resolved,
            reason,
            residual_de: residual,
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::color::oklab::{delta_e, from_lch, hex_to_lch, in_gamut, max_chroma};
    use SeparationReason::*;

    const MODES: [Mode; 2] = [Mode::Dark, Mode::Light];

    fn lch(hexes: &[&str]) -> Vec<Lch> {
        hexes.iter().map(|h| hex_to_lch(h)).collect()
    }

    fn ls(slots: &[Toned]) -> Vec<f64> {
        slots.iter().map(|s| s.l).collect()
    }

    fn mean_l(slots: &[Toned]) -> f64 {
        slots.iter().map(|s| s.l).sum::<f64>() / slots.len() as f64
    }

    fn spread(slots: &[Toned]) -> f64 {
        let l = ls(slots);
        l.iter().cloned().fold(f64::MIN, f64::max) - l.iter().cloned().fold(f64::MAX, f64::min)
    }

    fn rank_order(slots: &[Toned]) -> Vec<usize> {
        let mut order: Vec<usize> = (0..slots.len()).collect();
        order.sort_by(|&a, &b| slots[a].l.total_cmp(&slots[b].l));
        order
    }

    fn assert_rank_preserved(before: &[Toned], after: &[Toned]) {
        let ranked: Vec<f64> = rank_order(before).iter().map(|&i| after[i].l).collect();
        let mut sorted = ranked.clone();
        sorted.sort_by(f64::total_cmp);
        assert_eq!(ranked, sorted);
    }

    fn assert_within_budget(before: &[Toned], after: &[Toned]) {
        for (a, b) in before.iter().zip(after) {
            assert!((a.l - b.l).abs() <= MAX_SEPARATION_SHIFT + 1e-9);
        }
    }

    fn toned(l: f64, h_deg: f64, c_src: f64) -> Toned {
        Toned {
            l,
            h: h_deg.to_radians(),
            c_src,
        }
    }

    // --- chroma rule -----------------------------------------------------

    #[test]
    fn colored_slot_is_lifted_toward_its_ceiling() {
        let h = 29f64.to_radians();
        let c = chroma_for(0.30, h, 0.02);
        assert!(c > 0.02);
        assert!(c <= max_chroma(0.30, h));
    }

    #[test]
    fn chroma_never_exceeds_the_ceiling() {
        for l in [0.15, 0.35, 0.55] {
            for h_deg in [60.0f64, 110.0, 195.0, 264.0] {
                let h = h_deg.to_radians();
                assert!(chroma_for(l, h, 0.30) <= max_chroma(l, h) + 1e-9);
            }
        }
    }

    #[test]
    fn neutral_slot_is_not_enriched() {
        assert!(chroma_for(0.30, 0.0, 0.001) < NEUTRAL_C);
    }

    // --- tone ------------------------------------------------------------

    #[test]
    fn output_lands_inside_the_mode_envelope() {
        for mode in MODES {
            let (lo, hi) = envelope(mode);
            for src in [
                lch(&["#000000"]),
                lch(&["#ffffff"]),
                lch(&["#e01050", "#10e050", "#5010e0"]),
            ] {
                for slot in tone(&src, mode) {
                    assert!(slot.l >= lo - 1e-9 && slot.l <= hi + 1e-9);
                }
            }
        }
    }

    #[test]
    fn output_is_in_gamut() {
        let src = lch(&["#ffee00", "#00e5ff", "#1010e0"]);
        for mode in MODES {
            for slot in tone(&src, mode) {
                let (l, a, b) = from_lch(slot.l, slot.c(), slot.h);
                assert!(in_gamut(l, a, b));
            }
        }
    }

    #[test]
    fn tone_never_modifies_hue() {
        let src = lch(&["#e01050", "#10e050", "#5010e0"]);
        for mode in MODES {
            for (got, want) in tone(&src, mode).iter().zip(&src) {
                assert!((got.h - want.2).abs() < 1e-12);
            }
        }
    }

    #[test]
    fn lightness_order_is_preserved() {
        let src = lch(&["#202020", "#808080", "#e8e8e8"]);
        for mode in MODES {
            let out = ls(&tone(&src, mode));
            assert!(out[0] < out[1] && out[1] < out[2]);
        }
    }

    #[test]
    fn bright_cover_is_compressed_not_merely_clamped() {
        let (_, hi) = envelope(Mode::Dark);
        let out = tone(&lch(&["#f2f2f2", "#e8e8e8", "#fafafa"]), Mode::Dark);
        let pinned = out.iter().filter(|s| s.l >= hi - 1e-9).count();
        assert!(
            pinned < 3,
            "all slots pinned at the ceiling: clamped, not compressed"
        );
        assert!(spread(&out) > 0.02);
        assert!(mean_l(&out) < hi - 0.005);
    }

    #[test]
    fn dark_cover_stays_darker_than_a_bright_one() {
        let dark = tone(&lch(&["#101010", "#181818", "#121212"]), Mode::Dark);
        let bright = tone(&lch(&["#f2f2f2", "#e8e8e8", "#fafafa"]), Mode::Dark);
        assert!(mean_l(&dark) < mean_l(&bright));
    }

    #[test]
    fn within_cover_spread_is_largely_preserved() {
        let src = lch(&["#101010", "#808080", "#f0f0f0"]);
        let src_l: Vec<f64> = src.iter().map(|s| s.0).collect();
        let src_spread = src_l.iter().cloned().fold(f64::MIN, f64::max)
            - src_l.iter().cloned().fold(f64::MAX, f64::min);
        assert!(spread(&tone(&src, Mode::Dark)) > 0.5 * src_spread);
    }

    #[test]
    fn flat_cover_stays_flat() {
        let out = tone(&lch(&["#4a4a4a", "#4c4c4c", "#4b4b4b"]), Mode::Dark);
        assert!(spread(&out) < 0.05);
    }

    #[test]
    fn grayscale_stays_grayscale() {
        let src = lch(&["#202020", "#808080", "#d0d0d0"]);
        for mode in MODES {
            for slot in tone(&src, mode) {
                assert!(slot.c() < NEUTRAL_C);
            }
        }
    }

    #[test]
    fn light_mode_is_brighter_than_dark_for_the_same_source() {
        let src = lch(&["#e01050", "#10e050", "#5010e0"]);
        for (d, l) in tone(&src, Mode::Dark).iter().zip(tone(&src, Mode::Light)) {
            assert!(l.l > d.l);
        }
    }

    #[test]
    fn gamma_light_is_the_structural_inverse_of_dark() {
        assert!((gamma(Mode::Light) - 1.0 / gamma(Mode::Dark)).abs() < 0.005);
    }

    #[test]
    fn chroma_follows_lightness() {
        // Yellow: a hue with a low, fast-moving ceiling.
        let low = toned(0.15, 110.0, 0.30);
        let high = toned(0.45, 110.0, 0.30);
        assert!(low.c() < high.c());
        assert!(low.c() <= max_chroma(0.15, low.h) + 1e-9);
    }

    // --- separation ------------------------------------------------------

    /// Three tans that the old RGB distance check passed: a real collision
    /// from the corpus, not a synthetic one.
    fn collide(mode: Mode) -> Vec<Toned> {
        tone(&lch(&["#d1a973", "#b89265", "#9d7156"]), mode)
    }

    #[test]
    fn collision_is_separated() {
        let (slots, result) = separate(&collide(Mode::Dark), Mode::Dark, 3);
        for i in 0..3 {
            for j in i + 1..3 {
                assert!(delta_e(slots[i].to_lab(), slots[j].to_lab()) >= MIN_DE - 1e-9);
            }
        }
        assert!(result.resolved);
        assert_eq!(result.reason, Clear);
    }

    #[test]
    fn already_distinct_palette_is_untouched() {
        let before = tone(&lch(&["#e01050", "#10e050", "#5010e0"]), Mode::Dark);
        let (after, result) = separate(&before, Mode::Dark, 3);
        assert_eq!(ls(&before), ls(&after));
        assert_eq!(result.reason, Clear);
    }

    #[test]
    fn separate_never_modifies_hue() {
        let before = collide(Mode::Dark);
        let (after, _) = separate(&before, Mode::Dark, 3);
        assert_ne!(ls(&before), ls(&after));
        for (a, b) in before.iter().zip(&after) {
            assert!((a.h - b.h).abs() < 1e-12);
        }
    }

    #[test]
    fn no_slot_moves_beyond_the_displacement_budget() {
        let before = collide(Mode::Dark);
        let (after, _) = separate(&before, Mode::Dark, 3);
        assert_ne!(ls(&before), ls(&after));
        assert_within_budget(&before, &after);
    }

    #[test]
    fn no_slot_crosses_a_neighbour() {
        let before = collide(Mode::Dark);
        let (after, _) = separate(&before, Mode::Dark, 3);
        assert_ne!(ls(&before), ls(&after));
        assert_rank_preserved(&before, &after);
    }

    #[test]
    fn upper_envelope_near_collision_holds_rank_order_in_light_mode() {
        // A fuzz-found case where the neighbour clamp actually binds: without
        // it the highest slot is pushed down past the middle one.
        let before = [
            Toned {
                l: 0.7130591368263828,
                h: 4.139371341539623,
                c_src: 0.014453312347017083,
            },
            Toned {
                l: 0.6532652310289819,
                h: 1.671075333698124,
                c_src: 0.11318619824925734,
            },
            Toned {
                l: 0.6747783701248393,
                h: 0.4038952174457016,
                c_src: 0.014586514882551428,
            },
        ];
        let (after, _) = separate(&before, Mode::Light, 3);
        assert_ne!(ls(&before), ls(&after));
        assert_rank_preserved(&before, &after);
    }

    #[test]
    fn output_stays_inside_the_envelope() {
        let (lo, hi) = envelope(Mode::Dark);
        let before = collide(Mode::Dark);
        let (after, _) = separate(&before, Mode::Dark, 3);
        assert_ne!(ls(&before), ls(&after));
        for slot in after {
            assert!(slot.l >= lo - 1e-9 && slot.l <= hi + 1e-9);
        }
    }

    #[test]
    fn duplicate_slots_are_never_separated() {
        let toned = tone(&lch(&["#c81e5a"; 3]), Mode::Dark);
        let (after, result) = separate(&toned, Mode::Dark, 1);
        assert_eq!(after[0].l, after[1].l);
        assert_eq!(after[1].l, after[2].l);
        assert_eq!(result.reason, Duplicates);
        assert!(!result.resolved);
    }

    #[test]
    fn two_distinct_slots_separate_and_the_pad_follows() {
        let mut toned = tone(&lch(&["#d1a973", "#b89265"]), Mode::Dark);
        toned.push(toned[1]);
        let (after, result) = separate(&toned, Mode::Dark, 2);
        assert_eq!(after[1].l, after[2].l);
        assert!(delta_e(after[0].to_lab(), after[1].to_lab()) >= MIN_DE - 1e-9);
        assert!(result.resolved);
    }

    #[test]
    fn monochrome_collision_reports_rather_than_forcing() {
        let src = tone(&lch(&["#4a4a4a", "#4b4b4b", "#4c4c4c"]), Mode::Dark);
        let (_, result) = separate(&src, Mode::Dark, 3);
        assert!(!result.resolved);
        assert!(matches!(result.reason, Budget | Blocked | Passes));
        assert!(result.residual_de > 0.0 && result.residual_de < MIN_DE);
    }

    #[test]
    fn budget_exhaustion_is_reported_distinctly() {
        let (lo, hi) = envelope(Mode::Dark);
        let mid = (lo + hi) / 2.0;
        let slots = [
            toned(mid, 29.0, 0.05),
            toned(mid + 0.001, 29.0, 0.05),
            toned(mid + 0.002, 29.0, 0.05),
        ];
        let (after, result) = separate(&slots, Mode::Dark, 3);
        assert_eq!(result.reason, Budget);
        assert!(!result.resolved);
        assert_within_budget(&slots, &after);
    }

    #[test]
    fn budget_is_never_exceeded_whatever_ends_the_run() {
        let before = collide(Mode::Dark);
        let (after, _) = separate(&before, Mode::Dark, 3);
        assert_ne!(ls(&before), ls(&after));
        assert_within_budget(&before, &after);
    }

    #[test]
    fn separation_terminates_in_light_mode_too() {
        let before = collide(Mode::Light);
        let (after, result) = separate(&before, Mode::Light, 3);
        assert_ne!(ls(&before), ls(&after));
        let (lo, hi) = envelope(Mode::Light);
        for slot in &after {
            assert!(slot.l >= lo - 1e-9 && slot.l <= hi + 1e-9);
        }
        assert_ne!(result.reason, Duplicates);
    }

    #[test]
    fn out_of_envelope_input_still_holds_order_and_budget() {
        // Slots below the dark envelope entirely; only order and budget are
        // claimed, since the envelope cannot be guaranteed for such input.
        let before = [
            toned(0.10, 29.0, 0.05),
            toned(0.105, 29.0, 0.05),
            toned(0.11, 29.0, 0.05),
        ];
        let (after, result) = separate(&before, Mode::Dark, 3);
        assert_ne!(ls(&before), ls(&after));
        assert_rank_preserved(&before, &after);
        assert_within_budget(&before, &after);
        assert_ne!(result.reason, Duplicates);
    }

    #[test]
    fn blocked_is_reachable_when_a_slot_is_pinned_at_a_bound() {
        // Slot 0 sits at the ceiling and can never move up; slot 1 moves down
        // until its own budget is spent. Budget remains, nothing can move.
        let (_, hi) = envelope(Mode::Dark);
        let slots = [toned(hi, 29.0, 0.05), toned(hi - 0.03, 29.0, 0.05)];
        let (after, result) = separate(&slots, Mode::Dark, 2);
        assert_eq!(result.reason, Blocked);
        assert!(!result.resolved);
        assert_eq!(after[0].l, hi);
        assert!((slots[1].l - after[1].l).abs() <= MAX_SEPARATION_SHIFT + 1e-9);
    }

    #[test]
    #[should_panic(expected = "exceeds the number of slots")]
    fn n_distinct_beyond_slots_is_a_caller_bug() {
        separate(&collide(Mode::Dark), Mode::Dark, 4);
    }
}
