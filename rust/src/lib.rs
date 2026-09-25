//! Rust port of the mpris-chroma daemon.
//!
//! Ported module by module alongside the Python package in `../mpris_chroma`,
//! which stays the reference implementation until this crate reaches parity.
//! Each ported module keeps the Python module's behavior bit-for-bit where it
//! is observable (hex output), checked against golden fixtures generated from
//! the Python code by `tools/dump_golden.py`.

pub mod color;
pub mod state;
