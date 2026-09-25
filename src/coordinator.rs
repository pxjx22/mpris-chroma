//! Main-thread coordinator for the event pipeline (port of `coordinator.py`,
//! SEC-001 phase 4b, SEC-018 phase 4c).
//!
//! I/O-free by construction: the runtime owns the event sources and calls
//! these handlers. The coordinator parses events, updates player state,
//! decides the desired end-state, and submits it to the worker — it never
//! blocks on resolution, download, or ctl. Results come back through
//! [`Coordinator::adopt`] on the same thread, where the generation check
//! makes stale adoption impossible (design guarantee a).
//!
//! Everything the Python injects as separate callables (submit, schedule,
//! cancel, jitter, clock) is one [`Host`], so tests drive the coordinator
//! with a recording fake and the runtime supplies the real one.

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

use crate::select::{Selection, decide};
use crate::state::{Mode, PlaybackStatus, PlayerState};
use crate::worker::{CoverTarget, Desired, JobResult, Outcome};

pub const MPRIS_PREFIX: &str = "org.mpris.MediaPlayer2.";

/// Dropped-input logging (SEC-011 §2.4): one warning per category per
/// interval, so a garbage flood cannot turn into a journal flood.
pub const DROP_LOG_INTERVAL: Duration = Duration::from_secs(60);

/// Retry backoff (SEC-018): capped exponential with jitter, then terminal.
/// Three attempts at 1s/2s/4s ~ 7s total.
pub const RETRY_BASE_MS: u64 = 1000;
pub const RETRY_CAP_MS: u64 = 30_000;
pub const RETRY_MAX_ATTEMPTS: u32 = 3;
pub const RETRY_JITTER: f64 = 0.15;

/// Delay before retry `attempt` (1-based): `min(BASE * 2^(n-1), CAP) * jitter`,
/// truncated like Python's `int()`.
pub fn backoff_delay_ms(attempt: u32, jitter: f64) -> u64 {
    let factor = 1u64
        .checked_shl(attempt.saturating_sub(1))
        .unwrap_or(u64::MAX);
    let capped = RETRY_BASE_MS.saturating_mul(factor).min(RETRY_CAP_MS);
    (capped as f64 * jitter) as u64
}

/// Map the portal's color-scheme value to a palette mode. Anything that is
/// not an explicit light preference (0 = no preference, future values) falls
/// back to dark.
pub fn mode_from_color_scheme(value: u32) -> Mode {
    if value == 2 { Mode::Light } else { Mode::Dark }
}

/// Map a D-Bus name to playerctl's `{{playerName}}` key, or `None` if it is
/// not an MPRIS player.
pub fn player_name_from_bus(bus_name: &str) -> Option<&str> {
    bus_name.strip_prefix(MPRIS_PREFIX)
}

/// A player's cover-resolution status (SEC-018 §3).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum CoverStatus {
    /// Never attempted (or reset); resolvable on selection.
    Pending,
    /// Last attempt confirmed a palette for this identity.
    Ready,
    /// Transient failure; the winner's timer owns the retry.
    Retrying,
    /// Transient failures past the cap; the player's next MPRIS event
    /// re-opens one bounded window.
    Exhausted,
    /// Deterministic policy refusal; only an art-identity change unlocks it.
    Rejected,
}

impl CoverStatus {
    fn eligible(self) -> bool {
        matches!(self, Self::Pending | Self::Ready | Self::Retrying)
    }
}

