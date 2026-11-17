//! What the Python telemetry answers (`rust/vectors/telemetry.json`), asserted of the Rust: every metric's name, unit,
//! kind, buckets and points through a real export, the HTTP server's spans and metrics, the audit records and lines,
//! the export identity, the settings, the log level and the log line.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

#[path = "support/otlp_sink.rs"]
mod otlp_sink;

use axum::Router;
use axum::routing::get;
use lakehouse_core::service::health::HealthWatch;
use lakehouse_core::service::lifecycle::Lifecycle;
use lakehouse_core::service::shell::Shell;
use lakehouse_core::service::trace;
use lakehouse_core::signing::RefusalReason;
use lakehouse_core::telemetry::audit::{AuditAction, AuditOutcome};
use lakehouse_core::telemetry::http::HttpTelemetry;
use lakehouse_core::telemetry::identity::{ProjectedTokenClient, bearer_of};
use lakehouse_core::telemetry::instruments::{BusMetrics, LineageDoor, LineageMetrics, LineageOutcome, OutboxLane, OutboxMetrics};
use lakehouse_core::telemetry::logging::{self, Value as Logged, format_line, log_level};
use lakehouse_core::telemetry::{LevelFilter, Settings, Telemetry};
use opentelemetry_http::HttpClient;
use otlp_sink::{Sink, attributes, floats, snapshot};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::AtomicU64;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime};

const TRACEPARENT: &str = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";

fn vectors() -> Value {
    let text = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/telemetry.json")).unwrap();
    serde_json::from_str(&text).unwrap()
}

fn group(name: &str) -> Vec<Value> {
    vectors()[name].as_array().unwrap().clone()
}

/// A snapshot with every number a float and each metric's points in one order, so two recordings compare as sets.
fn normalized(entries: &[Value]) -> Vec<Value> {
    let mut entries: Vec<Value> = entries
        .iter()
        .map(|entry| {
            let mut entry = floats(entry);
            let mut points = entry["points"].as_array().unwrap().clone();
            points.sort_by_key(Value::to_string);
            entry["points"] = json!(points);
            entry
        })
        .collect();
    entries.sort_by_key(|entry| entry["name"].as_str().unwrap().to_owned());
    entries
}

fn quiet() -> logging::Writer {
    Arc::new(Mutex::new(std::io::sink()))
}

fn token_file(directory: &std::path::Path) -> std::path::PathBuf {
    let path = directory.join("token");
    std::fs::write(&path, "fixture-token").unwrap();
    path
}

struct Scratch(std::path::PathBuf);

