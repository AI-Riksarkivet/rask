//! An OTLP/HTTP gateway in the test: it decodes the protobuf bodies it is sent, keeps them with the bearer each came
//! with, and answers with the status the test sets.

#![allow(dead_code, clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::Router;
use axum::body::Bytes;
use axum::http::{HeaderMap, StatusCode, Uri};
use axum::routing::post;
use lakehouse_core::telemetry::Settings;
use opentelemetry_proto::tonic::collector::logs::v1::ExportLogsServiceRequest;
use opentelemetry_proto::tonic::collector::metrics::v1::ExportMetricsServiceRequest;
use opentelemetry_proto::tonic::collector::trace::v1::ExportTraceServiceRequest;
use opentelemetry_proto::tonic::common::v1::{AnyValue, KeyValue, any_value};
use opentelemetry_proto::tonic::metrics::v1::metric::Data;
use opentelemetry_proto::tonic::metrics::v1::number_data_point;
use prost::Message;
use serde_json::{Map, Value, json};
use std::net::SocketAddr;
use std::path::Path;
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(Clone, Debug)]
pub struct Received {
    pub path: String,
    pub authorization: Option<String>,
    pub body: Bytes,
}

#[derive(Clone)]
pub struct Sink {
    pub addr: SocketAddr,
    received: Arc<Mutex<Vec<Received>>>,
    status: Arc<AtomicU16>,
}

impl Sink {
    pub async fn start() -> Sink {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let received = Arc::new(Mutex::new(Vec::new()));
        let status = Arc::new(AtomicU16::new(200));
        let (kept, answer) = (Arc::clone(&received), Arc::clone(&status));
        let take = move |uri: Uri, headers: HeaderMap, body: Bytes| {
            let (kept, answer) = (Arc::clone(&kept), Arc::clone(&answer));
            async move {
                let authorization = headers.get("authorization").map(|value| value.to_str().unwrap().to_owned());
                kept.lock().unwrap().push(Received {
                    path: uri.path().to_owned(),
                    authorization,
                    body,
                });
                StatusCode::from_u16(answer.load(Ordering::SeqCst)).unwrap()
            }
        };
        let router = Router::new().route("/v1/{signal}", post(take));
        tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        Sink { addr, received, status }
    }

    pub fn answer(&self, status: u16) {
        self.status.store(status, Ordering::SeqCst);
    }

    pub fn received(&self) -> Vec<Received> {
        self.received.lock().unwrap().clone()
    }

    pub fn on(&self, path: &str) -> Vec<Received> {
        self.received().into_iter().filter(|received| received.path == path).collect()
    }

    pub fn settings(&self, token_file: &Path) -> Settings {
        settings_at(&format!("http://{}", self.addr), token_file)
    }

    pub fn traces(&self) -> Vec<ExportTraceServiceRequest> {
        self.on("/v1/traces")
            .iter()
            .map(|received| ExportTraceServiceRequest::decode(received.body.clone()).unwrap())
            .collect()
    }

    pub fn logs(&self) -> Vec<ExportLogsServiceRequest> {
        self.on("/v1/logs")
            .iter()
            .map(|received| ExportLogsServiceRequest::decode(received.body.clone()).unwrap())
            .collect()
    }

    pub fn metrics(&self) -> Vec<ExportMetricsServiceRequest> {
        self.on("/v1/metrics")
            .iter()
            .map(|received| ExportMetricsServiceRequest::decode(received.body.clone()).unwrap())
            .collect()
    }
}

/// Settings exporting every signal to `base`, with a metric interval long enough that only a flush exports.
pub fn settings_at(base: &str, token_file: &Path) -> Settings {
    Settings {
        service_name: "lineage".to_owned(),
        traces: Some(format!("{base}/v1/traces")),
        metrics: Some(format!("{base}/v1/metrics")),
        logs: Some(format!("{base}/v1/logs")),
        token_file: token_file.to_owned(),
        timeout: Duration::from_secs(2),
        metric_interval: Duration::from_secs(3600),
        resource: vec![("service.name".to_owned(), "lineage".to_owned())],
    }
}

pub fn any(value: &AnyValue) -> Value {
    match value.value.as_ref() {
        Some(any_value::Value::StringValue(text)) => json!(text),
        Some(any_value::Value::IntValue(number)) => json!(number),
        Some(any_value::Value::DoubleValue(number)) => json!(number),
        Some(any_value::Value::BoolValue(flag)) => json!(flag),
        other => panic!("an attribute of another kind: {other:?}"),
    }
}

pub fn attributes(attributes: &[KeyValue]) -> Map<String, Value> {
    let mut sorted: Vec<_> = attributes.iter().map(|kv| (kv.key.clone(), any(kv.value.as_ref().unwrap()))).collect();
    sorted.sort_by(|a, b| a.0.cmp(&b.0));
    sorted.into_iter().collect()
}

/// A metrics export as the vectors record a snapshot: each metric of `scopes` with its unit, kind, buckets and points,
/// every number a float so an integer bound and a float one compare equal.
pub fn snapshot(request: &ExportMetricsServiceRequest, scopes: &[&str], values: bool) -> Vec<Value> {
    let mut out = Vec::new();
    for resource in &request.resource_metrics {
        for scope in &resource.scope_metrics {
            let name = scope.scope.as_ref().map(|scope| scope.name.as_str()).unwrap_or_default();
            if !scopes.contains(&name) {
                continue;
            }
            for metric in &scope.metrics {
                let mut entry = json!({"scope": name, "name": metric.name, "unit": metric.unit});
                let mut points = Vec::new();
                let number = |value: &Option<number_data_point::Value>| match value {
                    Some(number_data_point::Value::AsInt(number)) => json!(number),
                    Some(number_data_point::Value::AsDouble(number)) => json!(number),
                    None => Value::Null,
                };
                match metric.data.as_ref().unwrap() {
                    Data::Sum(sum) => {
                        entry["kind"] = json!(if sum.is_monotonic { "counter" } else { "updowncounter" });
                        points.extend(
                            sum.data_points
                                .iter()
                                .map(|p| json!({"attributes": attributes(&p.attributes), "value": number(&p.value)})),
                        );
                    }
                    Data::Gauge(gauge) => {
                        entry["kind"] = json!("gauge");
                        points.extend(
                            gauge
                                .data_points
                                .iter()
                                .map(|p| json!({"attributes": attributes(&p.attributes), "value": number(&p.value)})),
                        );
                    }
                    Data::Histogram(histogram) => {
                        entry["kind"] = json!("histogram");
                        for p in &histogram.data_points {
                            entry["bounds"] = json!(p.explicit_bounds);
                            let mut point = json!({"attributes": attributes(&p.attributes), "count": p.count});
                            if values {
                                point["sum"] = json!(p.sum.unwrap());
                                point["buckets"] = json!(p.bucket_counts);
                            }
                            points.push(point);
                        }
                    }
                    other => panic!("a metric of another kind: {other:?}"),
                }
                if points.is_empty() {
                    continue;
                }
                points.sort_by_key(|point| point.to_string());
                entry["points"] = json!(points);
                out.push(floats(&entry));
            }
        }
    }
    out.sort_by_key(|entry| entry["name"].as_str().unwrap_or_default().to_owned());
    out
}

/// `value` with every number as a float.
pub fn floats(value: &Value) -> Value {
    match value {
        Value::Number(number) => json!(number.as_f64().unwrap()),
        Value::Array(items) => Value::Array(items.iter().map(floats).collect()),
        Value::Object(members) => Value::Object(members.iter().map(|(key, value)| (key.clone(), floats(value))).collect()),
        other => other.clone(),
    }
}
