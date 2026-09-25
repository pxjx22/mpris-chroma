//! The long-lived `playerctl --follow` watcher (port of `sync._follow_cmd`,
//! `_spawn_follow`, `_make_io_reader`, `_terminate_child`,
//! `_resolve_playerctl`; PY-002, SEC-011 §3, SEC-013, SEC-017).

use std::ffi::OsString;
use std::io::{self, Read};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::mpsc::SyncSender;
use std::thread::JoinHandle;
use std::time::Duration;

use wait_timeout::ChildExt;

use crate::framing::{LineFramer, MAX_LINE_BYTES, READ_CHUNK};
use crate::runtime::Event;

/// Players to follow; the coordinator is handed the same string, so the
/// follow set and the accept set cannot drift (SEC-011 §2.2).
pub const PLAYERS: &str = "jellyfin-tui,spotify";

/// How long the child gets to exit on SIGTERM before SIGKILL, so shutdown is
/// bounded well under systemd's stop timeout (SEC-013).
pub const CHILD_STOP_TIMEOUT: Duration = Duration::from_secs(5);

/// argv for the streaming multi-player watcher. `-a` emits a named event for
/// every followed player on any change; the `metadata` subcommand is
/// required, or playerctl prints usage and exits.
pub fn follow_cmd(playerctl: &Path, players: &str) -> Vec<OsString> {
    vec![
        playerctl.into(),
        format!("--player={players}").into(),
        "-a".into(),
        "--follow".into(),
        "metadata".into(),
        "--format".into(),
        "{{playerName}}\t{{status}}\t{{mpris:artUrl}}".into(),
    ]
}

/// Find an executable on `path_var` (a `PATH` value), like `shutil.which`.
pub fn which(name: &str, path_var: &std::ffi::OsStr) -> Option<PathBuf> {
    std::env::split_paths(path_var)
        .filter(|dir| dir.is_absolute())
        .map(|dir| dir.join(name))
        .find(|p| {
            p.metadata()
                .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        })
}

/// Resolve playerctl to an absolute path once, at startup (SEC-017), so a
/// `PATH` changed afterwards cannot substitute a different binary at spawn.
pub fn resolve_playerctl() -> Result<PathBuf, String> {
    let path = std::env::var_os("PATH").unwrap_or_default();
    which("playerctl", &path).ok_or_else(|| {
        "playerctl not found on PATH — install it (Arch: pacman -S playerctl)".into()
    })
}

