//! Cover resolution (port of `cover.py`, SEC-002/003/004/009/012/015/018).
//!
//! [`CoverResolver::resolve`] turns a player's art URL into a local file or a
//! typed refusal:
//!
//! - `file://` -> a file confined beneath the player's covers dir ([`local`]);
//! - `http(s)://` -> a cached download, fetched once per URL ([`fetch`],
//!   [`cache`]), under the destination policy ([`policy`]);
//! - anything else, with a covers dir -> the newest image in it.
//!
//! Expected failures are values ([`Resolution`]), classified transient vs
//! policy here so the worker never interprets them. A panic (a bug) is not
//! caught here; the worker's backstop reports it.

pub mod cache;
pub mod fetch;
pub mod local;
pub mod policy;

use std::collections::HashMap;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use url::Url;

use cache::{CACHE_LIMITS, CacheLimits};
use local::DirScanner;

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

/// What resolving a cover produced.
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

/// A contained failure while fetching or storing a remote cover.
#[derive(Debug)]
pub enum CoverError {
    /// Refused by destination policy.
    Rejected(String),
    /// Declared or actual size over the cap.
    TooLarge,
    /// The total transfer deadline (or a network timeout) elapsed.
    Timeout,
    /// Shutdown asked the download to stop.
    Aborted,
    /// Network, HTTP or filesystem failure.
    Io(String),
}

impl From<io::Error> for CoverError {
    fn from(e: io::Error) -> Self {
        if e.kind() == io::ErrorKind::TimedOut {
            CoverError::Timeout
        } else {
            CoverError::Io(e.to_string())
        }
    }
}

impl CoverError {
    fn name(&self) -> &'static str {
        match self {
            Self::Rejected(_) => "CoverRejected",
            Self::TooLarge => "CoverTooLarge",
            Self::Timeout => "CoverTimeout",
            Self::Aborted => "CoverAborted",
            Self::Io(_) => "OSError",
        }
    }

    /// Policy/content refusals the same metadata will reproduce are
    /// Rejected; everything else is transient.
    pub fn classify(&self) -> Resolution {
        match self {
            Self::Rejected(_) | Self::TooLarge => Resolution::Rejected(self.name().into()),
            _ => Resolution::Retryable(self.name().into()),
        }
    }
}

impl fmt::Display for CoverError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Rejected(why) | Self::Io(why) => write!(f, "{}: {why}", self.name()),
            _ => f.write_str(self.name()),
        }
    }
}

/// Downloads a remote cover's bytes, applying the destination policy.
pub trait Fetch {
    fn fetch(&mut self, url: &str, should_stop: &dyn Fn() -> bool) -> Result<Vec<u8>, CoverError>;
}

/// The production fetcher: allowlisted domains, the system resolver, and the
/// pinned HTTPS transport.
pub struct HttpFetch {
    pub domains: Vec<String>,
    pub dns: Box<dyn policy::Dns + Send>,
    pub transport: Box<dyn fetch::Transport + Send>,
}

impl HttpFetch {
    pub fn from_env() -> Self {
        HttpFetch {
            domains: policy::art_domains(),
            dns: Box::new(policy::SystemDns {
                timeout: fetch::DOWNLOAD_TIMEOUT,
            }),
            transport: Box::new(fetch::UreqTransport),
        }
    }
}

impl Fetch for HttpFetch {
    fn fetch(&mut self, url: &str, should_stop: &dyn Fn() -> bool) -> Result<Vec<u8>, CoverError> {
        fetch::fetch(
            url,
            fetch::FetchEnv {
                domains: &self.domains,
                dns: self.dns.as_ref(),
                transport: self.transport.as_mut(),
                now: &Instant::now,
                should_stop,
            },
        )
    }
}

/// At most one warning per key per interval, so a repeatedly failing source
/// cannot flood the journal.
pub const LOG_INTERVAL: Duration = Duration::from_secs(60);

