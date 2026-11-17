//! The admin doors over what is at risk: the outbox's staged events (listed, and replayed one at a time) and the
//! deliveries the ingest lane parked (counted by run, and re-presented). A replay is a re-ingest, so it passes the gates
//! an arrival passes; a staged event the caller could not see is answered as one nothing staged.

use super::ingest::recorded;
use super::{Answer, Query, Refusal, answer, audit, caller, segment};
use crate::authz::gates::{DatasetFilter, authorizing, require_parks_reader, require_parks_replayer};
use crate::authz::scope::Governed;
use crate::gate::arrived_data;
use crate::intake;
use crate::model::{Dataset, Event, parse_event};
use crate::state::State;
use axum::extract::{Path, Request, State as Shared};
use axum::http::StatusCode;
use lakehouse_bus::envelope::{CloudEvent, lineage_envelope};
use lakehouse_bus::publish_event;
use lakehouse_core::authz::table_object;
use lakehouse_core::delivery::Verdict;
use lakehouse_core::events::dataset_uuids;
use lakehouse_core::outbox::{Outbox, backlog, in_order};
use lakehouse_core::pywire::json::{Json, parse};
use lakehouse_core::service::problem::{ErrorCode, NsError};
use lakehouse_core::storage::StorageError;
use lakehouse_core::telemetry::audit::{AuditAction, AuditOutcome};
use lakehouse_core::telemetry::instruments::{LineageDoor, LineageOutcome};
use serde_json::{Map, Value, json};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

fn store_down(error: StorageError) -> Refusal {
    NsError::new(ErrorCode::ServiceUnavailable, error.to_string()).into()
}

/// The envelope a staged object holds and the event it carries, or `None` for a poison object.
fn staged(body: &[u8]) -> Option<(CloudEvent, Event)> {
    let envelope = CloudEvent::parse(body).ok()?;
    let event = parse_event(&Value::Object(envelope.data.clone())).ok()?;
    Some((envelope, event))
}

/// What an operator is shown of a staged event; a poison object is shown by its key alone.
fn summary(key: &str, event: Option<&Event>) -> Value {
    let names = |datasets: &[Dataset]| -> Vec<String> { datasets.iter().filter(|d| !d.name.is_empty()).map(|d| d.name.clone()).collect() };
    let (run_id, kind, job, inputs) = match event {
        Some(Event::Run(run)) => {
            let run_id = Some(run.run.run_id.as_str()).filter(|id| !id.is_empty()).unwrap_or(key);
            (run_id, Some(run.event_type.as_str()), Some(run.job.name.as_str()), names(&run.inputs))
        }
        _ => (key, None, None, Vec::new()),
    };
    let outputs = event.map(|event| names(event.outputs())).unwrap_or_default();
    json!({
        "run_id": run_id, "event_type": kind, "event_time": event.map(Event::event_time), "job": job,
        "inputs": inputs, "outputs": outputs, "parseable": event.is_some(),
    })
}

/// A kept delivery's event as the document its signature covers.
fn as_arrived(arrived: &Map<String, Value>) -> Json {
    serde_json::to_string(arrived).ok().and_then(|text| parse(&text).ok()).unwrap_or(Json::Null)
}

/// The outbox snapshot and the staged events the caller may see, oldest first, at most `limit`.
pub async fn list(Shared(state): Shared<State>, request: Request) -> Answer {
    let caller = caller(&state, request.headers()).await?;
    let mut query = Query::of(request.uri());
    let limit = query.int("limit", 1, Some(500)).unwrap_or(100);
    query.checked()?;
    if state.gate.fga_enabled && caller.subject.is_none() {
        return Err(NsError::new(ErrorCode::Unauthenticated, "authentication required").into());
    }
    let Some(outbox) = &state.outbox else {
        return answer(StatusCode::OK, json!({"depth": 0, "oldest_age_seconds": 0.0, "events": [], "limit": limit}));
    };
    let listing = outbox.listing().await.map_err(store_down)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |since| i64::try_from(since.as_nanos()).unwrap_or(i64::MAX));
    let (depth, oldest_age) = backlog(&listing, now);
    let read = outbox.read_staged(&listing, usize::try_from(limit).ok()).await.map_err(store_down)?;
    let parsed: Vec<Option<Event>> = read.iter().map(|staged_event| staged(&staged_event.body).map(|(_, event)| event)).collect();
    let mut filter = DatasetFilter::new(state.gate.authority(), caller.subject.as_deref());
    let mut scopes = filter
        .scope_unrecorded(&parsed.iter().flatten().cloned().collect::<Vec<_>>())
        .await?
        .into_iter();
    let items: Vec<(Value, Governed)> = read
        .iter()
        .zip(&parsed)
        .map(|(staged_event, event)| {
            let governs = if event.is_some() {
                scopes.next().unwrap_or_default()
            } else {
                Governed::default()
            };
            (summary(&staged_event.key, event.as_ref()), governs)
        })
        .collect();
    let shown: Vec<Value> = filter
        .governed(items, |(_, governs)| governs.clone())
        .await?
        .into_iter()
        .map(|(shown, _)| shown)
        .collect();
    answer(
        StatusCode::OK,
        json!({"depth": depth, "oldest_age_seconds": oldest_age, "events": shown, "limit": limit}),
    )
}

