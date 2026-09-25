//! Off-main-thread palette worker (port of `worker.py`, SEC-001 phase 4b).
//!
//! I/O-free itself: the stages (resolve, extract, apply, revert) are injected
//! through [`Stages`] and results go to an injected `report`, which the
//! runtime wires to the event loop's channel. So the unit suite drives the
//! worker synchronously, and a real thread only in the lifecycle tests.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Duration;

use crate::apply::CtlError;
use crate::cover::{ContentId, Resolution};
use crate::state::Mode;

/// The inputs cover resolution needs to materialize one cover.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct CoverTarget {
    pub art_url: String,
    pub covers_dir: Option<PathBuf>,
}

/// The end-state the worker converges wlchroma toward. `target: None` is a
/// revert to the config preset; a `CoverTarget` is an apply.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Desired {
    pub target: Option<CoverTarget>,
    pub mode: Mode,
}

/// How a job ended (SEC-018 taxonomy).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Outcome {
    /// Resolve returned Ready and ctl confirmed the change.
    Committed,
    /// The (content_id, mode) guard hit: wlchroma already shows this.
    SkippedDuplicate,
    /// Resolve returned Retryable, or ctl failed (transient).
    FailedRetryable,
    /// Resolve returned Rejected (deterministic policy or content).
    Rejected,
}

/// What the worker hands back to the coordinator's `adopt`.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct JobResult {
    pub generation: u64,
    pub outcome: Outcome,
    /// The resolved cover on an apply's commit/skip, else `None`.
    pub cover_id: Option<String>,
}

/// One unit of work: the coordinator's generation and the desired end-state.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Job {
    pub generation: u64,
    pub desired: Desired,
}

// --- mailbox -------------------------------------------------------------

/// A one-slot, replace-on-put handoff between the coordinator (producer) and
/// the single worker (consumer). Holding only one slot is the coalescing
/// mechanism: a newer desired state overwrites an unconsumed older one.
#[derive(Default)]
pub struct Mailbox {
    slot: Mutex<Option<Job>>,
    ready: Condvar,
}

impl Mailbox {
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Option<Job>> {
        // The slot holds plain data, so a panic elsewhere cannot leave it
        // half-updated; recover from poisoning rather than cascade it.
        self.slot.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn put(&self, job: Job) {
        *self.lock() = Some(job);
        self.ready.notify_one();
    }

    /// Block until a job is available or `stop` is set. `stop` wins: it
    /// returns `None` and drains any pending job so nothing lingers past
    /// shutdown.
    pub fn get(&self, stop: &AtomicBool) -> Option<Job> {
        let mut slot = self.lock();
        while slot.is_none() && !stop.load(Ordering::SeqCst) {
            slot = self.ready.wait(slot).unwrap_or_else(|e| e.into_inner());
        }
        if stop.load(Ordering::SeqCst) {
            *slot = None;
            return None;
        }
        slot.take()
    }

    /// Drop any pending job without blocking (shutdown step 3).
    pub fn clear(&self) {
        *self.lock() = None;
    }

    /// Wake a blocked `get` so it re-checks its stop flag (shutdown). Taking
    /// the lock orders this after the caller's stop store, so the waiter
    /// cannot miss it.
    pub fn wake(&self) {
        let _slot = self.lock();
        self.ready.notify_all();
    }

    /// True iff a strictly-newer job is already waiting. Strict `>` so a
    /// same-generation resubmit (a dir-scan re-run, design §3) does not
    /// preempt the running job.
    pub fn superseded(&self, generation: u64) -> bool {
        self.lock()
            .as_ref()
            .is_some_and(|j| j.generation > generation)
    }
}

// --- worker --------------------------------------------------------------

/// The work a job is made of. Resolve contains its own expected failures
/// (returning a typed [`Resolution`]); apply/revert report ctl failures as
/// [`CtlError`]. Anything else — a panic — is a bug, backstopped by
/// [`Worker::serve`].
pub trait Stages {
    fn resolve(&mut self, art_url: &str, covers_dir: Option<&Path>) -> Resolution;
    fn extract(&mut self, path: &Path, mode: Mode, content_id: ContentId) -> [String; 3];
    fn apply(&mut self, colors: &[String; 3]) -> Result<(), CtlError>;
    fn revert(&mut self) -> Result<(), CtlError>;
}

/// The last change the worker confirmed wlchroma shows (layer-2 dedup).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Committed {
    /// The config preset (a confirmed revert) in this mode.
    Revert(Mode),
    /// This cover content in this mode.
    Cover(ContentId, Mode),
}

