//! The CloudEvents envelope each of the lakehouse's events rides the bus in (CloudEvents 1.0, structured JSON mode).
//!
//! Every attribute is read off the event, so one event has one envelope whoever publishes it, and its `id` is the
//! message id the stream deduplicates on. `data` is the event exactly as its producer signed it. The contract a reader
//! holds is `docs/COMMIT-EVENTS.md` § The envelope on the bus.

use crate::instant::{self, Instant};
use crate::pywire;
use lakehouse_core::events::{LANCE_FACET, dataset_uuids, static_event_id};
use serde_json::{Map, Value};

/// The content type of a structured-mode CloudEvent: the whole envelope is the message body.
pub const STRUCTURED_CONTENT_TYPE: &str = "application/cloudevents+json";
/// Soft cap: an event with a wide schema facet stays well under it, so crossing it is worth a line, not a refusal.
pub const WARN_PAYLOAD_BYTES: usize = 64 * 1024;
/// Hard cap, just under JetStream's default `max_payload` of about 1 MiB: no body over it could be delivered.
pub const MAX_PAYLOAD_BYTES: usize = 900 * 1024;

const SPEC_VERSION: &str = "1.0";
const DATA_CONTENT_TYPE: &str = "application/json";
/// The `source` of an event that names no `producer`: the attribute is required.
const LINEAGE_SOURCE: &str = "rask/lineage";
const LINEAGE_DATASET_TYPE: &str = "rask.lineage.dataset.v1";
/// The state a dataset change is identified under: it has happened by the time it is announced.
const DATASET_STATE: &str = "COMPLETE";
/// OpenLineage's own state for a run event that declares no other transition.
const UNDECLARED_STATE: &str = "OTHER";
const TERMINAL_STATES: [&str; 3] = ["COMPLETE", "FAIL", "ABORT"];
const TRACESTATE_MEMBERS: usize = 32;

/// One event in its envelope.
#[derive(Debug, Clone, PartialEq)]
pub struct CloudEvent {
    /// The event's identity on the bus, the same for every copy of it and for no other event.
    pub id: String,
    /// Who produced the event.
    pub source: String,
    /// What kind of event it is, versioned (the attribute `type`).
    pub kind: String,
    /// The one object the event is about, where it is about exactly one.
    pub subject: Option<String>,
    /// When what the event tells happened.
    pub time: Option<Instant>,
    /// The schema `data` follows, where the event names one.
    pub dataschema: Option<String>,
    /// The event as its producer signed it.
    pub data: Map<String, Value>,
    /// The W3C trace context the envelope was staged under, which a publish of it continues.
    pub traceparent: Option<String>,
    pub tracestate: Option<String>,
}

/// Why a text holds no envelope.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("no envelope: {0}")]
pub struct Unreadable(pub String);

/// An envelope whose body is past the claim-check cap: events carry pointers, never data.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("event payload is {bytes} bytes (> {MAX_PAYLOAD_BYTES}): events carry POINTERS, never data (claim-check invariant, docs/DATA-CONTRACT.md)")]
pub struct Oversize {
    pub bytes: usize,
}

/// The trace context a publish runs under, stamped on the body it sends.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TraceContext {
    pub traceparent: String,
    pub tracestate: Option<String>,
}

impl TraceContext {
    /// The context `traceparent` names, with `tracestate` where it is well formed; none for a malformed `traceparent`,
    /// which is never adopted.
    pub fn continuing(traceparent: Option<&str>, tracestate: Option<&str>) -> Option<Self> {
        let traceparent = traceparent.filter(|value| is_traceparent(value))?;
        Some(TraceContext {
            traceparent: traceparent.to_owned(),
            tracestate: tracestate.filter(|value| !value.is_empty() && is_tracestate(value)).map(str::to_owned),
        })
    }
}

