//! Each request a service answers, as a SERVER span of the request's trace and in the HTTP server metrics of
//! OpenTelemetry's stable conventions (`http.server.request.duration` and its kin), which the platform's HTTP alerts
//! and dashboards read by name, `http.route` and `http.response.status_code`. The probes are not measured.

use super::Telemetry;
use crate::service::trace;
use axum::body::HttpBody as _;
use axum::extract::{MatchedPath, Request, State};
use axum::http::Version;
use axum::middleware::Next;
use axum::response::Response;
use opentelemetry::metrics::{Histogram, UpDownCounter};
use opentelemetry::trace::{FutureExt as _, SpanKind, Status, TraceContextExt as _, Tracer as _};
use opentelemetry::{Context, KeyValue};
use opentelemetry_sdk::trace::SdkTracer;
use std::sync::{Arc, OnceLock};
use std::time::Instant;

/// The scope the server's spans and metrics are recorded under.
pub const HTTP_SCOPE: &str = "lance.http.server";
/// The paths the chart excludes from the server's telemetry: the probes, which run every few seconds.
const EXCLUDED: [&str; 5] = ["/livez", "/readyz", "/healthz", "/api/health", "/metrics"];
const METHODS: [&str; 9] = ["CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"];
const SECONDS: [f64; 14] = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0];

/// The server's instruments, held by the router's layers.
#[derive(Clone)]
pub struct HttpTelemetry {
    tracer: SdkTracer,
    duration: Histogram<f64>,
    active: UpDownCounter<i64>,
    request_size: Histogram<u64>,
    response_size: Histogram<u64>,
}

/// The route a request matched, written by the layer that sees it and read by the one that records.
#[derive(Clone, Default)]
struct Route(Arc<OnceLock<String>>);

impl HttpTelemetry {
    pub fn new(telemetry: &Telemetry) -> HttpTelemetry {
        let meter = telemetry.meter(HTTP_SCOPE);
        let size = |name, description| meter.u64_histogram(name).with_unit("By").with_description(description).build();
        HttpTelemetry {
            tracer: telemetry.tracer(HTTP_SCOPE),
            duration: meter
                .f64_histogram("http.server.request.duration")
                .with_unit("s")
                .with_description("Duration of HTTP server requests.")
                .with_boundaries(SECONDS.to_vec())
                .build(),
            active: meter
                .i64_up_down_counter("http.server.active_requests")
                .with_unit("{request}")
                .with_description("Number of active HTTP server requests.")
                .build(),
            request_size: size("http.server.request.body.size", "Size of HTTP server request bodies."),
            response_size: size("http.server.response.body.size", "Size of HTTP server response bodies."),
        }
    }

    /// Note the route a request matched; layered on the doors with `route_layer`, so only a matched request has one.
    pub async fn matched(request: Request, next: Next) -> Response {
        if let (Some(path), Some(route)) = (request.extensions().get::<MatchedPath>(), request.extensions().get::<Route>()) {
            let _ = route.0.set(path.as_str().to_owned());
        }
        next.run(request).await
    }

    /// Serve `request` in a SERVER span and record it; layered inside the request's trace.
    pub async fn served(State(this): State<HttpTelemetry>, mut request: Request, next: Next) -> Response {
        let path = request.uri().path().to_owned();
        if EXCLUDED.contains(&path.as_str()) {
            return next.run(request).await;
        }
        let started = Instant::now();
        let method = request.method().as_str();
        let method = if METHODS.contains(&method) { method.to_owned() } else { "_OTHER".to_owned() };
        let version = match request.version() {
            Version::HTTP_10 => "1.0",
            Version::HTTP_2 => "2",
            Version::HTTP_3 => "3",
            _ => "1.1",
        };
        let [host, agent, length, traceparent] = ["host", "user-agent", "content-length", "traceparent"]
            .map(|name| request.headers().get(name).and_then(|value| value.to_str().ok()).map(str::to_owned));
        let parent = traceparent.as_deref().and_then(trace::remote_parent);
        let mut attributes = vec![
            KeyValue::new("url.scheme", "http"),
            KeyValue::new("network.protocol.version", version),
            KeyValue::new("url.path", path),
            KeyValue::new("http.request.method", method.clone()),
        ];
        if let Some(query) = request.uri().query() {
            attributes.push(KeyValue::new("url.query", query.to_owned()));
        }
        if let Some(host) = host {
            let (address, port) = host
                .rsplit_once(':')
                .and_then(|(address, port)| Some((address.to_owned(), port.parse::<i64>().ok()?)))
                .unwrap_or((host, 80));
            attributes.extend([KeyValue::new("server.address", address), KeyValue::new("server.port", port)]);
        }
        attributes.extend(agent.map(|agent| KeyValue::new("user_agent.original", agent)));
        let active = [KeyValue::new("http.request.method", method.clone()), KeyValue::new("url.scheme", "http")];
        this.active.add(1, &active);
        let route = Route::default();
        request.extensions_mut().insert(route.clone());
        let span = this
            .tracer
            .span_builder(method.clone())
            .with_kind(SpanKind::Server)
            .start_with_context(&this.tracer, &parent.unwrap_or_default());
        let context = Context::current_with_span(span);
        let response = next.run(request).with_context(context.clone()).await;
        this.active.add(-1, &active);

        let status = response.status().as_u16();
        let mut measured = vec![
            KeyValue::new("http.request.method", method.clone()),
            KeyValue::new("url.scheme", "http"),
            KeyValue::new("network.protocol.version", version),
            KeyValue::new("http.response.status_code", i64::from(status)),
        ];
        measured.extend(route.0.get().map(|route| KeyValue::new("http.route", route.clone())));
        measured.extend((status >= 500).then(|| KeyValue::new("error.type", status.to_string())));
        this.duration.record(started.elapsed().as_secs_f64(), &measured);
        if let Some(length) = length.and_then(|length| length.parse::<u64>().ok()) {
            this.request_size.record(length, &measured);
        }
        if let Some(length) = response.body().size_hint().exact() {
            this.response_size.record(length, &measured);
        }
        let span = context.span();
        span.update_name(route.0.get().map_or(method.clone(), |route| format!("{method} {route}")));
        let answered = ["http.route", "http.response.status_code", "error.type"];
        span.set_attributes(
            attributes
                .into_iter()
                .chain(measured.into_iter().filter(|kv| answered.contains(&kv.key.as_str()))),
        );
        if status >= 500 {
            span.set_status(Status::error(""));
        }
        span.end();
        response
    }
}
