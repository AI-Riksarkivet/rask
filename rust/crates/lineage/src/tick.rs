//! The relay tick (`lineage.api.relay`): drain the outbox, retire kept deliveries the feed now holds, forward the feed and
//! prune what retention and the forwarder release. It runs on lineage's own timer, each window claimed on the control
//! root, so it runs once a window whatever the replica count; never on a bus schedule, since the relay exists for the bus
//! being away. Each step degrades to a warning, so one step's failure leaves the others to the tick.
//!
//! Every step is idempotent, so a tick that outlasts its window and overlaps the next is safe: a drained event lands no
//! row twice, is published again at least once and dropped (a second drop finds nothing); two prunes delete the same rows
//! once; two forwards send an event at least once, and neither moves the cursor back.

use crate::authz::gates::authorizing;
use crate::forward::{self, Forwarded};
use crate::gate::{Gate, arrived_data};
use crate::intake::{Authorizing, Recording};
use crate::model::{Event, author_sub_from_payload, parse_event};
use crate::relay::{self, Drained, drain_one};
use crate::settings::Settings;
use crate::signals::LANE;
use lakehouse_bus::{Bus, CloudEvent, lineage_envelope, publish_event};
use lakehouse_core::outbox::{Outbox, Staged, backlog};
use lakehouse_core::pywire;
use lakehouse_core::secrets::fits_a_header;
use lakehouse_core::secrets::store::{Budget, SecretStore, read_required_secret};
use lakehouse_core::storage::records::Records;
use lakehouse_core::storage::{Listed, S3};
use lakehouse_core::telemetry::instruments::{LineageDoor, LineageOutcome as Counted};
use lakehouse_core::timers::{Timer, host_name};
use serde_json::{Map, Value};
use std::sync::Arc;
use std::time::Duration;

/// The relay timer's name, which its claim record carries.
pub const RELAY_TIMER: &str = "lineage-relay";

/// What one tick did.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RelayReport {
    pub outbox_drained: u64,
    pub outbox_stranded: u64,
    pub outbox_refused: u64,
    /// Refusals whose verdict was kept, and whose staged event was therefore safe to drop.
    pub outbox_refusals_recorded: u64,
    pub pruned_events: u64,
    pub parks_retired: u64,
    pub forwarded: u64,
    pub forward_refused: u64,
    pub forward_stalled: Option<String>,
}

/// What a tick does besides draining one staged event (`relay::Effects`).
pub trait Tick: relay::Effects {
    /// Every object directly in the outbox.
    fn listing(&mut self) -> impl Future<Output = Result<Vec<Listed>, String>>;
    /// The staged events of `listing`, oldest first and at most `limit`, read.
    fn read_staged(&mut self, listing: &[Listed], limit: Option<usize>) -> impl Future<Output = Result<Vec<Staged>, String>>;
    /// The staged object the drain's effects act on next, and the envelope it holds where it holds an event.
    fn draining(&mut self, staged: &Staged, envelope: Option<&CloudEvent>);
    /// How a refusal the drain kept is counted.
    fn counted(&mut self, _: Counted) {}
    fn retire_parks(&mut self) -> impl Future<Output = Result<u64, String>>;
    fn forward(&mut self) -> impl Future<Output = Result<Forwarded, String>>;
    /// The seq the forwarder has delivered through, which retention waits for.
    fn forwarded_through(&mut self) -> impl Future<Output = Result<i64, String>>;
    fn prune(&mut self, retention_days: u32, delivered_through: Option<i64>) -> impl Future<Output = Result<u64, String>>;
}

async fn drain(effects: &mut impl Tick, limit: Option<usize>, report: &mut RelayReport) -> Result<(), String> {
    let listing = effects.listing().await?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |since| i64::try_from(since.as_nanos()).unwrap_or(i64::MAX));
    let (depth, oldest) = backlog(&listing, now);
    if depth > 0 {
        tracing::info!(depth, oldest_age_seconds = oldest, "lineage_outbox_backlog");
    }
    for staged in effects.read_staged(&listing, limit).await? {
        let envelope = CloudEvent::parse(&staged.body).ok();
        let event = envelope.as_ref().and_then(|envelope| parse_event(&Value::Object(envelope.data.clone())).ok());
        effects.draining(&staged, envelope.as_ref().filter(|_| event.is_some()));
        let arrived = envelope.as_ref().map(|envelope| &envelope.data);
        let drained = drain_one(&staged.key, event.as_ref().zip(arrived), effects).await;
        match drained {
            Drained::Drained => report.outbox_drained += 1,
            Drained::Stranded => report.outbox_stranded += 1,
            Drained::Refused { kept } => {
                report.outbox_refused += 1;
                if let Some(counted) = kept {
                    report.outbox_refusals_recorded += 1;
                    effects.counted(counted);
                }
            }
            Drained::Poison => tracing::warn!(outbox_key = staged.key, "lineage_outbox_poison_dropped"),
        }
    }
    Ok(())
}

