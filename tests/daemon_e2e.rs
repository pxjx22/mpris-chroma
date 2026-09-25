//! End to end: the real daemon pipeline (startup revert, playerctl reader,
//! coordinator, worker, cover dir scan, Pillow-exact palette, wlchroma-ctl,
//! sequenced shutdown), driven by a fake `playerctl` script and observed
//! through a fake `wlchroma-ctl` that logs its argv. No session bus: D-Bus
//! and signals are separate sources with their own tests.
//!
//! The expected colours come from the Python pipeline
//! (`fixtures/image_golden.json`), so this also proves the whole Rust path
//! produces Python's palette for a real cover.

use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::mpsc::sync_channel;
use std::time::{Duration, Instant};

use mpris_chroma::daemon::{self, Config};
use mpris_chroma::runtime::{EVENT_QUEUE, Event};
use mpris_chroma::state::Mode;

const WLCHROMA_CONFIG: &str = "version = 2\n[effect]\nname = \"colormix\"\n[effect.settings]\npalette = [\"#120C14\", \"#4A2F5C\", \"#6D8F4F\"]\n";
const PRESET: &str = "set-colors #120C14 #4A2F5C #6D8F4F 2000";

struct Env {
    dir: PathBuf,
}

impl Env {
    fn new(tag: &str) -> Env {
        let dir =
            std::env::temp_dir().join(format!("mpris-chroma-e2e-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("covers")).unwrap();
        fs::write(dir.join("wlchroma.toml"), WLCHROMA_CONFIG).unwrap();
        let ctl = format!(
            "#!/bin/sh\necho \"$@\" >> '{}'\n",
            dir.join("ctl.log").display()
        );
        script(&dir.join("wlchroma-ctl"), &ctl);
        Env { dir }
    }

    fn playerctl(&self, body: &str) -> PathBuf {
        let p = self.dir.join("playerctl");
        script(&p, &format!("#!/bin/sh\n{body}\n"));
        p
    }

    fn config(&self, playerctl: PathBuf) -> Config {
        Config {
            players: "jellyfin-tui,spotify".into(),
            covers_dirs: HashMap::from([("jellyfin-tui".into(), self.dir.join("covers"))]),
            playerctl,
            ctl_program: self.dir.join("wlchroma-ctl").display().to_string(),
            wlchroma_config: self.dir.join("wlchroma.toml"),
            fade_ms: 2000,
            cache_dir: self.dir.join("cache"),
            mode: Mode::Dark,
        }
    }

    fn ctl_log(&self) -> Vec<String> {
        fs::read_to_string(self.dir.join("ctl.log"))
            .unwrap_or_default()
            .lines()
            .map(String::from)
            .collect()
    }

    fn wait_for_ctl_lines(&self, n: usize) -> Vec<String> {
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            let log = self.ctl_log();
            if log.len() >= n || Instant::now() > deadline {
                return log;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn script(path: &Path, body: &str) {
    fs::write(path, body).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

/// Python's dark palette for `images/blobs.png` (see image_golden.json).
fn python_blobs_dark() -> String {
    let golden: serde_json::Value = serde_json::from_str(
        &fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/image_golden.json"
        ))
        .unwrap(),
    )
    .unwrap();
    let case = golden["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "blobs.png")
        .unwrap();
    let dark: Vec<&str> = case["dark"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    format!("set-colors {} 2000", dark.join(" "))
}

#[test]
fn a_playing_dir_scan_player_gets_pythons_palette_and_shutdown_reverts() {
    let env = Env::new("apply");
    fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/images/blobs.png"
        ),
        env.dir.join("covers/blobs.png"),
    )
    .unwrap();
    // jellyfin-tui with no art URL: resolved by scanning its covers dir.
    let playerctl = env.playerctl("printf 'jellyfin-tui\\tPlaying\\t\\n'\nexec sleep 1000");
    let (tx, rx) = sync_channel(EVENT_QUEUE);
    let shutdown = tx.clone();
    let config = env.config(playerctl);
    let daemon = std::thread::spawn(move || daemon::run(config, tx, rx));

    let log = env.wait_for_ctl_lines(2);
    assert_eq!(
        log,
        [PRESET.to_string(), python_blobs_dark()],
        "startup revert, then the cover"
    );

    shutdown.send(Event::Shutdown).unwrap();
    assert_eq!(daemon.join().unwrap(), 0);
    assert_eq!(
        env.ctl_log().last().map(String::as_str),
        Some(PRESET),
        "shutdown reverts"
    );
}

#[test]
fn stopping_playback_reverts_to_the_preset() {
    let env = Env::new("stop");
    fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/images/logo.png"
        ),
        env.dir.join("covers/logo.png"),
    )
    .unwrap();
    let playerctl = env.playerctl(
        "printf 'jellyfin-tui\\tPlaying\\t\\n'\nsleep 1\nprintf 'jellyfin-tui\\tStopped\\t\\n'\nexec sleep 1000",
    );
    let (tx, rx) = sync_channel(EVENT_QUEUE);
    let shutdown = tx.clone();
    let config = env.config(playerctl);
    let daemon = std::thread::spawn(move || daemon::run(config, tx, rx));

    let log = env.wait_for_ctl_lines(3);
    assert_eq!(log.len(), 3, "{log:?}");
    assert_eq!(log[0], PRESET);
    assert!(
        log[1].starts_with("set-colors #") && log[1] != PRESET,
        "{log:?}"
    );
    assert_eq!(log[2], PRESET, "stop reverts");
    shutdown.send(Event::Shutdown).unwrap();
    assert_eq!(daemon.join().unwrap(), 0);
}

#[test]
fn playerctl_dying_exits_non_zero_for_a_restart() {
    let env = Env::new("hangup");
    let playerctl = env.playerctl("exit 0");
    let (tx, rx) = sync_channel(EVENT_QUEUE);
    let start = Instant::now();
    assert_eq!(daemon::run(env.config(playerctl), tx, rx), 1);
    assert!(start.elapsed() < Duration::from_secs(15));
    assert_eq!(env.ctl_log(), [PRESET], "only the startup revert");
}

/// A private session bus, killed on drop; `None` where dbus-daemon is absent.
struct Bus {
    daemon: std::process::Child,
    address: String,
}

impl Bus {
    fn start() -> Option<Bus> {
        use std::io::BufRead;
        let mut daemon = std::process::Command::new("dbus-daemon")
            .args(["--session", "--nofork", "--print-address=1"])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn()
            .ok()?;
        let mut line = String::new();
        std::io::BufReader::new(daemon.stdout.take()?)
            .read_line(&mut line)
            .ok()?;
        Some(Bus {
            daemon,
            address: line.trim().to_string(),
        })
    }
}

impl Drop for Bus {
    fn drop(&mut self) {
        let _ = self.daemon.kill();
        let _ = self.daemon.wait();
    }
}

#[test]
fn the_binary_reverts_on_player_vanish_and_stops_cleanly_on_sigterm() {
    let Some(bus) = Bus::start() else {
        eprintln!("skipped: dbus-daemon not available");
        return;
    };
    let env = Env::new("binary");
    // The binary finds everything under $HOME and playerctl on $PATH.
    let home = env.dir.join("home");
    let covers = home.join(".local/share/jellyfin-tui/covers");
    fs::create_dir_all(&covers).unwrap();
    fs::create_dir_all(home.join(".config/wlchroma")).unwrap();
    fs::copy(
        env.dir.join("wlchroma.toml"),
        home.join(".config/wlchroma/config.toml"),
    )
    .unwrap();
    fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/images/blobs.png"
        ),
        covers.join("blobs.png"),
    )
    .unwrap();
    let bin_dir = env.dir.join("bin");
    fs::create_dir_all(&bin_dir).unwrap();
    script(
        &bin_dir.join("playerctl"),
        "#!/bin/sh\nprintf 'jellyfin-tui\\tPlaying\\t\\n'\nexec sleep 1000\n",
    );

