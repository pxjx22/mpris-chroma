//! Typed state model for the sync daemon (port of `state.py`).
//!
//! A per-player record plus the small types the selection and palette-mode
//! logic speak in. The generation token and the cover-state machine are
//! coordinator-scoped, so they live on the coordinator, not here.

/// Theme mode. Hue always comes from the cover; the mode only decides where
/// the palette sits in lightness and how much chroma it carries (see
/// `tone::envelope`).
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Mode {
    Dark,
    Light,
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Dark => "dark",
            Mode::Light => "light",
        }
    }
}

impl std::str::FromStr for Mode {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "dark" => Ok(Mode::Dark),
            "light" => Ok(Mode::Light),
            other => Err(format!("unknown mode {other:?}")),
        }
    }
}

/// The complete MPRIS PlaybackStatus domain (SEC-011 §2.3). The Python keeps
/// this as a string and checks it against a set at the coordinator; parsing
/// into this enum is that same check, so a `PlayerState` cannot hold anything
/// else.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum PlaybackStatus {
    Playing,
    Paused,
    Stopped,
}

impl std::str::FromStr for PlaybackStatus {
    type Err = ();

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "Playing" => Ok(Self::Playing),
            "Paused" => Ok(Self::Paused),
            "Stopped" => Ok(Self::Stopped),
            _ => Err(()),
        }
    }
}

/// One player's current state, as tracked in the coordinator's `players` map.
/// Replaced wholesale on each event, never mutated in place.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct PlayerState {
    pub status: PlaybackStatus,
    /// The player's `mpris:artUrl`, unresolved; empty if none reported.
    pub art_url: String,
    /// Monotonic recency counter (higher = more recent).
    pub seq: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_three_mpris_statuses_parse() {
        assert_eq!("Playing".parse(), Ok(PlaybackStatus::Playing));
        assert_eq!("Paused".parse(), Ok(PlaybackStatus::Paused));
        assert_eq!("Stopped".parse(), Ok(PlaybackStatus::Stopped));
        for bad in ["playing", "Playing ", "", "Buffering"] {
            assert_eq!(bad.parse::<PlaybackStatus>(), Err(()), "{bad:?}");
        }
    }

    #[test]
    fn mode_round_trips_through_its_name() {
        for mode in [Mode::Dark, Mode::Light] {
            assert_eq!(mode.as_str().parse(), Ok(mode));
        }
        assert!("Dark".parse::<Mode>().is_err());
    }
}