/// Feed retention: rows received longer ago than the budget, and, with a forwarder, only rows it delivered, since the
/// feed is the only copy it sends from. A failure is a warning, never a failed tick.
async fn prune(effects: &mut impl Tick, settings: &Settings) -> u64 {
    if settings.events_retention_days == 0 {
        return 0;
    }
    let delivered = if settings.forward_url.is_empty() {
        None
    } else {
        match effects.forwarded_through().await {
            Ok(seq) => Some(seq),
            Err(error) => {
                tracing::warn!(error, "lineage_event_prune_failed");
                return 0;
            }
        }
    };
    match effects.prune(settings.events_retention_days, delivered).await {
        Ok(pruned) => pruned,
        Err(error) => {
            tracing::warn!(error, "lineage_event_prune_failed");
            0
        }
    }
}

/// One tick: drain the outbox, retire recovered parks, forward the feed, then prune.
pub async fn tick(effects: &mut impl Tick, settings: &Settings) -> RelayReport {
    let mut report = RelayReport::default();
    if !settings.outbox_uri.is_empty()
        && let Err(error) = drain(effects, settings.drain_limit(), &mut report).await
    {
        tracing::warn!(error, "lineage_outbox_drain_failed");
    }
    match effects.retire_parks().await {
        Ok(retired) => report.parks_retired = retired,
        Err(error) => tracing::warn!(error, "lineage_parks_retire_failed"),
    }
    if !settings.forward_url.is_empty() {
        match effects.forward().await {
            Ok(forwarded) => {
                (report.forwarded, report.forward_refused, report.forward_stalled) = (forwarded.forwarded, forwarded.refused, forwarded.stalled);
            }
            Err(error) => {
                tracing::warn!(error, "lineage_forward_failed");
                report.forward_stalled = Some(error);
            }
        }
    }
    // After the forward, so a row this tick delivered past its retention goes in this tick.
    report.pruned_events = prune(effects, settings).await;
    tracing::info!(report = ?report, "lineage_relay_tick");
    report
}

/// The relay of one replica: its tick over the real store, outbox, bus and backend, and the timer it runs on.
pub struct Relay {
    settings: Arc<Settings>,
    /// The gate a staged event passes, whose store it is recorded in and whose signals count it.
    gate: Arc<Gate>,
    bus: Option<Arc<Bus>>,
    outbox: Option<Outbox>,
    timer: Option<Timer<Records>>,
    secrets: Option<Arc<SecretStore>>,
    client: reqwest::Client,
}

impl Relay {
    /// `s3` reaches the outbox and the control root where either is configured; `secrets` holds the backend's key where
    /// one is named; `bus` is where a drained event is published again (none publishes nothing).
    pub fn new(
        settings: Arc<Settings>,
        gate: Arc<Gate>,
        bus: Option<Arc<Bus>>,
        s3: Option<Arc<S3>>,
        secrets: Option<Arc<SecretStore>>,
    ) -> Result<Self, String> {
        let s3_for = |what: &str| {
            s3.clone()
                .ok_or_else(|| format!("{what} is on the object store and no store session was given"))
        };
        let outbox = match settings.outbox_uri.as_str() {
            "" => None,
            uri => Some(Outbox::new(uri, s3_for("LINEAGE_OUTBOX_URI")?)?),
        };
        let timer = if settings.relay_period_seconds > 0.0 {
            gate.signals.outbox.create_series(LANE);
            let claims = Records::new(&settings.timer_root, s3_for("LINEAGE_TIMER_ROOT")?)?;
            // A replica that did not run the window knows nothing of the backlog the one that ran it measured.
            let measured = Arc::clone(&gate);
            let timer = Timer::new(RELAY_TIMER, settings.relay_period_seconds, claims, &host_name())?.instrumented(gate.signals.timer());
            Some(timer.on_elsewhere(move || measured.signals.outbox.forget_backlog(LANE)))
        } else {
            None
        };
        // A bearer reaches only the backend git names: no proxy and no redirect.
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .timeout(settings.forward_timeout)
            .build()
            .map_err(|error| format!("the forwarder's HTTP client cannot be built: {error}"))?;
        Ok(Relay {
            settings,
            gate,
            bus,
            outbox,
            timer,
            secrets,
            client,
        })
    }

