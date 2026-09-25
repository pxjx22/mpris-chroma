//! Desired-state selection (port of `select.py`).

use std::collections::HashMap;
use std::path::PathBuf;

use crate::state::{Mode, PlaybackStatus, PlayerState};
use crate::worker::{CoverTarget, Desired};

/// What `decide` wants the worker to converge toward.
///
/// The Python returns `(Desired, name)`, `(Desired(None), None)` or `None`;
/// each shape gets its own variant here.
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Selection {
    /// Apply the winner's cover. The winner's name lets adopt() transition
    /// that player's cover state (SEC-018). `desired.target` is always `Some`.
    Apply { desired: Desired, winner: String },
    /// No player is Playing: revert to the config preset (`target: None`).
    Revert(Desired),
    /// Players are Playing but none is actionable: keep the current palette.
    Hold,
}

/// Pick the desired end-state from the per-player states (4b SEC-001, ranking
/// restored in 4c SEC-018).
///
/// Decides from *unresolved* state, since resolution happens off-thread:
///
/// - No player Playing -> `Revert`.
/// - Newest Playing player (by seq) with an eligible *art source* -> `Apply`.
///   A newest player that is ineligible or source-less falls back to the next
///   newest.
/// - Playing players exist but none is actionable -> `Hold`.
///
/// An "art source" is a non-empty art_url OR a configured covers_dir for that
/// player, so a jellyfin-tui line with empty art still resolves via its
/// directory scan rather than holding.
pub fn decide(
    players: &HashMap<String, PlayerState>,
    mode: Mode,
    covers_dir_for: impl Fn(&str) -> Option<PathBuf>,
    eligible: impl Fn(&str) -> bool,
) -> Selection {
    let mut playing: Vec<(&String, &PlayerState)> = players
        .iter()
        .filter(|(_, p)| p.status == PlaybackStatus::Playing)
        .collect();
    if playing.is_empty() {
        return Selection::Revert(Desired { target: None, mode });
    }
    // Newest first. seq is unique per coordinator; the name tie-break only
    // makes a HashMap's arbitrary iteration order irrelevant if it ever isn't.
    playing.sort_by(|a, b| b.1.seq.cmp(&a.1.seq).then_with(|| a.0.cmp(b.0)));
    for (name, p) in playing {
        let covers_dir = covers_dir_for(name);
        if (!p.art_url.is_empty() || covers_dir.is_some()) && eligible(name) {
            let target = CoverTarget {
                art_url: p.art_url.clone(),
                covers_dir,
            };
            return Selection::Apply {
                desired: Desired {
                    target: Some(target),
                    mode,
                },
                winner: name.clone(),
            };
        }
    }
    Selection::Hold
}

#[cfg(test)]
mod tests {
    use super::*;
    use PlaybackStatus::*;

    const JF_DIR: &str = "/covers/jf";

    /// Only jellyfin-tui has a local cover directory (as in production).
    fn covers_dir_for(name: &str) -> Option<PathBuf> {
        (name == "jellyfin-tui").then(|| PathBuf::from(JF_DIR))
    }

    fn players(list: &[(&str, PlaybackStatus, &str, u64)]) -> HashMap<String, PlayerState> {
        list.iter()
            .map(|&(name, status, art, seq)| {
                let state = PlayerState {
                    status,
                    art_url: art.into(),
                    seq,
                };
                (name.to_string(), state)
            })
            .collect()
    }

    fn d(list: &[(&str, PlaybackStatus, &str, u64)], mode: Mode, ineligible: &[&str]) -> Selection {
        decide(&players(list), mode, covers_dir_for, |n| {
            !ineligible.contains(&n)
        })
    }

    fn apply(art: &str, dir: Option<&str>, mode: Mode, winner: &str) -> Selection {
        Selection::Apply {
            desired: Desired {
                target: Some(CoverTarget {
                    art_url: art.into(),
                    covers_dir: dir.map(PathBuf::from),
                }),
                mode,
            },
            winner: winner.into(),
        }
    }

