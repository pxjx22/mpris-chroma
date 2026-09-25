//! The event loop (the GLib main loop and its wiring in `sync.py`).
//!
//! Every source — the playerctl reader, the D-Bus listeners, signals, the
//! worker's results — is a thread that sends [`Event`]s into one bounded
//! channel; [`run_loop`] drains it on one thread and drives the coordinator.
//! The coordinator's single retry timer is a deadline, checked after every
//! event as well as by the receive timeout, so a flood of input cannot starve
//! it (the property `test_flood_integration.py` guards). The channel is
//! bounded, so a flooding producer blocks on send and backpressure reaches
//! the playerctl pipe, as one-read-per-dispatch does under GLib.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

use crate::coordinator::{Coordinator, Host, RETRY_JITTER, TimerId};
use crate::worker::{Desired, Job, JobResult, Mailbox};

/// Capacity of the event channel: enough to absorb a burst, small enough
/// that a flood blocks its producer quickly.
pub const EVENT_QUEUE: usize = 64;

/// Something happened that the coordinator should hear about.
#[derive(Debug)]
pub enum Event {
    /// One framed playerctl line.
    Line(String),
    /// The framer refused an oversized line.
    Oversize(String),
    /// playerctl's stdout closed or failed: playerctl is gone.
    Hangup,
    /// A D-Bus name was lost.
    Vanish(String),
    /// The portal's color-scheme changed.
    Scheme(u32),
    /// The portal sent a color-scheme we cannot read.
    SchemeMalformed(String),
    /// A worker result.
    Worker(JobResult),
    /// SIGTERM/SIGINT (or a test) asked us to stop.
    Shutdown,
}

/// Why the loop returned.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum LoopExit {
    /// Graceful stop requested.
    Shutdown,
    /// playerctl died: exit non-zero so systemd restarts us.
    Hangup,
    /// The worker thread is gone: exit non-zero so systemd restarts us.
    WorkerDied,
    /// Every sender is gone (only possible in tests).
    Disconnected,
}

type Jitter = Box<dyn FnMut() -> f64 + Send>;

/// A small xorshift PRNG for retry jitter: spreading retries needs no
/// cryptographic quality, and this avoids a dependency.
pub fn default_jitter() -> Jitter {
    let seed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0x9e37_79b9_7f4a_7c15, |d| d.as_nanos() as u64)
        | 1;
    let mut state = seed;
    Box::new(move || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        let unit = (state >> 11) as f64 / (1u64 << 53) as f64; // [0, 1)
        1.0 - RETRY_JITTER + 2.0 * RETRY_JITTER * unit
    })
}

/// The coordinator's [`Host`] in the running daemon.
pub struct RuntimeHost {
    mailbox: Arc<Mailbox>,
    worker_alive: Arc<AtomicBool>,
    /// The armed retry timer and when it is due.
    pub timer: Option<(TimerId, Instant)>,
    next_timer: TimerId,
    start: Instant,
    jitter: Jitter,
    /// Set when a submit found the worker dead.
    pub worker_dead: bool,
}

impl RuntimeHost {
    pub fn new(mailbox: Arc<Mailbox>, worker_alive: Arc<AtomicBool>, jitter: Jitter) -> Self {
        RuntimeHost {
            mailbox,
            worker_alive,
            timer: None,
            next_timer: 0,
            start: Instant::now(),
            jitter,
            worker_dead: false,
        }
    }
}

impl Host for RuntimeHost {
    /// Put the job on the mailbox, unless the worker thread has died: then
    /// flag it and drop the job. A daemon that kept accepting jobs nothing
    /// runs would degrade silently behind a healthy-looking PID; instead the
    /// loop exits non-zero for systemd to restart us (SEC-001 §2.3).
    fn submit(&mut self, generation: u64, desired: Desired) {
        if !self.worker_alive.load(Ordering::SeqCst) {
            self.worker_dead = true;
            return;
        }
        self.mailbox.put(Job {
            generation,
            desired,
        });
    }

    fn schedule_retry(&mut self, delay_ms: u64) -> TimerId {
        self.next_timer += 1;
        let due = Instant::now() + Duration::from_millis(delay_ms);
        self.timer = Some((self.next_timer, due));
        self.next_timer
    }

    fn cancel_retry(&mut self, timer: TimerId) {
        if self.timer.is_some_and(|(id, _)| id == timer) {
            self.timer = None;
        }
    }

    fn jitter(&mut self) -> f64 {
        (self.jitter)()
    }

    fn now(&self) -> Duration {
        self.start.elapsed()
    }
}

/// Fire the retry timer if it is due.
fn fire_due_timer(coord: &mut Coordinator<RuntimeHost>) {
    if let Some((_, due)) = coord.host().timer {
        if Instant::now() >= due {
            coord.host_mut().timer = None; // consumed by firing
            coord.fire_retry();
        }
    }
}

/// Where the loop's events come from: the channel in the daemon, a scripted
/// source in tests.
pub trait EventSource {
    /// The next event, waiting at most `timeout` (forever if `None`).
    fn next_event(&mut self, timeout: Option<Duration>) -> Next;
}

/// What waiting for an event produced.
pub enum Next {
    Event(Event),
    Timeout,
    Disconnected,
}

impl EventSource for Receiver<Event> {
    fn next_event(&mut self, timeout: Option<Duration>) -> Next {
        match timeout {
            Some(t) => match self.recv_timeout(t) {
                Ok(e) => Next::Event(e),
                Err(RecvTimeoutError::Timeout) => Next::Timeout,
                Err(RecvTimeoutError::Disconnected) => Next::Disconnected,
            },
            None => self.recv().map_or(Next::Disconnected, Next::Event),
        }
    }
}

