//! The metrics the services record, under the names, units, kinds and attribute keys the Python services record them
//! with, because the platform's alert rules and dashboards read them by name. Every attribute is a closed set or a
//! chart value; nothing off a message labels a series.
//!
//! An alert over a counter reads its increase, and a series born at its first add has no 0 before it, so the first
//! failure would never page: each group creates its series at 0 when what records them starts (`create_*`).

use crate::signing::RefusalReason;
use opentelemetry::KeyValue;
use opentelemetry::metrics::{Counter, Histogram, Meter, ObservableGauge};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, OnceLock};

/// The boundaries, in seconds, of the duration histograms OpenTelemetry's messaging conventions advise.
const MESSAGING_SECONDS: [f64; 14] = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0];
/// Second-scale boundaries for an ingest; the SDK's default ones are spaced for milliseconds.
const INGEST_SECONDS: [f64; 11] = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0];

fn counter(meter: &Meter, name: &'static str, unit: &'static str, description: &'static str) -> Counter<u64> {
    meter.u64_counter(name).with_unit(unit).with_description(description).build()
}

fn histogram(meter: &Meter, name: &'static str, description: &'static str, boundaries: &[f64]) -> Histogram<f64> {
    meter
        .f64_histogram(name)
        .with_unit("s")
        .with_description(description)
        .with_boundaries(boundaries.to_vec())
        .build()
}

/// The terminal outcome of one event at a lineage door.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LineageOutcome {
    Ingested,
    Refused,
    Retried,
    DeadLettered,
    ParkedAlreadyRecorded,
    Unrepairable,
    Recovered,
    SignatureRefused,
}

impl LineageOutcome {
    pub const ALL: [LineageOutcome; 8] = [
        LineageOutcome::Ingested,
        LineageOutcome::Refused,
        LineageOutcome::Retried,
        LineageOutcome::DeadLettered,
        LineageOutcome::ParkedAlreadyRecorded,
        LineageOutcome::Unrepairable,
        LineageOutcome::Recovered,
        LineageOutcome::SignatureRefused,
    ];

    pub fn label(self) -> &'static str {
        match self {
            LineageOutcome::Ingested => "ingested",
            LineageOutcome::Refused => "refused",
            LineageOutcome::Retried => "retried",
            LineageOutcome::DeadLettered => "dead_lettered",
            LineageOutcome::ParkedAlreadyRecorded => "parked_already_recorded",
            LineageOutcome::Unrepairable => "unrepairable",
            LineageOutcome::Recovered => "recovered",
            LineageOutcome::SignatureRefused => "signature_refused",
        }
    }
}

/// Which ingress an event arrived through: each fails for its own reason and is repaired in its own place.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LineageDoor {
    Subscriber,
    Http,
    DeadLetter,
    Outbox,
}

impl LineageDoor {
    pub const ALL: [LineageDoor; 4] = [LineageDoor::Subscriber, LineageDoor::Http, LineageDoor::DeadLetter, LineageDoor::Outbox];

    pub fn label(self) -> &'static str {
        match self {
            LineageDoor::Subscriber => "subscriber",
            LineageDoor::Http => "http",
            LineageDoor::DeadLetter => "dead_letter",
            LineageDoor::Outbox => "outbox",
        }
    }
}

/// Lineage's own golden signals (scope `lance.lineage`).
pub struct LineageMetrics {
    processed: Counter<u64>,
    ingest: Histogram<f64>,
    forwarded: Counter<u64>,
    stalled: Counter<u64>,
    verified: Counter<u64>,
    refused: Counter<u64>,
}

impl LineageMetrics {
    pub fn new(meter: &Meter) -> LineageMetrics {
        LineageMetrics {
            processed: counter(
                meter,
                "lineage.events.processed",
                "{event}",
                "Lineage events each door processed, by outcome and door; the outbox drain counts only its refusals.",
            ),
            ingest: histogram(
                meter,
                "lineage.ingest.duration",
                "Wall-clock seconds to record one event in the feed and the edge list (successful records only).",
                &INGEST_SECONDS,
            ),
            forwarded: counter(
                meter,
                "lineage.forward.events",
                "{event}",
                "Events the forwarder POSTed to the configured graph backend, by outcome: taken, or refused by the backend.",
            ),
            stalled: counter(
                meter,
                "lineage.forward.stalled",
                "{tick}",
                "Relay ticks whose forward stopped short of the feed's head: the backend was down or refused the forwarder.",
            ),
            verified: counter(
                meter,
                "lineage.signature.verified",
                "{event}",
                "Events whose rask_signature a listed signer's published Ed25519 key verified, by the signing identity.",
            ),
            refused: counter(
                meter,
                "lineage.signature.refused",
                "{event}",
                "Events refused because no listed signer's published key verifies their signature, by reason. A refusal is acked, so this count is the only trace it leaves.",
            ),
        }
    }