impl CloudEvent {
    /// The envelope a staged text holds, read as the outbox's `staged_envelope` reads it.
    pub fn parse(text: &[u8]) -> Result<Self, Unreadable> {
        let fields: Map<String, Value> = serde_json::from_slice(text).map_err(|error| Unreadable(error.to_string()))?;
        let required = |name: &str| match fields.get(name) {
            Some(Value::String(value)) if !value.is_empty() => Ok(value.clone()),
            _ => Err(Unreadable(format!("{name} is not a non-empty string"))),
        };
        let optional = |name: &str| match fields.get(name) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(value)) if !value.is_empty() => Ok(Some(value.clone())),
            _ => Err(Unreadable(format!("{name} is neither absent nor a non-empty string"))),
        };
        let trace = |name: &str, valid: fn(&str) -> bool| match fields.get(name) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(value)) if valid(value) => Ok(Some(value.clone())),
            _ => Err(Unreadable(format!("{name} is not a W3C {name}"))),
        };
        let time = match fields.get("time") {
            None | Some(Value::Null) => None,
            Some(value) => Some(instant::parsed(value).ok_or_else(|| Unreadable("time is no instant with an offset".to_owned()))?),
        };
        let Some(Value::Object(data)) = fields.get("data") else {
            return Err(Unreadable("data is not an object".to_owned()));
        };
        Ok(CloudEvent {
            id: required("id")?,
            source: required("source")?,
            kind: required("type")?,
            subject: optional("subject")?,
            time,
            dataschema: optional("dataschema")?,
            data: data.clone(),
            traceparent: trace("traceparent", is_traceparent)?,
            tracestate: trace("tracestate", is_tracestate)?,
        })
    }

    /// The JSON object the bus carries: each attribute that is set, and `data` as it was given.
    pub fn wire(&self) -> Map<String, Value> {
        let mut wire = Map::new();
        let mut put = |name: &str, value: &str| {
            wire.insert(name.to_owned(), Value::String(value.to_owned()));
        };
        put("specversion", SPEC_VERSION);
        put("datacontenttype", DATA_CONTENT_TYPE);
        put("id", &self.id);
        put("source", &self.source);
        put("type", &self.kind);
        let optional = [
            ("subject", self.subject.clone()),
            ("time", self.time.as_ref().map(Instant::render)),
            ("dataschema", self.dataschema.clone()),
            ("traceparent", self.traceparent.clone()),
            ("tracestate", self.tracestate.clone()),
        ];
        for (name, value) in optional {
            if let Some(value) = value {
                put(name, &value);
            }
        }
        wire.insert("data".to_owned(), Value::Object(self.data.clone()));
        wire
    }

    /// The text an outbox stages the envelope as.
    pub fn staged_text(&self) -> String {
        pywire::dumps(&Value::Object(self.wire()))
    }

    /// This envelope carrying `staged`'s trace context: a relay's envelope, kept in the trace its event was staged under.
    pub fn with_trace_of(mut self, staged: &CloudEvent) -> Self {
        self.traceparent.clone_from(&staged.traceparent);
        self.tracestate.clone_from(&staged.tracestate);
        self
    }

    /// The bytes a publish sends: the envelope without the trace it was staged with, then the publish's own trace
    /// context, so a stager's text reaches no consumer.
    pub fn body(&self, publish: Option<&TraceContext>) -> Result<Vec<u8>, Oversize> {
        let mut wire = self.wire();
        wire.shift_remove("traceparent");
        wire.shift_remove("tracestate");
        if let Some(context) = publish {
            wire.insert("traceparent".to_owned(), Value::String(context.traceparent.clone()));
            if let Some(tracestate) = &context.tracestate {
                wire.insert("tracestate".to_owned(), Value::String(tracestate.clone()));
            }
        }
        let body = pywire::dumps(&Value::Object(wire)).into_bytes();
        if body.len() > MAX_PAYLOAD_BYTES {
            return Err(Oversize { bytes: body.len() });
        }
        Ok(body)
    }
}

/// The envelope of one OpenLineage event, a run's or a dataset change's, on the lineage topic: the event under its
/// identity and state, from its `producer`, about the one governed table it writes where it writes exactly one.
pub fn lineage_envelope(event: &Map<String, Value>) -> CloudEvent {
    CloudEvent {
        id: lineage_id(event),
        source: text(event.get("producer")).unwrap_or_else(|| LINEAGE_SOURCE.to_owned()),
        kind: lineage_type(event),
        subject: written_table(event),
        time: event.get("eventTime").and_then(|time| instant::from_isoformat(&pywire::py_str(time))),
        dataschema: text(event.get("schemaURL")),
        data: event.clone(),
        traceparent: None,
        tracestate: None,
    }
}

/// Whether lineage's doors read `event` as a dataset change rather than a run's event.
fn is_dataset_change(event: &Map<String, Value>) -> bool {
    event.contains_key("dataset") && !event.contains_key("run")
}

fn run_state(event: &Map<String, Value>) -> String {
    pywire::str_or(event.get("eventType"), UNDECLARED_STATE)
}

