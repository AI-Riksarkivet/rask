//! The trace a request belongs to, which every error body names so a caller can quote it.

use axum::extract::Request;
use axum::middleware::Next;
use axum::response::Response;
use uuid::Uuid;

tokio::task_local! {
    static TRACE_ID: String;
}

/// The trace id, parent span id and flags a W3C `traceparent` header carries, where it is a well-formed version 00 one.
pub(crate) fn parent_of(traceparent: &str) -> Option<(&str, &str, &str)> {
    let lower_hex = |text: &str, length: usize| text.len() == length && text.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'));
    let nonzero = |text: &str| text.bytes().any(|byte| byte != b'0');
    let mut parts = traceparent.split('-');
    let (version, trace_id, span_id, flags) = (parts.next()?, parts.next()?, parts.next()?, parts.next()?);
    let well_formed = parts.next().is_none()
        && version == "00"
        && lower_hex(trace_id, 32)
        && nonzero(trace_id)
        && lower_hex(span_id, 16)
        && nonzero(span_id)
        && lower_hex(flags, 2);
    well_formed.then_some((trace_id, span_id, flags))
}

/// The trace id of the request being served, where one is.
pub fn current_trace_id() -> Option<String> {
    TRACE_ID.try_with(Clone::clone).ok()
}

/// `work`, run as part of the trace `trace_id`.
pub async fn within<T>(trace_id: String, work: impl Future<Output = T>) -> T {
    TRACE_ID.scope(trace_id, work).await
}

/// Serve the request under the trace its `traceparent` names, or under a new one.
pub async fn traced(request: Request, next: Next) -> Response {
    let sent = request
        .headers()
        .get("traceparent")
        .and_then(|value| value.to_str().ok())
        .and_then(parent_of)
        .map(|(trace_id, _, _)| trace_id);
    let trace_id = sent.map_or_else(|| Uuid::new_v4().simple().to_string(), str::to_owned);
    within(trace_id, next.run(request)).await
}
