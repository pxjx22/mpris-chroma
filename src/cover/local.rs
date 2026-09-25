//! Local covers: confined `file://` URLs and the covers-directory scan (port
//! of `cover._resolve_local_cover` / `_scan_covers_dir`, SEC-004, SEC-012,
//! PERF-003).

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use url::Url;

use super::cache::looks_like_image;
use super::{Resolution, content_id};

fn ready(path: PathBuf) -> Resolution {
    match content_id(&path) {
        Ok(content_id) => Resolution::Ready { path, content_id },
        Err(_) => Resolution::Retryable("cover vanished".into()),
    }
}

/// Resolve a `file://` URL to a regular file confined beneath `root`.
///
/// Requires an empty or `localhost` authority (the URL parser normalizes
/// `localhost` away) and a configured root; the symlink-resolved target must
/// be a regular file at or beneath the resolved root. Authority smuggling and
/// symlink escape are policy (Rejected); a missing or not-yet-written file is
/// transient (Retryable).
pub fn resolve_file(art_url: &str, root: Option<&Path>) -> Resolution {
    let Ok(url) = Url::parse(art_url) else {
        return Resolution::Rejected("unparseable file url".into());
    };
    if url
        .host_str()
        .is_some_and(|h| !h.is_empty() && h != "localhost")
    {
        return Resolution::Rejected("file authority not permitted".into());
    }
    let Some(root) = root else {
        return Resolution::Rejected("no cover root for file://".into());
    };
    let Ok(real_root) = fs::canonicalize(root) else {
        return Resolution::Retryable("cover root missing".into());
    };
    let Ok(path) = url.to_file_path() else {
        return Resolution::Rejected("not a local file path".into());
    };
    let Ok(target) = fs::canonicalize(&path) else {
        return Resolution::Retryable("local cover file missing".into()); // write may lag
    };
    if !target.is_file() {
        return Resolution::Retryable("not a regular file".into());
    }
    if !target.starts_with(&real_root) {
        return Resolution::Rejected("path outside cover root".into());
    }
    ready(target)
}

/// The newest-cover scan, memoized per directory on the directory's mtime.
/// Jellyfin names covers by content hash (a new cover is a new file, never
/// an overwrite), so any change that matters bumps the directory mtime; a
/// quiescent directory is not re-enumerated on every event.
#[derive(Default)]
pub struct DirScanner {
    memo: HashMap<PathBuf, (SystemTime, PathBuf)>,
    /// Full scans performed (observability for tests).
    pub scans: usize,
}

/// One candidate: a regular non-symlink file whose leading bytes are an
/// accepted image signature, with its mtime. Any error (vanished, permission
/// denied) skips the candidate rather than failing the scan.
fn candidate(entry: &fs::DirEntry) -> Option<(SystemTime, PathBuf)> {
    // DirEntry::file_type/metadata do not follow symlinks.
    if !entry.file_type().ok()?.is_file() {
        return None;
    }
    let mtime = entry.metadata().ok()?.modified().ok()?;
    let mut header = [0u8; 12];
    let n = File::open(entry.path()).ok()?.read(&mut header).ok()?;
    looks_like_image(&header[..n]).then(|| (mtime, entry.path()))
}

impl DirScanner {
    /// The newest validated image in `dir`: symlinks are never followed and
    /// non-images (a sidecar .nfo, a partial write) cannot shadow the real
    /// newest cover.
    pub fn scan(&mut self, dir: &Path) -> Resolution {
        let Ok(dir_mtime) = fs::metadata(dir).and_then(|m| m.modified()) else {
            self.memo.remove(dir);
            return Resolution::Retryable("covers dir missing".into());
        };
        if let Some((mtime, path)) = self.memo.get(dir) {
            if *mtime == dir_mtime {
                if let Ok(content_id) = content_id(path) {
                    return Resolution::Ready {
                        path: path.clone(),
                        content_id,
                    };
                }
                self.memo.remove(dir); // cached file vanished
            }
        }
        self.scans += 1;
        let Ok(entries) = fs::read_dir(dir) else {
            self.memo.remove(dir);
            return Resolution::Retryable("covers dir unreadable".into());
        };
        // Strictly newer wins, so the first of equal mtimes is kept.
        let mut best: Option<(SystemTime, PathBuf)> = None;
        for c in entries.flatten().filter_map(|e| candidate(&e)) {
            if best.as_ref().is_none_or(|b| c.0 > b.0) {
                best = Some(c);
            }
        }
        let Some((_, newest)) = best else {
            self.memo.remove(dir);
            return Resolution::Retryable("no cover in dir yet".into()); // write may lag
        };
        self.memo
            .insert(dir.to_path_buf(), (dir_mtime, newest.clone()));
        ready(newest)
    }
}