/// The identity of an OpenLineage event: a run event's run id, or the static id of a dataset change.
fn event_identity(event: &Map<String, Value>) -> String {
    let run_id = event.get("run").and_then(Value::as_object).and_then(|run| run.get("runId"));
    if let Some(run_id) = run_id.filter(|run_id| !pywire::falsy(run_id)) {
        return pywire::py_str(run_id);
    }
    let dataset = event.get("dataset").and_then(Value::as_object);
    let member = |key: &str| pywire::str_or(dataset.and_then(|dataset| dataset.get(key)), "");
    let operation = dataset
        .and_then(|dataset| dataset.get("facets"))
        .and_then(Value::as_object)
        .and_then(|facets| facets.get(LANCE_FACET))
        .and_then(Value::as_object)
        .and_then(|lance| lance.get("operation"));
    static_event_id(
        &pywire::str_or(event.get("producer"), ""),
        &member("namespace"),
        &member("name"),
        &pywire::str_or(operation, ""),
        &pywire::str_or(event.get("eventTime"), ""),
    )
}

fn lineage_id(event: &Map<String, Value>) -> String {
    let identity = event_identity(event);
    if is_dataset_change(event) {
        return format!("{identity}@{DATASET_STATE}");
    }
    let state = run_state(event);
    if TERMINAL_STATES.contains(&state.as_str()) {
        return format!("{identity}@{state}");
    }
    // A state that ends nothing can be told again as the run goes on, so each telling is its own event.
    format!("{identity}@{state}@{}", pywire::str_or(event.get("eventTime"), ""))
}

fn lineage_type(event: &Map<String, Value>) -> String {
    if is_dataset_change(event) {
        return LINEAGE_DATASET_TYPE.to_owned();
    }
    format!("rask.lineage.run.{}.v1", run_state(event).to_lowercase())
}

/// The one governed table `event` writes or changes, as the object its grants are held by (`table:<uuid>`).
fn written_table(event: &Map<String, Value>) -> Option<String> {
    let only = if is_dataset_change(event) {
        event.get("dataset")
    } else {
        match event.get("outputs") {
            Some(Value::Array(outputs)) if outputs.len() == 1 => outputs.first(),
            _ => None,
        }
    };
    let facets = only?.as_object()?.get("facets")?.as_object()?;
    dataset_uuids(facets).table_uuid.map(|uuid| format!("table:{uuid}"))
}

fn text(value: Option<&Value>) -> Option<String> {
    value.and_then(Value::as_str).filter(|value| !value.is_empty()).map(str::to_owned)
}

/// Whether `value` is a well-formed W3C `traceparent` (version `00`) naming a real trace and span.
pub fn is_traceparent(value: &str) -> bool {
    let hex = |part: &str, length: usize| part.len() == length && part.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'));
    let parts: Vec<&str> = value.split('-').collect();
    match parts.as_slice() {
        [version, trace, span, flags] => {
            *version == "00" && hex(trace, 32) && hex(span, 16) && hex(flags, 2) && trace.bytes().any(|b| b != b'0') && span.bytes().any(|b| b != b'0')
        }
        _ => false,
    }
}

/// Whether `value` is a well-formed W3C `tracestate`: at most 32 list-members, each a key and a value.
pub fn is_tracestate(value: &str) -> bool {
    let members: Vec<&str> = value.split(',').collect();
    members.len() <= TRACESTATE_MEMBERS && members.iter().all(|member| is_tracestate_member(member))
}

fn is_tracestate_member(member: &str) -> bool {
    let member = member.trim_start_matches([' ', '\t']).trim_end_matches([' ', '\t']);
    let Some((key, value)) = member.split_once('=') else {
        return false;
    };
    let key_char = |c: char| matches!(c, 'a'..='z' | '0'..='9' | '_' | '-' | '*' | '/');
    let simple = |key: &str, first: fn(char) -> bool, most: usize| {
        let mut chars = key.chars();
        chars.next().is_some_and(first) && key.len() <= most + 1 && chars.all(key_char)
    };
    let key_valid = match key.split_once('@') {
        None => simple(key, |c| c.is_ascii_lowercase(), 255),
        Some((tenant, system)) => simple(tenant, |c| c.is_ascii_lowercase() || c.is_ascii_digit(), 240) && simple(system, |c| c.is_ascii_lowercase(), 13),
    };
    let value_char = |c: char| matches!(c, '\x20'..='\x2b' | '\x2d'..='\x3c' | '\x3e'..='\x7e');
    key_valid && !value.is_empty() && value.len() <= 256 && value.chars().all(value_char) && !value.ends_with(' ')
}
