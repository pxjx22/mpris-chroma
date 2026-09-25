//! Bounded, policy-checked artwork download (port of `cover._fetch` and the
//! validating redirect handler, SEC-001/002/003).
//!
//! Every hop — the first request and each redirect — goes through
//! [`check_destination`], and the transport connects only to the addresses
//! that check returned. The body is streamed under a byte cap and a total
//! deadline, with a stop flag polled per chunk so shutdown does not wait out
//! a download.

use std::io::Read;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use url::Url;

use super::CoverError;
use super::policy::{Dns, check_destination};

/// Per-network-operation timeout (connect, request, response head).
pub const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(5);
/// Total transfer deadline, independent of the per-operation timeout, so a
/// server cannot drip bytes forever while staying under it.
pub const DOWNLOAD_DEADLINE: Duration = Duration::from_secs(20);
/// Hard cap on compressed artwork.
pub const MAX_COVER_BYTES: u64 = 10 * 1024 * 1024;
/// urllib's redirect limit.
pub const MAX_REDIRECTS: usize = 10;
const CHUNK: usize = 64 * 1024;

/// One HTTP response, as far as the fetch loop needs it.
pub struct Response {
    pub status: u16,
    pub location: Option<String>,
    pub content_length: Option<String>,
    pub body: Box<dyn Read + Send>,
}

/// Performs one GET, connecting only to `addrs` (and verifying TLS against
/// the URL's host). No redirects, no proxy.
pub trait Transport {
    fn get(&mut self, url: &Url, addrs: &[SocketAddr]) -> Result<Response, CoverError>;
}

/// What a fetch needs besides the URL.
pub struct FetchEnv<'a> {
    pub domains: &'a [String],
    pub dns: &'a dyn Dns,
    pub transport: &'a mut dyn Transport,
    pub now: &'a dyn Fn() -> Instant,
    pub should_stop: &'a dyn Fn() -> bool,
}

/// Fetch `url`'s bytes: policy-checked and pinned on every hop, redirects
/// followed (301/302/303/307/308, at most [`MAX_REDIRECTS`]), body bounded.
pub fn fetch(url: &str, env: FetchEnv<'_>) -> Result<Vec<u8>, CoverError> {
    let deadline = (env.now)() + DOWNLOAD_DEADLINE;
    let mut url = Url::parse(url).map_err(|e| CoverError::Rejected(format!("bad url: {e}")))?;
    for _ in 0..=MAX_REDIRECTS {
        let addrs = check_destination(&url, env.domains, env.dns)?;
        if (env.now)() > deadline {
            return Err(CoverError::Timeout);
        }
        let resp = env.transport.get(&url, &addrs)?;
        match resp.status {
            200..=299 => return read_body(resp, deadline, env.now, env.should_stop),
            301 | 302 | 303 | 307 | 308 => {
                let loc = resp.location.ok_or_else(|| {
                    CoverError::Io(format!("HTTP {} without Location", resp.status))
                })?;
                url = url
                    .join(&loc)
                    .map_err(|e| CoverError::Rejected(format!("bad redirect: {e}")))?;
            }
            s => return Err(CoverError::Io(format!("HTTP {s}"))),
        }
    }
    Err(CoverError::Io("too many redirects".into()))
}

/// Stream a response body under the byte cap and the deadline. A declared
/// oversize is refused before any body is read; the cap is enforced against
/// actual bytes too, so peak memory is bounded by the cap.
pub fn read_body(
    resp: Response,
    deadline: Instant,
    now: &dyn Fn() -> Instant,
    should_stop: &dyn Fn() -> bool,
) -> Result<Vec<u8>, CoverError> {
    if let Some(declared) = resp.content_length.as_deref() {
        // A malformed header is ignored; the actual-byte check still applies.
        if declared
            .trim()
            .parse::<u64>()
            .is_ok_and(|n| n > MAX_COVER_BYTES)
        {
            return Err(CoverError::TooLarge);
        }
    }
    let mut body = resp.body;
    let mut out = Vec::new();
    let mut chunk = vec![0u8; CHUNK];
    loop {
        if should_stop() {
            return Err(CoverError::Aborted);
        }
        if now() > deadline {
            return Err(CoverError::Timeout);
        }
        let n = match body.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(CoverError::from(e)),
        };
        if out.len() as u64 + n as u64 > MAX_COVER_BYTES {
            return Err(CoverError::TooLarge);
        }
        out.extend_from_slice(&chunk[..n]);
    }
    Ok(out)
}

// --- the real transport --------------------------------------------------

