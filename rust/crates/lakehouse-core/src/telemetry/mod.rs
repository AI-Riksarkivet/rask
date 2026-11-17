//! What a service tells the platform about itself: traces, metrics and logs over OTLP/HTTP to the platform's gateway,
//! each export presenting the pod's projected token (ADR 0215), the metrics the alert rules read by name, the
//! `lance.audit` trail, and the log lines on stdout (ADR 0237).
//!
//! With the chart's export off, every handle still works and nothing leaves the process but the stdout lines.

pub mod audit;
pub mod http;
pub mod identity;
pub mod instruments;
pub mod logging;
pub mod settings;

use crate::service::trace;
use identity::ProjectedTokenClient;
use logging::{LogSink, Logging, Writer};
use opentelemetry::KeyValue;
use opentelemetry::metrics::{Meter, MeterProvider as _};
use opentelemetry::trace::{TraceId, TracerProvider as _};
use opentelemetry_otlp::{LogExporter, MetricExporter, Protocol, SpanExporter, WithExportConfig, WithHttpConfig};
use opentelemetry_sdk::Resource;
use opentelemetry_sdk::logs::SdkLoggerProvider;
use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider};
use opentelemetry_sdk::resource::TelemetryResourceDetector;
use opentelemetry_sdk::trace::{IdGenerator, RandomIdGenerator, SdkTracer, SdkTracerProvider};
pub use settings::Settings;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
pub use tracing::level_filters::LevelFilter;

/// A root span started while a request is served is a span of the request's trace, so its trace id is the one its
/// log lines, audit records and problem body name.
#[derive(Debug)]
struct RequestTraceIds;

impl IdGenerator for RequestTraceIds {
    fn new_trace_id(&self) -> TraceId {
        trace::current_trace_id()
            .and_then(|id| TraceId::from_hex(&id).ok())
            .unwrap_or_else(|| RandomIdGenerator::default().new_trace_id())
    }

    fn new_span_id(&self) -> opentelemetry::trace::SpanId {
        RandomIdGenerator::default().new_span_id()
    }
}

/// The process's telemetry: held by the service for its life and shut down at its end, which sends what is batched.
pub struct Telemetry {
    tracers: SdkTracerProvider,
    meters: SdkMeterProvider,
    loggers: SdkLoggerProvider,
    logs: Arc<LogSink>,
    failures: Arc<AtomicU64>,
}

impl Telemetry {
    /// Telemetry as `settings` name it (`None`: nothing exported), logging at `level` to `out`. Called on the runtime
    /// the exports are sent from.
    pub fn new(settings: Option<&Settings>, level: LevelFilter, out: Writer) -> Result<Telemetry, String> {
        let failures = Arc::new(AtomicU64::new(0));
        let (mut tracers, mut meters, mut loggers) = (
            SdkTracerProvider::builder().with_id_generator(RequestTraceIds),
            SdkMeterProvider::builder(),
            SdkLoggerProvider::builder(),
        );
        if let Some(settings) = settings {
            let runtime = tokio::runtime::Handle::try_current().map_err(|_| "telemetry is started on the service's runtime".to_owned())?;
            let said = Arc::new(LogSink::new(level, out.clone(), None));
            let client = ProjectedTokenClient::new(settings.token_file.clone(), settings.timeout, runtime, Arc::clone(&failures), said)?;
            let built = |error: opentelemetry_otlp::ExporterBuildError| format!("an OTLP exporter could not be built: {error}");
            let mut attributes: Vec<KeyValue> = settings.resource.iter().map(|(key, value)| KeyValue::new(key.clone(), value.clone())).collect();
            attributes.push(KeyValue::new("service.instance.id", uuid::Uuid::new_v4().to_string()));
            let resource = Resource::builder_empty()
                .with_detector(Box::new(TelemetryResourceDetector))
                .with_attributes(attributes)
                .build();
            tracers = tracers.with_resource(resource.clone());
            meters = meters.with_resource(resource.clone());
            loggers = loggers.with_resource(resource);
            if let Some(url) = &settings.traces {
                let exporter = SpanExporter::builder().with_http().with_http_client(client.clone());
                let exporter = exporter.with_protocol(Protocol::HttpBinary).with_endpoint(url).with_timeout(settings.timeout);
                tracers = tracers.with_batch_exporter(exporter.build().map_err(built)?);
            }
            if let Some(url) = &settings.metrics {
                let exporter = MetricExporter::builder().with_http().with_http_client(client.clone());
                let exporter = exporter.with_protocol(Protocol::HttpBinary).with_endpoint(url).with_timeout(settings.timeout);
                meters = meters.with_reader(
                    PeriodicReader::builder(exporter.build().map_err(built)?)
                        .with_interval(settings.metric_interval)
                        .build(),
                );
            }
            if let Some(url) = &settings.logs {
                let exporter = LogExporter::builder().with_http().with_http_client(client);
                let exporter = exporter.with_protocol(Protocol::HttpBinary).with_endpoint(url).with_timeout(settings.timeout);
                loggers = loggers.with_batch_exporter(exporter.build().map_err(built)?);
            }
        }
        let loggers = loggers.build();
        let logs = Arc::new(LogSink::new(level, out, settings.and_then(|settings| settings.logs.as_ref()).map(|_| &loggers)));
        Ok(Telemetry {
            tracers: tracers.build(),
            meters: meters.build(),
            loggers,
            logs,
            failures,
        })
    }

