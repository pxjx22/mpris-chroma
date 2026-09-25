//! Destination policy for remote artwork (port of `cover._check_destination`
//! and friends, SEC-003).
//!
//! Only https to an allowlisted provider host, on the default port, without
//! userinfo, resolving only to globally routable addresses. The allowlist is
//! the primary SSRF control; the address check is a DNS-rebinding backstop.
//! Unlike the Python, the addresses checked here are the addresses connected
//! to: [`check_destination`] returns them and the transport is pinned to
//! them, so there is no second lookup for a rebinding server to answer
//! differently.

use std::io;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, ToSocketAddrs};
use std::sync::mpsc;
use std::time::Duration;

use url::Url;

use super::CoverError;

/// Allowlisted HTTPS artwork provider domains: a host passes if it equals one
/// or is a subdomain of one, so Spotify's CDN subdomains pass without
/// enumeration.
pub const DEFAULT_ART_DOMAINS: [&str; 2] = ["scdn.co", "spotifycdn.com"];

/// Built-in domains plus any comma-separated `MPRIS_CHROMA_ART_DOMAINS`, so
/// a new provider needs no code change.
pub fn art_domains_from(extra: Option<&str>) -> Vec<String> {
    let mut domains: Vec<String> = DEFAULT_ART_DOMAINS.iter().map(|d| d.to_string()).collect();
    for d in extra.unwrap_or("").split(',') {
        let d = d.trim().to_lowercase();
        if !d.is_empty() && !domains.contains(&d) {
            domains.push(d);
        }
    }
    domains
}

pub fn art_domains() -> Vec<String> {
    art_domains_from(std::env::var("MPRIS_CHROMA_ART_DOMAINS").ok().as_deref())
}

/// True if `host` equals an allowlisted domain or is a subdomain of one. The
/// required leading dot refuses lookalikes like `evilscdn.co`.
pub fn host_allowed(host: &str, domains: &[String]) -> bool {
    let host = host.to_lowercase();
    domains.iter().any(|d| {
        host == *d
            || host
                .strip_suffix(d.as_str())
                .is_some_and(|rest| rest.ends_with('.'))
    })
}

fn v4_in(ip: Ipv4Addr, net: [u8; 4], prefix: u32) -> bool {
    let mask = if prefix == 0 {
        0
    } else {
        u32::MAX << (32 - prefix)
    };
    u32::from(ip) & mask == u32::from(Ipv4Addr::from(net)) & mask
}

fn v6_in(ip: Ipv6Addr, net: [u16; 8], prefix: u32) -> bool {
    let mask = if prefix == 0 {
        0
    } else {
        u128::MAX << (128 - prefix)
    };
    u128::from(ip) & mask == u128::from(Ipv6Addr::from(net)) & mask
}

/// True only for a globally routable unicast address.
///
/// Deliberately a superset of Python's `ipaddress` refusals (which shift
/// between Python versions): every IANA special-purpose IPv4 block is
/// refused, and for IPv6 only global unicast 2000::/3 passes, minus the
/// protocol-assignment, documentation and 6to4 blocks. IPv4-mapped and NAT64
/// forms fall outside 2000::/3 and are refused outright rather than judged by
/// their embedded address. Refusing too much only costs a cover; allowing
/// too much is the SSRF.
pub fn is_global(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            const NON_GLOBAL: [([u8; 4], u32); 15] = [
                ([0, 0, 0, 0], 8),       // "this network", incl. unspecified
                ([10, 0, 0, 0], 8),      // private
                ([100, 64, 0, 0], 10),   // shared address space (CGNAT)
                ([127, 0, 0, 0], 8),     // loopback
                ([169, 254, 0, 0], 16),  // link-local
                ([172, 16, 0, 0], 12),   // private
                ([192, 0, 0, 0], 24),    // IETF protocol assignments
                ([192, 0, 2, 0], 24),    // TEST-NET-1
                ([192, 88, 99, 0], 24),  // 6to4 relay anycast (deprecated)
                ([192, 168, 0, 0], 16),  // private
                ([198, 18, 0, 0], 15),   // benchmarking
                ([198, 51, 100, 0], 24), // TEST-NET-2
                ([203, 0, 113, 0], 24),  // TEST-NET-3
                ([224, 0, 0, 0], 4),     // multicast
                ([240, 0, 0, 0], 4),     // reserved, incl. broadcast
            ];
            !NON_GLOBAL.iter().any(|&(net, p)| v4_in(v4, net, p))
        }
        IpAddr::V6(v6) => {
            const NON_GLOBAL: [([u16; 8], u32); 4] = [
                ([0x2001, 0, 0, 0, 0, 0, 0, 0], 23), // IETF protocol assignments
                ([0x2001, 0xdb8, 0, 0, 0, 0, 0, 0], 32), // documentation
                ([0x2002, 0, 0, 0, 0, 0, 0, 0], 16), // 6to4
                ([0x3fff, 0, 0, 0, 0, 0, 0, 0], 20), // documentation
            ];
            v6_in(v6, [0x2000, 0, 0, 0, 0, 0, 0, 0], 3)
                && !NON_GLOBAL.iter().any(|&(net, p)| v6_in(v6, net, p))
        }
    }
}

