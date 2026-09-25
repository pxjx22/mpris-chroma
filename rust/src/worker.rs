//! Off-main-thread palette worker (port of `worker.py`).
//!
//! Only the job-description types are ported so far, because `select` speaks
//! in them. The mailbox and worker thread land in their own step.

use std::path::PathBuf;

use crate::state::Mode;

/// The inputs cover resolution needs to materialize one cover.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct CoverTarget {
    pub art_url: String,
    pub covers_dir: Option<PathBuf>,
}

/// The end-state the worker converges wlchroma toward. `target: None` is a
/// revert to the config preset; a `CoverTarget` is an apply.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Desired {
    pub target: Option<CoverTarget>,
    pub mode: Mode,
}
