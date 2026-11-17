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
use crate::intake;
use crate::model::{Event, author_sub_from_payload, run_id_from_payload};
use crate::store::tables::PARKED_KEY_PREFIX;
use crate::store::{Store, delivery_feed_key};
use lakehouse_bus::{Bus, Handler, HandlingPanicked};
use lakehouse_core::delivery::{Delivery, Lane, Verdict};
use lakehouse_core::pywire;
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, PoisonError};

/// The most of a delivery's own text a kept row's key, its run and author, and a log line carry.
const MAX_PLAIN_CHARS: usize = 512;

/// What lineage counted, by the door an event came through and what became of it (`lineage.events.processed`).
#[derive(Debug, Default)]
pub struct Tally(Mutex<BTreeMap<(&'static str, &'static str), u64>>);

impl Tally {
    pub fn count(&self, door: &'static str, outcome: &'static str) {
        *self.0.lock().unwrap_or_else(PoisonError::into_inner).entry((door, outcome)).or_default() += 1;
    }

    pub fn counted(&self, door: &str, outcome: &str) -> u64 {
        let counts = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        counts
            .iter()
            .filter(|((by, what), _)| *by == door && *what == outcome)
            .map(|(_, count)| count)
            .sum()
    }
}

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
    store: Store,
    gate: Arc<Gate>,
    pub tally: Arc<Tally>,
}

impl IngestLane {
    pub fn new(store: Store, gate: Arc<Gate>, tally: Arc<Tally>) -> Self {
        IngestLane { store, gate, tally }
    }

    /// Take `lane`'s deliveries from `bus` until `stop` resolves.
    pub async fn run(self: Arc<Self>, bus: &Bus, lane: &Lane, stop: impl Future<Output = ()>) -> Result<(), HandlingPanicked> {
        bus.consume(lane, self, stop).await
    }
}

impl Handler for IngestLane {
    async fn handle(&self, delivery: &Delivery) -> Result<Verdict, String> {
        let envelope = delivery.envelope();
        let authorize = async |event: &Event, _: &Map<String, Value>| authorizing(self.gate.authorize(event, arrived_data(&delivery.body)).await);
        let record = async |event: &Event, aliased: &[String]| self.store.record(event, aliased).await;
        let (verdict, counted) = intake::handle(envelope.as_ref(), Some(authorize), record).await;
        self.tally.count("subscriber", counted.name());
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
                    .store
                    .events_recorded([feed_key])
                    .await
                    .map_err(|error| error.to_string())?
                    .contains(feed_key);
            }
            if let (false, Some(key)) = (recorded, &parked.key) {
                let (run_id, author) = (parked.run_id.as_deref(), parked.author.as_deref());
                self.store
                    .record_refusal(key, run_id, author, reason, &parked.text)
                    .await
                    .map_err(|error| error.to_string())?;
            }
        }
        let (event_id, run_id, author) = parked.map_or((None, None, None), |parked| (parked.event_id, parked.run_id, parked.author));
        if recorded {
            tracing::warn!(event_id, run_id, reason, already_recorded = true, author, "lineage_delivery_parked");
            self.tally.count("dead_letter", "parked_already_recorded");
        } else {
            tracing::error!(event_id, run_id, reason, already_recorded = false, author, "lineage_delivery_parked");
            self.tally.count("dead_letter", "dead_lettered");
        }
        Ok(())
    }
}