/// Resolves a host to its addresses (network; faked in tests).
pub trait Dns {
    fn resolve(&self, host: &str) -> io::Result<Vec<IpAddr>>;
}

/// The system resolver (glibc `getaddrinfo`, so nsswitch/resolved apply),
/// bounded by `timeout`. `getaddrinfo` has no timeout of its own, so the
/// lookup runs on a helper thread that is abandoned if it overruns; it ends
/// when the resolver gives up. The Python lookup is unbounded.
pub struct SystemDns {
    pub timeout: Duration,
}

impl Dns for SystemDns {
    fn resolve(&self, host: &str) -> io::Result<Vec<IpAddr>> {
        let (tx, rx) = mpsc::channel();
        let host = host.to_string();
        std::thread::Builder::new()
            .name("cover-dns".into())
            .spawn(move || {
                let r = (host.as_str(), 0)
                    .to_socket_addrs()
                    .map(|addrs| addrs.map(|a| a.ip()).collect::<Vec<_>>());
                let _ = tx.send(r);
            })?;
        rx.recv_timeout(self.timeout).unwrap_or_else(|_| {
            Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "dns lookup timed out",
            ))
        })
    }
}

/// Refuse `url` unless it is https to an allowlisted host on the default port,
/// without userinfo, resolving only to global addresses. On success, the
/// checked socket addresses: the only ones the transport may connect to.
pub fn check_destination(
    url: &Url,
    domains: &[String],
    dns: &dyn Dns,
) -> Result<Vec<SocketAddr>, CoverError> {
    let reject = |why: String| Err(CoverError::Rejected(why));
    if url.scheme() != "https" {
        return reject(format!("non-https scheme {:?}", url.scheme()));
    }
    if !url.username().is_empty() || url.password().is_some() {
        return reject("url userinfo not permitted".into());
    }
    // The URL parser drops a default port, so any port left is non-default.
    if let Some(port) = url.port() {
        return reject(format!("port {port} not permitted"));
    }
    let host = match url.host() {
        Some(url::Host::Domain(d)) if host_allowed(d, domains) => d,
        other => return reject(format!("host {other:?} not allowlisted")),
    };
    let addrs = dns.resolve(host).map_err(CoverError::from)?;
    if addrs.is_empty() {
        return reject(format!("no addresses for {host}"));
    }
    if let Some(bad) = addrs.iter().find(|a| !is_global(**a)) {
        return reject(format!("non-global address {bad} for {host}"));
    }
    Ok(addrs
        .into_iter()
        .map(|ip| SocketAddr::new(ip, 443))
        .collect())
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) const GLOBAL: &str = "23.192.228.84";

    pub(crate) struct FakeDns(pub Vec<&'static str>);
    impl Dns for FakeDns {
        fn resolve(&self, _: &str) -> io::Result<Vec<IpAddr>> {
            Ok(self.0.iter().map(|a| a.parse().unwrap()).collect())
        }
    }

    fn domains() -> Vec<String> {
        art_domains_from(None)
    }

    fn check(url: &str, addrs: &[&'static str]) -> Result<Vec<SocketAddr>, CoverError> {
        check_destination(
            &Url::parse(url).unwrap(),
            &domains(),
            &FakeDns(addrs.to_vec()),
        )
    }

    fn rejected(r: Result<Vec<SocketAddr>, CoverError>) -> bool {
        matches!(r, Err(CoverError::Rejected(_)))
    }

    #[test]
    fn accepts_allowlisted_https_url_and_returns_the_checked_addresses() {
        let addrs = check("https://i.scdn.co/img/x", &[GLOBAL]).unwrap();
        assert_eq!(addrs, [SocketAddr::new(GLOBAL.parse().unwrap(), 443)]);
        // An explicit default port is the default port.
        assert!(check("https://i.scdn.co:443/img/x", &[GLOBAL]).is_ok());
    }

    #[test]
    fn rejects_non_https_userinfo_port_and_foreign_hosts() {
        for url in [
            "http://i.scdn.co/x",
            "ftp://i.scdn.co/x",
            "https://user:pw@i.scdn.co/x",
            "https://user@i.scdn.co/x",
            "https://i.scdn.co:8080/x",
            "https://evil.example/x",
            "https://23.192.228.84/x", // an IP literal is never allowlisted
            "https://[2600::1]/x",
        ] {
            assert!(rejected(check(url, &[GLOBAL])), "{url}");
        }
    }

    #[test]
    fn rejects_non_global_addresses() {
        for addr in [
            "127.0.0.1",
            "::1",
            "10.0.0.1",
            "192.168.1.1",
            "172.16.0.1",
            "169.254.1.1",
            "fe80::1",
            "224.0.0.1",
            "240.0.0.1",
            "0.0.0.0",
            "::",
            // Beyond the Python list:
            "100.64.0.1",
            "192.0.2.1",
            "198.18.0.1",
            "255.255.255.255",
            "fc00::1",
            "fd12::1",
            "::ffff:127.0.0.1",
            "::ffff:23.192.228.84",
            "64:ff9b::7f00:1",
            "2001:db8::1",
            "2002:7f00:1::1",
            "ff02::1",
            "fec0::1",
        ] {
            assert!(rejected(check("https://i.scdn.co/x", &[addr])), "{addr}");
        }
    }

    #[test]
    fn rejects_when_any_resolved_address_is_non_global() {
        assert!(rejected(check(
            "https://i.scdn.co/x",
            &[GLOBAL, "127.0.0.1"]
        )));
    }

    #[test]
    fn rejects_when_nothing_resolves() {
        assert!(rejected(check("https://i.scdn.co/x", &[])));
    }

    #[test]
    fn global_addresses_pass() {
        for a in [
            "23.192.228.84",
            "8.8.8.8",
            "151.101.1.1",
            "2600:1406::1",
            "2a00:1450::1",
        ] {
            assert!(is_global(a.parse().unwrap()), "{a}");
        }
    }

    // --- host allowlist ---------------------------------------------------

    #[test]
    fn provider_domain_and_subdomains_allowed() {
        for h in [
            "scdn.co",
            "i.scdn.co",
            "mosaic.scdn.co",
            "image-cdn-fa.spotifycdn.com",
        ] {
            assert!(host_allowed(h, &domains()), "{h}");
        }
    }

    #[test]
    fn suffix_confusion_rejected() {
        for h in [
            "evilscdn.co",
            "scdn.co.attacker.com",
            "notspotifycdn.com",
            "i.scdn.co.evil.com",
            "example.com",
            ".scdn.co.",
        ] {
            assert!(!host_allowed(h, &domains()), "{h}");
        }
    }

    #[test]
    fn host_match_is_case_insensitive() {
        assert!(host_allowed("I.SCDN.CO", &domains()));
    }

    #[test]
    fn defaults_present_and_env_extends_them() {
        assert_eq!(art_domains_from(None), ["scdn.co", "spotifycdn.com"]);
        let d = art_domains_from(Some("art.example.net, CDN.foo.org,,scdn.co"));
        assert_eq!(
            d,
            [
                "scdn.co",
                "spotifycdn.com",
                "art.example.net",
                "cdn.foo.org"
            ]
        );
    }
}