    /// One tick, now.
    pub async fn tick(&self) -> RelayReport {
        let mut ticking = Ticking {
            relay: self,
            staged: None,
            arrived: Vec::new(),
            reason: String::new(),
        };
        let report = tick(&mut ticking, &self.settings).await;
        if self.outbox.is_some() {
            let outbox = &self.gate.signals.outbox;
            outbox.record_drained(LANE, report.outbox_drained);
            outbox.record_stranded(LANE, report.outbox_stranded);
            outbox.record_refused(LANE, report.outbox_refused);
        }
        report
    }

    /// Tick once a claimed window for as long as the process runs, held through a read-only window; returns at once
    /// where git gives the relay no period.
    pub async fn run(&self) {
        if let Some(timer) = &self.timer {
            timer
                .run(
                    || self.settings.read_only,
                    || async {
                        self.tick().await;
                        Ok(())
                    },
                )
                .await;
        }
    }

    /// The bearer the backend asks for, read from the secret store on every tick, so a rotated key needs no restart.
    async fn authorization(&self) -> Result<Option<String>, String> {
        let (name, field) = (&self.settings.forward_api_key_secret, &self.settings.forward_api_key_field);
        if name.is_empty() {
            return Ok(None);
        }
        let budget = Budget {
            timeout: Duration::from_secs(2),
            retries: 1,
            backoff: Duration::from_millis(100),
        };
        let bundle = read_required_secret(self.secrets.as_deref(), name, field, budget)
            .await
            .map_err(|error| format!("{error:?}"))?;
        // Less the newline a key seeded from a file ends with.
        let key = bundle.get(field).map(|key| key.trim()).unwrap_or_default();
        if !fits_a_header(key) {
            return Err(format!("secret {name:?} holds a key no HTTP header carries as it stands: failing closed"));
        }
        Ok(Some(format!("Bearer {key}")))
    }

    async fn post(&self, url: &str, event: &Map<String, Value>, bearer: Option<&str>) -> Result<u16, String> {
        let body = pywire::dumps(&Value::Object(event.clone()), pywire::COMPACT_UTF8);
        let mut request = self.client.post(url).header("content-type", "application/json").body(body);
        if let Some(bearer) = bearer {
            let mut value = reqwest::header::HeaderValue::from_str(bearer).map_err(|_| "the key cannot be sent as a header".to_owned())?;
            value.set_sensitive(true);
            request = request.header("authorization", value);
        }
        let answer = request.send().await.map_err(|error| error.without_url().to_string())?;
        Ok(answer.status().as_u16())
    }
}

/// One tick's effects, with the staged event the drain is on.
struct Ticking<'r> {
    relay: &'r Relay,
    staged: Option<CloudEvent>,
    /// The staged text as it was read, whose `data` the signature covers.
    arrived: Vec<u8>,
    /// Why the event the drain is on was refused, which its kept refusal says.
    reason: String,
}

impl Ticking<'_> {
    fn outbox(&self) -> Result<&Outbox, String> {
        self.relay.outbox.as_ref().ok_or_else(|| "no outbox is configured".to_owned())
    }
}

