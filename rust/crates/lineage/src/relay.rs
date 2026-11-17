//! What the relay does with one event still staged in the outbox: record it, publish it again, then drop it.
//!
//! The steps and their order are the relay's in `specs/lineage/EventPath.tla` (RelayJudge, RelayWrite,
//! RelayRepublish, RelayRefuse, RelayDrop): an event leaves the outbox only once it is recorded or kept as refused,
//! and any step that fails leaves it staged for the next tick.

use crate::intake::{Authorizing, Recording, Refusal, refusal_outcome};
use crate::model::Event;
use lakehouse_core::telemetry::instruments::LineageOutcome as Counted;
use serde_json::{Map, Value};

/// What became of one staged event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Drained {
    /// Recorded, published again and dropped.
    Drained,
    /// A step failed: it stays staged.
    Stranded,
    /// Its writer was refused. `kept` says the refusal was written, and with it how the event is counted.
    Refused { kept: Option<Counted> },
    /// It could not be read as an event, and was dropped.
    Poison,
}

/// The effects the relay has on one staged event. Each answers whether it was done.
pub trait Effects {
    fn authorize(&mut self, event: &Event, arrived: &Map<String, Value>) -> impl Future<Output = Authorizing>;
    fn record(&mut self, event: &Event, aliased: &[String]) -> impl Future<Output = Recording>;
    fn republish(&mut self) -> impl Future<Output = bool>;
    fn drop_staged(&mut self, key: &str) -> impl Future<Output = bool>;
    fn keep_refusal(&mut self, key: &str, event: &Event) -> impl Future<Output = bool>;
}

/// Keep the refusal, then drop the event. A drop that fails leaves it staged, to be kept again as the same row.
async fn refused(key: &str, event: &Event, refusal: Refusal, effects: &mut impl Effects) -> Drained {
    if !effects.keep_refusal(key, event).await {
        return Drained::Refused { kept: None };
    }
    effects.drop_staged(key).await;
    Drained::Refused {
        kept: Some(refusal_outcome(refusal)),
    }
}

/// Drain the event staged under `key`. `staged` is the event and the data it arrived as, or `None` where the staged
/// text is no event.
pub async fn drain_one(key: &str, staged: Option<(&Event, &Map<String, Value>)>, effects: &mut impl Effects) -> Drained {
    let Some((event, arrived)) = staged else {
        effects.drop_staged(key).await;
        return Drained::Poison;
    };
    let aliased = match effects.authorize(event, arrived).await {
        Authorizing::Allowed(aliased) => aliased,
        Authorizing::Refused(refusal) => return refused(key, event, refusal, effects).await,
        Authorizing::Unavailable(_) => return Drained::Stranded,
    };
    match effects.record(event, &aliased).await {
        Recording::Landed => {}
        Recording::ForeignRun => return refused(key, event, Refusal::ForeignRun, effects).await,
        Recording::Failed(_) => return Drained::Stranded,
    }
    if effects.republish().await && effects.drop_staged(key).await {
        Drained::Drained
    } else {
        Drained::Stranded
    }
}