    fn revert(mode: Mode) -> Selection {
        Selection::Revert(Desired { target: None, mode })
    }

    #[test]
    fn single_playing_with_art_applies_that_cover() {
        let got = d(&[("spotify", Playing, "https://x/a", 1)], Mode::Dark, &[]);
        assert_eq!(got, apply("https://x/a", None, Mode::Dark, "spotify"));
    }

    #[test]
    fn two_playing_most_recent_seq_wins() {
        let got = d(
            &[
                ("spotify", Playing, "https://x/s", 2),
                ("jellyfin-tui", Playing, "https://x/j", 1),
            ],
            Mode::Dark,
            &[],
        );
        assert_eq!(got, apply("https://x/s", None, Mode::Dark, "spotify"));
    }

    #[test]
    fn paused_and_playing_switches_to_playing() {
        let got = d(
            &[
                ("spotify", Paused, "https://x/s", 3),
                ("jellyfin-tui", Playing, "https://x/j", 2),
            ],
            Mode::Dark,
            &[],
        );
        assert_eq!(
            got,
            apply("https://x/j", Some(JF_DIR), Mode::Dark, "jellyfin-tui")
        );
    }

    #[test]
    fn all_paused_reverts() {
        let got = d(
            &[
                ("spotify", Paused, "https://x/s", 2),
                ("jellyfin-tui", Paused, "https://x/j", 1),
            ],
            Mode::Dark,
            &[],
        );
        assert_eq!(got, revert(Mode::Dark));
    }

    #[test]
    fn all_stopped_reverts() {
        assert_eq!(
            d(&[("spotify", Stopped, "", 4)], Mode::Dark, &[]),
            revert(Mode::Dark)
        );
    }

    #[test]
    fn empty_reverts() {
        assert_eq!(d(&[], Mode::Dark, &[]), revert(Mode::Dark));
    }

    #[test]
    fn playing_without_art_source_holds() {
        // spotify has no covers_dir and no art_url -> no source -> hold.
        assert_eq!(
            d(&[("spotify", Playing, "", 5)], Mode::Dark, &[]),
            Selection::Hold
        );
    }

    #[test]
    fn playing_jellyfin_empty_art_still_resolves_via_dir() {
        let got = d(&[("jellyfin-tui", Playing, "", 5)], Mode::Dark, &[]);
        assert_eq!(got, apply("", Some(JF_DIR), Mode::Dark, "jellyfin-tui"));
    }

    #[test]
    fn mode_is_carried_into_the_desired() {
        let got = d(&[("spotify", Playing, "https://x/a", 1)], Mode::Light, &[]);
        assert_eq!(got, apply("https://x/a", None, Mode::Light, "spotify"));
    }

    #[test]
    fn newest_ineligible_falls_back_to_older_eligible() {
        let got = d(
            &[
                ("spotify", Playing, "https://x/s", 2),
                ("jellyfin-tui", Playing, "https://x/j", 1),
            ],
            Mode::Dark,
            &["spotify"],
        );
        assert_eq!(
            got,
            apply("https://x/j", Some(JF_DIR), Mode::Dark, "jellyfin-tui")
        );
    }

    #[test]
    fn all_playing_ineligible_holds() {
        // Playing players exist but none can produce a cover -> hold, not revert.
        let got = d(
            &[("spotify", Playing, "https://x/s", 1)],
            Mode::Dark,
            &["spotify"],
        );
        assert_eq!(got, Selection::Hold);
    }

    #[test]
    fn newest_without_art_source_falls_back_to_older_with_source() {
        let got = d(
            &[
                ("spotify", Playing, "", 3),
                ("jellyfin-tui", Playing, "https://x/j", 2),
            ],
            Mode::Dark,
            &[],
        );
        assert_eq!(
            got,
            apply("https://x/j", Some(JF_DIR), Mode::Dark, "jellyfin-tui")
        );
    }
}
