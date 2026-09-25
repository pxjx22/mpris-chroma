//! Applying palettes via `wlchroma-ctl` (port of `apply.py`).
//!
//! Every ctl call is bounded by [`CTL_TIMEOUT`] with its exit status checked
//! (SEC-007), every colour is validated as `#rrggbb` at the argv boundary
//! (SEC-016), and the revert palette read from wlchroma's config is validated
//! the same way before it reaches ctl (SEC-008). `wlchroma-ctl` joins its argv
//! into one whitespace-delimited IPC line, so an unvalidated value bearing
//! whitespace or a newline could smuggle an extra token or a whole extra
//! protocol line.

use std::fmt;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use wait_timeout::ChildExt;

/// Smoothstep glide to the new palette (0 = instant). Long on purpose:
/// wlchroma only redraws on Wayland frame callbacks, which niri throttles for
/// an occluded layer surface, so a short fade settles before enough frames
/// render and snaps. A longer duration spans enough sparse callbacks to read
/// as a glide.
pub const FADE_MS: u32 = 2000;

/// wlchroma-ctl talks to a local Unix socket, so a healthy call returns
/// almost instantly. Bounded so a hung ctl can never stall the worker or
/// block shutdown until systemd's stop timeout.
pub const CTL_TIMEOUT: Duration = Duration::from_secs(5);

/// Characters of ctl stderr preserved in a [`CtlError`] message.
const MAX_DIAG: usize = 200;
/// Bytes of ctl stderr read at all; the rest is drained unread.
const MAX_STDERR_READ: u64 = 64 * 1024;
/// Minimum wait for ctl's stderr after it exits, even at the deadline.
const STDERR_GRACE: Duration = Duration::from_millis(100);

/// The named palette to fall back to when the config preset is unusable.
pub const FALLBACK_PALETTE: &str = "witch_hour";

/// A `wlchroma-ctl` call timed out, failed to start, exited non-zero, or was
/// refused a malformed palette.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct CtlError(pub String);

impl fmt::Display for CtlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for CtlError {}

/// `wlchroma-ctl` is a build output (zig-out/bin), not usually installed, so
/// `WLCHROMA_CTL` lets the service point at it; otherwise the bare name is
/// looked up on `PATH` when spawned.
pub fn default_ctl() -> String {
    std::env::var("WLCHROMA_CTL")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "wlchroma-ctl".into())
}

/// wlchroma's live config; its `[effect.settings] palette` is the preset a
/// closed or stopped player reverts to.
pub fn default_config_path() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    home.join(".config/wlchroma/config.toml")
}

// --- running ctl ---------------------------------------------------------

/// How a bounded run ended short of an exit status.
#[derive(Debug)]
pub enum RunError {
    Timeout,
    Spawn(std::io::Error),
}

/// A finished run: the exit code (`None` if killed by a signal) and stderr.
#[derive(Clone, Debug)]
pub struct RunOutput {
    pub code: Option<i32>,
    pub stderr: String,
}

/// Runs an argv with a timeout. Injected so tests never spawn processes.
pub trait Runner {
    fn run(&mut self, argv: &[String], timeout: Duration) -> Result<RunOutput, RunError>;
}

/// Spawns the process directly (never through a shell).
#[derive(Default)]
pub struct ProcessRunner;