type Report = Box<dyn FnMut(JobResult) + Send>;

/// Runs one job at a time: resolve -> extract -> apply, or revert.
pub struct Worker<S> {
    mailbox: Arc<Mailbox>,
    stages: S,
    report: Report,
    /// `None` never matches, so a fresh worker commits its first job.
    /// Production seeds `Some(Revert(startup_mode))` to mirror the inline
    /// startup revert (design §5.2).
    last_committed: Option<Committed>,
}

impl<S: Stages> Worker<S> {
    pub fn new(
        mailbox: Arc<Mailbox>,
        stages: S,
        report: impl FnMut(JobResult) + Send + 'static,
        initial_committed: Option<Committed>,
    ) -> Self {
        Self {
            mailbox,
            stages,
            report: Box::new(report),
            last_committed: initial_committed,
        }
    }

    /// Pump loop: serve jobs until `get` returns `None` (stop signalled).
    pub fn run(&mut self, stop: &AtomicBool) {
        while let Some(job) = self.mailbox.get(stop) {
            self.serve(job);
        }
    }

    /// One pump iteration after `get`: run the job and report a non-`None`
    /// result. A panic (a bug, not a contained resolve/ctl failure) is logged
    /// and reported as a retryable failure so the loop survives; the attempt
    /// cap then bounds how often a deterministic bug is retried.
    pub fn serve(&mut self, job: Job) {
        let generation = job.generation;
        let result = catch_unwind(AssertUnwindSafe(|| self.run_once(job))).unwrap_or_else(|_| {
            log::error!("worker job (generation={generation}) failed unexpectedly");
            Some(JobResult {
                generation,
                outcome: Outcome::FailedRetryable,
                cover_id: None,
            })
        });
        if let Some(result) = result {
            (self.report)(result);
        }
    }

    /// Run one job. Returns `None` when the job was superseded mid-flight (the
    /// newer job in the mailbox reports the authoritative outcome).
    pub fn run_once(&mut self, job: Job) -> Option<JobResult> {
        let Job {
            generation,
            desired,
        } = job;
        let result = |outcome, cover_id| {
            Some(JobResult {
                generation,
                outcome,
                cover_id,
            })
        };
        let Some(target) = desired.target else {
            let key = Committed::Revert(desired.mode);
            if self.last_committed == Some(key) {
                log::debug!("revert skipped: wlchroma already shows the preset");
                return result(Outcome::SkippedDuplicate, None);
            }
            if self.mailbox.superseded(generation) {
                return None; // a newer desire is waiting; drop this stale revert
            }
            if let Err(e) = self.stages.revert() {
                log::info!("revert failed, will retry: {e}");
                return result(Outcome::FailedRetryable, None);
            }
            log::info!("reverted to the preset");
            self.last_committed = Some(key);
            return result(Outcome::Committed, None);
        };
        let (path, content_id) = match self
            .stages
            .resolve(&target.art_url, target.covers_dir.as_deref())
        {
            Resolution::Retryable(why) => {
                log::info!("cover not available yet, will retry: {why}");
                return result(Outcome::FailedRetryable, None);
            }
            Resolution::Rejected(why) => {
                log::info!("cover rejected: {why}");
                return result(Outcome::Rejected, None);
            }
            Resolution::Ready { path, content_id } => (path, content_id),
        };
        let cover_id = Some(path.display().to_string());
        // Dedup on content identity, not pathname.
        let key = Committed::Cover(content_id, desired.mode);
        if self.last_committed == Some(key) {
            log::debug!("{} already applied ({})", path.display(), desired.mode);
            return result(Outcome::SkippedDuplicate, cover_id);
        }
        if self.mailbox.superseded(generation) {
            return None; // drop before extract + ctl
        }
        let colors = self.stages.extract(&path, desired.mode, content_id);
        if self.mailbox.superseded(generation) {
            // Re-check immediately before ctl: extract is not free, so a newer
            // desire may have arrived during it (guarantee b).
            return None;
        }
        if let Err(e) = self.stages.apply(&colors) {
            log::info!("apply failed, will retry: {e}");
            return result(Outcome::FailedRetryable, None);
        }
        log::info!(
            "applied {} from {} ({})",
            colors.join(" "),
            path.display(),
            desired.mode
        );
        self.last_committed = Some(key);
        result(Outcome::Committed, cover_id)
    }
}

