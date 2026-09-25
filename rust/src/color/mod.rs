//! Color pipeline (port of `colors.py` and the modules it builds on).
//!
//! Ported so far: the mode-dependent half, [`render_palette`], which tones
//! already-selected picks and separates them. The mode-free half (decode,
//! quantize, select) and `PaletteMemo` come next; see `rust/README.md`.

pub mod oklab;
pub mod ramp;
pub mod tone;

use crate::state::Mode;
use oklab::Lch;
use tone::{SeparationReason, separate, tone};

/// Returned for a cover that yielded no usable picks.
pub const DEFAULT_ACCENT: &str = "#a48ec7";

/// Mode-derived half of extraction: tone into the mode's Oklab envelope, then
/// separate. `picks` are source OkLCh colors from selection; the first
/// `n_distinct` are real, any others repeat the last real one. `label` names
/// the cover in diagnostics only.
pub fn render_palette(picks: &[Lch], n_distinct: usize, mode: Mode, label: &str) -> [String; 3] {
    if picks.is_empty() {
        return [DEFAULT_ACCENT; 3].map(String::from);
    }
    let (slots, result) = separate(&tone(picks, mode), mode, n_distinct);
    if !result.resolved && result.reason != SeparationReason::Duplicates {
        // An unseparable palette is an accepted outcome, but never a silent one.
        log::debug!(
            "palette for {label} left a pair at dE {:.3} ({})",
            result.residual_de,
            result.reason.as_str()
        );
    }
    std::array::from_fn(|i| slots[i].to_hex())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_picks_fall_back_to_the_default_accent() {
        assert_eq!(render_palette(&[], 0, Mode::Dark, "x"), [DEFAULT_ACCENT; 3]);
    }

    #[test]
    fn a_single_pick_repeats_rather_than_inventing_colors() {
        let pick = oklab::hex_to_lch("#c81e5a");
        let [a, b, c] = render_palette(&[pick; 3], 1, Mode::Dark, "x");
        assert_eq!(a, b);
        assert_eq!(b, c);
    }
}