impl Scratch {
    fn new(name: &str) -> Scratch {
        let path = std::env::temp_dir().join(format!("telemetry-vectors-{name}-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&path).unwrap();
        Scratch(path)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

async fn latest_metrics(telemetry: &Telemetry, sink: &Sink, scopes: &[&str], values: bool) -> Vec<Value> {
    telemetry.flush().await;
    let exports = sink.metrics();
    let last = exports.last().expect("a metrics export");
    normalized(&snapshot(last, scopes, values))
}

fn outcome(label: &str) -> LineageOutcome {
    *LineageOutcome::ALL.iter().find(|outcome| outcome.label() == label).unwrap()
}

fn door(label: &str) -> LineageDoor {
    *LineageDoor::ALL.iter().find(|door| door.label() == label).unwrap()
}

fn lane(label: &str) -> OutboxLane {
    [OutboxLane::Lineage, OutboxLane::Control]
        .into_iter()
        .find(|lane| lane.label() == label)
        .unwrap()
}

fn failure(value: &Value) -> Option<&'static str> {
    value.as_str().map(|text| match text {
        "TimeoutError" => "TimeoutError",
        other => panic!("a failure the corpus does not name: {other}"),
    })
}

/// The instruments, driven through the Python corpus, export what the Python recorded at every snapshot.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_instruments_record_what_the_python_records() {
    let scratch = Scratch::new("instruments");
    let sink = Sink::start().await;
    let telemetry = Telemetry::new(Some(&sink.settings(&token_file(&scratch.0))), LevelFilter::OFF, quiet()).unwrap();
    let (lineage, bus, outbox) = (
        LineageMetrics::new(&telemetry.meter("lance.lineage")),
        BusMetrics::new(&telemetry.meter("lance.bus")),
        OutboxMetrics::new(&telemetry.meter("lance.outbox")),
    );
    let steps = group("metric_steps");
    let mut collected = 0;
    for step in &steps {
        let args = step["args"].as_array().cloned().unwrap_or_default();
        let text = |index: usize| args[index].as_str().unwrap().to_owned();
        let number = |index: usize| args[index].as_f64().unwrap();
        let count = |index: usize| args[index].as_u64().unwrap();
        match step["op"].as_str().unwrap() {
            "collect" => {
                let rust = latest_metrics(&telemetry, &sink, &["lance.lineage", "lance.bus", "lance.outbox"], true).await;
                let python = normalized(step["snapshot"].as_array().unwrap());
                assert_eq!(rust, python, "snapshot {collected}");
                collected += 1;
            }
            "lineage.create_outcome_series" => lineage.create_outcome_series(),
            "lineage.create_signature_refused_series" => lineage.create_signature_refused_series(),
            "lineage.record_outcome" => lineage.record_outcome(outcome(&text(0)), door(&text(1))),
            "lineage.record_ingest_duration" => lineage.record_ingest_duration(number(0)),
            "lineage.record_forwarded" => lineage.record_forwarded(count(0), count(1), args[2].as_bool().unwrap()),
            "lineage.record_signature_verified" => lineage.record_signature_verified(&text(0)),
            "lineage.record_signature_refused" => {
                let reason = RefusalReason::ALL.into_iter().find(|reason| reason.label() == text(0)).unwrap();
                lineage.record_signature_refused(reason);
            }
            "bus.create_delivery_series" => {
                let outcomes: Vec<String> = args[1].as_array().unwrap().iter().map(|o| o.as_str().unwrap().to_owned()).collect();
                bus.create_delivery_series(&text(0), &outcomes.iter().map(String::as_str).collect::<Vec<_>>());
            }
            "bus.record_delivery" => bus.record_delivery(&text(0), &text(1)),
            "bus.record_pull_failed" => bus.record_pull_failed(&text(0)),
            "bus.record_processing" => bus.record_processing(&text(0), args[1].as_str(), number(2)),
            "bus.record_publish" => bus.record_publish(&text(0), number(1), failure(&args[2])),
            "bus.record_refusal" => bus.record_refusal(&text(0), if text(1) == "oversize" { "oversize" } else { panic!("reason") }),
            "outbox.create_series" => outbox.create_series(lane(&text(0))),
            "outbox.record_staged" => outbox.record_staged(lane(&text(0))),
            "outbox.record_published" => outbox.record_published(lane(&text(0))),
            "outbox.record_publish_failed" => outbox.record_publish_failed(lane(&text(0))),
            "outbox.record_stage_failed" => outbox.record_stage_failed(lane(&text(0))),
            "outbox.record_drop_failed" => outbox.record_drop_failed(lane(&text(0))),
            "outbox.record_poison_dropped" => outbox.record_poison_dropped(lane(&text(0))),
            "outbox.record_drained" => outbox.record_drained(lane(&text(0)), count(1)),
            "outbox.record_stranded" => outbox.record_stranded(lane(&text(0)), count(1)),
            "outbox.record_refused" => outbox.record_refused(lane(&text(0)), count(1)),
            "outbox.observe_backlog" => outbox.observe_backlog(lane(&text(0)), count(1), number(2)),
            "outbox.forget_backlog" => outbox.forget_backlog(lane(&text(0))),
            other => panic!("an op the corpus does not name: {other}"),
        }
    }
    assert_eq!(collected, 5);
    telemetry.shutdown().await;
}

fn audit_vectors() -> Value {
    group("audit").remove(0)
}

fn logged(value: &Value) -> Logged {
    match value {
        Value::String(text) => Logged::Str(text.clone()),
        Value::Bool(flag) => Logged::Bool(*flag),
        Value::Number(number) if number.is_i64() => Logged::Int(number.as_i64().unwrap()),
        Value::Number(number) => Logged::Float(number.as_f64().unwrap()),
        other => panic!("a field of another kind: {other}"),
    }
}

/// The trail emits what the Python trail emits, record and line, under its scope, and counts what it emits.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_audit_trail_emits_what_the_python_emits() {
    let scratch = Scratch::new("audit");
    let sink = Sink::start().await;
    let lines: Arc<Mutex<Vec<u8>>> = Arc::new(Mutex::new(Vec::new()));
    let telemetry = Telemetry::new(Some(&sink.settings(&token_file(&scratch.0))), LevelFilter::OFF, lines.clone()).unwrap();
    let vectors = audit_vectors();
    assert_eq!(
        AuditAction::ALL.iter().map(|action| json!(action.0)).collect::<Vec<_>>(),
        *vectors["actions"].as_array().unwrap()
    );
    assert_eq!(
        AuditOutcome::ALL.iter().map(|outcome| json!(outcome.label())).collect::<Vec<_>>(),
        *vectors["outcomes"].as_array().unwrap()
    );
    let constants = &vectors["constants"][0];
    assert_eq!(constants["format"], lakehouse_core::telemetry::audit::AUDIT_FORMAT);
    assert_eq!(constants["scope"], lakehouse_core::telemetry::audit::AUDIT_SCOPE);
    assert_eq!(constants["all_columns"], lakehouse_core::telemetry::audit::ALL_COLUMNS);
    for case in vectors["cases"].as_array().unwrap() {
        let audit = telemetry.audit(case["enabled"].as_bool().unwrap());
        let before = sink.logs().len();
        lines.lock().unwrap().clear();
        let fields: Vec<(String, Logged)> = case["fields"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(key, value)| (key.clone(), logged(value)))
            .collect();
        let fields: Vec<(&str, Logged)> = fields.iter().map(|(key, value)| (key.as_str(), value.clone())).collect();
        let call = || {
            if case["call"] == "audit" {
                let action = *AuditAction::ALL.iter().find(|action| action.0 == case["action"]).unwrap();
                let outcome = AuditOutcome::ALL.into_iter().find(|outcome| outcome.label() == case["outcome"]).unwrap();
                audit.record(action, outcome, case["subject"].as_str(), case["resource"].as_str(), &fields);
            } else {
                let columns: Vec<&str> = case["columns"]
                    .as_array()
                    .map(|c| c.iter().map(|c| c.as_str().unwrap()).collect())
                    .unwrap_or_default();
                audit.read(
                    case["subject"].as_str().unwrap(),
                    case["resource"].as_str().unwrap(),
                    case["version"].as_i64(),
                    &columns,
                    &fields,
                );
            }
        };
        match case["traceparent"].as_str() {
            Some(traceparent) => trace::within(traceparent[3..35].to_owned(), async { call() }).await,
            None => call(),
        }
        telemetry.flush().await;
        let records: Vec<Value> = sink.logs()[before..]
            .iter()
            .flat_map(|request| request.resource_logs.iter())
            .flat_map(|resource| resource.scope_logs.iter())
            .flat_map(|scope| {
                scope.log_records.iter().map(|record| {
                    json!({
                        "scope": scope.scope.as_ref().unwrap().name,
                        "body": otlp_sink::any(record.body.as_ref().unwrap()),
                        "severity_number": record.severity_number,
                        "severity_text": record.severity_text,
                        "attributes": attributes(&record.attributes),
                    })
                })
            })
            .collect();
        let python: Vec<Value> = case["records"]
            .as_array()
            .unwrap()
            .iter()
            .map(|record| {
                let mut record = record.clone();
                let sorted: BTreeMap<String, Value> = record["attributes"].as_object().unwrap().clone().into_iter().collect();
                record["attributes"] = json!(sorted);
                record
            })
            .collect();
        assert_eq!(records, python, "{}", case["call"]);
        let written = String::from_utf8(lines.lock().unwrap().clone()).unwrap();
        let rust: Vec<&str> = written.lines().map(|line| &line[23..]).collect();
        let python: Vec<&str> = case["lines"].as_array().unwrap().iter().map(|line| &line.as_str().unwrap()[23..]).collect();
        assert_eq!(rust, python);
    }
    let counted = latest_metrics(&telemetry, &sink, &["lance.audit"], true).await;
    assert_eq!(counted, normalized(vectors["counted"].as_array().unwrap()));
    telemetry.shutdown().await;
}

async fn served(doors: Router, telemetry: &Telemetry) -> String {
    let shell = Shell {
        lifecycle: Arc::new(Lifecycle::default()),
        health: Arc::new(HealthWatch::new(Vec::new())),
        max_body_bytes: 1024,
        request_deadline: Duration::from_secs(5),
    };
    shell.lifecycle.mark_started();
    let router = shell.instrumented_router(doors, &HttpTelemetry::new(telemetry));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    format!("http://{addr}")
}

fn things() -> Router {
    async fn thing(axum::extract::Path(name): axum::extract::Path<String>) -> axum::Json<Value> {
        assert_ne!(name, "boom", "the door fails");
        axum::Json(json!({"name": name}))
    }
    Router::new().route("/v1/things/{name}", get(thing).post(thing))
}

/// The server's spans and metrics name what the Python instrumentation names, for the same requests.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn each_request_is_measured_as_the_python_instrumentation_measures_it() {
    let scratch = Scratch::new("http");
    let sink = Sink::start().await;
    let telemetry = Telemetry::new(Some(&sink.settings(&token_file(&scratch.0))), LevelFilter::OFF, quiet()).unwrap();
    let base = served(things(), &telemetry).await;
    let vectors = group("http").remove(0);
    let client = reqwest::Client::builder().no_proxy().user_agent("testclient").build().unwrap();
    for sent in vectors["requests"].as_array().unwrap() {
        let method = reqwest::Method::from_bytes(sent["method"].as_str().unwrap().as_bytes()).unwrap();
        let mut request = client.request(method, format!("{base}{}", sent["path"].as_str().unwrap()));
        for (name, value) in sent["headers"].as_object().unwrap() {
            request = request.header(name, value.as_str().unwrap());
        }
        if let Some(body) = sent["body"].as_str() {
            request = request.body(body.to_owned());
        }
        request.send().await.unwrap();
    }
    let metrics = latest_metrics(&telemetry, &sink, &[lakehouse_core::telemetry::http::HTTP_SCOPE], false).await;
    let python: Vec<Value> = vectors["metrics"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| {
            let mut entry = entry.clone();
            entry["scope"] = json!(lakehouse_core::telemetry::http::HTTP_SCOPE);
            entry
        })
        .collect();
    assert_eq!(metrics, normalized(&python));