    fn outcome(outcome: LineageOutcome, door: LineageDoor) -> [KeyValue; 2] {
        [
            KeyValue::new("lance.lineage.outcome", outcome.label()),
            KeyValue::new("lance.lineage.door", door.label()),
        ]
    }

    /// Every outcome's series at every door, at 0; called when lineage starts.
    pub fn create_outcome_series(&self) {
        for outcome in LineageOutcome::ALL {
            for door in LineageDoor::ALL {
                self.processed.add(0, &Self::outcome(outcome, door));
            }
        }
    }

    pub fn record_outcome(&self, outcome: LineageOutcome, door: LineageDoor) {
        self.processed.add(1, &Self::outcome(outcome, door));
    }

    /// One successful ingest's latency.
    pub fn record_ingest_duration(&self, seconds: f64) {
        self.ingest.record(seconds, &[]);
    }

    /// One tick's forward; every series is added to, at 0 too, so each exists from the first tick a backend is set.
    pub fn record_forwarded(&self, forwarded: u64, refused: u64, stalled: bool) {
        self.forwarded.add(forwarded, &[KeyValue::new("lance.lineage.outcome", "forwarded")]);
        self.forwarded.add(refused, &[KeyValue::new("lance.lineage.outcome", "refused")]);
        self.stalled.add(u64::from(stalled), &[]);
    }

    /// One event verified for `identity`, a signer the chart lists.
    pub fn record_signature_verified(&self, identity: &str) {
        self.verified.add(1, &[KeyValue::new("lance.lineage.identity", identity.to_owned())]);
    }

    /// Every refusal reason's series at 0; called when the bus door is registered.
    pub fn create_signature_refused_series(&self) {
        for reason in RefusalReason::ALL {
            self.refused.add(0, &[KeyValue::new("lance.lineage.reason", reason.label())]);
        }
    }

    pub fn record_signature_refused(&self, reason: RefusalReason) {
        self.refused.add(1, &[KeyValue::new("lance.lineage.reason", reason.label())]);
    }
}

/// The bus's instruments (scope `lance.bus`), named by the messaging conventions where those name them.
pub struct BusMetrics {
    refused: Counter<u64>,
    publish: Histogram<f64>,
    deliveries: Counter<u64>,
    pulls_failed: Counter<u64>,
    processing: Histogram<f64>,
}

impl BusMetrics {
    pub fn new(meter: &Meter) -> BusMetrics {
        BusMetrics {
            refused: counter(
                meter,
                "bus.publish.refused",
                "{event}",
                "Publishes the claim-check guard refused before anything was sent to the bus.",
            ),
            publish: histogram(
                meter,
                "messaging.client.operation.duration",
                "How long each publish to the bus took, from the call to the stream's acknowledgement or the failure; `error.type` is set on a publish that was not delivered.",
                &MESSAGING_SECONDS,
            ),
            deliveries: counter(
                meter,
                "bus.deliveries",
                "{message}",
                "Deliveries this process took from the bus, by the consumer they came from and how each was settled.",
            ),
            pulls_failed: counter(
                meter,
                "bus.pulls.failed",
                "{pull}",
                "Asks for a consumer's next message, or a followed subject's, that the bus refused or dropped, by consumer.",
            ),
            processing: histogram(
                meter,
                "messaging.process.duration",
                "How long the handling of each delivery took, from the message's arrival to its settlement.",
                &MESSAGING_SECONDS,
            ),
        }
    }

    fn delivery(consumer: &str, outcome: &str) -> [KeyValue; 2] {
        [
            KeyValue::new("lance.bus.consumer", consumer.to_owned()),
            KeyValue::new("lance.bus.outcome", outcome.to_owned()),
        ]
    }

