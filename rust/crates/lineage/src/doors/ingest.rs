//! The OpenLineage HTTP door (`POST /api/v1/lineage`): it records an event and announces nothing. The caller is bound
//! as the author, every governed name is resolved to its uuid as the caller sees it, the caller's rung is asked on
//! every output and input, and only then is the event recorded.

use super::{Answer, Refusal, answer, caller, field_error};
use crate::authz::names::{enforce_author, stamp_resolved_uuids};
use crate::intake::{Recording, refusal_outcome};
use crate::model::{Event, parse_event, without_signature_facet};
use crate::state::State;
use axum::body::to_bytes;
use axum::extract::{Request, State as Shared};
use axum::http::StatusCode;
use axum::http::header::CONTENT_TYPE;
use axum::response::IntoResponse;
use lakehouse_core::pywire::json;
use lakehouse_core::service::problem::{ErrorCode, NsError, Problem};
use lakehouse_core::telemetry::instruments::{LineageDoor, LineageOutcome};
use serde::Deserialize;
use serde_json::{Map, Value};
use std::time::Instant;

/// Whether a body of this content type is read as JSON: `application/json` or `application/*+json`, as an email
/// header parses it; a body with no content type is not.
fn is_json(content_type: Option<&str>) -> bool {
    let kind = content_type
        .and_then(|value| value.split(';').next())
        .map(|kind| kind.trim().to_ascii_lowercase());
    match kind.as_deref().and_then(|kind| kind.split_once('/')) {
        Some(("application", subtype)) if !subtype.contains('/') => subtype == "json" || subtype.ends_with("+json"),
        _ => false,
    }
}

/// The body as the framework's body parameter takes it: an object, or each way it is refused.
fn body_of(content_type: Option<&str>, bytes: &[u8]) -> Result<Map<String, Value>, Refusal> {
    let invalid = |message: &str, kind: &str| Refusal::Invalid(vec![field_error("body", message, kind)]);
    if bytes.is_empty() {
        return Err(invalid("Field required", "missing"));
    }
    if !is_json(content_type) {
        return Err(invalid("Input should be a valid dictionary", "dict_type"));
    }
    // What the framework answers a body its JSON reader raised on for another reason than its syntax.
    let unparsed = || {
        let problem = Problem::new(
            ErrorCode::InvalidInput,
            400,
            "Bad Request",
            "bad-request",
            "There was an error parsing the body",
        );
        Refusal::Answered(Box::new(problem.into_response()))
    };
    let bytes = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes);
    let text = std::str::from_utf8(bytes).map_err(|_| unparsed())?;
    if let Err(error) = json::parse(text) {
        if error.message == "nested too deep" {
            return Err(unparsed());
        }
        // CPython places a trailing comma's error at the comma, not at the bracket after it.
        let read = text.get(..error.position).unwrap_or_default();
        let before = read.trim_end_matches([' ', '\t', '\n', '\r']);
        let trailing = before.ends_with(',') && matches!(text.as_bytes().get(error.position), Some(b'}' | b']'));
        let at = if trailing { before.chars().count() - 1 } else { read.chars().count() };
        return Err(Refusal::Invalid(vec![field_error(&format!("body.{at}"), "JSON decode error", "json_invalid")]));
    }
    // The reader above bounded the nesting, so the value is read without serde's own, lower, bound.
    let mut reader = serde_json::Deserializer::from_str(text);
    reader.disable_recursion_limit();
    match Value::deserialize(&mut reader) {
        Ok(Value::Object(body)) => Ok(body),
        Ok(Value::Null) => Err(invalid("Field required", "missing")),
        Ok(_) => Err(invalid("Input should be a valid dictionary", "dict_type")),
        Err(error) => Err(invalid(&error.to_string(), "value_error")),
    }
}

/// Ingest one OpenLineage event, a run event or a change to a dataset that no run performed.
pub async fn ingest(Shared(state): Shared<State>, request: Request) -> Answer {
    let caller = caller(&state, request.headers()).await?;
    let content_type = request.headers().get(CONTENT_TYPE).and_then(|value| value.to_str().ok()).map(str::to_owned);
    let Ok(bytes) = to_bytes(request.into_body(), usize::MAX).await else {
        return Err(Refusal::Answered(Box::new(StatusCode::PAYLOAD_TOO_LARGE.into_response())));
    };
    let body = body_of(content_type.as_deref(), &bytes)?;
    // A signature only the bus and the outbox can verify; here it is a claim nobody checked.
    let mut event = parse_event(&without_signature_facet(&Value::Object(body)))
        .map_err(|error| Refusal::Invalid(vec![field_error("body", &error.to_string(), "value_error")]))?;
    let (subject, bearer) = (caller.subject.as_deref(), caller.bearer.as_deref());
    enforce_author(&mut event, subject);
    let decider = state.gate.decider.as_deref().filter(|_| state.gate.fga_enabled);
    stamp_resolved_uuids(&mut event, state.names.as_ref(), bearer, decider, subject, &state.gate.catalog).await?;
    let metrics = &state.gate.signals.lineage;
    // A refusal names a run the graph should have held, so it is counted by the outcome every door counts it by; a 401,
    // a 422 or an outage names none.
    let aliased = state.gate.ingest(&event, subject).await.inspect_err(|denial| {
        if let Ok(refusal) = denial.refusal() {
            metrics.record_outcome(refusal_outcome(refusal), LineageDoor::Http);
        }
    })?;
    let started = Instant::now();
    let recording = state.gate.store.record(&event, &aliased.into_iter().collect::<Vec<_>>()).await;
    if recording == Recording::ForeignRun {
        metrics.record_outcome(LineageOutcome::Unrepairable, LineageDoor::Http);
    }
    recorded(recording, &event)?;
    metrics.record_ingest_duration(started.elapsed().as_secs_f64());
    metrics.record_outcome(LineageOutcome::Ingested, LineageDoor::Http);
    let run = match &event {
        Event::Run(run) => Value::String(run.run.run_id.clone()),
        Event::Dataset(_) => Value::Null,
    };
    answer(StatusCode::CREATED, serde_json::json!({"status": "ingested", "run": run}))
}

/// A recording's refusal as a door answers it: another author's run is refused for good, anything else is an outage.
pub fn recorded(recording: Recording, event: &Event) -> Result<(), Refusal> {
    match recording {
        Recording::Landed => Ok(()),
        Recording::ForeignRun => {
            let run = match event {
                Event::Run(run) => run.run.run_id.as_str(),
                Event::Dataset(_) => "",
            };
            let message = format!("run {run} was founded by another subject; only its author may extend it");
            Err(NsError::new(ErrorCode::PermissionDenied, message).named("ForeignRunError").into())
        }
        Recording::Failed(error) => Err(NsError::new(ErrorCode::ServiceUnavailable, error).into()),
    }
}