    let spans: Vec<Value> = sink
        .traces()
        .iter()
        .flat_map(|request| request.resource_spans.iter())
        .flat_map(|resource| resource.scope_spans.iter())
        .flat_map(|scope| scope.spans.iter())
        .map(|span| {
            let keys: BTreeSet<String> = span.attributes.iter().map(|kv| kv.key.clone()).collect();
            let status = match span.status.as_ref().map(|status| status.code) {
                Some(2) => "error",
                _ => "unset",
            };
            let remote = !span.parent_span_id.is_empty();
            let trace_id: String = span.trace_id.iter().map(|byte| format!("{byte:02x}")).collect();
            json!({"name": span.name, "kind": span.kind, "attributes": keys, "status": status, "trace_id": remote.then_some(trace_id)})
        })
        .collect();
    let python = vectors["spans"].as_array().unwrap();
    assert_eq!(spans.len(), python.len());
    let unsent = BTreeSet::from(["client.address".to_owned(), "client.port".to_owned()]);
    for (rust, python) in spans.iter().zip(python) {
        assert_eq!(rust["name"], python["name"]);
        assert_eq!(rust["kind"], 2, "SERVER");
        assert_eq!(rust["status"], python["status"]);
        assert_eq!(rust["trace_id"], python["trace_id"]);
        let python_keys: BTreeSet<String> = python["attributes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|key| key.as_str().unwrap().to_owned())
            .collect();
        let rust_keys: BTreeSet<String> = serde_json::from_value(rust["attributes"].clone()).unwrap();
        assert_eq!(
            python_keys.difference(&rust_keys).cloned().collect::<BTreeSet<_>>(),
            unsent,
            "{}",
            python["name"]
        );
        assert!(rust_keys.is_subset(&python_keys), "{}", python["name"]);
    }
    telemetry.shutdown().await;
}

/// The names an alert or a dashboard can read: every metric the Python records, the Rust records, and no other.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_rust_records_every_metric_name_the_python_records() {
    let all = vectors();
    let mut python: BTreeSet<String> = BTreeSet::new();
    for step in all["metric_steps"].as_array().unwrap() {
        for entry in step["snapshot"].as_array().into_iter().flatten() {
            python.insert(entry["name"].as_str().unwrap().to_owned());
        }
    }
    for entry in all["http"][0]["metrics"]
        .as_array()
        .unwrap()
        .iter()
        .chain(all["audit"][0]["counted"].as_array().unwrap())
    {
        python.insert(entry["name"].as_str().unwrap().to_owned());
    }