    /// Telemetry as this process's environment names it, logging to stdout at `RASK_LOG_LEVEL`.
    pub fn from_env() -> Result<Telemetry, String> {
        let level = logging::log_level(std::env::var("RASK_LOG_LEVEL").ok().as_deref())?;
        Telemetry::new(Settings::from_env()?.as_ref(), level, logging::stdout())
    }

    /// Make this the process's log subscriber, so every `tracing` event is a log line and, where export is on, a record,
    /// and its tracers the process's, which a span started anywhere in the process (`opentelemetry::global::tracer`) is
    /// exported by.
    pub fn install(&self) -> Result<(), String> {
        tracing::subscriber::set_global_default(self.subscriber()).map_err(|_| "a log subscriber is already installed".to_owned())?;
        opentelemetry::global::set_tracer_provider(self.tracers.clone());
        Ok(())
    }

    /// The `tracing` subscriber that writes this telemetry's log lines and records.
    pub fn subscriber(&self) -> Logging {
        Logging::new(Arc::clone(&self.logs))
    }

    /// The meter of an instrumentation scope, such as `lance.lineage`.
    pub fn meter(&self, scope: &'static str) -> Meter {
        self.meters.meter(scope)
    }

    /// The tracer of an instrumentation scope, for spans a service starts itself.
    pub fn tracer(&self, scope: &'static str) -> SdkTracer {
        self.tracers.tracer(scope)
    }

    /// The governance trail, emitting when the chart's `auth.audit` turns it on.
    pub fn audit(&self, enabled: bool) -> audit::Audit {
        audit::Audit::new(enabled, Arc::clone(&self.logs), &self.meter(audit::AUDIT_SCOPE))
    }

    /// The exports the gateway did not take, since the start: a refusal, a transport failure or a token not presented.
    pub fn export_failures(&self) -> u64 {
        self.failures.load(Ordering::Relaxed)
    }

    /// Send what is batched now, off the runtime the exports are sent on. An export that fails was counted and said
    /// where it failed, so nothing more is answered here: a failed export never reaches the caller.
    pub async fn flush(&self) {
        let (tracers, meters, loggers) = (self.tracers.clone(), self.meters.clone(), self.loggers.clone());
        let _ = tokio::task::spawn_blocking(move || (tracers.force_flush(), meters.force_flush(), loggers.force_flush())).await;
    }

    /// Send what is batched and stop exporting.
    pub async fn shutdown(self) {
        let Telemetry { tracers, meters, loggers, .. } = self;
        let _ = tokio::task::spawn_blocking(move || (tracers.shutdown(), meters.shutdown(), loggers.shutdown())).await;
    }
}
