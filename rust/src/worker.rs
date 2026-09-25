//! Off-main-thread palette worker (port of `worker.py`).
//!
//! Only the job and result types are ported so far, because `select` and the
//! coordinator speak in them. The mailbox and worker thread land in their own
//! step.

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

/// How a job ended (SEC-018 taxonomy).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Outcome {
    /// Resolve returned Ready and ctl confirmed the change.
    Committed,
    /// The (content_id, mode) guard hit: wlchroma already shows this.
    SkippedDuplicate,
    /// Resolve returned Retryable, or ctl failed (transient).
    FailedRetryable,
    /// Resolve returned Rejected (deterministic policy or content).
    Rejected,
}

/// What the worker hands back to the coordinator's `adopt`.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct JobResult {
    pub generation: u64,
    pub outcome: Outcome,
    /// The resolved cover on an apply's commit/skip, else `None`.
    pub cover_id: Option<String>,
}