    let scratch = Scratch::new("names");
    let sink = Sink::start().await;
    let telemetry = Telemetry::new(Some(&sink.settings(&token_file(&scratch.0))), LevelFilter::OFF, quiet()).unwrap();
    let lineage = LineageMetrics::new(&telemetry.meter("lance.lineage"));
    lineage.create_outcome_series();
    lineage.create_signature_refused_series();
    lineage.record_ingest_duration(0.1);
    lineage.record_forwarded(0, 0, false);
    lineage.record_signature_verified("rask-sa-catalog");
    let bus = BusMetrics::new(&telemetry.meter("lance.bus"));
    bus.create_delivery_series("lineage-events", &["taken"]);
    bus.record_processing("lineage.events.v1", None, 0.1);
    bus.record_publish("lineage.events.v1", 0.1, None);
    bus.record_refusal("lineage.events.v1", "oversize");
    let outbox = OutboxMetrics::new(&telemetry.meter("lance.outbox"));
    outbox.create_series(OutboxLane::Lineage);
    outbox.observe_backlog(OutboxLane::Lineage, 0, 0.0);
    telemetry.audit(true).record(AuditAction::AUTHN, AuditOutcome::Allow, None, None, &[]);
    let base = served(things(), &telemetry).await;
    reqwest::Client::builder()
        .no_proxy()
        .build()
        .unwrap()
        .post(format!("{base}/v1/things/a"))
        .body("{}")
        .send()
        .await
        .unwrap();
    telemetry.flush().await;
    let rust: BTreeSet<String> = sink
        .metrics()
        .last()
        .unwrap()
        .resource_metrics
        .iter()
        .flat_map(|resource| resource.scope_metrics.iter())
        .flat_map(|scope| scope.metrics.iter().map(|metric| metric.name.clone()))
        .collect();
    assert_eq!(rust, python);
    telemetry.shutdown().await;
}