/// Spawn the watcher: argv (never a shell), stdout piped, stderr inherited to
/// the journal, stdin closed.
pub fn spawn_follow(playerctl: &Path, players: &str) -> io::Result<Child> {
    let argv = follow_cmd(playerctl, players);
    Command::new(&argv[0])
        .args(&argv[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .spawn()
}

/// Read playerctl's stdout on its own thread: one bounded read at a time,
/// framed, each line (or oversize drop) sent as an event. The send blocks
/// when the loop is behind, which stops the reads and lets the pipe fill:
/// backpressure instead of unbounded buffering. EOF or a read error is a
/// hangup: playerctl is gone.
pub fn start_reader(mut stdout: ChildStdout, tx: SyncSender<Event>) -> io::Result<JoinHandle<()>> {
    std::thread::Builder::new()
        .name("playerctl-reader".into())
        .spawn(move || {
            let drop_tx = tx.clone();
            let mut framer = LineFramer::new(
                MAX_LINE_BYTES,
                Some(Box::new(move |detail| {
                    let _ = drop_tx.send(Event::Oversize(detail));
                })),
            );
            let mut buf = vec![0u8; READ_CHUNK];
            loop {
                let n = match stdout.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => n,
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                    Err(_) => break,
                };
                for line in framer.feed(&buf[..n]) {
                    if tx.send(Event::Line(line)).is_err() {
                        return; // the loop is gone
                    }
                }
            }
            let _ = tx.send(Event::Hangup);
        })
}

/// SIGTERM the child and guarantee it is reaped within `timeout`: if it
/// ignores the term, SIGKILL and reap. An already-exited child is fine.
pub fn terminate_child(child: &mut Child, timeout: Duration) {
    if let Ok(Some(_)) = child.try_wait() {
        return; // already exited and now reaped
    }
    // SAFETY: kill(2) with our own child's pid; the child is not yet reaped
    // (try_wait above), so the pid cannot have been recycled.
    unsafe {
        libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
    }
    if let Ok(Some(_)) = child.wait_timeout(timeout) {
        return;
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::sync_channel;
    use std::time::Instant;

    fn sh(script: &str) -> Child {
        Command::new("/bin/sh")
            .args(["-c", script])
            .stdout(Stdio::piped())
            .spawn()
            .unwrap()
    }

    #[test]
    fn follow_cmd_watches_both_players_with_names() {
        let cmd = follow_cmd(Path::new("/usr/bin/playerctl"), PLAYERS);
        let s: Vec<String> = cmd
            .iter()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(s[0], "/usr/bin/playerctl"); // resolved path as argv[0]
        assert!(s.contains(&"--follow".into()) && s.contains(&"-a".into()));
        assert!(s.contains(&"--player=jellyfin-tui,spotify".into()));
        let metadata = s.iter().position(|a| a == "metadata").unwrap();
        let format = s.iter().position(|a| a == "--format").unwrap();
        assert!(metadata < format, "metadata must precede --format");
        assert!(s[format + 1].contains("{{playerName}}"));
    }

    #[test]
    fn which_finds_executables_only_on_absolute_path_entries() {
        let dir = std::env::temp_dir().join(format!("mpris-chroma-which-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let exe = dir.join("playerctl");
        std::fs::write(&exe, "#!/bin/sh\n").unwrap();
        std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
        let plain = dir.join("notexec");
        std::fs::write(&plain, "x").unwrap();
        let path = std::env::join_paths(["relative/dir".into(), dir.clone()]).unwrap();
        assert_eq!(which("playerctl", &path), Some(exe));
        assert_eq!(which("notexec", &path), None);
        assert_eq!(which("missing", &path), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn reader_sends_framed_lines_then_a_hangup() {
        let mut child = sh("printf 'spotify\\tPlaying\\thttps://x/a\\npartial'");
        let (tx, rx) = sync_channel(8);
        let reader = start_reader(child.stdout.take().unwrap(), tx).unwrap();
        let events: Vec<Event> = rx.iter().collect();
        reader.join().unwrap();
        child.wait().unwrap();
        assert!(matches!(&events[0], Event::Line(l) if l == "spotify\tPlaying\thttps://x/a"));
        assert!(
            matches!(events[1], Event::Hangup),
            "partial tail never emitted"
        );
        assert_eq!(events.len(), 2);
    }

    #[test]
    fn reader_reports_an_oversize_line_and_keeps_going() {
        let script = format!(
            "head -c {} /dev/zero | tr '\\0' a; printf '\\ngood\\n'",
            MAX_LINE_BYTES + 10
        );
        let mut child = sh(&script);
        let (tx, rx) = sync_channel(8);
        start_reader(child.stdout.take().unwrap(), tx).unwrap();
        let events: Vec<Event> = rx.iter().collect();
        child.wait().unwrap();
        assert!(matches!(events[0], Event::Oversize(_)));
        assert!(matches!(&events[1], Event::Line(l) if l == "good"));
    }

    #[test]
    fn cooperative_child_is_terminated_without_kill() {
        let mut child = sh("exec sleep 30");
        let start = Instant::now();
        terminate_child(&mut child, Duration::from_secs(5));
        assert!(start.elapsed() < Duration::from_secs(2));
        use std::os::unix::process::ExitStatusExt;
        let status = child.wait().unwrap();
        assert_eq!(status.signal(), Some(libc::SIGTERM));
    }

    #[test]
    fn child_ignoring_sigterm_is_killed_and_reaped() {
        let mut child = sh("trap '' TERM; exec sleep 30");
        std::thread::sleep(Duration::from_millis(100)); // let the trap install
        let start = Instant::now();
        terminate_child(&mut child, Duration::from_millis(200));
        assert!(start.elapsed() < Duration::from_secs(2));
        use std::os::unix::process::ExitStatusExt;
        assert_eq!(child.wait().unwrap().signal(), Some(libc::SIGKILL));
    }

    #[test]
    fn already_exited_child_is_tolerated() {
        let mut child = sh("exit 0");
        std::thread::sleep(Duration::from_millis(100));
        terminate_child(&mut child, Duration::from_secs(1));
        terminate_child(&mut child, Duration::from_secs(1)); // twice is fine
    }
}
