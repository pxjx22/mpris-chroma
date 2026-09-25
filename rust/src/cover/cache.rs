//! The downloaded-cover cache (port of `cover._publish` / `_evict` /
//! `_looks_like_image`, SEC-009).
//!
//! Only content with an accepted image signature is published; publishing is
//! atomic (private temp file in the same directory, then rename, which
//! replaces a planted symlink rather than writing through it); growth is
//! bounded by age, total bytes and entry count, least recently used first.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime};

use sha2::{Digest, Sha256};

/// Cache growth budget: generous for a laptop's playback history, far below
/// anything that fills a disk.
#[derive(Clone, Copy, Debug)]
pub struct CacheLimits {
    pub max_bytes: u64,
    pub max_entries: usize,
    pub max_age: Duration,
}

pub const CACHE_LIMITS: CacheLimits = CacheLimits {
    max_bytes: 128 * 1024 * 1024,
    max_entries: 512,
    max_age: Duration::from_secs(30 * 24 * 60 * 60),
};

pub fn default_cache_dir() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    home.join(".cache/mpris-chroma/covers")
}

/// Where a URL's cover lives: sha256 of the URL, so no URL text reaches the
/// filesystem.
pub fn cache_path(cache_dir: &Path, url: &str) -> PathBuf {
    let digest = Sha256::digest(url.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    cache_dir.join(format!("{hex}.img"))
}

/// True only if `data` begins with a JPEG, PNG or WebP signature: the same
/// formats the decoder accepts, as a cheap pre-publication sniff (the
/// decoder still fully revalidates).
pub fn looks_like_image(data: &[u8]) -> bool {
    data.starts_with(b"\xff\xd8\xff")
        || data.starts_with(b"\x89PNG\r\n\x1a\n")
        || (data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP")
}

/// A regular file that is not a symlink (a planted symlink is a miss).
pub fn is_real_file(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_file())
}

static TEMP_SEQ: AtomicU64 = AtomicU64::new(0);

/// Create a private (0600), exclusive temp file next to `dest`.
fn create_temp(dir: &Path) -> io::Result<(PathBuf, fs::File)> {
    loop {
        let nanos = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map_or(0, |d| d.subsec_nanos());
        let seq = TEMP_SEQ.fetch_add(1, Ordering::Relaxed);
        let name = format!(".tmp-{}-{seq}-{nanos:08x}.img", std::process::id());
        let path = dir.join(name);
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
        {
            Ok(f) => return Ok((path, f)),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
}

/// Atomically publish `data` at `dest`: a reader never sees a partial file,
/// a crash cannot leave a published partial entry, and the temp file is
/// removed on every failure path.
///
/// The data is synced before the rename. Without that, a crash after the
/// rename can leave a zero-length entry under the published name (delayed
/// allocation), which the cache would then serve as a hit for every play of
/// that URL. The directory is synced after, best-effort, so the rename
/// itself survives a crash. (The Python syncs neither.)
pub fn publish(dest: &Path, data: &[u8]) -> io::Result<()> {
    let dir = dest.parent().unwrap_or(Path::new("."));
    let (tmp, mut file) = create_temp(dir)?;
    let result = file
        .write_all(data)
        .and_then(|()| file.sync_all())
        .and_then(|()| {
            drop(file);
            fs::rename(&tmp, dest)
        });
    match &result {
        Ok(()) => {
            if let Ok(d) = fs::File::open(dir) {
                let _ = d.sync_all();
            }
        }
        Err(_) => {
            let _ = fs::remove_file(&tmp);
        }
    }
    result
}

/// Bound cache growth: remove entries older than `max_age`, then evict least
/// recently used until under both budgets. Best-effort and race-tolerant: a
/// file vanishing underneath is ignored. Dotfiles (temps) and symlinks are
/// never considered, so eviction never follows a link.
pub fn evict(cache_dir: &Path, now: SystemTime, limits: CacheLimits) {
    let Ok(dir) = fs::read_dir(cache_dir) else {
        return;
    };
    let mut entries: Vec<(SystemTime, u64, PathBuf)> = Vec::new();
    for entry in dir.flatten() {
        if entry.file_name().to_string_lossy().starts_with('.') {
            continue;
        }
        // DirEntry::metadata does not follow symlinks.
        let Ok(meta) = entry.metadata() else { continue };
        if meta.file_type().is_symlink() {
            continue;
        }
        let Ok(mtime) = meta.modified() else { continue };
        let age = now.duration_since(mtime).unwrap_or_default();
        if age > limits.max_age {
            let _ = fs::remove_file(entry.path());
        } else {
            entries.push((mtime, meta.len(), entry.path()));
        }
    }
    entries.sort(); // oldest (least recently used) first
    let mut total: u64 = entries.iter().map(|e| e.1).sum();
    let mut count = entries.len();
    for (_, size, path) in entries {
        if total <= limits.max_bytes && count <= limits.max_entries {
            break;
        }
        let _ = fs::remove_file(path);
        total -= size;
        count -= 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cover::tests::{PNG, TmpDir, set_mtime};

    fn temps(dir: &Path) -> Vec<PathBuf> {
        fs::read_dir(dir)
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with(".tmp-")
            })
            .collect()
    }

    #[test]
    fn signatures() {
        assert!(looks_like_image(b"\xff\xd8\xff\xe0JFIF"));
        assert!(looks_like_image(b"\x89PNG\r\n\x1a\ndata"));
        assert!(looks_like_image(b"RIFF\x00\x00\x00\x00WEBPvp"));
        for blob in [
            &b"<html>"[..],
            b"%PDF-1.4",
            b"GIF89a...",
            b"",
            b"just text",
            b"RIFF\0\0\0\0WAVE",
        ] {
            assert!(!looks_like_image(blob), "{blob:?}");
        }
    }

    #[test]
    fn cache_path_is_the_url_digest() {
        let p = cache_path(Path::new("/c"), "https://i.scdn.co/image/abc");
        let name = p.file_name().unwrap().to_string_lossy().into_owned();
        assert_eq!(name.len(), 64 + 4);
        assert!(name.ends_with(".img") && !name.contains("scdn"));
    }

    #[test]
    fn publish_writes_atomically_and_privately() {
        use std::os::unix::fs::PermissionsExt;
        let t = TmpDir::new("pub");
        let dest = t.0.join("abc.img");
        publish(&dest, PNG).unwrap();
        assert!(is_real_file(&dest));
        assert_eq!(fs::read(&dest).unwrap(), PNG);
        assert_eq!(
            fs::metadata(&dest).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert!(temps(&t.0).is_empty());
    }

    #[test]
    fn publish_failure_leaves_no_hit_or_temp() {
        // Renaming a file onto a non-empty directory fails.
        let t = TmpDir::new("pubfail");
        let dest = t.0.join("abc.img");
        fs::create_dir(&dest).unwrap();
        fs::write(dest.join("keep"), b"x").unwrap();
        assert!(publish(&dest, PNG).is_err());
        assert!(!is_real_file(&dest));
        assert!(temps(&t.0).is_empty());
    }

    #[test]
    fn publish_over_a_symlink_replaces_it_and_leaves_the_target_untouched() {
        let t = TmpDir::new("pubsym");
        let target = t.0.join("target.txt");
        fs::write(&target, b"ORIGINAL").unwrap();
        let dest = t.0.join("planted.img");
        std::os::unix::fs::symlink(&target, &dest).unwrap();
        publish(&dest, PNG).unwrap();
        assert!(is_real_file(&dest));
        assert_eq!(fs::read(&dest).unwrap(), PNG);
        assert_eq!(fs::read(&target).unwrap(), b"ORIGINAL");
        assert!(temps(&t.0).is_empty());
    }

    #[test]
    fn republish_overwrites_atomically() {
        let t = TmpDir::new("repub");
        let dest = t.0.join("abc.img");
        publish(&dest, PNG).unwrap();
        publish(&dest, b"\x89PNG\r\n\x1a\nSECOND").unwrap();
        assert_eq!(fs::read(&dest).unwrap(), b"\x89PNG\r\n\x1a\nSECOND");
        assert!(temps(&t.0).is_empty());
    }

    #[test]
    fn aged_entries_are_removed() {
        let t = TmpDir::new("age");
        let (old, new) = (t.0.join("old.img"), t.0.join("new.img"));
        fs::write(&old, PNG).unwrap();
        fs::write(&new, PNG).unwrap();
        set_mtime(&old, 1);
        evict(&t.0, SystemTime::now(), CACHE_LIMITS);
        assert!(!old.exists());
        assert!(new.exists());
    }

    #[test]
    fn total_size_is_bounded_lru() {
        let t = TmpDir::new("lru");
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1000);
        for i in 0..5u8 {
            let p = t.0.join(format!("f{i}.img"));
            let mut data = b"\x89PNG\r\n\x1a\n".to_vec();
            data.extend([i; 200]);
            fs::write(&p, data).unwrap();
            set_mtime(&p, i as u64 + 1); // f0 oldest ... f4 newest
        }
        let limits = CacheLimits {
            max_bytes: 500,
            ..CACHE_LIMITS
        };
        evict(&t.0, now, limits);
        assert!(t.0.join("f4.img").exists());
        assert!(!t.0.join("f0.img").exists());
        let total: u64 = fs::read_dir(&t.0)
            .unwrap()
            .flatten()
            .map(|e| e.metadata().unwrap().len())
            .sum();
        assert!(total <= 500);
    }

    #[test]
    fn entry_count_is_bounded_and_symlinks_and_temps_are_left_alone() {
        let t = TmpDir::new("count");
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1000);
        for i in 0..4 {
            let p = t.0.join(format!("f{i}.img"));
            fs::write(&p, PNG).unwrap();
            set_mtime(&p, i + 1);
        }
        let outside = t.0.join("outside.txt");
        fs::write(&outside, b"x").unwrap();
        set_mtime(&outside, 500);
        std::os::unix::fs::symlink(&outside, t.0.join("link.img")).unwrap();
        fs::write(t.0.join(".tmp-inflight.img"), PNG).unwrap();
        let limits = CacheLimits {
            max_entries: 3, // the 4 covers + outside.txt = 5 entries
            ..CACHE_LIMITS
        };
        evict(&t.0, now, limits);
        assert!(!t.0.join("f0.img").exists() && !t.0.join("f1.img").exists());
        assert!(t.0.join("f3.img").exists());
        assert!(
            t.0.join("link.img").symlink_metadata().is_ok(),
            "symlink not evicted"
        );
        assert!(t.0.join(".tmp-inflight.img").exists(), "temp not evicted");
    }
}
