//! A fixed timer of a lakehouse service, as `lakehouse.timers` keeps one: a loop in the service's own process that
//! wakes once a window, and runs its work only where its claim of the window landed on the store.
//!
//! Every replica runs the loop. Each claims the window before it works, with one conditional write of a small record
//! under the control root (made only if none exists, or carrying the ETag of the record it read); the store lets one
//! such write land, as it picks one writer of a Lance version (`lance_docs/lance/format/table/transaction.md`
//! § Storage Primitives). A window is named by when it began on the STORE's clock, read with the claim.
//!
//! Work that fails is logged and runs again at the next window: a timer's work is level-triggered, so a window lost is
//! time lost and nothing else.

use crate::pywire;
use crate::storage::StorageError;
use crate::storage::records::{Found, Records, StoreRead, Unless};
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::json;
use std::time::Duration;
use tokio::time::Instant;

pub const PREFIX: &str = "_timers";
/// How soon a timer whose claim could not be read or written reads it again, where its period is longer.
pub const RETRY: Duration = Duration::from_secs(15);
/// The shortest a timer waits between two turns, so a boundary met exactly is never a spin.
const SHORTEST_WAIT: Duration = Duration::from_millis(50);

/// How one turn ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Ran,
    Failed,
    /// The window is another replica's, or was claimed before this turn.
    Elsewhere,
    /// The service holds its timers, as through a read-only window: nothing was claimed.
    Held,
    /// The claim could not be read or written, so nothing ran.
    Unread,
}

/// One turn: how it ended, the window it was about (in microseconds since the epoch), and how long until the next.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Turn {
    pub outcome: Outcome,
    pub window: Option<i64>,
    pub wait: Duration,
}

/// Where a timer's claims are read and written.
pub trait Claims {
    fn read_at(&self, key: &str) -> impl Future<Output = Result<StoreRead, StorageError>> + Send;
    /// Put `body` unless `unless` holds; `false` when it did.
    fn put(&self, key: &str, body: Vec<u8>, unless: Unless<'_>) -> impl Future<Output = Result<bool, StorageError>> + Send;
}

impl Claims for Records {
    async fn read_at(&self, key: &str) -> Result<StoreRead, StorageError> {
        Records::read_at(self, key).await
    }

    async fn put(&self, key: &str, body: Vec<u8>, unless: Unless<'_>) -> Result<bool, StorageError> {
        Records::put(self, key, body, unless).await
    }
}

pub fn claim_key(name: &str) -> String {
    format!("{PREFIX}/{name}.json")
}

/// `seconds` as whole microseconds, rounded half to even as Python's `datetime` and `timedelta` take a float.
fn micros(seconds: f64) -> i64 {
    let whole = seconds.floor();
    (whole as i64) * 1_000_000 + ((seconds - whole) * 1e6).round_ties_even() as i64
}

/// Python's `a // b` for floats.
fn floor_div(a: f64, b: f64) -> f64 {
    let rest = a % b;
    let mut quotient = (a - rest) / b;
    if rest != 0.0 && (b < 0.0) != (rest < 0.0) {
        quotient -= 1.0;
    }
    let floored = quotient.floor();
    if quotient - floored > 0.5 { floored + 1.0 } else { floored }
}

/// The window the store's clock `at` (microseconds) is in, for a period of `period` seconds, and the seconds until the
/// next one begins: Python's float arithmetic, so a Python and a Rust replica name the same window.
pub fn window_of(at: i64, period: f64) -> (i64, f64) {
    let window = micros(floor_div(at as f64 / 1e6, period) * period);
    (window, (window + micros(period) - at) as f64 / 1e6)
}

/// Whether a claim of `claimed` (microseconds) is another replica's for the window `window` the store's clock `at` is in:
/// at or after the window, and not more than a period ahead of the store's own time, since a store's front ends stamp
/// whole seconds and may differ by one. One further ahead was claimed by a clock that ran away, and is claimed over.
pub fn held_elsewhere(claimed: i64, window: i64, at: i64, period: f64) -> bool {
    window <= claimed && claimed <= at + micros(period)
}

/// `at` (microseconds) as pydantic writes a UTC datetime: `Z`, and six digits of fraction only where there is one.
pub fn iso(at: i64) -> String {
    let format = if at % 1_000_000 == 0 { SecondsFormat::Secs } else { SecondsFormat::Micros };
    DateTime::<Utc>::from_timestamp_micros(at).map_or_else(String::new, |at| at.to_rfc3339_opts(format, true))
}

/// The record a claim of `window` writes, as Python's `json.dumps` writes it, so either replica reads the other's.
pub fn claim_record(name: &str, window: i64, claimed_at: i64, by: &str) -> String {
    let claim = json!({"timer": name, "window": iso(window), "claimed_at": iso(claimed_at), "by": by});
    pywire::dumps(&claim, pywire::DEFAULT)
}

/// The window a stored claim names, or why it names none.
fn claimed_window(body: &[u8]) -> Result<i64, String> {
    let claim: serde_json::Value = serde_json::from_slice(body).map_err(|error| format!("the claim is no JSON: {error}"))?;
    let unread = || "the claim names no timer, window, claim time and claimant".to_owned();
    let text = |name: &str| claim.get(name).and_then(serde_json::Value::as_str).ok_or_else(unread);
    text("timer")?;
    text("by")?;
    DateTime::parse_from_rfc3339(text("claimed_at")?).map_err(|_| unread())?;
    Ok(DateTime::parse_from_rfc3339(text("window")?).map_err(|_| unread())?.timestamp_micros())
}

/// One fixed timer of one replica.
pub struct Timer<C> {
    pub name: String,
    period: f64,
    claims: C,
    holder: String,
}

/// What one read of the claim and the write after it settled: whether the window is this replica's, which, and the
/// seconds until the next.
struct Asked {
    mine: bool,
    window: i64,
    until_next: f64,
}

impl<C: Claims> Timer<C> {
    /// A timer of `period` seconds claimed on `claims` by `holder` (the pod's host name).
    pub fn new(name: &str, period: f64, claims: C, holder: &str) -> Result<Self, String> {
        if !(period > 0.0 && period.is_finite()) {
            return Err(format!("timer {name:?} needs a period above zero seconds, got {period}"));
        }
        Ok(Timer {
            name: name.to_owned(),
            period,
            claims,
            holder: holder.to_owned(),
        })
    }