impl Runner for ProcessRunner {
    fn run(&mut self, argv: &[String], timeout: Duration) -> Result<RunOutput, RunError> {
        let (program, args) = argv.split_first().expect("non-empty argv");
        let mut child = Command::new(program)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(RunError::Spawn)?;
        let deadline = Instant::now() + timeout;
        // Read stderr on its own thread so a chatty child cannot fill the
        // pipe and deadlock against our wait. The pipe closes only when every
        // holder exits, and a grandchild can outlive ctl while holding it, so
        // the reader is never joined unboundedly: its result is awaited until
        // the same deadline, then abandoned (the thread ends with the pipe).
        let mut stderr = child.stderr.take().expect("piped stderr");
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = (&mut stderr).take(MAX_STDERR_READ).read_to_end(&mut buf);
            let _ = tx.send(buf.clone());
            let _ = std::io::copy(&mut stderr, &mut std::io::sink());
        });
        let status = match child.wait_timeout(timeout).map_err(RunError::Spawn)? {
            Some(status) => status,
            None => {
                // As subprocess.run: kill, reap, and do not wait on the pipes.
                let _ = child.kill();
                let _ = child.wait();
                return Err(RunError::Timeout);
            }
        };
        // At least a short grace: ctl exiting right at the deadline has its
        // stderr in flight, and the diagnostic is worth a few milliseconds.
        let left = deadline
            .saturating_duration_since(Instant::now())
            .max(STDERR_GRACE);
        let buf = rx.recv_timeout(left).unwrap_or_default();
        Ok(RunOutput {
            code: status.code(),
            stderr: String::from_utf8_lossy(&buf).into_owned(),
        })
    }
}

/// `#rrggbb` exactly: '#' plus six ASCII hex digits and nothing else — no
/// trailing newline, no whitespace. Case is preserved (wlchroma parses hex;
/// existing configs' output must not silently change).
pub fn valid_hex(value: &str) -> bool {
    let b = value.as_bytes();
    b.len() == 7 && b[0] == b'#' && b[1..].iter().all(u8::is_ascii_hexdigit)
}

/// The three `[effect.settings] palette` colours, or `None` if the config is
/// unreadable, not UTF-8, not TOML, or the palette is anything but three
/// well-formed `#rrggbb` strings.
pub fn config_palette(config_path: &Path) -> Option<[String; 3]> {
    let bytes = std::fs::read(config_path).ok()?;
    let text = String::from_utf8(bytes).ok()?;
    let table: toml::Table = text.parse().ok()?;
    let palette = table
        .get("effect")?
        .get("settings")?
        .get("palette")?
        .as_array()?;
    let [a, b, c] = palette.as_slice() else {
        return None;
    };
    let hex = |v: &toml::Value| v.as_str().filter(|s| valid_hex(s)).map(String::from);
    Some([hex(a)?, hex(b)?, hex(c)?])
}

/// The ctl client: which binary, how it is run, and where the revert preset
/// lives.
pub struct Ctl<R: Runner = ProcessRunner> {
    pub program: String,
    pub runner: R,
    pub config_path: PathBuf,
    pub fade_ms: u32,
}

impl Ctl {
    /// Production wiring: `WLCHROMA_CTL` or `wlchroma-ctl`, real processes,
    /// the user's wlchroma config, the default fade.
    pub fn from_env() -> Self {
        Ctl {
            program: default_ctl(),
            runner: ProcessRunner,
            config_path: default_config_path(),
            fade_ms: FADE_MS,
        }
    }
}

impl<R: Runner> Ctl<R> {
    /// Run one ctl command, bounded, with its exit status checked.
    fn run(&mut self, args: &[&str]) -> Result<(), CtlError> {
        let mut argv = vec![self.program.clone()];
        argv.extend(args.iter().map(|s| s.to_string()));
        match self.runner.run(&argv, CTL_TIMEOUT) {
            Err(RunError::Timeout) => Err(CtlError(format!(
                "wlchroma-ctl timed out after {}s",
                CTL_TIMEOUT.as_secs()
            ))),
            Err(RunError::Spawn(e)) => Err(CtlError(format!("wlchroma-ctl could not run: {e}"))),
            Ok(out) if out.code == Some(0) => Ok(()),
            Ok(out) => {
                let err: String = out.stderr.trim().chars().take(MAX_DIAG).collect();
                let code = out
                    .code
                    .map_or_else(|| "on a signal".into(), |c| c.to_string());
                Err(CtlError(format!("wlchroma-ctl exited {code}: {err}")))
            }
        }
    }