/// The object staged for a run: under its own key, else the newest `<run>@*` object.
async fn resolve(outbox: &Outbox, run_id: &str) -> Result<Option<(String, Vec<u8>)>, Refusal> {
    if let Some(body) = outbox.read(run_id).await.map_err(store_down)? {
        return Ok(Some((run_id.to_owned(), body)));
    }
    let listing = outbox.listing().await.map_err(store_down)?;
    let prefix = format!("{run_id}@");
    let Some((key, _)) = in_order(&listing, None).into_iter().rfind(|(key, _)| key.starts_with(&prefix)) else {
        return Ok(None);
    };
    Ok(outbox.read(&key).await.map_err(store_down)?.map(|body| (key, body)))
}

/// What a replay acted on, for the audit trail: the run, or the table a dataset event names.
fn audited(event: &Event, run_id: &str) -> String {
    match event {
        Event::Dataset(change) => dataset_uuids(&change.dataset.facets)
            .table_uuid
            .map_or_else(|| format!("table:{}", change.dataset.name), |uuid| table_object(&uuid)),
        Event::Run(run) => format!("run:{}", Some(run.run.run_id.as_str()).filter(|id| !id.is_empty()).unwrap_or(run_id)),
    }
}

/// Re-ingest one staged event, announce it again, then drop it: the gates of the outbox drain over the staged bytes,
/// and the ingest door's over the operator.
pub async fn replay(Shared(state): Shared<State>, Path(run_id): Path<String>, request: Request) -> Answer {
    let run_id = segment(run_id)?;
    let caller = caller(&state, request.headers()).await?;
    let missing = || -> Refusal { NsError::new(ErrorCode::TransactionNotFound, format!("no staged lineage event for run {run_id}")).into() };
    let Some(outbox) = &state.outbox else { return Err(missing()) };
    let (key, body) = resolve(outbox, &run_id).await?.ok_or_else(missing)?;
    let Some((envelope, event)) = staged(&body) else {
        if state.gate.fga_enabled {
            return Err(missing());
        }
        return Err(NsError::new(ErrorCode::Unsupported, format!("staged event for run {run_id} is unparseable (poison)")).into());
    };
    let subject = caller.subject.as_deref();
    let mut filter = DatasetFilter::new(state.gate.authority(), subject);
    let refs = filter.scope_unrecorded(std::slice::from_ref(&event)).await?.pop().unwrap_or_default();
    if state.gate.fga_enabled && !refs.names_nothing() && (!refs.unresolved.is_empty() || !refs.objects.is_subset(&filter.visible(&refs.objects).await?)) {
        return Err(missing());
    }
    let aliased = state.gate.replay(&event, arrived_data(&body), subject).await?;
    recorded(state.gate.store.record(&event, &aliased.into_iter().collect::<Vec<_>>()).await, &event)?;
    let (who, resource) = (subject.unwrap_or_default(), audited(&event, &run_id));
    if let Some(publisher) = &state.publisher {
        let again = lineage_envelope(&envelope.data).with_trace_of(&envelope);
        if let Err(error) = publish_event(&publisher.bus, &again, &publisher.topic, publisher.within, None).await {
            audit(AuditAction::DLQ_REPLAY, AuditOutcome::Failure, who, &resource);
            tracing::warn!(run_id, error = %error, "lineage_dlq_replay_unannounced");
            let message = format!("replayed {run_id} into the feed, but re-announcing it failed; it stays staged for the relay to retry");
            return Err(NsError::new(ErrorCode::ServiceUnavailable, message).into());
        }
    }
    outbox.drop_staged(&key).await.map_err(store_down)?;
    audit(AuditAction::DLQ_REPLAY, AuditOutcome::Success, who, &resource);
    tracing::info!(run_id, "lineage_dlq_replayed");
    answer(StatusCode::OK, json!({"status": "replayed", "run_id": run_id}))
}