/// What a cover's state is keyed on: the art URL, or for dir-scan players
/// with no URL, their covers directory.
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum CoverIdentity {
    Url(String),
    Dir(PathBuf),
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub struct CoverState {
    pub identity: CoverIdentity,
    pub status: CoverStatus,
}

/// Which players have a local covers directory (dir-scan players).
type CoversDirFor = Box<dyn Fn(&str) -> Option<PathBuf>>;

/// A handle for an armed retry timer.
pub type TimerId = u64;

/// Everything the coordinator needs from the outside world.
pub trait Host {
    /// Hand a job to the worker's mailbox. Must not block.
    fn submit(&mut self, generation: u64, desired: Desired);
    /// Arm the (single) one-shot retry timer. When it fires, the runtime calls
    /// [`Coordinator::fire_retry`].
    fn schedule_retry(&mut self, delay_ms: u64) -> TimerId;
    /// Disarm a timer returned by `schedule_retry`.
    fn cancel_retry(&mut self, timer: TimerId);
    /// Retry jitter multiplier in [1 - RETRY_JITTER, 1 + RETRY_JITTER].
    fn jitter(&mut self) -> f64;
    /// Monotonic time, for rate limiting.
    fn now(&self) -> Duration;
    /// Emit a warning (the runtime logs it; tests count it).
    fn warn(&mut self, message: String) {
        log::warn!("{message}");
    }
}

/// Owns per-player state and the generation-versioned scheduling of worker
/// jobs. Every method runs on the one event-loop thread.
pub struct Coordinator<H: Host> {
    pub players: HashMap<String, PlayerState>,
    /// Per-player resolution state.
    pub covers: HashMap<String, CoverState>,
    pub seq: u64,
    pub generation: u64,
    pub mode: Mode,
    /// Bookkeeping/observability only.
    pub applied: Option<String>,
    /// The target whose palette wlchroma is showing: the retone anchor
    /// (design §5). Set on a committed/skipped apply, cleared by a committed
    /// revert (the preset is not ours to re-tone).
    pub applied_target: Option<CoverTarget>,
    /// Last decided value.
    pub last_desired: Option<Desired>,
    /// Dedup key.
    pub last_submitted: Option<Desired>,
    pub stopping: bool,
    host: H,
    covers_dir_for: CoversDirFor,
    /// Winner of the last submit.
    submitted_player: Option<String>,
    // Winner-only retry means at most one armed timer, so retry state is a
    // few scalars, not a map.
    retry_timer: Option<TimerId>,
    retry_desire: Option<Desired>,
    retry_player: Option<String>,
    retry_attempt: u32,
    /// The same string handed to `playerctl --player=` (SEC-011 §2.2), so the
    /// follow set and the accept set cannot drift.
    allowed_bases: Vec<String>,
    last_drop_log: HashMap<&'static str, Duration>,
}

impl<H: Host> Coordinator<H> {
    pub fn new(
        host: H,
        covers_dir_for: impl Fn(&str) -> Option<PathBuf> + 'static,
        mode: Mode,
        allowed_players: &str,
    ) -> Self {
        let allowed_bases = allowed_players
            .split(',')
            .map(str::trim)
            .filter(|p| !p.is_empty())
            .map(String::from)
            .collect();
        Self {
            players: HashMap::new(),
            covers: HashMap::new(),
            seq: 0,
            generation: 0,
            mode,
            applied: None,
            applied_target: None,
            last_desired: None,
            last_submitted: None,
            stopping: false,
            host,
            covers_dir_for: Box::new(covers_dir_for),
            submitted_player: None,
            retry_timer: None,
            retry_desire: None,
            retry_player: None,
            retry_attempt: 0,
            allowed_bases,
            last_drop_log: HashMap::new(),
        }
    }

    pub fn host(&self) -> &H {
        &self.host
    }

    pub fn host_mut(&mut self) -> &mut H {
        &mut self.host
    }

    /// Warn about dropped input, rate-limited per category. Public because
    /// the framer's oversize drops go through the same limiter.
    pub fn log_drop(&mut self, category: &'static str, detail: &str) {
        let t = self.host.now();
        let due = self
            .last_drop_log
            .get(category)
            .is_none_or(|&last| t.saturating_sub(last) >= DROP_LOG_INTERVAL);
        if due {
            self.last_drop_log.insert(category, t);
            self.host
                .warn(format!("dropped playerctl input ({category}): {detail}"));
        }
    }

    // --- event handlers --------------------------------------------------

    /// Validate and parse one `name\tstatus\tartUrl` playerctl line, update
    /// that player's state and cover state, and (re)decide. Does no I/O.
    ///
    /// This is the trust boundary for player-controlled bytes (SEC-011 §2): a
    /// rejected line mutates nothing — no player state, no seq bump, no
    /// decision — so a hostile peer cannot move the pipeline with forged input.
    pub fn on_line(&mut self, line: &str) {
        let parts: Vec<&str> = line.trim_end_matches('\n').split('\t').collect();
        // Exactly three fields or nothing — never salvage the first three. A
        // tab inside artUrl (playerctl renders it raw) produces extra fields,
        // and acting on the prefix would apply a URL the player never sent.
        let [name, status, art_url] = parts[..] else {
            let n = parts.len();
            self.log_drop("fields", &format!("expected 3 fields, got {n}"));
            return;
        };
        if !self.allowed_name(name) {
            self.log_drop("name", "line named a player we do not follow");
            return;
        }
        let Ok(status) = status.parse::<PlaybackStatus>() else {
            // Dropping (rather than treating it as not-playing) keeps prior state.
            self.log_drop("status", "unrecognized playback status");
            return;
        };
        self.seq += 1;
        let state = PlayerState {
            status,
            art_url: art_url.to_string(),
            seq: self.seq,
        };
        self.players.insert(name.to_string(), state);
        // Cover-state maintenance (§3): a new art identity resets to Pending;
        // Exhausted also resets on the player's own next event with an
        // unchanged identity (one fresh bounded window per event). Rejected
        // resets only on an identity change, so identical lines cannot spin.
        match self.art_identity(name, art_url) {
            None => {
                self.covers.remove(name);
            }
            Some(ident) => match self.covers.get_mut(name) {
                Some(st) if st.identity == ident => {
                    if st.status == CoverStatus::Exhausted {
                        st.status = CoverStatus::Pending;
                        // F1: a url player's desired value is unchanged on its
                        // next line, so value-dedup would absorb the resubmit
                        // and strand it. Fires only on the Exhausted->Pending
                        // edge, so identical lines between episodes still dedup.
                        self.last_submitted = None;
                    }
                }
                _ => {
                    let fresh = CoverState {
                        identity: ident,
                        status: CoverStatus::Pending,
                    };
                    self.covers.insert(name.to_string(), fresh);
                }
            },
        }
        self.decide_and_submit(true);
    }

    /// A D-Bus name was lost. If it was a tracked player, evict it and
    /// re-decide — this is what reverts to the preset when the last player
    /// closes (`playerctl --follow` emits no line for a vanished player).
    pub fn on_vanish(&mut self, bus_name: &str) {
        let Some(name) = player_name_from_bus(bus_name) else {
            return;
        };
        if self.players.remove(name).is_none() {
            return;
        }
        self.covers.remove(name); // a returning player starts Pending
        self.decide_and_submit(false);
    }

    /// The portal color-scheme changed. Adopt the new mode, then (§5):
    ///
    /// - if what is showing is not the pipeline's winner (e.g. an older cover
    ///   held while a newer player retries), re-tone the shown cover;
    /// - otherwise re-run selection under the new mode, which re-tones the
    ///   winner and supersedes any in-flight old-mode job;
    /// - reverted/idle: just record the mode for the next apply.
    pub fn on_scheme(&mut self, value: u32) {
        let new_mode = mode_from_color_scheme(value);
        if new_mode == self.mode {
            return;
        }
        self.mode = new_mode;
        let (winner_desired, winner_name) = match self.decide() {
            Selection::Apply { desired, winner } => (Some(desired), Some(winner)),
            Selection::Revert(desired) => (Some(desired), None),
            Selection::Hold => (None, None),
        };
        if let Some(shown) = self.applied_target.clone() {
            let winner_is_shown = winner_desired
                .as_ref()
                .is_some_and(|d| d.target.as_ref() == Some(&shown));
            if !winner_is_shown {
                let retone = Desired {
                    target: Some(shown),
                    mode: new_mode,
                };
                self.maybe_submit(retone, None, true);
                return;
            }
        }
        if let Some(desired) = winner_desired.filter(|d| d.target.is_some()) {
            self.maybe_submit(desired, winner_name, true);
        }
    }

    /// A worker result, delivered on the event-loop thread. The generation
    /// only changes on this thread, so the check is exact and a stale result
    /// can never corrupt state (guarantee a).
    pub fn adopt(&mut self, result: JobResult) {
        if self.stopping || result.generation != self.generation {
            return;
        }
        match result.outcome {
            Outcome::Committed | Outcome::SkippedDuplicate => {
                // A skipped duplicate means wlchroma already shows this
                // content+mode: success for bookkeeping purposes.
                self.applied = result.cover_id;
                self.applied_target = self.last_submitted.as_ref().and_then(|d| d.target.clone());
                self.retry_desire = None;
                self.retry_attempt = 0; // backoff restarts
                self.set_cover_status(CoverStatus::Ready);
            }
            Outcome::FailedRetryable => {
                // §4.1: the dedup key stays set (an identical line dedups) and
                // the backoff timer owns resubmission.
                let exhausted = self.arm_retry();
                let status = if exhausted {
                    CoverStatus::Exhausted
                } else {
                    CoverStatus::Retrying
                };
                self.set_cover_status(status);
            }
            Outcome::Rejected => {
                // Terminal without a metadata change: no timer, and the kept
                // dedup key means repeats of the same line cannot spin.
                self.set_cover_status(CoverStatus::Rejected);
            }
        }
        // §5 defect 3: re-select after every transition, so a terminal one
        // falls back to an older ready cover without an unrelated event.
        // dir_resubmit=false so this cannot race the retry timer.
        self.decide_and_submit(false);
    }

    /// Enter shutdown: stop scheduling, cancel any armed retry, and
    /// invalidate every in-flight result by bumping the generation.
    pub fn begin_shutdown(&mut self) {
        self.stopping = true;
        self.generation += 1;
        self.cancel_retry();
    }

    /// The armed retry timer fired. Force-resubmit the retried desire with a
    /// fresh generation, bypassing value-dedup (its value equals
    /// `last_submitted`, so dedup would absorb it).
    pub fn fire_retry(&mut self) {
        self.retry_timer = None; // consumed by firing; never cancel it again
        if self.stopping {
            return;
        }
        let Some(desire) = self.retry_desire.clone() else {
            return;
        };
        // Guard 2: the desired state moved on between arm and fire. Not
        // reachable through the public API today (in the Python either): every
        // change of desired value bumps the generation, which cancels the
        // retry and clears `retry_desire` above. Kept as defence in depth; no
        // test exercises it, so removing it would not fail the suite.
        if self.last_desired.as_ref() != Some(&desire) {
            return;
        }
        self.generation += 1; // a genuine new attempt; its result must be adoptable
        self.submitted_player = self.retry_player.clone();
        self.host.submit(self.generation, desire);
    }

    // --- internals -------------------------------------------------------

    /// Mirrors playerctl's `--player=` matching, which ignores the MPRIS
    /// `.instanceN` suffix: allowed iff equal to a configured base or starting
    /// with `base.` — never a bare prefix, or `spotifyevil` rides in on
    /// `spotify`.
    fn allowed_name(&self, name: &str) -> bool {
        self.allowed_bases.iter().any(|base| {
            name == base
                || name
                    .strip_prefix(base.as_str())
                    .is_some_and(|rest| rest.starts_with('.'))
        })
    }

    /// The cover-state key (§3): the art_url when present, else the player's
    /// covers_dir for dir-scan players, else `None` (no art source).
    fn art_identity(&self, name: &str, art_url: &str) -> Option<CoverIdentity> {
        if !art_url.is_empty() {
            return Some(CoverIdentity::Url(art_url.to_string()));
        }
        (self.covers_dir_for)(name).map(CoverIdentity::Dir)
    }

    fn decide(&self) -> Selection {
        let covers = &self.covers;
        decide(
            &self.players,
            self.mode,
            |n| (self.covers_dir_for)(n),
            // State-based only; no state yet means never attempted: eligible.
            |n| covers.get(n).is_none_or(|st| st.status.eligible()),
        )
    }

    /// Transition the cover state of the player whose job just reported.
    /// Retone submissions carry no player, so they transition nothing.
    fn set_cover_status(&mut self, status: CoverStatus) {
        let Some(player) = &self.submitted_player else {
            return;
        };
        if let Some(st) = self.covers.get_mut(player) {
            st.status = status;
        }
    }

    fn decide_and_submit(&mut self, dir_resubmit: bool) {
        match self.decide() {
            Selection::Hold => {} // keep the current palette
            Selection::Apply { desired, winner } => {
                self.maybe_submit(desired, Some(winner), dir_resubmit);
            }
            Selection::Revert(desired) => self.maybe_submit(desired, None, dir_resubmit),
        }
    }

    fn maybe_submit(&mut self, desired: Desired, player: Option<String>, dir_resubmit: bool) {
        if self.stopping {
            return;
        }
        self.last_desired = Some(desired.clone());
        if self.last_submitted.as_ref() == Some(&desired) {
            // Same value. For a stable identity (revert or non-empty art_url)
            // that is a true duplicate. For a dir-scan identity the file may
            // have changed, so a LINE event resubmits, reusing the current
            // generation: it cannot preempt a running job (design §3).
            let stable = desired
                .target
                .as_ref()
                .is_none_or(|t| !t.art_url.is_empty());
            if !dir_resubmit || stable {
                return;
            }
            self.submitted_player = player;
            self.host.submit(self.generation, desired);
            return;
        }
        match (&desired.target, &player) {
            (Some(t), Some(p)) if t.art_url.is_empty() => {
                log::info!("{p} is playing: covers dir scan ({})", desired.mode)
            }
            (Some(t), Some(p)) => log::info!("{p} is playing: {} ({})", t.art_url, desired.mode),
            (Some(t), None) => log::info!("cover {} ({})", t.art_url, desired.mode),
            (None, _) => log::info!("nothing playing: revert to the preset"),
        }
        self.generation += 1;
        self.cancel_retry(); // guard 1: a new desired value supersedes any armed retry
        self.last_submitted = Some(desired.clone());
        self.submitted_player = player;
        self.host.submit(self.generation, desired);
    }

    /// A retryable failure for the current desire: count the attempt and arm
    /// the backoff timer. Returns true past the cap (exhausted, terminal).
    fn arm_retry(&mut self) -> bool {
        let desire = self.last_submitted.clone();
        if desire != self.retry_desire {
            self.retry_desire = desire; // new target: fresh count
            self.retry_attempt = 0;
        }
        self.retry_attempt += 1;
        if self.retry_attempt > RETRY_MAX_ATTEMPTS {
            if self.retry_attempt == RETRY_MAX_ATTEMPTS + 1 {
                let msg = format!(
                    "giving up on {:?} after {RETRY_MAX_ATTEMPTS} attempts",
                    self.retry_desire
                );
                self.host.warn(msg); // log the transition once
            }
            return true;
        }
        if let Some(timer) = self.retry_timer.take() {
            // A same-desire failure while a timer is armed (e.g. a dir-scan
            // line resubmitted alongside it) replaces the timer.
            self.host.cancel_retry(timer);
        }
        let jitter = self.host.jitter();
        let delay = backoff_delay_ms(self.retry_attempt, jitter);
        self.retry_timer = Some(self.host.schedule_retry(delay));
        self.retry_player = self.submitted_player.clone();
        false
    }

    /// Cancel any armed retry. The superseded player loses its retry chain
    /// (winner-only retry), so it is demoted Retrying -> Pending: a future
    /// re-selection attempts it fresh instead of stranding it.
    fn cancel_retry(&mut self) {
        if let Some(timer) = self.retry_timer.take() {
            self.host.cancel_retry(timer);
            if let Some(st) = self
                .retry_player
                .as_ref()
                .and_then(|p| self.covers.get_mut(p))
            {
                if st.status == CoverStatus::Retrying {
                    st.status = CoverStatus::Pending;
                }
            }
        }
        self.retry_desire = None;
        self.retry_player = None;
        self.retry_attempt = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PLAYERS: &str = "jellyfin-tui,spotify";
    const JF_DIR: &str = "/covers/jf";

    #[derive(Default)]
    struct FakeHost {
        submitted: Vec<(u64, Desired)>,
        scheduled: Vec<u64>, // delay per armed timer
        cancelled: Vec<TimerId>,
        next_handle: TimerId,
        clock: Duration,
        warnings: Vec<String>,
    }

    impl Host for FakeHost {
        fn submit(&mut self, generation: u64, desired: Desired) {
            self.submitted.push((generation, desired));
        }
        fn schedule_retry(&mut self, delay_ms: u64) -> TimerId {
            self.next_handle += 1;
            self.scheduled.push(delay_ms);
            self.next_handle
        }
        fn cancel_retry(&mut self, timer: TimerId) {
            self.cancelled.push(timer);
        }
        fn jitter(&mut self) -> f64 {
            1.0 // deterministic in tests
        }
        fn now(&self) -> Duration {
            self.clock
        }
        fn warn(&mut self, message: String) {
            self.warnings.push(message);
        }
    }

    type C = Coordinator<FakeHost>;

    fn harness_with(mode: Mode, allowed: &str) -> C {
        let covers_dir_for = |name: &str| (name == "jellyfin-tui").then(|| PathBuf::from(JF_DIR));
        Coordinator::new(FakeHost::default(), covers_dir_for, mode, allowed)
    }

    fn harness() -> C {
        harness_with(Mode::Dark, PLAYERS)
    }

    fn line(name: &str, status: &str, art: &str) -> String {
        format!("{name}\t{status}\t{art}\n")
    }

    fn target(art: &str, dir: Option<&str>) -> CoverTarget {
        CoverTarget {
            art_url: art.into(),
            covers_dir: dir.map(PathBuf::from),
        }
    }

    fn apply(art: &str, dir: Option<&str>, mode: Mode) -> Desired {
        Desired {
            target: Some(target(art, dir)),
            mode,
        }
    }

    fn revert(mode: Mode) -> Desired {
        Desired { target: None, mode }
    }

    fn result(generation: u64, outcome: Outcome, cover_id: Option<&str>) -> JobResult {
        JobResult {
            generation,
            outcome,
            cover_id: cover_id.map(String::from),
        }
    }

    trait Harness {
        fn submitted(&self) -> &[(u64, Desired)];
        fn last(&self) -> (u64, Desired);
        fn last_gen(&self) -> u64;
        fn adopt_last(&mut self, outcome: Outcome, cover_id: Option<&str>);
        fn commit(&mut self, cover_id: &str);
        fn fire_last_timer(&mut self);
    }

    impl Harness for C {
        fn submitted(&self) -> &[(u64, Desired)] {
            &self.host().submitted
        }
        fn last(&self) -> (u64, Desired) {
            self.submitted().last().expect("a submission").clone()
        }
        fn last_gen(&self) -> u64 {
            self.last().0
        }
        fn adopt_last(&mut self, outcome: Outcome, cover_id: Option<&str>) {
            let g = self.last_gen();
            self.adopt(result(g, outcome, cover_id));
        }
        fn commit(&mut self, cover_id: &str) {
            self.adopt_last(Outcome::Committed, Some(cover_id));
        }
        fn fire_last_timer(&mut self) {
            assert!(!self.host().scheduled.is_empty(), "no timer armed");
            self.fire_retry();
        }
    }

    use Outcome::*;

    // --- bus names (test_bus.py) -----------------------------------------

    #[test]
    fn bus_names_map_to_player_names() {
        assert_eq!(
            player_name_from_bus("org.mpris.MediaPlayer2.spotify"),
            Some("spotify")
        );
        assert_eq!(
            player_name_from_bus("org.mpris.MediaPlayer2.jellyfin-tui.instance7"),
            Some("jellyfin-tui.instance7")
        );
        assert_eq!(player_name_from_bus("org.freedesktop.Notifications"), None);
        assert_eq!(player_name_from_bus(":1.42"), None);
    }

    // --- colour scheme map -----------------------------------------------

    #[test]
    fn color_scheme_maps_light_only_for_2() {
        assert_eq!(mode_from_color_scheme(1), Mode::Dark);
        assert_eq!(mode_from_color_scheme(2), Mode::Light);
        assert_eq!(mode_from_color_scheme(0), Mode::Dark);
        assert_eq!(mode_from_color_scheme(7), Mode::Dark);
    }

    // --- on_line ---------------------------------------------------------

    #[test]
    fn playing_with_art_submits_an_apply() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        assert_eq!(h.submitted(), [(1, apply("https://x/a", None, Mode::Dark))]);
    }

    #[test]
    fn malformed_line_is_ignored() {
        let mut h = harness();
        h.on_line("garbage\n");
        assert!(h.submitted().is_empty());
    }

    #[test]
    fn repeated_identical_line_is_deduped() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        assert_eq!(h.submitted().len(), 1);
    }

    #[test]
    fn newer_cover_supersedes_and_bumps_gen() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.on_line(&line("spotify", "Playing", "https://x/b"));
        let gens: Vec<u64> = h.submitted().iter().map(|s| s.0).collect();
        assert_eq!(gens, [1, 2]);
    }

    #[test]
    fn stop_submits_a_revert() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.on_line(&line("spotify", "Stopped", ""));
        assert_eq!(h.last().1, revert(Mode::Dark));
    }

    #[test]
    fn playing_without_art_source_holds_no_submit() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", ""));
        assert!(h.submitted().is_empty());
    }

    #[test]
    fn dir_scan_player_always_resubmits_reusing_gen() {
        let mut h = harness();
        h.on_line(&line("jellyfin-tui", "Playing", ""));
        h.on_line(&line("jellyfin-tui", "Playing", ""));
        let gens: Vec<u64> = h.submitted().iter().map(|s| s.0).collect();
        assert_eq!(gens, [1, 1]); // resubmitted, gen reused
    }

    // --- on_vanish -------------------------------------------------------

    #[test]
    fn last_player_vanishing_submits_revert_and_evicts() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.on_vanish("org.mpris.MediaPlayer2.spotify");
        assert!(!h.players.contains_key("spotify"));
        assert_eq!(h.last().1, revert(Mode::Dark));
    }

    #[test]
    fn non_player_bus_name_is_ignored() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        let before = h.submitted().len();
        h.on_vanish("org.freedesktop.Notifications");
        assert_eq!(h.submitted().len(), before);
        assert!(h.players.contains_key("spotify"));
    }

    #[test]
    fn unknown_player_vanishing_is_noop() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        let before = h.submitted().len();
        h.on_vanish("org.mpris.MediaPlayer2.firefox");
        assert_eq!(h.submitted().len(), before);
    }

    // --- on_scheme -------------------------------------------------------

    #[test]
    fn flip_retones_current_target_in_new_mode() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.on_scheme(2);
        assert_eq!(h.last().1, apply("https://x/a", None, Mode::Light));
    }

    #[test]
    fn same_mode_is_noop() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        let before = h.submitted().len();
        h.on_scheme(1);
        assert_eq!(h.submitted().len(), before);
    }

    #[test]
    fn flip_with_nothing_applied_updates_mode_only() {
        let mut h = harness();
        h.on_scheme(2);
        assert!(h.submitted().is_empty());
        assert_eq!(h.mode, Mode::Light);
    }

    #[test]
    fn flip_after_revert_does_not_retone() {
        let mut h = harness();
        h.on_line(&line("spotify", "Stopped", ""));
        let before = h.submitted().len();
        h.on_scheme(2);
        assert_eq!(h.submitted().len(), before);
    }

    // --- adopt -----------------------------------------------------------

    #[test]
    fn committed_updates_applied_bookkeeping() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.commit("/cache/a");
        assert_eq!(h.applied.as_deref(), Some("/cache/a"));
    }

    #[test]
    fn stale_gen_result_is_dropped() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a")); // gen 1
        h.on_line(&line("spotify", "Playing", "https://x/b")); // gen 2
        h.adopt(result(1, Committed, Some("/cache/a")));
        assert_eq!(h.applied, None);
    }

    #[test]
    fn slow_job_does_not_block_a_later_theme_flip() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a")); // in flight
        h.on_scheme(2);
        assert_eq!(h.last().1.mode, Mode::Light);
    }

    // --- backoff ---------------------------------------------------------

    #[test]
    fn backoff_delay_math() {
        assert_eq!(backoff_delay_ms(1, 1.0), RETRY_BASE_MS);
        assert_eq!(backoff_delay_ms(2, 1.0), 2 * RETRY_BASE_MS);
        assert_eq!(backoff_delay_ms(3, 1.0), 4 * RETRY_BASE_MS);
        assert_eq!(backoff_delay_ms(30, 1.0), RETRY_CAP_MS);
        assert_eq!(backoff_delay_ms(200, 1.0), RETRY_CAP_MS); // no shift overflow
        assert_eq!(
            backoff_delay_ms(1, 1.5),
            (1.5 * RETRY_BASE_MS as f64) as u64
        );
    }

    // --- retry timer -----------------------------------------------------

    fn fail_apply(h: &mut C, art: &str) -> u64 {
        h.on_line(&line("spotify", "Playing", art));
        let g = h.last_gen();
        h.adopt(result(g, FailedRetryable, None));
        g
    }

    #[test]
    fn retryable_failure_arms_timer_and_identical_line_dedups() {
        let mut h = harness();
        fail_apply(&mut h, "https://x/a");
        assert_eq!(h.host().scheduled, [RETRY_BASE_MS]);
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        assert_eq!(h.submitted().len(), 1); // deduped; the timer will retry
    }

    #[test]
    fn timer_fire_force_resubmits_with_fresh_gen() {
        let mut h = harness();
        let g = fail_apply(&mut h, "https://x/a");
        h.fire_last_timer();
        assert_eq!(h.submitted().len(), 2);
        assert_eq!(h.last_gen(), g + 1);
        assert_eq!(h.last().1, h.submitted()[0].1);
    }

    #[test]
    fn backoff_doubles_across_consecutive_failures() {
        let mut h = harness();
        let g = fail_apply(&mut h, "https://x/a");
        h.fire_last_timer();
        h.adopt(result(g + 1, FailedRetryable, None));
        assert_eq!(h.host().scheduled, [RETRY_BASE_MS, 2 * RETRY_BASE_MS]);
    }

    #[test]
    fn attempt_cap_exhausts_no_further_timer() {
        let mut h = harness();
        let g = fail_apply(&mut h, "https://x/a");
        for i in 1..=RETRY_MAX_ATTEMPTS as u64 {
            h.fire_last_timer();
            h.adopt(result(g + i, FailedRetryable, None));
        }
        assert_eq!(h.host().scheduled.len(), RETRY_MAX_ATTEMPTS as usize);
        assert_eq!(h.host().warnings.len(), 1, "giving-up logged once");
    }

    #[test]
    fn gen_bump_cancels_armed_timer() {
        let mut h = harness();
        fail_apply(&mut h, "https://x/a");
        let armed = h.host().next_handle;
        h.on_line(&line("spotify", "Playing", "https://x/b"));
        assert_eq!(h.host().cancelled, [armed]);
    }

    #[test]
    fn late_fire_after_desire_changed_is_dropped() {
        let mut h = harness();
        fail_apply(&mut h, "https://x/a");
        h.on_line(&line("spotify", "Playing", "https://x/b"));
        let before = h.submitted().len();
        h.fire_retry(); // the race where cancel lost to the dispatch
        assert_eq!(h.submitted().len(), before);
    }

    #[test]
    fn rejected_arms_no_timer_and_identical_line_dedups() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.adopt_last(Rejected, None);
        assert!(h.host().scheduled.is_empty());
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        assert_eq!(h.submitted().len(), 1);
    }

    #[test]
    fn new_desire_restarts_backoff_from_base() {
        let mut h = harness();
        let g = fail_apply(&mut h, "https://x/a");
        h.fire_last_timer();
        h.adopt(result(g + 1, FailedRetryable, None)); // A at 2x now
        h.on_line(&line("spotify", "Playing", "https://x/b"));
        h.adopt_last(FailedRetryable, None);
        assert_eq!(h.host().scheduled.last(), Some(&RETRY_BASE_MS));
    }

    #[test]
    fn shutdown_cancels_armed_timer_and_late_fire_is_noop() {
        let mut h = harness();
        fail_apply(&mut h, "https://x/a");
        let armed = h.host().next_handle;
        h.begin_shutdown();
        assert!(h.host().cancelled.contains(&armed));
        let before = h.submitted().len();
        h.fire_retry(); // late dispatch after shutdown
        assert_eq!(h.submitted().len(), before);
    }

    // --- ranking (design §5) ---------------------------------------------

    #[test]
    fn rejected_newest_falls_back_to_older_ready_without_new_event() {
        let mut h = harness();
        h.on_line(&line("jellyfin-tui", "Playing", "https://x/j"));
        h.commit("/cache/j");
        h.on_line(&line("spotify", "Playing", "https://x/s"));
        h.adopt_last(Rejected, None);
        assert_eq!(h.last().1.target, Some(target("https://x/j", Some(JF_DIR))));
    }

    #[test]
    fn own_event_of_applied_player_does_not_flap_to_other_ready() {
        let mut h = harness();
        h.on_line(&line("jellyfin-tui", "Playing", "https://x/j"));
        h.commit("/cache/j");
        h.on_line(&line("spotify", "Playing", "https://x/s"));
        h.commit("/cache/s");
        let before = h.submitted().len();
        h.on_line(&line("spotify", "Playing", "https://x/s"));
        assert_eq!(h.submitted().len(), before);
    }

    #[test]
    fn retrying_newest_stays_selected_and_timer_survives() {
        let mut h = harness();
        h.on_line(&line("jellyfin-tui", "Playing", "https://x/j"));
        h.commit("/cache/j");
        h.on_line(&line("spotify", "Playing", "https://x/s"));
        let before = h.submitted().len();
        h.adopt_last(FailedRetryable, None);
        assert_eq!(h.submitted().len(), before); // no fallback submission
        assert!(h.host().cancelled.is_empty()); // timer survives
        assert_eq!(h.host().scheduled.len(), 1);
    }

    #[test]
    fn exhausted_newest_falls_back_to_older_ready() {
        let mut h = harness();
        h.on_line(&line("jellyfin-tui", "Playing", "https://x/j"));
        h.commit("/cache/j");
        h.on_line(&line("spotify", "Playing", "https://x/s"));
        h.adopt_last(FailedRetryable, None);
        for _ in 0..RETRY_MAX_ATTEMPTS {
            h.fire_last_timer();
            h.adopt_last(FailedRetryable, None);
        }
        assert_eq!(h.last().1.target, Some(target("https://x/j", Some(JF_DIR))));
    }

    #[test]
    fn transient_failure_retries_and_eventually_applies() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.adopt_last(FailedRetryable, None);
        h.fire_last_timer();
        h.commit("/cache/a");
        assert_eq!(h.applied.as_deref(), Some("/cache/a"));
    }

    #[test]
    fn retone_anchors_on_applied_cover_not_retrying_winner() {
        let mut h = harness();
        h.on_line(&line("jellyfin-tui", "Playing", "https://x/j"));
        h.commit("/cache/j");
        h.on_line(&line("spotify", "Playing", "https://x/s"));
        h.adopt_last(FailedRetryable, None); // spotify retrying
        h.on_scheme(2);
        assert_eq!(h.last().1, apply("https://x/j", Some(JF_DIR), Mode::Light));
    }

    #[test]
    fn retone_commit_restarts_the_superseded_retry_chain() {
        let mut h = harness();
        h.on_line(&line("jellyfin-tui", "Playing", "https://x/j"));
        h.commit("/cache/j");
        h.on_line(&line("spotify", "Playing", "https://x/s"));
        h.adopt_last(FailedRetryable, None);
        h.on_scheme(2); // retone jellyfin; spotify's timer dies
        assert_eq!(h.host().cancelled.len(), 1);
        h.commit("/cache/j"); // the retone lands
        assert_eq!(h.last().1, apply("https://x/s", None, Mode::Light));
    }

    #[test]
    fn exhausted_dir_player_recovers_on_its_next_line() {
        let mut h = harness();
        h.on_line(&line("jellyfin-tui", "Playing", ""));
        h.adopt_last(FailedRetryable, None);
        for _ in 0..RETRY_MAX_ATTEMPTS {
            h.fire_last_timer();
            h.adopt_last(FailedRetryable, None);
        }
        let before = h.submitted().len();
        h.on_line(&line("jellyfin-tui", "Playing", ""));
        assert_eq!(h.submitted().len(), before + 1);
    }

    #[test]
    fn exhausted_url_player_recovers_on_its_next_line() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.adopt_last(FailedRetryable, None);
        for _ in 0..RETRY_MAX_ATTEMPTS {
            h.fire_last_timer();
            h.adopt_last(FailedRetryable, None);
        }
        let before = h.submitted().len();
        h.on_line(&line("spotify", "Playing", "https://x/a")); // same track
        assert_eq!(h.submitted().len(), before + 1);
        let n = h.submitted().len();
        assert!(h.submitted()[n - 1].0 > h.submitted()[n - 2].0, "fresh gen");
    }

    #[test]
    fn rejected_player_recovers_on_identity_change() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/bad"));
        h.adopt_last(Rejected, None);
        h.on_line(&line("spotify", "Playing", "https://x/good"));
        assert_eq!(h.last().1.target, Some(target("https://x/good", None)));
    }

    // --- line validation (SEC-011 §2) ------------------------------------

    #[test]
    fn line_with_extra_fields_is_rejected() {
        let mut h = harness();
        h.on_line("spotify\tPlaying\thttps://x/a\tPlaying\thttps://evil\n");
        assert!(h.submitted().is_empty());
    }

    #[test]
    fn line_with_too_few_fields_is_rejected() {
        let mut h = harness();
        h.on_line("spotify\tPlaying\n");
        assert!(h.players.is_empty());
    }

    #[test]
    fn empty_art_url_is_still_three_fields_and_accepted() {
        let mut h = harness();
        h.on_line("jellyfin-tui\tPlaying\t\n");
        assert_eq!(h.submitted().len(), 1);
        assert_eq!(h.last().1.target, Some(target("", Some(JF_DIR))));
    }

    #[test]
    fn forged_line_naming_an_unfollowed_player_is_rejected() {
        let mut h = harness();
        h.on_line(&line("evilplayer", "Playing", "https://evil/a"));
        assert!(h.submitted().is_empty());
        assert!(h.players.is_empty());
    }

    #[test]
    fn instance_suffixed_name_is_accepted() {
        let mut h = harness();
        h.on_line(&line("spotify.instance42", "Playing", "https://x/a"));
        assert_eq!(h.submitted().len(), 1);
    }

    #[test]
    fn lookalike_prefixed_name_is_rejected() {
        let mut h = harness();
        h.on_line(&line("spotifyevil", "Playing", "https://x/a"));
        assert!(h.submitted().is_empty());
    }

    #[test]
    fn allowlist_comes_from_the_configured_player_set() {
        let mut h = harness_with(Mode::Dark, "onlythis");
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        assert!(h.submitted().is_empty());
        h.on_line(&line("onlythis", "Playing", "https://x/a"));
        assert_eq!(h.submitted().len(), 1);
    }

    #[test]
    fn unknown_status_is_rejected_not_treated_as_stopped() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.on_line(&line("spotify", "Bogus", "https://x/a"));
        assert_eq!(h.submitted().len(), 1); // no revert submitted
        assert_eq!(h.players["spotify"].status, PlaybackStatus::Playing);
    }

    #[test]
    fn rejected_line_mutates_nothing() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        let (players, seq, covers) = (h.players.clone(), h.seq, h.covers.clone());
        for bad in [
            "garbage\n".to_string(),
            "spotify\tPlaying\n".to_string(),
            line("evilplayer", "Playing", "u"),
            line("spotify", "Bogus", "u"),
            "spotify\tPlaying\tu\textra\n".to_string(),
        ] {
            h.on_line(&bad);
        }
        assert_eq!(h.players, players);
        assert_eq!(h.seq, seq);
        assert_eq!(h.covers, covers);
        assert_eq!(h.submitted().len(), 1);
    }

    // --- inactive events (PERF-001) --------------------------------------

    #[test]
    fn pause_never_submits_a_cover_bearing_job() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.on_line(&line("spotify", "Paused", "https://x/a"));
        assert_eq!(h.last().1, revert(Mode::Dark));
        let rest: Vec<_> = h.submitted()[1..]
            .iter()
            .map(|s| s.1.target.clone())
            .collect();
        assert_eq!(rest, [None]);
    }

    #[test]
    fn stop_never_submits_a_cover_bearing_job() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.on_line(&line("spotify", "Stopped", "https://x/a"));
        let rest: Vec<_> = h.submitted()[1..]
            .iter()
            .map(|s| s.1.target.clone())
            .collect();
        assert_eq!(rest, [None]);
    }

    #[test]
    fn resume_does_submit_the_cover() {
        let mut h = harness();
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        h.on_line(&line("spotify", "Paused", "https://x/a"));
        h.on_line(&line("spotify", "Playing", "https://x/a"));
        assert_eq!(h.last().1.target, Some(target("https://x/a", None)));
    }

    #[test]
    fn paused_dir_scan_player_does_not_trigger_a_dir_resubmit() {
        let mut h = harness();
        h.on_line(&line("jellyfin-tui", "Playing", ""));
        h.on_line(&line("jellyfin-tui", "Paused", ""));
        let before = h.submitted().len();
        h.on_line(&line("jellyfin-tui", "Paused", ""));
        h.on_line(&line("jellyfin-tui", "Paused", ""));
        assert_eq!(h.submitted().len(), before);
    }

    // --- drop logging (SEC-011 §2.4) -------------------------------------

    #[test]
    fn repeated_drops_in_one_category_log_once_per_interval() {
        let mut h = harness();
        for _ in 0..50 {
            h.on_line(&line("evilplayer", "Playing", "https://evil/a"));
        }
        assert_eq!(h.host().warnings.len(), 1);
    }

    #[test]
    fn a_category_logs_again_after_the_interval() {
        let mut h = harness();
        h.on_line(&line("evilplayer", "Playing", "u"));
        h.host_mut().clock += DROP_LOG_INTERVAL;
        h.on_line(&line("evilplayer", "Playing", "u"));
        assert_eq!(h.host().warnings.len(), 2);
    }

    #[test]
    fn categories_are_limited_independently() {
        let mut h = harness();
        h.on_line(&line("evilplayer", "Playing", "u")); // bad name
        h.on_line(&line("spotify", "Bogus", "u")); // bad status
        h.on_line("spotify\tPlaying\n"); // bad field count
        assert_eq!(h.host().warnings.len(), 3);
    }

    #[test]
    fn framer_drops_share_the_same_limiter() {
        let mut h = harness();
        for _ in 0..10 {
            h.log_drop("oversize", "line exceeded the frame limit");
        }
        assert_eq!(h.host().warnings.len(), 1);
    }
}