// --- thread lifecycle ----------------------------------------------------

/// A worker running on its own thread.
pub struct WorkerHandle {
    mailbox: Arc<Mailbox>,
    stop: Arc<AtomicBool>,
    alive: Arc<AtomicBool>,
    finished: mpsc::Receiver<()>,
    thread: Option<thread::JoinHandle<()>>,
}

/// Clears `alive` and signals `finished` when the thread exits, however it
/// exits (including a panic that escapes the per-job backstop).
struct ExitSignal(mpsc::Sender<()>, Arc<AtomicBool>);

impl Drop for ExitSignal {
    fn drop(&mut self) {
        self.1.store(false, Ordering::SeqCst);
        let _ = self.0.send(());
    }
}

impl WorkerHandle {
    /// Spawn the worker thread.
    pub fn start<S: Stages + Send + 'static>(mut worker: Worker<S>) -> std::io::Result<Self> {
        let mailbox = Arc::clone(&worker.mailbox);
        let stop = Arc::new(AtomicBool::new(false));
        let (tx, finished) = mpsc::channel();
        let alive = Arc::new(AtomicBool::new(true));
        let (thread_stop, thread_alive) = (Arc::clone(&stop), Arc::clone(&alive));
        let thread = thread::Builder::new()
            .name("palette-worker".into())
            .spawn(move || {
                let _exit = ExitSignal(tx, thread_alive);
                worker.run(&thread_stop);
            })?;
        Ok(Self {
            mailbox,
            stop,
            alive,
            finished,
            thread: Some(thread),
        })
    }

    /// Signal stop, wake a blocked `get`, and wait up to `timeout` for the
    /// thread to finish (design §5 shutdown steps 4-5). Returns true if it
    /// did. A wedged thread is left detached rather than blocking shutdown.
    pub fn stop_and_join(&mut self, timeout: Duration) -> bool {
        self.stop.store(true, Ordering::SeqCst);
        self.mailbox.wake();
        if self.thread.is_none() {
            return true; // already joined
        }
        let done = matches!(
            self.finished.recv_timeout(timeout),
            Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected)
        );
        if done {
            if let Some(t) = self.thread.take() {
                let _ = t.join();
            }
        }
        done
    }

    pub fn is_alive(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    /// A shared view of [`Self::is_alive`], for the coordinator's host.
    pub fn liveness(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.alive)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    type Resolve = Box<dyn FnMut(&str, Option<&Path>) -> Resolution + Send>;
    type Extract = Box<dyn FnMut(&Path, Mode, ContentId) -> [String; 3] + Send>;
    type Ctl = Box<dyn FnMut(&[String; 3]) -> Result<(), CtlError> + Send>;
    type Revert = Box<dyn FnMut() -> Result<(), CtlError> + Send>;

    struct Fake {
        resolve: Resolve,
        extract: Extract,
        apply: Ctl,
        revert: Revert,
    }

    impl Stages for Fake {
        fn resolve(&mut self, art_url: &str, covers_dir: Option<&Path>) -> Resolution {
            (self.resolve)(art_url, covers_dir)
        }
        fn extract(&mut self, path: &Path, mode: Mode, content_id: ContentId) -> [String; 3] {
            (self.extract)(path, mode, content_id)
        }
        fn apply(&mut self, colors: &[String; 3]) -> Result<(), CtlError> {
            (self.apply)(colors)
        }
        fn revert(&mut self) -> Result<(), CtlError> {
            (self.revert)()
        }
    }

    fn ready_at(path: &str, content_id: ContentId) -> Resolution {
        Resolution::Ready {
            path: PathBuf::from(path),
            content_id,
        }
    }

    fn ready() -> Resolution {
        ready_at("/covers/a.jpg", (10, 100))
    }

    fn hexes() -> [String; 3] {
        ["#aa0000", "#00bb00", "#0000cc"].map(String::from)
    }

    fn fake() -> Fake {
        Fake {
            resolve: Box::new(|_, _| ready()),
            extract: Box::new(|_, _, _| hexes()),
            apply: Box::new(|_| Ok(())),
            revert: Box::new(|| Ok(())),
        }
    }

    /// A shared call log for closures that move into the worker.
    #[derive(Clone)]
    struct Log<T>(Arc<StdMutex<Vec<T>>>);
    impl<T> Default for Log<T> {
        fn default() -> Self {
            Self(Arc::default())
        }
    }
    impl<T: Clone> Log<T> {
        fn push(&self, v: T) {
            self.0.lock().unwrap().push(v);
        }
        fn get(&self) -> Vec<T> {
            self.0.lock().unwrap().clone()
        }
    }

    fn worker_with(mb: Arc<Mailbox>, stages: Fake, reported: &Log<JobResult>) -> Worker<Fake> {
        let r = reported.clone();
        Worker::new(mb, stages, move |res| r.push(res), None)
    }

    fn worker(stages: Fake) -> Worker<Fake> {
        worker_with(Arc::new(Mailbox::new()), stages, &Log::default())
    }

    fn apply_job(generation: u64, art: &str, mode: Mode) -> Job {
        Job {
            generation,
            desired: Desired {
                target: Some(CoverTarget {
                    art_url: art.into(),
                    covers_dir: None,
                }),
                mode,
            },
        }
    }

    fn revert_job(generation: u64, mode: Mode) -> Job {
        Job {
            generation,
            desired: Desired { target: None, mode },
        }
    }

    fn outcome(r: Option<JobResult>) -> Outcome {
        r.expect("a result").outcome
    }

    // --- mailbox (test_mailbox.py) ---------------------------------------

    #[test]
    fn put_then_get_returns_item() {
        let mb = Mailbox::new();
        mb.put(revert_job(1, Mode::Dark));
        assert_eq!(
            mb.get(&AtomicBool::new(false)),
            Some(revert_job(1, Mode::Dark))
        );
    }

    #[test]
    fn superseded_only_by_a_strictly_newer_pending_job() {
        let mb = Mailbox::new();
        assert!(!mb.superseded(0)); // empty slot
        mb.put(revert_job(5, Mode::Dark));
        assert!(mb.superseded(3));
        assert!(!mb.superseded(5)); // same-gen dir-scan resubmit must not preempt
    }

    #[test]
    fn get_with_stop_set_returns_none_and_discards_pending() {
        let mb = Mailbox::new();
        mb.put(revert_job(1, Mode::Dark));
        assert_eq!(mb.get(&AtomicBool::new(true)), None);
        assert!(!mb.superseded(0));
    }

    #[test]
    fn clear_drops_pending_item() {
        let mb = Mailbox::new();
        mb.put(revert_job(7, Mode::Dark));
        mb.clear();
        assert!(!mb.superseded(0));
    }

    #[test]
    fn put_replaces_unconsumed_item() {
        let mb = Mailbox::new();
        mb.put(revert_job(1, Mode::Dark));
        mb.put(revert_job(2, Mode::Light));
        assert_eq!(
            mb.get(&AtomicBool::new(false)),
            Some(revert_job(2, Mode::Light))
        );
    }

    // --- run_once: apply -------------------------------------------------

    #[test]
    fn apply_job_resolves_extracts_applies_and_commits() {
        let applied = Log::default();
        let a = applied.clone();
        let mut w = worker(Fake {
            apply: Box::new(move |c| {
                a.push(c.clone());
                Ok(())
            }),
            ..fake()
        });
        let r = w.run_once(apply_job(5, "http://x", Mode::Dark)).unwrap();
        assert_eq!((r.generation, r.outcome), (5, Outcome::Committed));
        assert_eq!(applied.get(), [hexes()]);
    }

    #[test]
    fn extract_receives_the_resolved_content_id() {
        let seen = Log::default();
        let s = seen.clone();
        let mut w = worker(Fake {
            resolve: Box::new(|_, _| ready_at("/covers/a.jpg", (42, 4242))),
            extract: Box::new(move |_, _, cid| {
                s.push(cid);
                hexes()
            }),
            ..fake()
        });
        w.run_once(apply_job(5, "http://x", Mode::Dark));
        assert_eq!(seen.get(), [(42, 4242)]);
    }

    // --- run_once: failures ----------------------------------------------

    #[test]
    fn retryable_resolution_is_failed_retryable_and_skips_work() {
        let calls = Log::default();
        let (c1, c2) = (calls.clone(), calls.clone());
        let mut w = worker(Fake {
            resolve: Box::new(|_, _| Resolution::Retryable("network".into())),
            extract: Box::new(move |_, _, _| {
                c1.push("extract");
                hexes()
            }),
            apply: Box::new(move |_| {
                c2.push("apply");
                Ok(())
            }),
            ..fake()
        });
        let r = w.run_once(apply_job(5, "http://x", Mode::Dark)).unwrap();
        assert_eq!((r.outcome, r.cover_id), (Outcome::FailedRetryable, None));
        assert!(calls.get().is_empty());
    }

    #[test]
    fn rejected_resolution_is_rejected_and_skips_work() {
        let applied = Log::default();
        let a = applied.clone();
        let mut w = worker(Fake {
            resolve: Box::new(|_, _| Resolution::Rejected("ssrf".into())),
            apply: Box::new(move |_| {
                a.push(());
                Ok(())
            }),
            ..fake()
        });
        assert_eq!(
            outcome(w.run_once(apply_job(5, "http://x", Mode::Dark))),
            Outcome::Rejected
        );
        assert!(applied.get().is_empty());
    }

    #[test]
    fn ctl_error_on_apply_is_failed_retryable() {
        let mut w = worker(Fake {
            apply: Box::new(|_| Err(CtlError("ctl down".into()))),
            ..fake()
        });
        let r = w.run_once(apply_job(5, "http://x", Mode::Dark));
        assert_eq!(outcome(r), Outcome::FailedRetryable);
    }

    // --- superseded (guarantee b) ----------------------------------------

    #[test]
    fn superseded_before_ctl_aborts_apply() {
        let mb = Arc::new(Mailbox::new());
        let applied = Log::default();
        let a = applied.clone();
        let stages = Fake {
            apply: Box::new(move |_| {
                a.push(());
                Ok(())
            }),
            ..fake()
        };
        let mut w = worker_with(Arc::clone(&mb), stages, &Log::default());
        mb.put(apply_job(9, "http://y", Mode::Dark)); // newer waiting
        assert_eq!(w.run_once(apply_job(5, "http://x", Mode::Dark)), None);
        assert!(applied.get().is_empty());
    }

    #[test]
    fn superseded_before_revert_aborts_revert() {
        let mb = Arc::new(Mailbox::new());
        let reverted = Log::default();
        let r = reverted.clone();
        let stages = Fake {
            revert: Box::new(move || {
                r.push(());
                Ok(())
            }),
            ..fake()
        };
        let mut w = worker_with(Arc::clone(&mb), stages, &Log::default());
        mb.put(revert_job(9, Mode::Dark));
        assert_eq!(w.run_once(revert_job(5, Mode::Dark)), None);
        assert!(reverted.get().is_empty());
    }

    #[test]
    fn superseded_during_extract_aborts_before_ctl() {
        let mb = Arc::new(Mailbox::new());
        let applied = Log::default();
        let a = applied.clone();
        let inner = Arc::clone(&mb);
        let stages = Fake {
            extract: Box::new(move |_, _, _| {
                inner.put(apply_job(9, "http://z", Mode::Dark)); // arrives mid-extract
                hexes()
            }),
            apply: Box::new(move |_| {
                a.push(());
                Ok(())
            }),
            ..fake()
        };
        let mut w = worker_with(Arc::clone(&mb), stages, &Log::default());
        assert_eq!(w.run_once(apply_job(5, "http://x", Mode::Dark)), None);
        assert!(applied.get().is_empty());
    }

    // --- serve -----------------------------------------------------------

    #[test]
    fn serve_reports_the_result() {
        let reported = Log::default();
        let mut w = worker_with(Arc::new(Mailbox::new()), fake(), &reported);
        w.serve(apply_job(5, "http://x", Mode::Dark));
        let outcomes: Vec<_> = reported.get().iter().map(|r| r.outcome).collect();
        assert_eq!(outcomes, [Outcome::Committed]);
    }

    #[test]
    fn serve_does_not_report_when_superseded() {
        let mb = Arc::new(Mailbox::new());
        let reported = Log::default();
        let mut w = worker_with(Arc::clone(&mb), fake(), &reported);
        mb.put(apply_job(9, "http://y", Mode::Dark));
        w.serve(apply_job(5, "http://x", Mode::Dark));
        assert!(reported.get().is_empty());
    }

    #[test]
    fn serve_backstops_a_panic_as_failed_retryable() {
        let reported = Log::default();
        let stages = Fake {
            extract: Box::new(|_, _, _| panic!("bug")),
            ..fake()
        };
        let mut w = worker_with(Arc::new(Mailbox::new()), stages, &reported);
        w.serve(apply_job(5, "http://x", Mode::Dark));
        let outcomes: Vec<_> = reported.get().iter().map(|r| r.outcome).collect();
        assert_eq!(outcomes, [Outcome::FailedRetryable]);
    }

    // --- run loop --------------------------------------------------------

    #[test]
    fn run_serves_each_item_until_stopped() {
        // The Python drives this with a fake mailbox that drains to None; here
        // job 1's report queues job 2, and job 2's report sets stop, which the
        // real get honours.
        let mb = Arc::new(Mailbox::new());
        let stop = Arc::new(AtomicBool::new(false));
        let gens = Log::default();
        let (g, s, inner) = (gens.clone(), Arc::clone(&stop), Arc::clone(&mb));
        let mut w = Worker::new(
            Arc::clone(&mb),
            fake(),
            move |r: JobResult| {
                g.push(r.generation);
                if r.generation == 1 {
                    inner.put(apply_job(2, "http://b", Mode::Dark));
                } else {
                    s.store(true, Ordering::SeqCst);
                }
            },
            None,
        );
        mb.put(apply_job(1, "http://a", Mode::Dark));
        w.run(&stop);
        assert_eq!(gens.get(), [1, 2]);
    }

    // --- revert ----------------------------------------------------------

    #[test]
    fn revert_job_calls_revert_and_commits_with_no_cover() {
        let calls = Log::default();
        let (c1, c2) = (calls.clone(), calls.clone());
        let mut w = worker(Fake {
            revert: Box::new(move || {
                c1.push("revert");
                Ok(())
            }),
            apply: Box::new(move |_| {
                c2.push("apply");
                Ok(())
            }),
            ..fake()
        });
        let r = w.run_once(revert_job(5, Mode::Dark)).unwrap();
        assert_eq!((r.outcome, r.cover_id), (Outcome::Committed, None));
        assert_eq!(calls.get(), ["revert"]);
    }

    #[test]
    fn revert_ctl_error_is_failed_retryable() {
        let mut w = worker(Fake {
            revert: Box::new(|| Err(CtlError("ctl down".into()))),
            ..fake()
        });
        assert_eq!(
            outcome(w.run_once(revert_job(5, Mode::Dark))),
            Outcome::FailedRetryable
        );
    }

    // --- commit dedup (layer 2) ------------------------------------------

    #[test]
    fn identical_apply_second_time_is_skipped_duplicate() {
        let calls = Log::default();
        let (c1, c2) = (calls.clone(), calls.clone());
        let mut w = worker(Fake {
            extract: Box::new(move |_, _, _| {
                c1.push("extract");
                hexes()
            }),
            apply: Box::new(move |_| {
                c2.push("apply");
                Ok(())
            }),
            ..fake()
        });
        let r1 = w.run_once(apply_job(5, "http://x", Mode::Dark));
        let r2 = w.run_once(apply_job(6, "http://x", Mode::Dark)).unwrap();
        assert_eq!(outcome(r1), Outcome::Committed);
        assert_eq!(r2.outcome, Outcome::SkippedDuplicate);
        assert_eq!(r2.cover_id.as_deref(), Some("/covers/a.jpg"));
        assert_eq!(calls.get(), ["extract", "apply"]);
    }

    #[test]
    fn different_mode_reextracts() {
        let applied = Log::default();
        let a = applied.clone();
        let mut w = worker(Fake {
            apply: Box::new(move |_| {
                a.push(());
                Ok(())
            }),
            ..fake()
        });
        w.run_once(apply_job(5, "http://x", Mode::Dark));
        let r2 = w.run_once(apply_job(6, "http://x", Mode::Light));
        assert_eq!(outcome(r2), Outcome::Committed);
        assert_eq!(applied.get().len(), 2);
    }

    #[test]
    fn same_path_new_content_id_reextracts() {
        let mut ids = [(10, 100), (10, 200)].into_iter(); // same size, new mtime
        let applied = Log::default();
        let a = applied.clone();
        let mut w = worker(Fake {
            resolve: Box::new(move |_, _| ready_at("/covers/a.jpg", ids.next().unwrap())),
            apply: Box::new(move |_| {
                a.push(());
                Ok(())
            }),
            ..fake()
        });
        let r1 = w.run_once(apply_job(5, "http://x", Mode::Dark));
        let r2 = w.run_once(apply_job(6, "http://x", Mode::Dark));
        assert_eq!(
            (outcome(r1), outcome(r2)),
            (Outcome::Committed, Outcome::Committed)
        );
        assert_eq!(applied.get().len(), 2);
    }

    #[test]
    fn repeat_revert_is_skipped_duplicate() {
        let reverted = Log::default();
        let r = reverted.clone();
        let mut w = worker(Fake {
            revert: Box::new(move || {
                r.push(());
                Ok(())
            }),
            ..fake()
        });
        let r1 = w.run_once(revert_job(5, Mode::Dark));
        let r2 = w.run_once(revert_job(6, Mode::Dark));
        assert_eq!(outcome(r1), Outcome::Committed);
        assert_eq!(outcome(r2), Outcome::SkippedDuplicate);
        assert_eq!(reverted.get().len(), 1);
    }

    #[test]
    fn initial_committed_seed_skips_matching_first_revert() {
        let reverted = Log::default();
        let r = reverted.clone();
        let stages = Fake {
            revert: Box::new(move || {
                r.push(());
                Ok(())
            }),
            ..fake()
        };
        let mut w = Worker::new(
            Arc::new(Mailbox::new()),
            stages,
            |_| {},
            Some(Committed::Revert(Mode::Dark)),
        );
        assert_eq!(
            outcome(w.run_once(revert_job(5, Mode::Dark))),
            Outcome::SkippedDuplicate
        );
        assert!(reverted.get().is_empty());
    }

    // --- real-thread lifecycle (test_worker_integration.py) --------------
    // Opt-in in the Python; cheap and deterministic here, so always run.

    #[test]
    fn started_worker_serves_a_submitted_job() {
        let mb = Arc::new(Mailbox::new());
        let (tx, rx) = mpsc::channel();
        let w = Worker::new(Arc::clone(&mb), fake(), move |r| tx.send(r).unwrap(), None);
        let mut handle = WorkerHandle::start(w).unwrap();
        mb.put(apply_job(1, "http://x", Mode::Dark));
        let r = rx
            .recv_timeout(Duration::from_secs(2))
            .expect("worker reported");
        assert_eq!(r.outcome, Outcome::Committed);
        assert!(
            handle.stop_and_join(Duration::from_secs(2)),
            "worker did not stop"
        );
        assert!(!handle.is_alive());
    }

    #[test]
    fn stop_wakes_a_worker_blocked_on_an_empty_mailbox() {
        let mb = Arc::new(Mailbox::new());
        let w = Worker::new(Arc::clone(&mb), fake(), |_| {}, None);
        let mut handle = WorkerHandle::start(w).unwrap();
        thread::sleep(Duration::from_millis(50)); // let it block in get()
        assert!(
            handle.stop_and_join(Duration::from_secs(2)),
            "blocked worker not woken"
        );
        assert!(!handle.is_alive());
    }

    #[test]
    fn liveness_clears_when_the_thread_dies() {
        // A panic in `report` escapes the per-job backstop and kills the
        // thread; the liveness flag must say so.
        let mb = Arc::new(Mailbox::new());
        let w = Worker::new(Arc::clone(&mb), fake(), |_| panic!("report blew up"), None);
        let handle = WorkerHandle::start(w).unwrap();
        let alive = handle.liveness();
        assert!(alive.load(Ordering::SeqCst));
        mb.put(apply_job(1, "http://x", Mode::Dark));
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while alive.load(Ordering::SeqCst) && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(!handle.is_alive());
    }

    #[test]
    fn stop_and_join_gives_up_on_a_wedged_job() {
        // A job stuck in a stage must not hang shutdown past the timeout.
        let mb = Arc::new(Mailbox::new());
        let (entered_tx, entered) = mpsc::channel();
        let (release, release_rx) = mpsc::channel::<()>();
        let release_rx = StdMutex::new(release_rx);
        let stages = Fake {
            extract: Box::new(move |_, _, _| {
                entered_tx.send(()).unwrap();
                let _ = release_rx.lock().unwrap().recv();
                hexes()
            }),
            ..fake()
        };
        let w = Worker::new(Arc::clone(&mb), stages, |_| {}, None);
        let mut handle = WorkerHandle::start(w).unwrap();
        mb.put(apply_job(1, "http://x", Mode::Dark));
        entered.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(!handle.stop_and_join(Duration::from_millis(100)));
        release.send(()).unwrap(); // let it finish so the test exits cleanly
        assert!(handle.stop_and_join(Duration::from_secs(2)));
    }
}