    /// Take turn after turn for as long as the process runs; `held` is asked at every turn.
    pub async fn run(&self, held: impl Fn() -> bool, mut work: impl AsyncFnMut() -> Result<(), String>) {
        loop {
            let turn = self.turn(held(), async || work().await).await;
            tokio::time::sleep(turn.wait).await;
        }
    }

    /// Claim the window the store's clock is in and run `work` where the claim landed; how long until the next turn.
    pub async fn turn(&self, held: bool, work: impl AsyncFnOnce() -> Result<(), String>) -> Turn {
        let period = Duration::try_from_secs_f64(self.period).unwrap_or(Duration::MAX);
        if held {
            tracing::info!(timer = self.name, "timer_held");
            return self.ended(Outcome::Held, None, period);
        }
        let began = Instant::now();
        let asked = match self.ask().await {
            Ok(asked) => asked,
            Err(error) => {
                let error: String = error.chars().take(300).collect();
                tracing::warn!(timer = self.name, error, "timer_claim_unread");
                return self.ended(Outcome::Unread, None, period.min(RETRY));
            }
        };
        let until_next = Duration::try_from_secs_f64(asked.until_next).unwrap_or_default();
        if !asked.mine {
            return self.ended(Outcome::Elsewhere, Some(asked.window), until_next);
        }
        let outcome = match work().await {
            Ok(()) => Outcome::Ran,
            Err(error) => {
                tracing::error!(timer = self.name, error, "timer_work_failed");
                Outcome::Failed
            }
        };
        self.ended(outcome, Some(asked.window), until_next.saturating_sub(began.elapsed()))
    }

    fn ended(&self, outcome: Outcome, window: Option<i64>, wait: Duration) -> Turn {
        tracing::debug!(timer = self.name, outcome = ?outcome, "timer_turn");
        Turn {
            outcome,
            window,
            wait: wait.max(SHORTEST_WAIT),
        }
    }

    async fn ask(&self) -> Result<Asked, String> {
        let key = claim_key(&self.name);
        let read = self.claims.read_at(&key).await.map_err(|error| error.to_string())?;
        let at = read.at.timestamp_micros();
        let (window, until_next) = window_of(at, self.period);
        let (held, etag, denied) = match read.found {
            Found::Held { body, etag } => (Some(claimed_window(&body)?), Some(etag), None),
            Found::Absent => (None, None, None),
            Found::Denied(error) => (None, None, Some(error)),
        };
        if let Some(claimed) = held.filter(|claimed| held_elsewhere(*claimed, window, at, self.period)) {
            return Ok(Asked {
                mine: false,
                window: claimed,
                until_next,
            });
        }
        let record = claim_record(&self.name, window, at, &self.holder).into_bytes();
        let unless = etag.as_deref().map_or(Unless::Present, Unless::Changed);
        let mine = self.claims.put(&key, record, unless).await.map_err(|error| error.to_string())?;
        if let (false, Some(denied)) = (mine, denied) {
            // The record exists and this identity may not read it: a fault to say, never another replica's window.
            return Err(denied.to_string());
        }
        Ok(Asked { mine, window, until_next })
    }
}

/// This pod's host name, which a claim names its holder by.
pub fn host_name() -> String {
    std::env::var("HOSTNAME")
        .ok()
        .or_else(|| std::fs::read_to_string("/etc/hostname").ok())
        .map(|name| name.trim().to_owned())
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| "unknown".to_owned())
}
