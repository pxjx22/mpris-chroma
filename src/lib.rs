//! mpris-chroma: sync wlchroma's background colours to the playing cover.
//!
//! Ported module by module from the original Python daemon, keeping its
//! observable behaviour (hex output) bit for bit, checked against golden
//! fixtures recorded from the Python. See `docs/rust-port.md`.

pub mod apply;
pub mod color;
pub mod coordinator;
pub mod cover;
pub mod daemon;
pub mod framing;
pub mod runtime;
pub mod select;
pub mod sources;
pub mod state;
pub mod worker;