type Warn = Box<dyn FnMut(String) + Send>;

/// Resolves covers, owning the cache location, the fetcher, the dir-scan
/// memo and the failure-log limiter (module globals in the Python).
pub struct CoverResolver<F = HttpFetch> {
    pub cache_dir: PathBuf,
    pub cache_limits: CacheLimits,
    fetcher: F,
    scanner: DirScanner,
    last_logged: HashMap<String, Instant>,
    warn: Warn,
}

impl CoverResolver {
    /// Production wiring: `~/.cache/mpris-chroma/covers`, the real fetcher.
    pub fn from_env() -> Self {
        Self::new(cache::default_cache_dir(), HttpFetch::from_env())
    }
}

impl<F: Fetch> CoverResolver<F> {
    pub fn new(cache_dir: PathBuf, fetcher: F) -> Self {
        CoverResolver {
            cache_dir,
            cache_limits: CACHE_LIMITS,
            fetcher,
            scanner: DirScanner::default(),
            last_logged: HashMap::new(),
            warn: Box::new(|m| log::warn!("{m}")),
        }
    }

    /// Route warnings somewhere else (tests count them).
    pub fn with_warn(mut self, warn: impl FnMut(String) + Send + 'static) -> Self {
        self.warn = Box::new(warn);
        self
    }

    pub fn fetcher_mut(&mut self) -> &mut F {
        &mut self.fetcher
    }

    pub fn scanner(&self) -> &DirScanner {
        &self.scanner
    }

    fn log_failure(&mut self, key: &str, message: String) {
        let now = Instant::now();
        let due = self
            .last_logged
            .get(key)
            .is_none_or(|t| now.duration_since(*t) >= LOG_INTERVAL);
        if due {
            self.last_logged.insert(key.to_string(), now);
            (self.warn)(message);
        }
    }

    /// Resolve the current cover. `should_stop` is polled during a download
    /// so an in-flight fetch aborts at shutdown.
    pub fn resolve(
        &mut self,
        art_url: &str,
        covers_dir: Option<&Path>,
        should_stop: &dyn Fn() -> bool,
    ) -> Resolution {
        if art_url.starts_with("file://") {
            // Authoritative: a confined hit or a typed refusal. It does not
            // fall through to the dir scan, which would re-admit a symlink the
            // confinement just rejected.
            return local::resolve_file(art_url, covers_dir);
        }
        if art_url.starts_with("http://") || art_url.starts_with("https://") {
            return self.resolve_remote(art_url, should_stop);
        }
        match covers_dir {
            Some(dir) => self.scanner.scan(dir),
            None => Resolution::Rejected("no art source".into()),
        }
    }

    fn resolve_remote(&mut self, url: &str, should_stop: &dyn Fn() -> bool) -> Resolution {
        let dest = cache::cache_path(&self.cache_dir, url);
        // Serve only a real regular file, never a planted symlink; a symlink
        // or partial entry is a miss and is atomically replaced below.
        if cache::is_real_file(&dest) {
            if let Ok(content_id) = content_id(&dest) {
                return Resolution::Ready {
                    path: dest,
                    content_id,
                };
            }
        }
        let data = match self.fetcher.fetch(url, should_stop) {
            Ok(data) => data,
            Err(e) => return self.contain(url, e),
        };
        if !cache::looks_like_image(&data) {
            // Not artwork, and the same URL will say the same again.
            return Resolution::Rejected("empty or non-image body".into());
        }
        if let Err(e) = self.store(&dest, &data) {
            return self.contain(url, CoverError::from(e));
        }
        match content_id(&dest) {
            Ok(content_id) => Resolution::Ready {
                path: dest,
                content_id,
            },
            Err(e) => self.contain(url, CoverError::from(e)),
        }
    }

