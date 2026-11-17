//! The router every service is served through: its probes, and the limits that hold whatever door a request reaches.
//!
//! From the outside in, a request passes: its trace, the no-store headers, the panic answer, the deadline, the body
//! cap, and the answer to a path or method no door serves. Then the service's own doors.

use crate::service::health::{HealthWatch, Readiness, ReadinessStatus};
use crate::service::lifecycle::Lifecycle;
use crate::service::problem::{ErrorCode, PROBLEM_JSON, Problem};
use crate::service::trace;
use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::middleware::{Next, from_fn, from_fn_with_state};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use http_body_util::Limited;
use serde_json::json;
use std::any::Any;
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::signal::unix::{SignalKind, signal};
use tower_http::catch_panic::CatchPanicLayer;

/// What a service hands the shell to be served in it.
#[derive(Clone)]
pub struct Shell {
    pub lifecycle: Arc<Lifecycle>,
    pub health: Arc<HealthWatch>,
    /// The most bytes a request body may hold.
    pub max_body_bytes: usize,
    /// How long a request may take before its caller is answered that it runs on.
    pub request_deadline: Duration,
}

async fn livez() -> Json<serde_json::Value> {
    Json(json!({"status": "ok"}))
}

async fn readyz(State(shell): State<Shell>) -> Response {
    let readiness = if shell.lifecycle.is_draining() {
        Readiness::of(ReadinessStatus::ShuttingDown)
    } else if !shell.lifecycle.is_started() {
        Readiness::of(ReadinessStatus::Starting)
    } else {
        shell.health.readiness()
    };
    let status = if readiness.in_rotation() {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (status, Json(readiness)).into_response()
}

fn is_problem(response: &Response) -> bool {
    response.headers().get(header::CONTENT_TYPE).is_some_and(|kind| kind == PROBLEM_JSON)
}

/// Answer a path or a method no door serves as a problem, keeping the headers the router set (`Allow`).
async fn unserved_as_problems(request: Request, next: Next) -> Response {
    let response = next.run(request).await;
    if !matches!(response.status(), StatusCode::NOT_FOUND | StatusCode::METHOD_NOT_ALLOWED) || is_problem(&response) {
        return response;
    }
    let mut problem = Problem::for_status(response.status()).into_response();
    let kept = response
        .headers()
        .iter()
        .filter(|(name, _)| *name != header::CONTENT_TYPE && *name != header::CONTENT_LENGTH);
    problem.headers_mut().extend(kept.map(|(name, value)| (name.clone(), value.clone())));
    problem
}

/// Refuse a body over the cap: at once where the request declares its length, and where the door reads past it.
async fn body_cap(State(shell): State<Shell>, request: Request, next: Next) -> Response {
    let max = shell.max_body_bytes;
    let too_large = || {
        let detail = format!(
            "request body exceeds the {max}-byte limit; write large payloads directly to object storage with vended credentials instead of through the catalog"
        );
        Problem::new(ErrorCode::InvalidInput, 413, "Payload Too Large", "payload-too-large", detail).into_response()
    };
    let declared = request
        .headers()
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok());
    if declared.is_some_and(|declared| declared > max as u64) {
        return too_large();
    }
    let response = next.run(request.map(|body| Body::new(Limited::new(body, max)))).await;
    if response.status() == StatusCode::PAYLOAD_TOO_LARGE && !is_problem(&response) {
        too_large()
    } else {
        response
    }
}

/// Answer a request that outlives the deadline, and let it run on: stopping a write halfway leaves less known than
/// letting it finish. A caller that hangs up before the deadline stops its request.
async fn deadline(State(shell): State<Shell>, request: Request, next: Next) -> Response {
    struct StopUnlessLetGo(Option<tokio::task::AbortHandle>);
    impl Drop for StopUnlessLetGo {
        fn drop(&mut self) {
            if let Some(door) = self.0.take() {
                door.abort();
            }
        }
    }

    let (method, path) = (request.method().clone(), request.uri().path().to_owned());
    let trace_id = trace::current_trace_id().unwrap_or_default();
    let mut door = tokio::spawn(trace::within(trace_id, next.run(request)));
    let mut hung_up = StopUnlessLetGo(Some(door.abort_handle()));
    match tokio::time::timeout(shell.request_deadline, &mut door).await {
        Ok(Ok(response)) => response,
        Ok(Err(failed)) => match failed.try_into_panic() {
            Ok(panic) => std::panic::resume_unwind(panic),
            Err(_) => Problem::for_status(StatusCode::INTERNAL_SERVER_ERROR).into_response(),
        },
        Err(_) => {
            hung_up.0 = None;
            let seconds = shell.request_deadline.as_secs_f64();
            tracing::error!(%method, path, seconds, "request_deadline_passed");
            let detail =
                format!("the request did not finish within {seconds:.0} s and runs on; its outcome is unknown, so read what it changes before retrying it");
            Problem::new(ErrorCode::ServiceUnavailable, 503, "Service Unavailable", "request-deadline", detail).into_response()
        }
    }
}

fn panicked(_: Box<dyn Any + Send + 'static>) -> Response {
    tracing::error!("request_panicked");
    let detail = "the request panicked; nothing it changed is known, so read what it changes before retrying it";
    Problem::new(ErrorCode::Internal, 500, "Internal Server Error", "internal-error", detail).into_response()
}

/// Tell every cache to keep no answer, and that an answer depends on who asked.
async fn no_store(request: Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.entry(header::CACHE_CONTROL).or_insert(HeaderValue::from_static("no-store"));
    let varies: Vec<&str> = headers
        .get_all(header::VARY)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .map(str::trim)
        .collect();
    if !varies.iter().any(|field| *field == "*" || field.eq_ignore_ascii_case("authorization")) {
        headers.append(header::VARY, HeaderValue::from_static("Authorization"));
    }
    response
}

impl Shell {
    /// `doors` with the probes beside them, behind every limit the shell holds a request to.
    pub fn router(&self, doors: Router) -> Router {
        let probes = Router::new().route("/livez", get(livez)).route("/readyz", get(readyz)).with_state(self.clone());
        doors
            .merge(probes)
            .layer(from_fn(unserved_as_problems))
            .layer(from_fn_with_state(self.clone(), body_cap))
            .layer(from_fn_with_state(self.clone(), deadline))
            .layer(CatchPanicLayer::custom(panicked))
            .layer(from_fn(no_store))
            .layer(from_fn(trace::traced))
    }

    /// Serve `router` on `listener` until SIGTERM, which turns `/readyz` to shutting down and lets open requests end.
    pub async fn serve(&self, listener: TcpListener, router: Router) -> std::io::Result<()> {
        let mut terminate = signal(SignalKind::terminate())?;
        let lifecycle = Arc::clone(&self.lifecycle);
        let told_to_stop = async move {
            terminate.recv().await;
            lifecycle.mark_draining();
            tracing::info!("drain_armed_by_sigterm");
        };
        axum::serve(listener, router).with_graceful_shutdown(told_to_stop).await
    }
}
