//! What lineage does with one event that reached it, by the bus or from the outbox: read it, ask who may write it,
//! record it. The verdicts are the Judge step of `specs/lineage/EventPath.tla`: only a refusal that a grant could
//! lift is parked; what can never be recorded is taken and counted; what could not be asked or written is asked for again.

use crate::model::{Event, parse_event};
use lakehouse_core::delivery::Verdict;
use serde_json::{Map, Value};

/// Why an event's writer may not write it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    /// The writer holds no grant on an output. A grant would lift it, so the event is kept.
    Denied,
    /// No listed signer's key verifies the event's signature.
    Unverified,
    /// The event names no author.
    Unauthored,
    /// The run was founded by another subject.
    ForeignRun,
    /// An output is no table of this catalog.
    Ungoverned,
}

/// What asking who may write an event came to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Authorizing {
    /// Allowed, with the table uuids its outputs resolved to under another name.
    Allowed(Vec<String>),
    Refused(Refusal),
    Unavailable(String),
}

/// What writing an event's rows came to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Recording {
    Landed,
    ForeignRun,
    Failed(String),
}

/// What an event is counted as, in `lineage.events.processed`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Counted {
    Ingested,
    Refused,
    Retried,
    Unrepairable,
    SignatureRefused,
}

impl Counted {
    pub fn name(self) -> &'static str {
        match self {
            Counted::Ingested => "ingested",
            Counted::Refused => "refused",
            Counted::Retried => "retried",
            Counted::Unrepairable => "unrepairable",
            Counted::SignatureRefused => "signature_refused",
        }
    }
}

/// An event that cannot be read is taken: no later delivery of the same bytes reads better.
pub const UNREAD: (Verdict, Counted) = (Verdict::Taken, Counted::Unrepairable);

/// What follows asking: the aliased tables to record with, or the verdict that ends the handling.
pub fn after_authorizing(authorizing: Authorizing) -> Result<Vec<String>, (Verdict, Counted)> {
    match authorizing {
        Authorizing::Allowed(aliased) => Ok(aliased),
        Authorizing::Refused(Refusal::Denied) => Err((Verdict::Park, Counted::Refused)),
        Authorizing::Refused(Refusal::Unverified) => Err((Verdict::Taken, Counted::SignatureRefused)),
        Authorizing::Refused(Refusal::Unauthored | Refusal::ForeignRun | Refusal::Ungoverned) => Err((Verdict::Taken, Counted::Unrepairable)),
        Authorizing::Unavailable(error) => {
            tracing::warn!(error, "lineage_authz_unavailable");
            Err((Verdict::Again, Counted::Retried))
        }
    }
}

pub fn after_recording(recording: Recording) -> (Verdict, Counted) {
    match recording {
        Recording::Landed => (Verdict::Taken, Counted::Ingested),
        Recording::ForeignRun => (Verdict::Taken, Counted::Unrepairable),
        Recording::Failed(error) => {
            tracing::warn!(error, "lineage_ingest_failed");
            (Verdict::Again, Counted::Retried)
        }
    }
}

/// Handle the event an envelope carries. `authorize` is absent where the route already judged the writer.
pub async fn handle<A, R>(envelope: Option<&Map<String, Value>>, authorize: Option<A>, record: R) -> (Verdict, Counted)
where
    A: AsyncFnOnce(&Event, &Map<String, Value>) -> Authorizing,
    R: AsyncFnOnce(&Event, &[String]) -> Recording,
{
    let data = envelope.and_then(|envelope| envelope.get("data"));
    let Some(event) = data.and_then(|data| parse_event(data).ok()) else {
        tracing::error!("lineage_event_invalid");
        return UNREAD;
    };
    let aliased = match authorize {
        None => Vec::new(),
        Some(authorize) => match after_authorizing(authorize(&event, data.and_then(Value::as_object).unwrap_or(&Map::new())).await) {
            Ok(aliased) => aliased,
            Err(ended) => return ended,
        },
    };
    after_recording(record(&event, &aliased).await)
}