    fn store(&self, dest: &Path, data: &[u8]) -> io::Result<()> {
        use std::os::unix::fs::DirBuilderExt;
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.cache_dir)?;
        cache::publish(dest, data)?;
        cache::evict(&self.cache_dir, SystemTime::now(), self.cache_limits);
        Ok(())
    }

    /// Log (hostname only, so URL userinfo and query secrets never reach the
    /// journal) and classify a contained failure.
    fn contain(&mut self, url: &str, e: CoverError) -> Resolution {
        let host = Url::parse(url)
            .ok()
            .and_then(|u| u.host_str().map(String::from))
            .unwrap_or_else(|| "?".into());
        self.log_failure(
            &host,
            format!("cover fetch failed for {host}: {}", e.name()),
        );
        e.classify()
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::fs;
    use std::sync::{Arc, Mutex};

    /// A valid PNG *signature* plus filler: cover resolution sniffs the
    /// signature, not full decodability.
    pub(crate) const PNG: &[u8] = b"\x89PNG\r\n\x1a\n\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0";

    pub(crate) struct TmpDir(pub PathBuf);
    impl TmpDir {
        pub(crate) fn new(tag: &str) -> Self {
            static N: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
            let n = N.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let dir = std::env::temp_dir().join(format!(
                "mpris-chroma-cover-{tag}-{}-{n}",
                std::process::id()
            ));
            let _ = fs::remove_dir_all(&dir);
            fs::create_dir_all(&dir).unwrap();
            TmpDir(dir)
        }
    }
    impl Drop for TmpDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    pub(crate) fn set_mtime(path: &Path, secs: u64) {
        let t = UNIX_EPOCH + Duration::from_secs(secs);
        let f = fs::File::options().write(true).open(path).unwrap();
        f.set_modified(t).unwrap();
    }

    fn set_dir_mtime(dir: &Path, t: SystemTime) {
        fs::File::open(dir).unwrap().set_modified(t).unwrap();
    }

    /// Scripted fetch results; records (url, should_stop()) per call.
    #[derive(Default)]
    struct FakeFetch {
        replies: Vec<Result<Vec<u8>, CoverError>>,
        calls: Vec<(String, bool)>,
    }
    impl Fetch for FakeFetch {
        fn fetch(
            &mut self,
            url: &str,
            should_stop: &dyn Fn() -> bool,
        ) -> Result<Vec<u8>, CoverError> {
            self.calls.push((url.to_string(), should_stop()));
            assert!(!self.replies.is_empty(), "unexpected fetch of {url}");
            self.replies.remove(0)
        }
    }

    fn resolver(
        cache: &Path,
        replies: Vec<Result<Vec<u8>, CoverError>>,
    ) -> CoverResolver<FakeFetch> {
        CoverResolver::new(
            cache.to_path_buf(),
            FakeFetch {
                replies,
                calls: vec![],
            },
        )
    }

    fn no_stop() -> bool {
        false
    }

    fn path_of(r: Resolution) -> PathBuf {
        match r {
            Resolution::Ready { path, .. } => path,
            other => panic!("expected Ready, got {other:?}"),
        }
    }

    fn is_retryable(r: &Resolution) -> bool {
        matches!(r, Resolution::Retryable(_))
    }

    fn is_rejected(r: &Resolution) -> bool {
        matches!(r, Resolution::Rejected(_))
    }

    const URL: &str = "https://i.scdn.co/image/abc123";

    // --- content id / classification -------------------------------------

    #[test]
    fn content_id_is_size_and_mtime_ns() {
        use std::os::unix::fs::MetadataExt;
        let t = TmpDir::new("cid");
        let p = t.0.join("c.img");
        fs::write(&p, b"hello").unwrap();
        let st = fs::metadata(&p).unwrap();
        let ns = st.mtime() as i128 * 1_000_000_000 + st.mtime_nsec() as i128;
        assert_eq!(content_id(&p).unwrap(), (5, ns));
    }

    #[test]
    fn classification_splits_policy_from_transient() {
        assert!(is_rejected(&CoverError::Rejected("ssrf".into()).classify()));
        assert!(is_rejected(&CoverError::TooLarge.classify()));
        assert!(is_retryable(&CoverError::Timeout.classify()));
        assert!(is_retryable(&CoverError::Aborted.classify()));
        assert!(is_retryable(&CoverError::Io("network".into()).classify()));
    }

    // --- http(s) -----------------------------------------------------------

    #[test]
    fn cache_miss_downloads_and_writes() {
        let t = TmpDir::new("miss");
        let mut r = resolver(&t.0.join("covers"), vec![Ok(PNG.to_vec())]);
        let path = path_of(r.resolve(URL, None, &no_stop));
        assert_eq!(fs::read(&path).unwrap(), PNG);
        assert_eq!(r.fetcher_mut().calls, [(URL.to_string(), false)]);
    }

    #[test]
    fn cache_dir_is_created_private() {
        use std::os::unix::fs::PermissionsExt;
        let t = TmpDir::new("mode");
        let cache = t.0.join("covers");
        let mut r = resolver(&cache, vec![Ok(PNG.to_vec())]);
        r.resolve(URL, None, &no_stop);
        assert_eq!(
            fs::metadata(&cache).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }

    #[test]
    fn cache_hit_does_not_refetch() {
        let t = TmpDir::new("hit");
        let mut r = resolver(&t.0, vec![Ok(PNG.to_vec())]);
        let first = path_of(r.resolve(URL, None, &no_stop));
        let second = path_of(r.resolve(URL, None, &no_stop)); // would panic on a fetch
        assert_eq!(first, second);
    }

    #[test]
    fn fetch_failure_is_retryable() {
        let t = TmpDir::new("fail");
        let mut r = resolver(&t.0, vec![Err(CoverError::Io("network".into()))]);
        assert!(is_retryable(&r.resolve(URL, None, &no_stop)));
    }

    #[test]
    fn policy_failures_are_rejected() {
        let t = TmpDir::new("policy");
        let mut r = resolver(
            &t.0,
            vec![
                Err(CoverError::TooLarge),
                Err(CoverError::Rejected("ssrf".into())),
            ],
        );
        assert!(is_rejected(&r.resolve(URL, None, &no_stop)));
        assert!(is_rejected(&r.resolve(URL, None, &no_stop)));
    }

    #[test]
    fn empty_or_non_image_body_is_rejected_and_not_cached() {
        let t = TmpDir::new("nonimg");
        let mut r = resolver(&t.0, vec![Ok(vec![]), Ok(b"<html>nope</html>".to_vec())]);
        assert!(is_rejected(&r.resolve(URL, None, &no_stop)));
        assert!(is_rejected(&r.resolve(URL, None, &no_stop)));
        assert!(!cache::cache_path(&t.0, URL).exists());
    }

    #[test]
    fn publish_failure_is_retryable() {
        let t = TmpDir::new("pubfail");
        let dest = cache::cache_path(&t.0, URL);
        fs::create_dir_all(dest.join("blocker")).unwrap(); // rename onto it fails
        let mut r = resolver(&t.0, vec![Ok(PNG.to_vec())]);
        assert!(is_retryable(&r.resolve(URL, None, &no_stop)));
    }

    #[test]
    fn should_stop_is_forwarded_to_the_fetch() {
        let t = TmpDir::new("stop");
        let mut r = resolver(&t.0, vec![Ok(PNG.to_vec())]);
        r.resolve(URL, None, &|| true);
        assert_eq!(r.fetcher_mut().calls, [(URL.to_string(), true)]);
    }

    #[test]
    fn no_art_source_is_rejected() {
        let t = TmpDir::new("noart");
        assert!(is_rejected(
            &resolver(&t.0, vec![]).resolve("", None, &no_stop)
        ));
    }

    #[test]
    #[should_panic(expected = "bug")]
    fn a_panic_in_the_fetch_propagates() {
        // A bug is not an operational failure: it must reach the worker's
        // backstop rather than masquerade as "no cover".
        struct Buggy;
        impl Fetch for Buggy {
            fn fetch(&mut self, _: &str, _: &dyn Fn() -> bool) -> Result<Vec<u8>, CoverError> {
                panic!("bug")
            }
        }
        let t = TmpDir::new("bug");
        CoverResolver::new(t.0.clone(), Buggy).resolve(URL, None, &no_stop);
    }

    #[test]
    fn repeated_failures_are_rate_limited_and_log_only_the_host() {
        let t = TmpDir::new("ratelimit");
        let logged = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&logged);
        let url = "https://user:secret@i.scdn.co/image/x?token=abc";
        let mut r = resolver(
            &t.0,
            vec![
                Err(CoverError::Io("net".into())),
                Err(CoverError::Io("net".into())),
            ],
        )
        .with_warn(move |m| sink.lock().unwrap().push(m));
        r.resolve(url, None, &no_stop);
        r.resolve(url, None, &no_stop);
        let logged = logged.lock().unwrap();
        assert_eq!(logged.len(), 1);
        assert!(logged[0].contains("i.scdn.co"));
        assert!(!logged[0].contains("secret") && !logged[0].contains("token"));
    }

    #[test]
    fn a_planted_symlink_is_not_served_and_its_target_is_untouched() {
        let t = TmpDir::new("symserve");
        let outside = t.0.join("outside.txt");
        fs::write(&outside, b"SECRET").unwrap();
        let dest = cache::cache_path(&t.0, URL);
        std::os::unix::fs::symlink(&outside, &dest).unwrap();
        let mut r = resolver(&t.0, vec![Ok(PNG.to_vec())]);
        let path = path_of(r.resolve(URL, None, &no_stop));
        assert!(cache::is_real_file(&path));
        assert_eq!(fs::read(&path).unwrap(), PNG);
        assert_eq!(fs::read(&outside).unwrap(), b"SECRET");
    }

    // --- file:// confinement (SEC-004) ------------------------------------

    fn file_setup(tag: &str) -> (TmpDir, PathBuf, PathBuf) {
        let t = TmpDir::new(tag);
        let root = t.0.join("covers");
        fs::create_dir(&root).unwrap();
        let img = root.join("art.jpg");
        fs::write(&img, b"x").unwrap();
        (t, root, img)
    }

    fn resolve_file(url: &str, root: Option<&Path>) -> Resolution {
        let t = TmpDir::new("filecache");
        resolver(&t.0, vec![]).resolve(url, root, &no_stop)
    }

    #[test]
    fn file_url_inside_root_is_used() {
        let (_t, root, img) = file_setup("inside");
        let want = fs::canonicalize(&img).unwrap();
        assert_eq!(
            path_of(resolve_file(
                &format!("file://{}", img.display()),
                Some(&root)
            )),
            want
        );
        let localhost = format!("file://localhost{}", img.display());
        assert_eq!(path_of(resolve_file(&localhost, Some(&root))), want);
    }

    #[test]
    fn file_url_outside_root_or_with_remote_authority_is_rejected() {
        let (t, root, img) = file_setup("outside");
        let outside = t.0.join("secret.txt");
        fs::write(&outside, b"x").unwrap();
        for url in [
            format!("file://{}", outside.display()),
            "file:///etc/passwd".to_string(),
            format!("file://remote-host{}", img.display()),
            format!("file://{}/../secret.txt", root.display()),
        ] {
            assert!(is_rejected(&resolve_file(&url, Some(&root))), "{url}");
        }
    }

    #[test]
    fn symlink_escaping_the_root_is_rejected() {
        let (t, root, _) = file_setup("symesc");
        let outside = t.0.join("secret.txt");
        fs::write(&outside, b"x").unwrap();
        let link = root.join("link.jpg");
        std::os::unix::fs::symlink(&outside, &link).unwrap();
        assert!(is_rejected(&resolve_file(
            &format!("file://{}", link.display()),
            Some(&root)
        )));
    }

    #[test]
    fn file_url_without_a_root_is_rejected_and_a_missing_file_is_retryable() {
        let (_t, root, img) = file_setup("noroot");
        let url = format!("file://{}", img.display());
        assert!(is_rejected(&resolve_file(&url, None)));
        let missing = format!("file://{}/later.jpg", root.display());
        assert!(is_retryable(&resolve_file(&missing, Some(&root))));
    }

    #[test]
    fn percent_encoded_file_paths_are_decoded() {
        let (_t, root, _) = file_setup("pct");
        let img = root.join("a b.jpg");
        fs::write(&img, b"x").unwrap();
        let url = format!("file://{}/a%20b.jpg", root.display());
        assert_eq!(
            path_of(resolve_file(&url, Some(&root))),
            fs::canonicalize(&img).unwrap()
        );
    }

    // --- covers-dir scan (SEC-012, PERF-003) ------------------------------

    fn scan(dir: &Path) -> (Resolution, CoverResolver<FakeFetch>) {
        let t = TmpDir::new("scancache");
        let mut r = resolver(&t.0, vec![]);
        (r.resolve("", Some(dir), &no_stop), r)
    }

    #[test]
    fn non_url_art_falls_back_to_the_newest_cover() {
        let t = TmpDir::new("newest");
        let (old, new) = (t.0.join("old.jpeg"), t.0.join("new.jpeg"));
        fs::write(&old, PNG).unwrap();
        fs::write(&new, PNG).unwrap();
        set_mtime(&old, 1);
        let mut r = resolver(&t.0.join("unused"), vec![]);
        let got = r.resolve("jellyfin:Items/abc/Images/Primary", Some(&t.0), &no_stop);
        assert_eq!(path_of(got), new);
    }

    #[test]
    fn empty_missing_or_unreadable_covers_dir_is_retryable() {
        let t = TmpDir::new("emptydir");
        assert!(is_retryable(&scan(&t.0).0));
        assert!(is_retryable(&scan(&t.0.join("nope")).0));
        let not_a_dir = t.0.join("file");
        fs::write(&not_a_dir, b"x").unwrap();
        assert!(is_retryable(&scan(&not_a_dir).0)); // stat ok, listing fails
    }

    #[test]
    fn newer_non_image_file_does_not_replace_the_cover() {
        let t = TmpDir::new("nfo");
        fs::write(t.0.join("cover.nfo"), b"not an image").unwrap();
        set_mtime(&t.0.join("cover.nfo"), 100);
        let real = t.0.join("art.jpeg");
        fs::write(&real, PNG).unwrap();
        set_mtime(&real, 1);
        assert_eq!(path_of(scan(&t.0).0), real);
    }

    #[test]
    fn symlinked_covers_and_non_images_are_never_selected() {
        let t = TmpDir::new("symscan");
        let outside = TmpDir::new("symscan-out");
        let target = outside.0.join("outside.jpeg");
        fs::write(&target, PNG).unwrap();
        std::os::unix::fs::symlink(&target, t.0.join("link.jpeg")).unwrap();
        fs::write(t.0.join("readme.txt"), b"not an image").unwrap();
        assert!(is_retryable(&scan(&t.0).0));
    }

    #[test]
    fn an_unchanged_directory_is_not_rescanned() {
        let t = TmpDir::new("memo");
        let art = t.0.join("art.jpeg");
        fs::write(&art, PNG).unwrap();
        let mut r = resolver(&t.0.join("unused"), vec![]);
        r.resolve("", Some(&t.0), &no_stop);
        let dir_mtime = fs::metadata(&t.0).unwrap().modified().unwrap();
        // A newer cover appears but the directory mtime is put back, so only
        // a rescan could find it.
        let sneaky = t.0.join("sneaky.jpeg");
        fs::write(&sneaky, PNG).unwrap();
        set_mtime(&sneaky, 4_000_000_000);
        set_dir_mtime(&t.0, dir_mtime);
        assert_eq!(path_of(r.resolve("", Some(&t.0), &no_stop)), art);
        assert_eq!(r.scanner().scans, 1);
    }

    #[test]
    fn a_new_file_invalidates_the_memo() {
        let t = TmpDir::new("memo2");
        let old = t.0.join("old.jpeg");
        fs::write(&old, PNG).unwrap();
        set_mtime(&old, 1);
        let mut r = resolver(&t.0.join("unused"), vec![]);
        r.resolve("", Some(&t.0), &no_stop);
        set_dir_mtime(&t.0, UNIX_EPOCH + Duration::from_secs(1)); // then a real change
        let new = t.0.join("new.jpeg");
        fs::write(&new, PNG).unwrap();
        assert_eq!(path_of(r.resolve("", Some(&t.0), &no_stop)), new);
        assert_eq!(r.scanner().scans, 2);
    }

    // --- worker + real resolver identity (from test_worker.py) ------------

    struct RealStages {
        resolver: CoverResolver<FakeFetch>,
        extracted: usize,
    }
    impl crate::worker::Stages for RealStages {
        fn resolve(&mut self, art_url: &str, covers_dir: Option<&Path>) -> Resolution {
            self.resolver.resolve(art_url, covers_dir, &no_stop)
        }
        fn extract(&mut self, _: &Path, _: crate::state::Mode, _: ContentId) -> [String; 3] {
            self.extracted += 1;
            ["#111111", "#222222", "#333333"].map(String::from)
        }
        fn apply(&mut self, _: &[String; 3]) -> Result<(), crate::apply::CtlError> {
            Ok(())
        }
        fn revert(&mut self) -> Result<(), crate::apply::CtlError> {
            Ok(())
        }
    }

    fn dir_job(generation: u64, dir: &Path) -> crate::worker::Job {
        crate::worker::Job {
            generation,
            desired: crate::worker::Desired {
                target: Some(crate::worker::CoverTarget {
                    art_url: String::new(),
                    covers_dir: Some(dir.to_path_buf()),
                }),
                mode: crate::state::Mode::Dark,
            },
        }
    }

    fn real_worker(cache: &Path) -> crate::worker::Worker<RealStages> {
        crate::worker::Worker::new(
            Arc::new(crate::worker::Mailbox::new()),
            RealStages {
                resolver: resolver(cache, vec![]),
                extracted: 0,
            },
            |_| {},
            None,
        )
    }

    #[test]
    fn a_file_overwritten_in_place_is_re_extracted() {
        use crate::worker::Outcome::*;
        let t = TmpDir::new("overwrite");
        let img = t.0.join("cover.jpg");
        fs::write(&img, PNG).unwrap();
        let mut w = real_worker(&t.0.join("unused"));
        let r1 = w.run_once(dir_job(1, &t.0)).unwrap();
        let mut longer = PNG.to_vec();
        longer.extend([0u8; 64]);
        fs::write(&img, longer).unwrap(); // same path, new size
        set_mtime(&img, 1); // and a distinct mtime
        let r2 = w.run_once(dir_job(2, &t.0)).unwrap();
        assert_eq!((r1.outcome, r2.outcome), (Committed, Committed));
    }

    #[test]
    fn an_unchanged_file_is_a_skipped_duplicate() {
        use crate::worker::Outcome::*;
        let t = TmpDir::new("unchanged");
        fs::write(t.0.join("cover.jpg"), PNG).unwrap();
        let mut w = real_worker(&t.0.join("unused"));
        let r1 = w.run_once(dir_job(1, &t.0)).unwrap();
        let r2 = w.run_once(dir_job(2, &t.0)).unwrap();
        assert_eq!((r1.outcome, r2.outcome), (Committed, SkippedDuplicate));
    }
}
