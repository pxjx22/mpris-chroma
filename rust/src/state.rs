//! Typed state model for the sync daemon (port of `state.py`).
//!
//! Only `Mode` is ported so far; `PlayerState` lands with the coordinator.

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