/// The runs whose deliveries the park keeps, each with how many, in the order the table holds them.
pub async fn parked(Shared(state): Shared<State>, request: Request) -> Answer {
    let caller = caller(&state, request.headers()).await?;
    require_parks_reader(&state.gate.authority(), caller.subject.as_deref()).await?;
    let mut runs = Map::new();
    for delivery in state.gate.store.parked(None).await? {
        let count = runs.entry(delivery.run_id.unwrap_or_default()).or_insert(json!(0));
        *count = json!(count.as_u64().unwrap_or_default() + 1);
    }
    answer(StatusCode::OK, json!({"runs": runs}))
}

/// A text as Python's `repr` writes it in a list, for a refusal that lists what stays kept.
fn quoted(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') { '"' } else { '\'' };
    let escaped = text.replace('\\', "\\\\");
    let escaped = if quote == '\'' { escaped.replace('\'', "\\'") } else { escaped };
    format!("{quote}{escaped}{quote}")
}

/// Re-present the kept deliveries of a run through the ingest lane's own handling, each judged on its own feed row:
/// one the feed then holds is recovered and kept no longer.
pub async fn replay_parked(Shared(state): Shared<State>, Path(run_id): Path<String>, request: Request) -> Answer {
    let run_id = segment(run_id)?;
    let caller = caller(&state, request.headers()).await?;
    let (authority, subject) = (state.gate.authority(), caller.subject.as_deref());
    require_parks_replayer(&authority, subject).await?;
    let kept = state.gate.store.parked(Some(&run_id)).await?;
    if kept.is_empty() {
        let message = format!("no parked lineage delivery is kept for run {run_id}");
        return Err(NsError::new(ErrorCode::TransactionNotFound, message).into());
    }
    let (gate, metrics) = (&state.gate, &state.gate.signals.lineage);
    let mut answers = Vec::new();
    for delivery in &kept {
        let authorize = async |event: &Event, arrived: &Map<String, Value>| authorizing(gate.authorize(event, as_arrived(arrived)).await);
        let record = async |event: &Event, aliased: &[String]| {
            let started = Instant::now();
            let recording = gate.store.record(event, aliased).await;
            if recording == intake::Recording::Landed {
                metrics.record_ingest_duration(started.elapsed().as_secs_f64());
            }
            recording
        };
        let (verdict, counted) = intake::handle(delivery.cloud_event.as_object(), Some(authorize), record).await;
        metrics.record_outcome(counted, LineageDoor::DeadLetter);
        answers.push(verdict);
    }
    let held = state
        .gate
        .store
        .events_recorded(kept.iter().filter_map(|delivery| delivery.feed_key.as_deref()))
        .await?;
    let is_held = |key: &Option<String>| key.as_ref().is_some_and(|key| held.contains(key));
    let recovered: Vec<&str> = kept
        .iter()
        .filter(|delivery| is_held(&delivery.feed_key))
        .map(|delivery| delivery.key.as_str())
        .collect();
    if !recovered.is_empty() {
        state.gate.store.forget_parked(&recovered).await?;
        for _ in &recovered {
            metrics.record_outcome(LineageOutcome::Recovered, LineageDoor::DeadLetter);
        }
    }
    let who = subject.unwrap_or_default();
    let left: Vec<_> = kept.iter().zip(&answers).filter(|(delivery, _)| !is_held(&delivery.feed_key)).collect();
    if !left.is_empty() {
        audit(AuditAction::DLQ_PARKED_REPLAY, AuditOutcome::Failure, who, &format!("run:{run_id}"));
        let names: Vec<String> = left
            .iter()
            .map(|(delivery, verdict)| {
                let value = match verdict {
                    Verdict::Taken => "taken",
                    Verdict::Again => "again",
                    Verdict::Park => "park",
                };
                quoted(&format!("{}: {value}", delivery.feed_key.as_deref().unwrap_or(&delivery.key)))
            })
            .collect();
        let (count, names) = (left.len(), names.join(", "));
        if left.iter().any(|(_, verdict)| **verdict == Verdict::Again) {
            let message = format!("re-presenting run {run_id} met an outage; {count} delivery(ies) stay kept: [{names}]");
            return Err(NsError::new(ErrorCode::ServiceUnavailable, message).into());
        }
        let message = format!(
            "run {run_id} is still refused on re-presentation; {count} delivery(ies) stay kept, {} recovered: [{names}]",
            recovered.len()
        );
        return Err(NsError::new(ErrorCode::InvalidInput, message).into());
    }
    audit(AuditAction::DLQ_PARKED_REPLAY, AuditOutcome::Success, who, &format!("run:{run_id}"));
    tracing::info!(run_id, deliveries = kept.len(), "lineage_parked_replayed");
    answer(StatusCode::OK, json!({"status": "recovered", "run_id": run_id}))
}
