//! SIGTERM/SIGINT as an event (the GLib unix signal sources in `sync.py`).

use std::io;
use std::sync::mpsc::SyncSender;
use std::thread::JoinHandle;

use signal_hook::consts::{SIGINT, SIGTERM};
use signal_hook::iterator::Signals;

use crate::runtime::Event;

/// Turn SIGTERM and SIGINT into [`Event::Shutdown`]. The handler itself only
/// records the signal; the sequenced shutdown runs on the event loop.
pub fn watch_signals(tx: SyncSender<Event>) -> io::Result<JoinHandle<()>> {
    let mut signals = Signals::new([SIGTERM, SIGINT])?;
    std::thread::Builder::new()
        .name("signals".into())
        .spawn(move || {
            for _ in signals.forever() {
                if tx.send(Event::Shutdown).is_err() {
                    return;
                }
            }
        })
}