    /// `consumer`'s series at 0: one per outcome it settles with, and its failed asks.
    pub fn create_delivery_series(&self, consumer: &str, outcomes: &[&str]) {
        for outcome in outcomes {
            self.deliveries.add(0, &Self::delivery(consumer, outcome));
        }
        self.pulls_failed.add(0, &[KeyValue::new("lance.bus.consumer", consumer.to_owned())]);
    }

    /// One ask the bus refused or dropped. `consumer` is a chart value or a constant, never a value off a message.
    pub fn record_pull_failed(&self, consumer: &str) {
        self.pulls_failed.add(1, &[KeyValue::new("lance.bus.consumer", consumer.to_owned())]);
    }

    /// One delivery settled: `outcome` is a delivery's outcome label, or `failed` for a followed message.
    pub fn record_delivery(&self, consumer: &str, outcome: &str) {
        self.deliveries.add(1, &Self::delivery(consumer, outcome));
    }

    /// How long one delivery's handling on `subject` took; `consumer` is None for a subject a process follows.
    pub fn record_processing(&self, subject: &str, consumer: Option<&str>, seconds: f64) {
        let mut attributes = vec![
            KeyValue::new("messaging.system", "nats"),
            KeyValue::new("messaging.operation.name", "process"),
            KeyValue::new("messaging.destination.name", subject.to_owned()),
        ];
        attributes.extend(consumer.map(|consumer| KeyValue::new("messaging.consumer.group.name", consumer.to_owned())));
        self.processing.record(seconds, &attributes);
    }

    /// One publish refused before I/O. `topic` is a settings field or a constant; `reason` a closed set.
    pub fn record_refusal(&self, topic: &str, reason: &'static str) {
        self.refused.add(
            1,
            &[KeyValue::new("lance.bus.topic", topic.to_owned()), KeyValue::new("lance.bus.reason", reason)],
        );
    }

    /// One publish on `topic`: how long it took and, for one not delivered, the kind of failure, a closed set.
    pub fn record_publish(&self, topic: &str, seconds: f64, failure: Option<&'static str>) {
        let mut attributes = vec![
            KeyValue::new("messaging.system", "nats"),
            KeyValue::new("messaging.operation.name", "publish"),
            KeyValue::new("messaging.destination.name", topic.to_owned()),
        ];
        attributes.extend(failure.map(|failure| KeyValue::new("error.type", failure)));
        self.publish.record(seconds, &attributes);
    }
}

/// An outbox the lakehouse keeps: the `lance.outbox.lane` attribute.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum OutboxLane {
    Lineage,
    Control,
}

impl OutboxLane {
    pub fn label(self) -> &'static str {
        match self {
            OutboxLane::Lineage => "lineage",
            OutboxLane::Control => "control",
        }
    }
}

type Backlogs = Arc<Mutex<BTreeMap<OutboxLane, (f64, f64)>>>;

/// The outbox's four signals (scope `lance.outbox`), each series labelled by its lane.
pub struct OutboxMetrics {
    meter: Meter,
    counters: [Counter<u64>; 9],
    backlogs: Backlogs,
    gauges: OnceLock<[ObservableGauge<f64>; 2]>,
}

const STAGED: usize = 0;
const PUBLISHED: usize = 1;
const PUBLISH_FAILED: usize = 2;
const STAGE_FAILED: usize = 3;
const DROP_FAILED: usize = 4;
const DRAINED: usize = 5;
const STRANDED: usize = 6;
const REFUSED: usize = 7;
const POISON_DROPPED: usize = 8;