impl relay::Effects for Ticking<'_> {
    async fn authorize(&mut self, event: &Event, _: &Map<String, Value>) -> Authorizing {
        // Over the staged bytes, which the signature covers, as the producer stamped them.
        let gated = self.relay.gate.authorize(event, arrived_data(&self.arrived)).await;
        if let Err(denial) = &gated {
            self.reason.clone_from(&denial.message);
        }
        authorizing(gated)
    }

    async fn record(&mut self, event: &Event, aliased: &[String]) -> Recording {
        let recording = self.relay.gate.store.record(event, aliased).await;
        if recording == Recording::ForeignRun {
            self.reason = format!(
                "run {} was founded by another subject; only its author may extend it",
                run_id(event).unwrap_or_default()
            );
        }
        recording
    }

    async fn republish(&mut self) -> bool {
        let (Some(bus), Some(staged)) = (&self.relay.bus, &self.staged) else {
            return true;
        };
        let copy = lineage_envelope(&staged.data).with_trace_of(staged);
        match publish_event(bus, &copy, &self.relay.settings.events_topic, self.relay.settings.publish_timeout, None).await {
            Ok(()) => true,
            Err(error) => {
                tracing::warn!(error = %error, "lineage_outbox_republish_failed");
                false
            }
        }
    }

    async fn drop_staged(&mut self, key: &str) -> bool {
        let dropped = match self.outbox() {
            Ok(outbox) => outbox.drop_staged(key).await.map_err(|error| error.to_string()),
            Err(error) => Err(error),
        };
        dropped
            .inspect_err(|error| tracing::warn!(outbox_key = key, error, "lineage_outbox_left_staged"))
            .is_ok()
    }

    async fn keep_refusal(&mut self, key: &str, event: &Event) -> bool {
        let data = Value::Object(self.staged.as_ref().map(|staged| staged.data.clone()).unwrap_or_default());
        let text = pywire::dumps(&data, pywire::DEFAULT);
        let kept = self
            .relay
            .gate
            .store
            .record_refusal(key, run_id(event), author_sub_from_payload(&data), &self.reason, &text)
            .await;
        kept.inspect_err(|error| tracing::warn!(outbox_key = key, error = %error, "lineage_outbox_refusal_left_staged"))
            .is_ok()
    }
}

impl Tick for Ticking<'_> {
    async fn listing(&mut self) -> Result<Vec<Listed>, String> {
        let listing = self.outbox()?.listing().await.map_err(|error| error.to_string())?;
        // Every tick's snapshot, an empty one too, so the depth falls back to 0 instead of going stale.
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |since| i64::try_from(since.as_nanos()).unwrap_or(i64::MAX));
        let (depth, oldest) = backlog(&listing, now);
        self.relay.gate.signals.outbox.observe_backlog(LANE, depth as u64, oldest);
        Ok(listing)
    }

    async fn read_staged(&mut self, listing: &[Listed], limit: Option<usize>) -> Result<Vec<Staged>, String> {
        self.outbox()?.read_staged(listing, limit).await.map_err(|error| error.to_string())
    }

    fn draining(&mut self, staged: &Staged, envelope: Option<&CloudEvent>) {
        self.staged = envelope.cloned();
        if envelope.is_none() {
            self.relay.gate.signals.outbox.record_poison_dropped(LANE);
        }
        self.arrived.clone_from(&staged.body);
        self.reason.clear();
    }

    fn counted(&mut self, counted: Counted) {
        self.relay.gate.signals.lineage.record_outcome(counted, LineageDoor::Outbox);
    }

    async fn retire_parks(&mut self) -> Result<u64, String> {
        let retired = self.relay.gate.store.retire_recovered_parks().await.map_err(|error| error.to_string())?;
        Ok(u64::try_from(retired).unwrap_or(u64::MAX))
    }

    async fn forward(&mut self) -> Result<Forwarded, String> {
        let (relay, url) = (self.relay, self.relay.settings.endpoint_url());
        let post = |event: Map<String, Value>, bearer: Option<String>| {
            let url = url.clone();
            async move { relay.post(&url, &event, bearer.as_deref()).await }
        };
        let authorization = relay.authorization();
        let forwarded = forward::forward(&relay.gate.store, &relay.settings, authorization, post).await?;
        let metrics = &relay.gate.signals.lineage;
        metrics.record_forwarded(forwarded.forwarded, forwarded.refused, forwarded.stalled.is_some());
        Ok(forwarded)
    }

    async fn forwarded_through(&mut self) -> Result<i64, String> {
        let store = &self.relay.gate.store;
        let feed = store.feed_incarnation().await.map_err(|error| error.to_string())?;
        store.cursor(&self.relay.settings.follower(&feed)).await.map_err(|error| error.to_string())
    }

    async fn prune(&mut self, retention_days: u32, delivered_through: Option<i64>) -> Result<u64, String> {
        let pruned = self
            .relay
            .gate
            .store
            .prune_events(retention_days, delivered_through)
            .await
            .map_err(|error| error.to_string())?;
        if pruned > 0 {
            tracing::info!(pruned, retention_days, "lineage_events_pruned");
        }
        Ok(pruned)
    }
}

/// The run an event amends; none for a dataset change.
fn run_id(event: &Event) -> Option<&str> {
    match event {
        Event::Run(run) => Some(run.run.run_id.as_str()).filter(|run_id| !run_id.is_empty()),
        Event::Dataset(_) => None,
    }
}
