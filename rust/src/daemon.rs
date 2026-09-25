//! The daemon's lifecycle (port of `sync.main` minus the D-Bus/env glue in
//! `main.rs`): startup revert, worker, playerctl, the event loop, and the
//! sequenced shutdown (design §5).

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, SyncSender};
use std::time::Duration;

use crate::apply::{Ctl, CtlError, FADE_MS, ProcessRunner};
use crate::color::{ContentId, PaletteMemo};
use crate::coordinator::Coordinator;
use crate::cover::{CoverResolver, HttpFetch, Resolution};
use crate::runtime::{Event, LoopExit, RuntimeHost, default_jitter, run_loop};
use crate::sources::playerctl;
use crate::state::Mode;
use crate::worker::{Committed, Mailbox, Stages, Worker, WorkerHandle};

/// Bound on joining the worker at shutdown: an abortable download stage plus
/// one ctl call, far under systemd's stop timeout.
pub const WORKER_STOP_TIMEOUT: Duration = Duration::from_secs(10);

/// Everything the daemon needs to know, resolved by `main` (or a test).
pub struct Config {
    /// Followed players, handed to both `playerctl --player=` and the
    /// coordinator's allowlist.
    pub players: String,
    /// Players with a local covers directory (dir-scan players).
    pub covers_dirs: HashMap<String, PathBuf>,
    /// The resolved playerctl binary.
    pub playerctl: PathBuf,
    /// The wlchroma-ctl binary and the config holding the revert preset.
    pub ctl_program: String,
    pub wlchroma_config: PathBuf,
    pub fade_ms: u32,
    /// Where downloaded covers are cached.
    pub cache_dir: PathBuf,
    /// The starting palette mode.
    pub mode: Mode,
}

impl Config {
    fn ctl(&self) -> Ctl<ProcessRunner> {
        Ctl {
            program: self.ctl_program.clone(),
            runner: ProcessRunner,
            config_path: self.wlchroma_config.clone(),
            fade_ms: self.fade_ms,
        }
    }
}

/// Production defaults for the players and their covers directories.
pub fn default_covers_dirs() -> HashMap<String, PathBuf> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    HashMap::from([(
        "jellyfin-tui".to_string(),
        home.join(".local/share/jellyfin-tui/covers"),
    )])
}

/// The worker's stages in the running daemon.
struct DaemonStages {
    resolver: CoverResolver<HttpFetch>,
    memo: PaletteMemo,
    ctl: Ctl<ProcessRunner>,
    stopping: Arc<AtomicBool>,
}

impl Stages for DaemonStages {
    fn resolve(&mut self, art_url: &str, covers_dir: Option<&std::path::Path>) -> Resolution {
        let stopping = Arc::clone(&self.stopping);
        self.resolver.resolve(art_url, covers_dir, &move || {
            stopping.load(Ordering::SeqCst)
        })
    }

    fn extract(
        &mut self,
        path: &std::path::Path,
        mode: Mode,
        content_id: ContentId,
    ) -> [String; 3] {
        self.memo.get(path, mode, content_id)
    }

    fn apply(&mut self, colors: &[String; 3]) -> Result<(), CtlError> {
        self.ctl.apply(colors)
    }

    fn revert(&mut self) -> Result<(), CtlError> {
        self.ctl.revert()
    }
}

/// Revert to the config preset directly, containing a ctl failure. Used at
/// startup (before the loop) and once more at shutdown with the worker
/// stopped, so no in-flight ctl can commit after it.
fn bounded_revert(config: &Config) {
    if let Err(e) = config.ctl().revert() {
        log::warn!("revert failed: {e}");
    }
}

/// Run the daemon until shutdown or a fatal condition. `tx`/`rx` are the
/// event channel; other sources (D-Bus, signals) hold their own clones of
/// `tx`. Returns the process exit code: 0 for a requested shutdown, 1 when
/// playerctl or the worker died, so systemd's `Restart=on-failure` recovers.
pub fn run(config: Config, tx: SyncSender<Event>, mut rx: Receiver<Event>) -> i32 {
    // Start from a known-good default. The worker is seeded with this revert
    // so the first revert job is not redundantly re-applied.
    bounded_revert(&config);

    let mailbox = Arc::new(Mailbox::new());
    // Shutdown sets this; the resolve stage polls it so an in-flight
    // download aborts instead of waiting out its deadline.
    let stopping = Arc::new(AtomicBool::new(false));
    let stages = DaemonStages {
        resolver: CoverResolver::new(config.cache_dir.clone(), HttpFetch::from_env()),
        memo: PaletteMemo::new(),
        ctl: config.ctl(),
        stopping: Arc::clone(&stopping),
    };
    let report_tx = tx.clone();
    let worker = Worker::new(
        Arc::clone(&mailbox),
        stages,
        move |result| {
            let _ = report_tx.send(Event::Worker(result));
        },
        Some(Committed::Revert(config.mode)),
    );
    let mut worker = match WorkerHandle::start(worker) {
        Ok(w) => w,
        Err(e) => {
            log::error!("could not start the palette worker: {e}");
            return 1;
        }
    };

    let mut child = match playerctl::spawn_follow(&config.playerctl, &config.players) {
        Ok(c) => c,
        Err(e) => {
            log::error!("could not start playerctl: {e}");
            stopping.store(true, Ordering::SeqCst);
            worker.stop_and_join(WORKER_STOP_TIMEOUT);
            return 1;
        }
    };
    // From here on, every exit path reaps playerctl (SEC-013).
    let stdout = child.stdout.take().expect("piped stdout");
    let code = match playerctl::start_reader(stdout, tx.clone()) {
        Err(e) => {
            log::error!("could not start the playerctl reader: {e}");
            1
        }
        Ok(_reader) => {
            let covers_dirs = config.covers_dirs.clone();
            let host = RuntimeHost::new(Arc::clone(&mailbox), worker.liveness(), default_jitter());
            let mut coord = Coordinator::new(
                host,
                move |name: &str| covers_dirs.get(name).cloned(),
                config.mode,
                &config.players,
            );
            drop(tx); // sources hold their own senders
            match run_loop(&mut rx, &mut coord) {
                LoopExit::Shutdown => {
                    // Stop scheduling and invalidate in-flight results, drop
                    // queued work, abort a download, join the worker, then do
                    // the final revert with the worker stopped.
                    coord.begin_shutdown();
                    mailbox.clear();
                    stopping.store(true, Ordering::SeqCst);
                    if !worker.stop_and_join(WORKER_STOP_TIMEOUT) {
                        log::warn!(
                            "worker did not stop within {}s",
                            WORKER_STOP_TIMEOUT.as_secs()
                        );
                    }
                    bounded_revert(&config);
                    0
                }
                LoopExit::Hangup => {
                    log::error!("playerctl exited; exiting for restart");
                    1
                }
                LoopExit::WorkerDied => {
                    log::error!("palette worker thread died; exiting for restart");
                    1
                }
                LoopExit::Disconnected => 1,
            }
        }
    };
    stopping.store(true, Ordering::SeqCst);
    worker.stop_and_join(WORKER_STOP_TIMEOUT);
    playerctl::terminate_child(&mut child, playerctl::CHILD_STOP_TIMEOUT);
    code
}

/// The fade the daemon uses unless configured otherwise.
pub const DEFAULT_FADE_MS: u32 = FADE_MS;