impl OutboxMetrics {
    pub fn new(meter: &Meter) -> OutboxMetrics {
        let event = |name, description| counter(meter, name, "{event}", description);
        OutboxMetrics {
            meter: meter.clone(),
            counters: [
                event(
                    "outbox.events.staged",
                    "Lineage events written to the object-store outbox BEFORE the publish (the durable copy).",
                ),
                event(
                    "outbox.events.published",
                    "Lineage events whose publish was acked (the staged copy is then dropped).",
                ),
                event(
                    "outbox.publish.failed",
                    "Publishes that failed AFTER staging: the event survives on object storage for the reconcile relay.",
                ),
                event(
                    "outbox.stage.failed",
                    "Stages that failed, after which the event was published ANYWAY without a durable copy.",
                ),
                event(
                    "outbox.drop.failed",
                    "Published events whose staged copy could not be deleted; a later relay tick delivers and drops it.",
                ),
                event("outbox.events.drained", "Staged survivors the relay tick re-ingested into the lineage feed."),
                event(
                    "outbox.events.stranded",
                    "Staged events a tick FAILED on and deliberately LEFT staged, retried next tick.",
                ),
                event(
                    "outbox.events.refused",
                    "Staged events lineage refuses to record: a settled governance answer, re-refused every tick.",
                ),
                event("outbox.events.poison_dropped", "Staged objects discarded so they cannot wedge the drain."),
            ],
            backlogs: Arc::new(Mutex::new(BTreeMap::new())),
            gauges: OnceLock::new(),
        }
    }

    fn add(&self, which: usize, lane: OutboxLane, count: u64) {
        if let Some(counter) = self.counters.get(which) {
            counter.add(count, &[KeyValue::new("lance.outbox.lane", lane.label())]);
        }
    }

    /// Every counter's series of `lane` at 0; called by what stages into a lane or drains it, when it starts.
    pub fn create_series(&self, lane: OutboxLane) {
        for which in 0..self.counters.len() {
            self.add(which, lane, 0);
        }
    }

    pub fn record_staged(&self, lane: OutboxLane) {
        self.add(STAGED, lane, 1);
    }
    pub fn record_published(&self, lane: OutboxLane) {
        self.add(PUBLISHED, lane, 1);
    }
    pub fn record_publish_failed(&self, lane: OutboxLane) {
        self.add(PUBLISH_FAILED, lane, 1);
    }
    pub fn record_stage_failed(&self, lane: OutboxLane) {
        self.add(STAGE_FAILED, lane, 1);
    }
    pub fn record_drop_failed(&self, lane: OutboxLane) {
        self.add(DROP_FAILED, lane, 1);
    }
    pub fn record_poison_dropped(&self, lane: OutboxLane) {
        self.add(POISON_DROPPED, lane, 1);
    }
    /// What one tick delivered out of `lane`; a tick that delivered nothing adds 0.
    pub fn record_drained(&self, lane: OutboxLane, count: u64) {
        self.add(DRAINED, lane, count);
    }
    /// What one tick failed on and left staged; a tick that stranded nothing adds 0.
    pub fn record_stranded(&self, lane: OutboxLane, count: u64) {
        self.add(STRANDED, lane, count);
    }
    /// What one tick found refused; a tick that refused nothing adds 0, so a rule can read a zero.
    pub fn record_refused(&self, lane: OutboxLane, count: u64) {
        self.add(REFUSED, lane, count);
    }

    /// The backlog `lane`'s relay measured this tick, empty included. The gauges are registered at the first
    /// measurement, so only the process that measures exports them.
    pub fn observe_backlog(&self, lane: OutboxLane, depth: u64, oldest_age_seconds: f64) {
        self.gauges.get_or_init(|| {
            let gauge = |name, unit, description, pick: fn(&(f64, f64)) -> f64| {
                let backlogs = Arc::clone(&self.backlogs);
                self.meter
                    .f64_observable_gauge(name)
                    .with_unit(unit)
                    .with_description(description)
                    .with_callback(move |observer| {
                        if let Ok(backlogs) = backlogs.lock() {
                            for (lane, backlog) in backlogs.iter() {
                                observer.observe(pick(backlog), &[KeyValue::new("lance.outbox.lane", lane.label())]);
                            }
                        }
                    })
                    .build()
            };
            [
                gauge("outbox.depth", "{event}", "Events currently staged and undrained, by lane.", |backlog| {
                    backlog.0
                }),
                gauge("outbox.oldest_age", "s", "Age of the OLDEST staged event, by lane.", |backlog| backlog.1),
            ]
        });
        if let Ok(mut backlogs) = self.backlogs.lock() {
            backlogs.insert(lane, (depth as f64, oldest_age_seconds));
        }
    }

    /// Report no backlog of `lane` from this process until it measures one again: its last reading says nothing now.
    pub fn forget_backlog(&self, lane: OutboxLane) {
        if let Ok(mut backlogs) = self.backlogs.lock() {
            backlogs.remove(&lane);
        }
    }
}