/// The bearer a token file's contents give, as the Python session presents it, each export reading the file again.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn each_export_presents_the_token_the_python_presents() {
    let scratch = Scratch::new("identity");
    let sink = Sink::start().await;
    let path = scratch.0.join("token");
    let failures = Arc::new(AtomicU64::new(0));
    let said = Arc::new(logging::LogSink::new(LevelFilter::OFF, quiet(), None));
    let client = ProjectedTokenClient::new(path.clone(), Duration::from_secs(2), tokio::runtime::Handle::current(), failures, said).unwrap();
    for case in group("identity") {
        if case["exports"] == "no session" {
            let env = |name: &str| (name != "RASK_OTLP_TOKEN_FILE" && name == "OTEL_EXPORTER_OTLP_ENDPOINT").then(|| "http://gateway.test".to_owned());
            assert!(Settings::from_lookup(env).is_err(), "{}", case["name"]);
            continue;
        }
        for (contents, export) in case["files"].as_array().unwrap().iter().zip(case["exports"].as_array().unwrap()) {
            let _ = std::fs::remove_file(&path);
            if let Some(contents) = contents.as_str() {
                std::fs::write(&path, contents).unwrap();
                let decided = bearer_of(contents).ok().map(|value| value.to_str().unwrap().to_owned());
                assert_eq!(decided.as_deref(), export["authorization"].as_str(), "{}", case["name"]);
            }
            let before = sink.received().len();
            let request = http::Request::post(format!("http://{}/v1/traces", sink.addr))
                .body(bytes::Bytes::from_static(b"x"))
                .unwrap();
            let sent = client.send_bytes(request).await.is_ok();
            assert_eq!(sent, export["sent"].as_bool().unwrap(), "{}", case["name"]);
            let seen: Vec<Option<String>> = sink.received()[before..].iter().map(|received| received.authorization.clone()).collect();
            let expected: Vec<Option<String>> = export["authorization"].as_str().map(str::to_owned).into_iter().map(Some).collect();
            assert_eq!(seen, expected, "{}", case["name"]);
        }
    }
}