    let mut daemon = std::process::Command::new(env!("CARGO_BIN_EXE_mpris-chroma"))
        .env_clear()
        .env("HOME", &home)
        .env("PATH", format!("{}:/usr/bin:/bin", bin_dir.display()))
        .env("DBUS_SESSION_BUS_ADDRESS", &bus.address)
        .env("WLCHROMA_CTL", env.dir.join("wlchroma-ctl"))
        .spawn()
        .unwrap();

    let log = env.wait_for_ctl_lines(2);
    assert_eq!(log, [PRESET.to_string(), python_blobs_dark()]);

    // The player appears on the bus and then exits: the daemon reverts.
    let player = zbus::blocking::connection::Builder::address(bus.address.as_str())
        .unwrap()
        .build()
        .unwrap();
    player
        .request_name("org.mpris.MediaPlayer2.jellyfin-tui")
        .unwrap();
    drop(player);
    let log = env.wait_for_ctl_lines(3);
    assert_eq!(
        log.get(2).map(String::as_str),
        Some(PRESET),
        "vanish reverts: {log:?}"
    );

    // SAFETY: signalling our own child process.
    unsafe {
        libc::kill(daemon.id() as libc::pid_t, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(15);
    let status = loop {
        if let Some(s) = daemon.try_wait().unwrap() {
            break s;
        }
        assert!(Instant::now() < deadline, "daemon did not exit on SIGTERM");
        std::thread::sleep(Duration::from_millis(20));
    };
    assert_eq!(status.code(), Some(0));
    assert_eq!(
        env.ctl_log().last().map(String::as_str),
        Some(PRESET),
        "final revert"
    );
}