    /// Set the three colours, gliding over `fade_ms`.
    pub fn apply(&mut self, colors: &[String; 3]) -> Result<(), CtlError> {
        // Defence in depth at the argv boundary: callers already validate, so
        // this only ever rejects a bug.
        if !colors.iter().all(|c| valid_hex(c)) {
            return Err(CtlError(format!(
                "refusing to apply malformed palette: {colors:?}"
            )));
        }
        let fade = self.fade_ms.to_string();
        let mut args = vec!["set-colors", &colors[0], &colors[1], &colors[2]];
        if self.fade_ms > 0 {
            args.push(&fade);
        }
        self.run(&args)
    }

    /// Glide back to wlchroma's config preset; if the config is gone or
    /// malformed, fall back to the named default so the daemon still reverts.
    pub fn revert(&mut self) -> Result<(), CtlError> {
        match config_palette(&self.config_path) {
            Some(colors) => self.apply(&colors),
            None => self.run(&["set-palette", FALLBACK_PALETTE]),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONFIG: &str = "version = 2\n[effect]\nname = \"colormix\"\n[effect.settings]\npalette = [\"#120C14\", \"#4A2F5C\", \"#6D8F4F\"]\n";

    /// Records argv and returns a scripted result.
    struct Rec {
        calls: Vec<Vec<String>>,
        timeouts: Vec<Duration>,
        reply: fn() -> Result<RunOutput, RunError>,
    }

    fn ok() -> Result<RunOutput, RunError> {
        Ok(RunOutput {
            code: Some(0),
            stderr: String::new(),
        })
    }

    impl Runner for Rec {
        fn run(&mut self, argv: &[String], timeout: Duration) -> Result<RunOutput, RunError> {
            self.calls.push(argv.to_vec());
            self.timeouts.push(timeout);
            (self.reply)()
        }
    }

    fn ctl_with(reply: fn() -> Result<RunOutput, RunError>, config: &Path) -> Ctl<Rec> {
        Ctl {
            program: "CTL".into(),
            runner: Rec {
                calls: vec![],
                timeouts: vec![],
                reply,
            },
            config_path: config.to_path_buf(),
            fade_ms: FADE_MS,
        }
    }

    fn ctl(reply: fn() -> Result<RunOutput, RunError>) -> Ctl<Rec> {
        ctl_with(reply, Path::new("/no/such/config.toml"))
    }

    fn colors(a: &str, b: &str, c: &str) -> [String; 3] {
        [a, b, c].map(String::from)
    }

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|s| s.to_string()).collect()
    }