/// The settings read from each environment of the corpus, where the Python exporters, reader and resource read them.
#[test]
fn the_settings_are_read_as_the_python_reads_them() {
    for case in group("settings") {
        let env: BTreeMap<String, String> = serde_json::from_value(case["env"].clone()).unwrap();
        let read = Settings::from_lookup(|name| env.get(name).cloned());
        let python = &case["read"];
        let name = case["name"].as_str().unwrap();
        if !env.contains_key("OTEL_EXPORTER_OTLP_ENDPOINT") {
            assert_eq!(read, Ok(None), "{name}: no endpoint is export off, where Python exports to its default");
            continue;
        }
        if python["protocol"] != "http/protobuf" || python["exporting"] == "refused" {
            assert!(read.is_err(), "{name}");
            continue;
        }
        let settings = read.unwrap().unwrap();
        for (signal, endpoint) in [("traces", &settings.traces), ("metrics", &settings.metrics), ("logs", &settings.logs)] {
            let exported = !python["exporters"][signal].as_array().unwrap().is_empty();
            let expected = exported.then(|| python["exporting"][signal].as_str().unwrap().to_owned());
            assert_eq!(endpoint, &expected, "{name}: {signal}");
        }
        assert_eq!(settings.timeout.as_secs_f64(), python["timeout"].as_f64().unwrap(), "{name}");
        assert_eq!(settings.metric_interval.as_millis() as f64, python["interval_ms"].as_f64().unwrap(), "{name}");
        let resource: BTreeMap<String, String> = settings.resource.iter().cloned().collect();
        let expected: BTreeMap<String, String> = serde_json::from_value(python["resource"].clone()).unwrap();
        assert_eq!(resource, expected, "{name}");
        assert_eq!(settings.service_name, expected["service.name"], "{name}");
    }
}

/// `RASK_LOG_LEVEL` read as the Python root logger takes it.
#[test]
fn the_log_level_is_read_as_the_python_reads_it() {
    for case in group("levels") {
        let expected = match case["level"].as_str() {
            None => None,
            Some("NOTSET") => Some(LevelFilter::TRACE),
            Some("DEBUG") => Some(LevelFilter::DEBUG),
            Some("INFO") => Some(LevelFilter::INFO),
            Some("WARNING") => Some(LevelFilter::WARN),
            Some("ERROR") => Some(LevelFilter::ERROR),
            Some("CRITICAL") => Some(LevelFilter::OFF),
            Some(other) => panic!("{other}"),
        };
        assert_eq!(log_level(case["raw"].as_str()).ok(), expected, "{}", case["raw"]);
    }
}

/// A line as the Python handler writes it.
#[test]
fn a_line_is_written_as_the_python_writes_it() {
    let at = SystemTime::UNIX_EPOCH + Duration::from_millis(1_794_909_600_123);
    for case in group("lines") {
        let level = match case["level"].as_str().unwrap() {
            "DEBUG" => tracing::Level::DEBUG,
            "INFO" => tracing::Level::INFO,
            "WARNING" => tracing::Level::WARN,
            "ERROR" => tracing::Level::ERROR,
            other => panic!("{other}"),
        };
        let trace_id = case["traceparent"].as_str().map_or("-", |traceparent| &traceparent[3..35]);
        let fields: Vec<(String, Logged)> = case["fields"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(key, value)| (key.clone(), logged(value)))
            .collect();
        let line = format_line(at, level, case["name"].as_str().unwrap(), trace_id, case["message"].as_str().unwrap(), &fields);
        assert_eq!(line, case["line"].as_str().unwrap());
    }
    assert!(TRACEPARENT.len() == 55);
}
