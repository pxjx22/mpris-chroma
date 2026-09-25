//! `mpris-chroma`: sync wlchroma's background colours to the playing cover.
//!
//! Environment:
//! - `WLCHROMA_CTL`: path to `wlchroma-ctl` (default: found on `PATH`).
//! - `MPRIS_CHROMA_MODE=light|dark`: force the palette mode; otherwise the
//!   desktop color-scheme is followed live via the settings portal.
//! - `MPRIS_CHROMA_ART_DOMAINS`: extra allowlisted artwork domains.
//! - `MPRIS_CHROMA_LOG=debug|info|warn|error`: log level (default warn).

use std::sync::mpsc::sync_channel;

use mpris_chroma::apply::{default_config_path, default_ctl};
use mpris_chroma::cover::cache::default_cache_dir;
use mpris_chroma::daemon::{self, Config, DEFAULT_FADE_MS};
use mpris_chroma::runtime::EVENT_QUEUE;
use mpris_chroma::sources::{dbus, playerctl, signals};
use mpris_chroma::state::Mode;

/// Logs to stderr with sd-daemon priority prefixes, which journald parses
/// into levels.
struct JournalLogger(log::LevelFilter);

impl log::Log for JournalLogger {
    fn enabled(&self, meta: &log::Metadata<'_>) -> bool {
        meta.level() <= self.0
    }

    fn log(&self, record: &log::Record<'_>) {
        if self.enabled(record.metadata()) {
            let prio = match record.level() {
                log::Level::Error => 3,
                log::Level::Warn => 4,
                log::Level::Info => 6,
                log::Level::Debug | log::Level::Trace => 7,
            };
            eprintln!("<{prio}>{}: {}", record.target(), record.args());
        }
    }

    fn flush(&self) {}
}

fn init_logging() {
    let level = match std::env::var("MPRIS_CHROMA_LOG").as_deref() {
        Ok("debug") => log::LevelFilter::Debug,
        Ok("info") => log::LevelFilter::Info,
        Ok("error") => log::LevelFilter::Error,
        _ => log::LevelFilter::Warn,
    };
    let logger: &'static JournalLogger = Box::leak(Box::new(JournalLogger(level)));
    if log::set_logger(logger).is_ok() {
        log::set_max_level(level);
    }
}

fn main() {
    init_logging();

    // Resolve and verify playerctl once, up front (SEC-017).
    let playerctl = match playerctl::resolve_playerctl() {
        Ok(p) => p,
        Err(e) => {
            log::error!("{e}");
            std::process::exit(1);
        }
    };
    let bus = match zbus::blocking::Connection::session() {
        Ok(c) => c,
        Err(e) => {
            log::error!("cannot connect to the session bus: {e}");
            std::process::exit(1);
        }
    };

    let forced = std::env::var("MPRIS_CHROMA_MODE")
        .unwrap_or_default()
        .to_lowercase();
    let forced_mode = forced.parse::<Mode>().ok();
    let mode = forced_mode.unwrap_or_else(|| {
        dbus::read_color_scheme(&bus).map_or(
            Mode::Dark,
            mpris_chroma::coordinator::mode_from_color_scheme,
        )
    });

    let (tx, rx) = sync_channel(EVENT_QUEUE);
    let started = dbus::watch_vanish(&bus, tx.clone())
        .and_then(|_| signals::watch_signals(tx.clone()))
        .and_then(|_| match forced_mode {
            Some(_) => Ok(()),
            None => dbus::watch_scheme(&bus, tx.clone()).map(drop),
        });
    if let Err(e) = started {
        log::error!("could not start event sources: {e}");
        std::process::exit(1);
    }

    let config = Config {
        players: playerctl::PLAYERS.to_string(),
        covers_dirs: daemon::default_covers_dirs(),
        playerctl,
        ctl_program: default_ctl(),
        wlchroma_config: default_config_path(),
        fade_ms: DEFAULT_FADE_MS,
        cache_dir: default_cache_dir(),
        mode,
    };
    let code = daemon::run(config, tx, rx);
    drop(bus);
    std::process::exit(code);
}