/// Hands ureq exactly the pre-checked addresses, whatever it asks about.
#[derive(Debug)]
struct PinnedResolver(Arc<[SocketAddr]>);

impl ureq::unversioned::resolver::Resolver for PinnedResolver {
    fn resolve(
        &self,
        _uri: &ureq::http::Uri,
        _config: &ureq::config::Config,
        _timeout: ureq::unversioned::transport::NextTimeout,
    ) -> Result<ureq::unversioned::resolver::ResolvedSocketAddrs, ureq::Error> {
        let mut out = self.empty();
        for a in self.0.iter().take(16) {
            out.push(*a);
        }
        if out.is_empty() {
            return Err(ureq::Error::HostNotFound);
        }
        Ok(out)
    }
}

/// HTTPS over rustls with bundled Mozilla roots (webpki-roots). Proxies are
/// disabled explicitly: ureq otherwise honours `*_proxy` from the
/// environment, which would bypass the pinning.
#[derive(Default)]
pub struct UreqTransport;

impl Transport for UreqTransport {
    fn get(&mut self, url: &Url, addrs: &[SocketAddr]) -> Result<Response, CoverError> {
        let config = ureq::Agent::config_builder()
            .proxy(None)
            .max_redirects(0)
            .http_status_as_error(false)
            .timeout_connect(Some(DOWNLOAD_TIMEOUT))
            .timeout_send_request(Some(DOWNLOAD_TIMEOUT))
            .timeout_recv_response(Some(DOWNLOAD_TIMEOUT))
            .timeout_recv_body(Some(DOWNLOAD_DEADLINE))
            .timeout_global(Some(DOWNLOAD_DEADLINE + DOWNLOAD_TIMEOUT))
            .build();
        let agent = ureq::Agent::with_parts(
            config,
            ureq::unversioned::transport::DefaultConnector::default(),
            PinnedResolver(addrs.into()),
        );
        let resp = agent.get(url.as_str()).call().map_err(|e| match e {
            ureq::Error::Timeout(_) => CoverError::Timeout,
            other => CoverError::Io(other.to_string()),
        })?;
        let header = |name: &str| {
            resp.headers()
                .get(name)
                .and_then(|v| v.to_str().ok())
                .map(String::from)
        };
        let status = resp.status().as_u16();
        let location = header("location");
        let content_length = header("content-length");
        let body = Box::new(resp.into_body().into_reader());
        Ok(Response {
            status,
            location,
            content_length,
            body,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::super::policy::art_domains_from;
    use super::super::policy::tests::{FakeDns, GLOBAL};
    use super::*;
    use std::cell::Cell;
    use std::io::Cursor;

    /// A body that yields the given chunks one read at a time and records
    /// whether it was read at all.
    struct Chunks {
        chunks: Vec<Vec<u8>>,
        read: Arc<std::sync::atomic::AtomicBool>,
    }
    impl Read for Chunks {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            self.read.store(true, std::sync::atomic::Ordering::SeqCst);
            if self.chunks.is_empty() {
                return Ok(0);
            }
            let c = self.chunks.remove(0);
            buf[..c.len()].copy_from_slice(&c);
            Ok(c.len())
        }
    }

    fn body(chunks: Vec<&[u8]>) -> (Box<dyn Read + Send>, Arc<std::sync::atomic::AtomicBool>) {
        let read = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let b = Chunks {
            chunks: chunks.into_iter().map(|c| c.to_vec()).collect(),
            read: Arc::clone(&read),
        };
        (Box::new(b), read)
    }

    fn ok(body: Box<dyn Read + Send>, content_length: Option<&str>) -> Response {
        Response {
            status: 200,
            location: None,
            content_length: content_length.map(String::from),
            body,
        }
    }

    fn t0() -> Instant {
        Instant::now()
    }

    // --- body bounds (SEC-002) -------------------------------------------

    #[test]
    fn oversized_declared_length_is_refused_before_reading_the_body() {
        let (b, read) = body(vec![b"x"]);
        let declared = (MAX_COVER_BYTES + 1).to_string();
        let start = t0();
        let r = read_body(
            ok(b, Some(&declared)),
            start + DOWNLOAD_DEADLINE,
            &|| start,
            &|| false,
        );
        assert!(matches!(r, Err(CoverError::TooLarge)));
        assert!(!read.load(std::sync::atomic::Ordering::SeqCst));
    }

    #[test]
    fn malformed_declared_length_is_ignored() {
        let (b, _) = body(vec![b"IMG"]);
        let start = t0();
        let r = read_body(
            ok(b, Some("lots")),
            start + DOWNLOAD_DEADLINE,
            &|| start,
            &|| false,
        );
        assert_eq!(r.unwrap(), b"IMG");
    }

    #[test]
    fn undeclared_oversized_body_is_stopped_at_the_cap() {
        // One chunk short of the cap passes; the chunk that crosses it fails.
        let big = vec![0u8; CHUNK];
        let n = (MAX_COVER_BYTES as usize / CHUNK) + 1;
        let chunks: Vec<&[u8]> = (0..n).map(|_| big.as_slice()).collect();
        let (b, _) = body(chunks);
        let start = t0();
        let r = read_body(ok(b, None), start + DOWNLOAD_DEADLINE, &|| start, &|| false);
        assert!(matches!(r, Err(CoverError::TooLarge)));
    }

    #[test]
    fn total_deadline_aborts_a_slow_drip() {
        let (b, _) = body(vec![b"a"; 1000]);
        let start = t0();
        let ticks = Cell::new(0u32);
        let now = || {
            ticks.set(ticks.get() + 1);
            if ticks.get() > 1 {
                start + DOWNLOAD_DEADLINE + Duration::from_secs(1)
            } else {
                start
            }
        };
        let r = read_body(ok(b, None), start + DOWNLOAD_DEADLINE, &now, &|| false);
        assert!(matches!(r, Err(CoverError::Timeout)));
    }

    #[test]
    fn normal_image_is_returned_whole() {
        let (b, _) = body(vec![b"IMG", b"DATA"]);
        let start = t0();
        let r = read_body(
            ok(b, Some("7")),
            start + DOWNLOAD_DEADLINE,
            &|| start,
            &|| false,
        );
        assert_eq!(r.unwrap(), b"IMGDATA");
    }

    #[test]
    fn should_stop_aborts_mid_stream() {
        let (b, _) = body(vec![b"a"; 1000]);
        let start = t0();
        let checks = Cell::new(0);
        let stop = || {
            checks.set(checks.get() + 1);
            checks.get() > 1 // first chunk through, then abort
        };
        let r = read_body(ok(b, None), start + DOWNLOAD_DEADLINE, &|| start, &stop);
        assert!(matches!(r, Err(CoverError::Aborted)));
    }

    // --- fetch loop: policy on every hop ---------------------------------

    /// Scripted responses by URL; records every (url, pinned addrs) call.
    struct FakeTransport {
        routes: Vec<(&'static str, u16, Option<&'static str>, &'static [u8])>,
        calls: Vec<(String, Vec<SocketAddr>)>,
    }

    impl Transport for FakeTransport {
        fn get(&mut self, url: &Url, addrs: &[SocketAddr]) -> Result<Response, CoverError> {
            self.calls.push((url.to_string(), addrs.to_vec()));
            let (_, status, location, data) = *self
                .routes
                .iter()
                .find(|r| r.0 == url.as_str())
                .unwrap_or_else(|| panic!("unexpected request {url}"));
            Ok(Response {
                status,
                location: location.map(String::from),
                content_length: None,
                body: Box::new(Cursor::new(data.to_vec())),
            })
        }
    }

    fn run(url: &str, t: &mut FakeTransport, dns: &FakeDns) -> Result<Vec<u8>, CoverError> {
        let domains = art_domains_from(None);
        fetch(
            url,
            FetchEnv {
                domains: &domains,
                dns,
                transport: t,
                now: &Instant::now,
                should_stop: &|| false,
            },
        )
    }

    #[test]
    fn fetch_pins_the_transport_to_the_checked_addresses() {
        let mut t = FakeTransport {
            routes: vec![("https://i.scdn.co/a", 200, None, b"IMG")],
            calls: vec![],
        };
        assert_eq!(
            run("https://i.scdn.co/a", &mut t, &FakeDns(vec![GLOBAL])).unwrap(),
            b"IMG"
        );
        let pinned = vec![SocketAddr::new(GLOBAL.parse().unwrap(), 443)];
        assert_eq!(t.calls, [("https://i.scdn.co/a".to_string(), pinned)]);
    }

    #[test]
    fn a_rejected_destination_never_reaches_the_transport() {
        let mut t = FakeTransport {
            routes: vec![],
            calls: vec![],
        };
        let r = run("https://i.scdn.co/a", &mut t, &FakeDns(vec!["127.0.0.1"]));
        assert!(matches!(r, Err(CoverError::Rejected(_))));
        assert!(t.calls.is_empty());
    }

    #[test]
    fn redirect_to_loopback_is_rejected_before_it_is_requested() {
        let mut t = FakeTransport {
            routes: vec![(
                "https://i.scdn.co/a",
                302,
                Some("https://127.0.0.1/evil"),
                b"",
            )],
            calls: vec![],
        };
        let r = run("https://i.scdn.co/a", &mut t, &FakeDns(vec![GLOBAL]));
        assert!(matches!(r, Err(CoverError::Rejected(_))));
        assert_eq!(t.calls.len(), 1, "the redirect target was never requested");
    }

    #[test]
    fn redirect_to_a_foreign_host_or_plain_http_is_rejected() {
        for target in ["https://evil.example/x", "http://i.scdn.co/x"] {
            let mut t = FakeTransport {
                routes: vec![("https://i.scdn.co/a", 301, Some(target), b"")],
                calls: vec![],
            };
            let r = run("https://i.scdn.co/a", &mut t, &FakeDns(vec![GLOBAL]));
            assert!(matches!(r, Err(CoverError::Rejected(_))), "{target}");
        }
    }

    #[test]
    fn allowed_relative_redirect_is_followed() {
        let mut t = FakeTransport {
            routes: vec![
                ("https://i.scdn.co/a", 307, Some("/b"), b""),
                ("https://i.scdn.co/b", 200, None, b"IMG"),
            ],
            calls: vec![],
        };
        assert_eq!(
            run("https://i.scdn.co/a", &mut t, &FakeDns(vec![GLOBAL])).unwrap(),
            b"IMG"
        );
    }

    #[test]
    fn redirect_loops_are_bounded() {
        let mut t = FakeTransport {
            routes: vec![("https://i.scdn.co/a", 302, Some("/a"), b"")],
            calls: vec![],
        };
        let r = run("https://i.scdn.co/a", &mut t, &FakeDns(vec![GLOBAL]));
        assert!(matches!(r, Err(CoverError::Io(_))));
        assert_eq!(t.calls.len(), MAX_REDIRECTS + 1);
    }

    #[test]
    fn http_errors_and_location_less_redirects_are_io_errors() {
        for (status, loc) in [(404, None), (500, None), (302, None), (304, None)] {
            let mut t = FakeTransport {
                routes: vec![("https://i.scdn.co/a", status, loc, b"")],
                calls: vec![],
            };
            let r = run("https://i.scdn.co/a", &mut t, &FakeDns(vec![GLOBAL]));
            assert!(matches!(r, Err(CoverError::Io(_))), "{status}");
        }
    }

    // --- the real transport, against a local server ----------------------

    /// A one-shot plain-HTTP server on loopback that answers with `reply`
    /// and returns the request head it received.
    fn serve_once(reply: &'static str) -> (u16, std::thread::JoinHandle<String>) {
        use std::io::Write;
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let h = std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut head = Vec::new();
            let mut byte = [0u8];
            while !head.ends_with(b"\r\n\r\n") && s.read(&mut byte).unwrap() == 1 {
                head.push(byte[0]);
            }
            s.write_all(reply.as_bytes()).unwrap();
            String::from_utf8_lossy(&head).into_owned()
        });
        (port, h)
    }

    #[test]
    fn ureq_transport_connects_only_to_the_pinned_address() {
        // `cover.invalid` cannot resolve (RFC 6761), so the request can only
        // succeed by connecting to the pinned loopback address, with the Host
        // header still naming the URL's host.
        let (port, server) = serve_once("HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nIMG");
        let url = Url::parse(&format!("http://cover.invalid:{port}/x")).unwrap();
        let pinned = [SocketAddr::from(([127, 0, 0, 1], port))];
        let mut resp = UreqTransport.get(&url, &pinned).expect("pinned request");
        let mut got = Vec::new();
        resp.body.read_to_end(&mut got).unwrap();
        assert_eq!((resp.status, got.as_slice()), (200, &b"IMG"[..]));
        assert_eq!(resp.content_length.as_deref(), Some("3"));
        let head = server.join().unwrap().to_lowercase();
        assert!(
            head.contains(&format!("host: cover.invalid:{port}")),
            "{head}"
        );
    }

    #[test]
    fn ureq_transport_does_not_follow_redirects_itself() {
        let (port, server) = serve_once(
            "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/evil\r\nContent-Length: 0\r\n\r\n",
        );
        let url = Url::parse(&format!("http://cover.invalid:{port}/x")).unwrap();
        let resp = UreqTransport
            .get(&url, &[SocketAddr::from(([127, 0, 0, 1], port))])
            .unwrap();
        assert_eq!(resp.status, 302);
        assert_eq!(resp.location.as_deref(), Some("http://127.0.0.1:1/evil"));
        server.join().unwrap();
    }
}