    struct Tmp(PathBuf);
    impl Tmp {
        fn with(name: &str, bytes: &[u8]) -> Self {
            let dir = std::env::temp_dir()
                .join(format!("mpris-chroma-apply-{name}-{}", std::process::id()));
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("config.toml"), bytes).unwrap();
            Tmp(dir)
        }
        fn path(&self) -> PathBuf {
            self.0.join("config.toml")
        }
    }
    impl Drop for Tmp {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    // --- apply / revert --------------------------------------------------

    #[test]
    fn apply_calls_set_colors_with_fade() {
        let mut c = ctl(ok);
        c.apply(&colors("#aa0000", "#00bb00", "#0000bb")).unwrap();
        let want = argv(&["CTL", "set-colors", "#aa0000", "#00bb00", "#0000bb", "2000"]);
        assert_eq!(c.runner.calls, [want]);
    }

    #[test]
    fn zero_fade_omits_the_arg() {
        let mut c = ctl(ok);
        c.fade_ms = 0;
        c.apply(&colors("#aa0000", "#00bb00", "#0000bb")).unwrap();
        let want = argv(&["CTL", "set-colors", "#aa0000", "#00bb00", "#0000bb"]);
        assert_eq!(c.runner.calls, [want]);
    }

    #[test]
    fn revert_fades_to_config_palette() {
        let t = Tmp::with("revert", CONFIG.as_bytes());
        let mut c = ctl_with(ok, &t.path());
        c.revert().unwrap();
        let want = argv(&["CTL", "set-colors", "#120C14", "#4A2F5C", "#6D8F4F", "2000"]);
        assert_eq!(c.runner.calls, [want]);
    }

    #[test]
    fn revert_falls_back_to_witch_hour_when_config_unreadable() {
        let mut c = ctl(ok);
        c.revert().unwrap();
        assert_eq!(
            c.runner.calls,
            [argv(&["CTL", "set-palette", "witch_hour"])]
        );
    }

    #[test]
    fn revert_falls_back_when_a_palette_element_is_invalid() {
        let body = "version = 2\n[effect]\n[effect.settings]\npalette = [1, 2, 3]\n";
        let t = Tmp::with("badpal", body.as_bytes());
        let mut c = ctl_with(ok, &t.path());
        c.revert().unwrap();
        assert_eq!(
            c.runner.calls,
            [argv(&["CTL", "set-palette", "witch_hour"])]
        );
    }

    // --- config palette validation (SEC-008) -----------------------------

    fn palette_from(tag: &str, palette_toml: &str) -> Option<[String; 3]> {
        let body = format!(
            "version = 2\n[effect]\nname = \"colormix\"\n[effect.settings]\npalette = {palette_toml}\n"
        );
        let t = Tmp::with(tag, body.as_bytes());
        config_palette(&t.path())
    }

    #[test]
    fn valid_hex_accepted_and_case_preserved() {
        assert_eq!(
            palette_from("lower", r##"["#aa0000", "#00bb00", "#0000cc"]"##),
            Some(colors("#aa0000", "#00bb00", "#0000cc"))
        );
        assert_eq!(
            palette_from("upper", r##"["#120C14", "#4A2F5C", "#6D8F4F"]"##),
            Some(colors("#120C14", "#4A2F5C", "#6D8F4F"))
        );
    }

    #[test]
    fn malformed_palettes_are_rejected() {
        for (tag, bad) in [
            ("ints", "[1, 2, 3]"),
            ("floats", "[1.0, 2.0, 3.0]"),
            ("table", r##"["#aa0000", "#00bb00", {x = 1}]"##),
            ("nested", r##"["#aa0000", "#00bb00", ["#0000cc"]]"##),
            ("short", r##"["#aa000", "#00bb00", "#0000cc"]"##),
            ("long", r##"["#aa00000", "#00bb00", "#0000cc"]"##),
            ("hashless", r##"["aa0000", "#00bb00", "#0000cc"]"##),
            ("charset", r##"["#gg0000", "#00bb00", "#0000cc"]"##),
            ("space", r##"["#aa0000 ", "#00bb00", "#0000cc"]"##),
            ("newline", r##"["#aa0000\n", "#00bb00", "#0000cc"]"##),
            ("two", r##"["#aa0000", "#00bb00"]"##),
            ("four", r##"["#aa0000", "#00bb00", "#0000cc", "#111111"]"##),
            ("string", r##""#aa0000""##),
        ] {
            assert_eq!(palette_from(tag, bad), None, "{tag}: {bad}");
        }
    }

    #[test]
    fn invalid_utf8_returns_none_without_crashing() {
        let t = Tmp::with("utf8", b"palette = \xff\xff\n");
        assert_eq!(config_palette(&t.path()), None);
    }

    #[test]
    fn missing_keys_return_none() {
        let t = Tmp::with("keys", b"version = 2\n[effect]\nname = \"x\"\n");
        assert_eq!(config_palette(&t.path()), None);
    }

    // --- ctl reliability (SEC-007) ---------------------------------------

    #[test]
    fn every_call_is_bounded_by_the_timeout() {
        let mut c = ctl(ok);
        c.apply(&colors("#aa0000", "#00bb00", "#0000bb")).unwrap();
        c.revert().unwrap();
        assert_eq!(c.runner.timeouts, [CTL_TIMEOUT, CTL_TIMEOUT]);
    }

    #[test]
    fn hang_is_a_ctl_error() {
        let mut c = ctl(|| Err(RunError::Timeout));
        assert!(c.apply(&colors("#aa0000", "#00bb00", "#0000bb")).is_err());
    }

    #[test]
    fn spawn_failure_is_a_ctl_error() {
        let mut c = ctl(|| Err(RunError::Spawn(std::io::ErrorKind::NotFound.into())));
        assert!(c.apply(&colors("#aa0000", "#00bb00", "#0000bb")).is_err());
    }

    fn fails_with_boom() -> Result<RunOutput, RunError> {
        Ok(RunOutput {
            code: Some(1),
            stderr: "boom".into(),
        })
    }

    #[test]
    fn nonzero_exit_is_a_ctl_error_on_apply_and_both_reverts() {
        let mut c = ctl(fails_with_boom);
        let err = c
            .apply(&colors("#aa0000", "#00bb00", "#0000bb"))
            .unwrap_err();
        assert!(err.0.contains("exited 1: boom"), "{err}");
        assert!(c.revert().is_err()); // fallback path is checked too
        let t = Tmp::with("revfail", CONFIG.as_bytes());
        assert!(ctl_with(fails_with_boom, &t.path()).revert().is_err());
    }

    #[test]
    fn diagnostic_is_bounded() {
        let mut c = ctl(|| {
            Ok(RunOutput {
                code: Some(1),
                stderr: "x".repeat(10_000),
            })
        });
        let err = c
            .apply(&colors("#aa0000", "#00bb00", "#0000bb"))
            .unwrap_err();
        assert!(err.0.len() < 1000);
    }

    // --- argv safety (SEC-016) -------------------------------------------

    #[test]
    fn malformed_colours_are_refused_before_ctl_runs() {
        for bad in [
            "#aa0000\nset-palette evil",
            "#aa0000 evil",
            "red",
            "#aa0000 ",
        ] {
            let mut c = ctl(ok);
            assert!(
                c.apply(&colors(bad, "#00bb00", "#0000cc")).is_err(),
                "{bad:?}"
            );
            assert!(c.runner.calls.is_empty(), "ctl ran for {bad:?}");
        }
    }

    #[test]
    fn valid_hex_is_exact() {
        assert!(valid_hex("#aA09fF"));
        for bad in [
            "#aa000",
            "#aa00000",
            "aa00000",
            "#gg0000",
            "#aa000\n",
            "#aa00 0",
            "＃aa0000",
        ] {
            assert!(!valid_hex(bad), "{bad:?}");
        }
    }

    // --- the real process runner -----------------------------------------

    fn sh(script: &str) -> Vec<String> {
        argv(&["/bin/sh", "-c", script])
    }

    #[test]
    fn process_runner_reports_exit_code_and_stderr() {
        let out = ProcessRunner
            .run(&sh("echo oops >&2; exit 3"), CTL_TIMEOUT)
            .unwrap();
        assert_eq!(out.code, Some(3));
        assert_eq!(out.stderr.trim(), "oops");
    }

    #[test]
    fn process_runner_kills_a_hung_child_at_the_timeout() {
        let start = std::time::Instant::now();
        let r = ProcessRunner.run(&sh("sleep 30"), Duration::from_millis(200));
        assert!(matches!(r, Err(RunError::Timeout)));
        assert!(start.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn process_runner_does_not_wait_on_a_grandchild_holding_stderr() {
        // ctl exits at once but leaves a child holding the pipe: the exit
        // status still comes back within the timeout, not when the child ends.
        let start = std::time::Instant::now();
        let out = ProcessRunner
            .run(&sh("sleep 30 & exit 0"), Duration::from_millis(300))
            .unwrap();
        assert_eq!(out.code, Some(0));
        assert!(start.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn process_runner_survives_a_stderr_flood() {
        // Far more than a pipe buffer: must neither deadlock nor buffer it all.
        let out = ProcessRunner
            .run(
                &sh("head -c 1000000 /dev/zero | tr '\\0' x >&2"),
                CTL_TIMEOUT,
            )
            .unwrap();
        assert_eq!(out.code, Some(0));
        assert!(out.stderr.len() as u64 <= MAX_STDERR_READ);
    }

    #[test]
    fn process_runner_spawn_failure_is_reported() {
        let r = ProcessRunner.run(&argv(&["/no/such/wlchroma-ctl"]), CTL_TIMEOUT);
        assert!(matches!(r, Err(RunError::Spawn(_))));
    }
}
