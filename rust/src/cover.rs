//! Cover resolution (port of `cover.py`).
//!
//! Only the typed outcome and content identity are ported so far, because the
//! worker speaks in them. Fetching, caching and the local/dir-scan resolvers
//! land in their own step.

use std::io;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

/// A cover's content identity: `(size, mtime_ns)`, so an in-place overwrite
/// (new mtime) reads as a new cover (SEC-018).
pub type ContentId = (u64, i128);

/// Stat identity of a resolved cover. Uniform for local and remote covers:
/// the remote cache object is validated before publish and atomically
/// replaced (SEC-009), so its stat is a sound identity too.
pub fn content_id(path: &Path) -> io::Result<ContentId> {
    let meta = path.metadata()?;
    let mtime = meta.modified()?;
    let ns = match mtime.duration_since(UNIX_EPOCH) {
        Ok(d) => d.as_nanos() as i128,
        Err(e) => -(e.duration().as_nanos() as i128),
    };
    Ok((meta.len(), ns))
}

/// What resolving a cover produced. Expected failures are values, not errors,
/// and the transient-vs-policy split is cover-domain knowledge exported as
/// data, so the worker maps outcomes without interpreting failures.
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Resolution {
    /// The cover resolved to a local file.
    Ready {
        path: PathBuf,
        content_id: ContentId,
    },
    /// Transient (network, timeout, abort, a cover write lagging the metadata
    /// line). The coordinator retries with capped backoff.
    Retryable(String),
    /// Deterministic policy/content refusal (SSRF, confinement, over-size,
    /// non-image). Not retried without a metadata change.
    Rejected(String),
}
