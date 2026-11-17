//! A service's telemetry against an OTLP/HTTP gateway in the test (ADR 0237): every signal arrives presenting the
//! projected token, a rotated token is presented on the next export, the audit trail arrives under its own scope, and
//! a gateway that is down, refuses, or is sent no token costs the service nothing but a count.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

#[path = "support/otlp_sink.rs"]
mod otlp_sink;

use axum::Router;
use axum::routing::get;
use lakehouse_core::service::health::HealthWatch;
use lakehouse_core::service::lifecycle::Lifecycle;
use lakehouse_core::service::shell::Shell;
use lakehouse_core::service::trace;
use lakehouse_core::telemetry::audit::{AuditAction, AuditOutcome};
use lakehouse_core::telemetry::http::HttpTelemetry;
use lakehouse_core::telemetry::instruments::{LineageDoor, LineageMetrics, LineageOutcome};
use lakehouse_core::telemetry::logging::{Value, Writer};
use lakehouse_core::telemetry::{LevelFilter, Settings, Telemetry};
use otlp_sink::{Sink, attributes, settings_at};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const TRACE_ID: &str = "4bf92f3577b34da6a3ce929d0e0e4736";

struct Scratch(PathBuf);

impl Scratch {
    fn new(token: Option<&str>) -> Scratch {
        let path = std::env::temp_dir().join(format!("telemetry-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&path).unwrap();
        let scratch = Scratch(path);
        if let Some(token) = token {
            scratch.token(token);
        }
        scratch
    }

    fn file(&self) -> PathBuf {
        self.0.join("token")
    }

    fn token(&self, token: &str) {
        std::fs::write(self.file(), token).unwrap();
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn captured() -> (Writer, Arc<Mutex<Vec<u8>>>) {
    let buffer = Arc::new(Mutex::new(Vec::new()));
    (buffer.clone(), buffer)
}

fn text(buffer: &Arc<Mutex<Vec<u8>>>) -> String {
    String::from_utf8(buffer.lock().unwrap().clone()).unwrap()
}

async fn served(telemetry: &Telemetry) -> String {
    let shell = Shell {
        lifecycle: Arc::new(Lifecycle::default()),
        health: Arc::new(HealthWatch::new(Vec::new())),
        max_body_bytes: 1024,
        request_deadline: Duration::from_secs(5),
    };
    shell.lifecycle.mark_started();
    let doors = Router::new().route(
        "/v1/things/{name}",
        get(|| async {
            tracing::info!(target: "lineage::doors", served = true, "thing_served");
            "ok"
        }),
    );
    let router = shell.instrumented_router(doors, &HttpTelemetry::new(telemetry));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    format!("http://{addr}")
}

async fn ask(base: &str, traceparent: Option<&str>) -> (u16, Duration) {
    let started = Instant::now();
    let mut request = reqwest::Client::builder().no_proxy().build().unwrap().get(format!("{base}/v1/things/a"));
    if let Some(traceparent) = traceparent {
        request = request.header("traceparent", traceparent);
    }
    (request.send().await.unwrap().status().as_u16(), started.elapsed())
}

/// A served request, a log line and a counter reach the gateway as a trace, a log record and a metric, each export
/// presenting the pod's token, and the trace id joins the span, the record and the stdout line.
#[tokio::test]
async fn every_signal_is_exported_presenting_the_projected_token() {
    let scratch = Scratch::new(Some("fixture-token-1\n"));
    let sink = Sink::start().await;
    let (out, lines) = captured();
    let telemetry = Telemetry::new(Some(&sink.settings(&scratch.file())), LevelFilter::INFO, out).unwrap();
    let _logging = tracing::subscriber::set_default(telemetry.subscriber());
    LineageMetrics::new(&telemetry.meter("lance.lineage")).record_outcome(LineageOutcome::Ingested, LineageDoor::Http);
    let base = served(&telemetry).await;
    let (status, _) = ask(&base, Some(&format!("00-{TRACE_ID}-00f067aa0ba902b7-01"))).await;
    assert_eq!(status, 200);
    telemetry.flush().await;

    for signal in ["/v1/traces", "/v1/metrics", "/v1/logs"] {
        let exports = sink.on(signal);
        assert!(!exports.is_empty(), "{signal} was exported");
        assert!(
            exports.iter().all(|export| export.authorization.as_deref() == Some("Bearer fixture-token-1")),
            "{signal}"
        );
    }
    let span = sink
        .traces()
        .into_iter()
        .flat_map(|r| r.resource_spans)
        .flat_map(|r| r.scope_spans)
        .flat_map(|s| s.spans)
        .next()
        .unwrap();
    assert_eq!(span.name, "GET /v1/things/{name}");
    let span_trace: String = span.trace_id.iter().map(|byte| format!("{byte:02x}")).collect();
    assert_eq!(span_trace, TRACE_ID);
    let scopes: Vec<_> = sink.logs().into_iter().flat_map(|r| r.resource_logs).flat_map(|r| r.scope_logs).collect();
    let served = scopes
        .iter()
        .find(|scope| scope.scope.as_ref().unwrap().name == "lineage.doors")
        .expect("the door's log record");
    let record = &served.log_records[0];
    assert_eq!(otlp_sink::any(record.body.as_ref().unwrap()), "thing_served");
    assert_eq!(attributes(&record.attributes)["served"], true);
    let record_trace: String = record.trace_id.iter().map(|byte| format!("{byte:02x}")).collect();
    assert_eq!(record_trace, TRACE_ID);
    let resource = attributes(&sink.metrics()[0].resource_metrics[0].resource.as_ref().unwrap().attributes);
    assert_eq!(resource["service.name"], "lineage");
    assert!(resource.contains_key("service.instance.id"));

    let written = text(&lines);
    let line = written.lines().find(|line| line.contains("thing_served")).unwrap();
    let date = line.as_bytes();
    assert!(
        date[..10]
            .iter()
            .enumerate()
            .all(|(at, byte)| if at == 4 || at == 7 { *byte == b'-' } else { byte.is_ascii_digit() })
            && date[10] == b' '
    );
    assert!(
        line.contains(&format!("INFO    lineage.doors [{TRACE_ID}] — thing_served served=True")),
        "{line}"
    );
    assert!(!written.contains("fixture-token"), "the token is never written");
    telemetry.shutdown().await;
}

/// The kubelet replaces the token file: the next export presents the new token, read from the file again.
#[tokio::test]
async fn a_rotated_token_is_presented_on_the_next_export() {
    let scratch = Scratch::new(Some("fixture-token-1"));
    let sink = Sink::start().await;
    let (out, _) = captured();
    let telemetry = Telemetry::new(Some(&sink.settings(&scratch.file())), LevelFilter::INFO, out).unwrap();
    let lineage = LineageMetrics::new(&telemetry.meter("lance.lineage"));
    lineage.record_ingest_duration(0.1);
    telemetry.flush().await;
    scratch.token("fixture-token-2");
    lineage.record_ingest_duration(0.2);
    telemetry.flush().await;
    let presented: Vec<_> = sink.on("/v1/metrics").into_iter().map(|export| export.authorization.unwrap()).collect();
    assert_eq!(presented, ["Bearer fixture-token-1", "Bearer fixture-token-2"]);
    telemetry.shutdown().await;
}

/// The governance trail arrives under the scope a gateway routes it by, with the request's trace; a trail the chart
/// turned off sends nothing.
#[tokio::test]
async fn the_audit_trail_arrives_under_its_own_scope() {
    let scratch = Scratch::new(Some("fixture-token-1"));
    let sink = Sink::start().await;
    let (out, _) = captured();
    let telemetry = Telemetry::new(Some(&sink.settings(&scratch.file())), LevelFilter::OFF, out).unwrap();
    telemetry
        .audit(false)
        .record(AuditAction::AUTHZ, AuditOutcome::Deny, Some("user:eve"), Some("t"), &[]);
    let audit = telemetry.audit(true);
    trace::within(TRACE_ID.to_owned(), async {
        audit.record(
            AuditAction::DLQ_REPLAY,
            AuditOutcome::Success,
            Some("user:ada"),
            Some("run:1"),
            &[("count", Value::Int(2))],
        );
    })
    .await;
    telemetry.flush().await;
    let scopes: Vec<_> = sink.logs().into_iter().flat_map(|r| r.resource_logs).flat_map(|r| r.scope_logs).collect();
    assert_eq!(scopes.len(), 1);
    assert_eq!(scopes[0].scope.as_ref().unwrap().name, "lance.audit");
    assert_eq!(scopes[0].log_records.len(), 1, "the disabled trail sent nothing");
    let fields = attributes(&scopes[0].log_records[0].attributes);
    assert_eq!(fields["audit.action"], "dlq_replay");
    assert_eq!(fields["audit.subject"], "user:ada");
    assert_eq!(fields["audit.count"], 2);
    assert_eq!(fields["audit.trace_id"], TRACE_ID);
    telemetry.shutdown().await;
}

async fn blocks_nothing(settings: &Settings, expect_sent: bool, sink: Option<&Sink>) {
    let (out, lines) = captured();
    let telemetry = Telemetry::new(Some(settings), LevelFilter::INFO, out).unwrap();
    let _logging = tracing::subscriber::set_default(telemetry.subscriber());
    let base = served(&telemetry).await;
    telemetry
        .audit(true)
        .record(AuditAction::AUTHN, AuditOutcome::Allow, Some("user:ada"), None, &[]);
    for _ in 0..3 {
        let (status, took) = ask(&base, None).await;
        assert_eq!(status, 200);
        assert!(took < Duration::from_secs(1), "a request waited on the gateway: {took:?}");
    }
    let started = Instant::now();
    telemetry.flush().await;
    assert!(started.elapsed() < Duration::from_secs(20), "a flush is bounded by the export timeout");
    assert!(
        telemetry.export_failures() >= 3,
        "each signal's failed export is counted: {}",
        telemetry.export_failures()
    );
    assert!(text(&lines).contains("telemetry_export_failed"), "the failure is said on stdout");
    if let Some(sink) = sink {
        assert_eq!(!sink.received().is_empty(), expect_sent);
    }
    let (status, _) = ask(&base, None).await;
    assert_eq!(status, 200, "the service answers after the failed exports");
    telemetry.shutdown().await;
}

/// A gateway that is not listening blocks no request and crashes nothing; each failed export is counted and said.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_gateway_that_is_down_costs_a_count_and_nothing_else() {
    let scratch = Scratch::new(Some("fixture-token-1"));
    let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    blocks_nothing(&settings_at(&format!("http://127.0.0.1:{port}"), &scratch.file()), false, None).await;
}

/// A gateway that refuses the token, or answers that it is unavailable, is the same: counted, and nothing blocks.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_gateway_that_refuses_costs_a_count_and_nothing_else() {
    for status in [401, 503] {
        let scratch = Scratch::new(Some("fixture-token-1"));
        let sink = Sink::start().await;
        sink.answer(status);
        blocks_nothing(&sink.settings(&scratch.file()), true, Some(&sink)).await;
    }
}

/// A token file that cannot be read sends nothing, never an export without the token; the next readable one sends.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_token_that_cannot_be_read_is_never_sent_anonymously() {
    let scratch = Scratch::new(None);
    let sink = Sink::start().await;
    blocks_nothing(&sink.settings(&scratch.file()), false, Some(&sink)).await;
    let (out, _) = captured();
    let telemetry = Telemetry::new(Some(&sink.settings(&scratch.file())), LevelFilter::OFF, out).unwrap();
    LineageMetrics::new(&telemetry.meter("lance.lineage")).record_ingest_duration(0.1);
    telemetry.flush().await;
    assert!(sink.received().is_empty());
    scratch.token("fixture-token-3");
    telemetry.flush().await;
    assert!(
        sink.received()
            .iter()
            .all(|export| export.authorization.as_deref() == Some("Bearer fixture-token-3"))
    );
    assert!(!sink.received().is_empty());
    telemetry.shutdown().await;
}

/// With the chart's export off every handle works and nothing leaves the process but the stdout line.
#[tokio::test]
async fn export_off_sends_nothing_and_still_logs() {
    let sink = Sink::start().await;
    let (out, lines) = captured();
    let telemetry = Telemetry::new(None, LevelFilter::INFO, out).unwrap();
    let _logging = tracing::subscriber::set_default(telemetry.subscriber());
    let base = served(&telemetry).await;
    assert_eq!(ask(&base, None).await.0, 200);
    LineageMetrics::new(&telemetry.meter("lance.lineage")).create_outcome_series();
    telemetry.audit(true).record(AuditAction::AUTHN, AuditOutcome::Allow, None, None, &[]);
    telemetry.flush().await;
    assert!(sink.received().is_empty());
    assert!(text(&lines).contains("thing_served"));
    assert!(text(&lines).contains("lance.audit"));
    assert!(Path::new("/").exists());
    telemetry.shutdown().await;
}