/// Drain events into the coordinator until something ends the loop.
pub fn run_loop(events: &mut impl EventSource, coord: &mut Coordinator<RuntimeHost>) -> LoopExit {
    loop {
        let timeout = coord
            .host()
            .timer
            .map(|(_, due)| due.saturating_duration_since(Instant::now()));
        match events.next_event(timeout) {
            Next::Timeout => {}
            Next::Disconnected => return LoopExit::Disconnected,
            Next::Event(Event::Line(line)) => coord.on_line(&line),
            Next::Event(Event::Oversize(detail)) => coord.log_drop("oversize", &detail),
            Next::Event(Event::Vanish(name)) => coord.on_vanish(&name),
            Next::Event(Event::Scheme(value)) => coord.on_scheme(value),
            Next::Event(Event::SchemeMalformed(detail)) => coord.log_drop("scheme", &detail),
            Next::Event(Event::Worker(result)) => coord.adopt(result),
            Next::Event(Event::Hangup) => return LoopExit::Hangup,
            Next::Event(Event::Shutdown) => return LoopExit::Shutdown,
        }
        // After every event too, not only on timeout: under a constant stream
        // the wait never times out, and the retry must still fire on time.
        fire_due_timer(coord);
        if coord.host().worker_dead {
            return LoopExit::WorkerDied;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::Mode;
    use crate::worker::Outcome;
    use std::path::PathBuf;
    use std::sync::mpsc::sync_channel;

    fn coordinator(alive: bool, jitter: f64) -> (Coordinator<RuntimeHost>, Arc<Mailbox>) {
        let mb = Arc::new(Mailbox::new());
        let host = RuntimeHost::new(
            Arc::clone(&mb),
            Arc::new(AtomicBool::new(alive)),
            Box::new(move || jitter),
        );
        let dirs = |n: &str| (n == "jellyfin-tui").then(|| PathBuf::from("/covers"));
        let coord = Coordinator::new(host, dirs, Mode::Dark, "jellyfin-tui,spotify");
        (coord, mb)
    }

    /// The pending job, if any (generations start at 1).
    fn take(mb: &Mailbox) -> Option<Job> {
        mb.superseded(0)
            .then(|| mb.get(&AtomicBool::new(false)))
            .flatten()
    }

    #[test]
    fn jitter_stays_in_range() {
        let mut j = default_jitter();
        for _ in 0..10_000 {
            let v = j();
            assert!((1.0 - RETRY_JITTER..1.0 + RETRY_JITTER).contains(&v), "{v}");
        }
    }

    #[test]
    fn events_drive_the_coordinator_and_shutdown_ends_the_loop() {
        let (mut coord, mb) = coordinator(true, 1.0);
        let (tx, mut rx) = sync_channel(EVENT_QUEUE);
        tx.send(Event::Line(
            "spotify\tPlaying\thttps://i.scdn.co/a\n".into(),
        ))
        .unwrap();
        tx.send(Event::Shutdown).unwrap();
        assert_eq!(run_loop(&mut rx, &mut coord), LoopExit::Shutdown);
        let job = take(&mb).expect("a job");
        assert_eq!(job.generation, 1);
    }

    #[test]
    fn hangup_and_disconnect_end_the_loop() {
        let (mut coord, _) = coordinator(true, 1.0);
        let (tx, mut rx) = sync_channel(EVENT_QUEUE);
        tx.send(Event::Hangup).unwrap();
        assert_eq!(run_loop(&mut rx, &mut coord), LoopExit::Hangup);
        drop(tx);
        assert_eq!(run_loop(&mut rx, &mut coord), LoopExit::Disconnected);
    }

    #[test]
    fn a_dead_worker_ends_the_loop_and_drops_the_job() {
        let (mut coord, mb) = coordinator(false, 1.0);
        let (tx, mut rx) = sync_channel(EVENT_QUEUE);
        tx.send(Event::Line(
            "spotify\tPlaying\thttps://i.scdn.co/a\n".into(),
        ))
        .unwrap();
        assert_eq!(run_loop(&mut rx, &mut coord), LoopExit::WorkerDied);
        assert!(take(&mb).is_none());
    }

    /// Always has an event ready (it never times out): the worst case for
    /// a loop that only checks its timer when the wait times out.
    struct Flood {
        script: Vec<Event>,
        mailbox: Arc<Mailbox>,
        until: Instant,
    }

    impl EventSource for Flood {
        fn next_event(&mut self, _: Option<Duration>) -> Next {
            if !self.script.is_empty() {
                return Next::Event(self.script.remove(0));
            }
            // Stop once the retry (generation 2) is in the mailbox, or give up.
            if self.mailbox.superseded(1) || Instant::now() > self.until {
                return Next::Event(Event::Shutdown);
            }
            Next::Event(Event::Line("garbage".into()))
        }
    }

    #[test]
    fn a_retry_fires_on_time_under_an_input_flood() {
        // Arm a ~50 ms retry (1000 ms base * 0.05 jitter), then flood.
        let (mut coord, mb) = coordinator(true, 0.05);
        let armed = Instant::now();
        let mut flood = Flood {
            script: vec![
                Event::Line("spotify\tPlaying\thttps://i.scdn.co/a\n".into()),
                Event::Worker(JobResult {
                    generation: 1,
                    outcome: Outcome::FailedRetryable,
                    cover_id: None,
                }),
            ],
            mailbox: Arc::clone(&mb),
            until: armed + Duration::from_secs(3),
        };
        assert_eq!(run_loop(&mut flood, &mut coord), LoopExit::Shutdown);
        let job = take(&mb).expect("a job");
        assert_eq!(job.generation, 2, "the retry never fired under the flood");
        assert!(armed.elapsed() < Duration::from_secs(2));
    }
}
