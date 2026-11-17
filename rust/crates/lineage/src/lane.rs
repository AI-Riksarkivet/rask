//! Lineage's ingest lane: every event on its topic, taken from the chart's durable consumer on this process's own
//! connection to the bus (`lineage.api.ingest_lane`, `lineage.services.consumer`). `lakehouse_bus` pulls and settles each
//! delivery by `lakehouse_core::delivery::judged`; this is what lineage does with one, the model's Judge and its writes,
//! and what it keeps of one the lane ends unrecorded, the model's Park.
//!
//! A parked delivery is kept in the provenance namespace's `refusals` table, where the estate admin re-presents it, keyed
//! by its CloudEvent id, or by the feed row it would write where its envelope carries none; one whose row the feed
//! already holds is kept nowhere, since the event is recorded and only its redelivery ended.

use crate::authz::gates::authorizing;
use crate::gate::{Gate, arrived_data};
use crate::intake::{self, Recording};
use crate::model::{Event, author_sub_from_payload, run_id_from_payload};
use crate::store::delivery_feed_key;
use crate::store::tables::PARKED_KEY_PREFIX;
use lakehouse_bus::{Bus, Handler, HandlingPanicked};
use lakehouse_core::delivery::{Delivery, Lane, Verdict};
use lakehouse_core::pywire;
use lakehouse_core::telemetry::instruments::{LineageDoor, LineageOutcome};
use serde_json::{Map, Value};
use std::sync::Arc;
use std::time::Instant;

/// The most of a delivery's own text a kept row's key, its run and author, and a log line carry.
const MAX_PLAIN_CHARS: usize = 512;

/// `value` where a table and a log line carry it as it stands: a string of 1 to `MAX_PLAIN_CHARS` characters.
fn plain(value: Option<&str>) -> Option<String> {
    value.filter(|text| (1..=MAX_PLAIN_CHARS).contains(&text.chars().count())).map(str::to_owned)
}

/// What a park keeps of a delivery: the feed row whose presence makes the keeping moot, and the row it keeps otherwise.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Parking {
    pub event_id: Option<String>,
    pub run_id: Option<String>,
    pub author: Option<String>,
    pub feed_key: Option<String>,
    /// The key in `refusals`: its CloudEvent id, else the feed row it would write; none where it names neither.
    pub key: Option<String>,
    /// The envelope as Python's `json.dumps` writes it.
    pub text: String,
}

/// What a park of the delivery whose envelope is `envelope` keeps. A park takes only plain text from the delivery, so it
/// never fails for what a delivery carries: a park that failed so would fail on every delivery after it.
pub fn parking(envelope: &Map<String, Value>) -> Parking {
    let payload = envelope.get("data").unwrap_or(&Value::Null);
    let event_id = plain(envelope.get("id").and_then(Value::as_str));
    let feed_key = delivery_feed_key(&Value::Object(envelope.clone()));
    Parking {
        run_id: plain(run_id_from_payload(payload)),
        author: plain(author_sub_from_payload(payload)),
        key: event_id.clone().or_else(|| feed_key.clone()).map(|name| format!("{PARKED_KEY_PREFIX}{name}")),
        event_id,
        feed_key,
        text: pywire::dumps(&Value::Object(envelope.clone()), pywire::DEFAULT),
    }
}

/// What lineage does with each delivery of its consumer.
pub struct IngestLane {
    gate: Arc<Gate>,
}

impl IngestLane {
    /// The lane recording what `gate` admits in the gate's store, and counting it in the gate's signals.
    pub fn new(gate: Arc<Gate>) -> Self {
        IngestLane { gate }
    }

    /// Take `lane`'s deliveries from `bus` until `stop` resolves.
    pub async fn run(self: Arc<Self>, bus: &Bus, lane: &Lane, stop: impl Future<Output = ()>) -> Result<(), HandlingPanicked> {
        bus.consume(lane, self, stop).await
    }
}

impl Handler for IngestLane {
    async fn handle(&self, delivery: &Delivery) -> Result<Verdict, String> {
        let envelope = delivery.envelope();
        // The check of the process's supervision (`scripts/port_lineage_check.sh`) needs a handling that dies the way a
        // native library's panic would: a debug build alone takes this CloudEvent type as one. A release has no such type.
        #[cfg(debug_assertions)]
        if envelope.as_ref().and_then(|envelope| envelope.get("type")).and_then(Value::as_str) == Some("rask.check.handling-panics") {
            std::panic::resume_unwind(Box::new("the check's handling panicked"));
        }
        let (gate, metrics) = (&self.gate, &self.gate.signals.lineage);
        let authorize = async |event: &Event, _: &Map<String, Value>| authorizing(gate.authorize(event, arrived_data(&delivery.body)).await);
        let record = async |event: &Event, aliased: &[String]| {
            let started = Instant::now();
            let recording = gate.store.record(event, aliased).await;
            if recording == Recording::Landed {
                metrics.record_ingest_duration(started.elapsed().as_secs_f64());
            }
            recording
        };
        let (verdict, counted) = intake::handle(envelope.as_ref(), Some(authorize), record).await;
        metrics.record_outcome(counted, LineageDoor::Subscriber);
        Ok(verdict)
    }

    /// Keep a delivery the lane ends unrecorded, unless the feed already holds its row. A failure to ask or to keep is
    /// answered, and the lane asks for the delivery again.
    async fn park(&self, delivery: &Delivery, reason: &str) -> Result<(), String> {
        let parked = delivery.envelope().map(|envelope| parking(&envelope));
        let mut recorded = false;
        if let Some(parked) = &parked {
            if let Some(feed_key) = &parked.feed_key {
                recorded = self
                    .gate
                    .store
                    .events_recorded([feed_key])
                    .await
                    .map_err(|error| error.to_string())?
                    .contains(feed_key);
            }
            if let (false, Some(key)) = (recorded, &parked.key) {
                let (run_id, author) = (parked.run_id.as_deref(), parked.author.as_deref());
                self.gate
                    .store
                    .record_refusal(key, run_id, author, reason, &parked.text)
                    .await
                    .map_err(|error| error.to_string())?;
            }
        }
        let (event_id, run_id, author) = parked.map_or((None, None, None), |parked| (parked.event_id, parked.run_id, parked.author));
        let metrics = &self.gate.signals.lineage;
        if recorded {
            tracing::warn!(event_id, run_id, reason, already_recorded = true, author, "lineage_delivery_parked");
            metrics.record_outcome(LineageOutcome::ParkedAlreadyRecorded, LineageDoor::DeadLetter);
        } else {
            tracing::error!(event_id, run_id, reason, already_recorded = false, author, "lineage_delivery_parked");
            metrics.record_outcome(LineageOutcome::DeadLettered, LineageDoor::DeadLetter);
        }
        Ok(())
    }
}
