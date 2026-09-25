//! Applying palettes via `wlchroma-ctl` (port of `apply.py`).
//!
//! Only the error type is ported so far, because the worker speaks in it. The
//! bounded ctl invocation and the config-preset revert land in their own step.

use std::fmt;

/// A `wlchroma-ctl` call timed out, failed to start, or exited non-zero.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct CtlError(pub String);

impl fmt::Display for CtlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for CtlError {}
