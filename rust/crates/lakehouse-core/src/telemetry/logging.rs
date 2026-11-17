//! The service's log lines: every `tracing` event to stdout in the Python services' format, and, where export is on,
//! as an OTLP log record under the scope of the module that wrote it.
//!
//! The line is `2026-11-17 10:00:00,123 INFO    lineage.relay [<trace id>] — <message> key='value'`. Its leading date is
//! what the platform's file-log pipeline drops a line by when the pod exports the same record over OTLP, so a line
//! that began otherwise would be stored twice. Lines are UTC, as a pod's clock is.

use crate::pywire::float_repr;
use crate::service::trace;
use chrono::{DateTime, Utc};
use opentelemetry::logs::{AnyValue, LogRecord as _, Logger as _, LoggerProvider as _, Severity};
use opentelemetry::trace::{SpanId, TraceFlags, TraceId};
use opentelemetry_sdk::logs::{SdkLogger, SdkLoggerProvider};
use std::fmt::Write as _;
use std::io::Write;
use std::sync::{Arc, Mutex};
use std::time::SystemTime;
use tracing::field::{Field, Visit};
use tracing::level_filters::LevelFilter;
use tracing::span::{Attributes, Id, Record};
use tracing::{Event, Level, Metadata, Subscriber};

/// Where the lines go: stdout in a service, a buffer in a test.
pub type Writer = Arc<Mutex<dyn Write + Send>>;

/// The process's stdout.
pub fn stdout() -> Writer {
    Arc::new(Mutex::new(std::io::stdout()))
}

/// What a line's events are filtered at, from `RASK_LOG_LEVEL` (INFO when unset), as Python's root logger takes it.
/// CRITICAL keeps no line, since nothing in the lakehouse logs above ERROR.
pub fn log_level(raw: Option<&str>) -> Result<LevelFilter, String> {
    Ok(match raw.unwrap_or("INFO").to_uppercase().as_str() {
        "NOTSET" => LevelFilter::TRACE,
        "DEBUG" => LevelFilter::DEBUG,
        "INFO" => LevelFilter::INFO,
        "WARN" | "WARNING" => LevelFilter::WARN,
        "ERROR" => LevelFilter::ERROR,
        "CRITICAL" | "FATAL" => LevelFilter::OFF,
        _ => return Err("RASK_LOG_LEVEL names no level (DEBUG, INFO, WARNING, ERROR, CRITICAL)".to_owned()),
    })
}

/// A value a line carries after its message.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
}

impl Value {
    /// The value as Python's `repr` writes it, which is how the Python line renders a diagnostic.
    pub fn repr(&self) -> String {
        match self {
            Value::Str(text) => python_str_repr(text),
            Value::Int(number) => number.to_string(),
            Value::Float(number) if number.is_finite() => float_repr(*number),
            Value::Float(number) => if number.is_nan() {
                "nan"
            } else if *number > 0.0 {
                "inf"
            } else {
                "-inf"
            }
            .to_owned(),
            Value::Bool(true) => "True".to_owned(),
            Value::Bool(false) => "False".to_owned(),
        }
    }

    fn any(&self) -> AnyValue {
        match self {
            Value::Str(text) => AnyValue::from(text.clone()),
            Value::Int(number) => AnyValue::Int(*number),
            Value::Float(number) => AnyValue::Double(*number),
            Value::Bool(flag) => AnyValue::Boolean(*flag),
        }
    }
}

/// `repr(text)` for a `str`: single quotes unless only double quotes avoid an escape, control characters escaped.
fn python_str_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for character in text.chars() {
        match character {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            _ if character == quote => {
                out.push('\\');
                out.push(quote);
            }
            _ if u32::from(character) < 0x20 || (0x7f..0xa0).contains(&u32::from(character)) => {
                let _ = write!(out, "\\x{:02x}", u32::from(character));
            }
            _ => out.push(character),
        }
    }
    out.push(quote);
    out
}

/// One line as the Python formatter writes it: `%(asctime)s %(levelname)-7s %(name)s [%(trace_id)s] — %(message)s`
/// and then each diagnostic as `key=repr(value)`.
pub fn format_line(at: SystemTime, level: Level, name: &str, trace_id: &str, message: &str, fields: &[(String, Value)]) -> String {
    let at = DateTime::<Utc>::from(at);
    let level = level_name(level).0;
    let mut line = format!(
        "{},{:03} {level:<7} {name} [{trace_id}] — {message}",
        at.format("%Y-%m-%d %H:%M:%S"),
        at.timestamp_subsec_millis()
    );
    for (key, value) in fields {
        let _ = write!(line, " {key}={}", value.repr());
    }
    line
}

fn level_name(level: Level) -> (&'static str, Severity) {
    match level {
        Level::ERROR => ("ERROR", Severity::Error),
        Level::WARN => ("WARNING", Severity::Warn),
        Level::INFO => ("INFO", Severity::Info),
        Level::DEBUG => ("DEBUG", Severity::Debug),
        Level::TRACE => ("TRACE", Severity::Trace),
    }
}

/// The one place a line is written and a record emitted, for the subscriber and the audit trail alike.
pub struct LogSink {
    level: LevelFilter,
    out: Writer,
    logger: Option<SdkLogger>,
}

impl std::fmt::Debug for LogSink {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("LogSink").field("level", &self.level).finish_non_exhaustive()
    }
}

impl LogSink {
    pub fn new(level: LevelFilter, out: Writer, exporting: Option<&SdkLoggerProvider>) -> LogSink {
        LogSink {
            level,
            out,
            logger: exporting.map(|provider| provider.logger("lakehouse")),
        }
    }

    /// Whether a line at `level` is written.
    pub fn enabled(&self, level: Level) -> bool {
        self.level >= level
    }

    /// Write `message` from `name` (a dotted module path, the record's scope) at `level`, with `fields` after it.
    /// `exported` false keeps it on stdout: an export's own failure is never itself exported.
    pub fn emit(&self, level: Level, name: &str, message: &str, fields: &[(String, Value)], exported: bool) {
        let trace_id = trace::current_trace_id();
        let severity = level_name(level).1;
        let line = format_line(SystemTime::now(), level, name, trace_id.as_deref().unwrap_or("-"), message, fields);
        if let Ok(mut out) = self.out.lock() {
            let _ = writeln!(out, "{line}");
        }
        let Some(logger) = self.logger.as_ref().filter(|_| exported) else {
            return;
        };
        let mut record = logger.create_log_record();
        record.set_target(name.to_owned());
        record.set_severity_number(severity);
        record.set_severity_text(severity.name());
        record.set_body(AnyValue::from(message.to_owned()));
        record.set_timestamp(SystemTime::now());
        record.add_attributes(fields.iter().map(|(key, value)| (key.clone(), value.any())));
        if let Some(id) = trace_id.and_then(|id| TraceId::from_hex(&id).ok()) {
            record.set_trace_context(id, SpanId::INVALID, Some(TraceFlags::SAMPLED));
        }
        logger.emit(record);
    }
}

/// The `tracing` subscriber of a service: events become lines and records; spans are not kept, since a request's trace
/// is carried by [`trace`] and its spans are OpenTelemetry's own.
pub struct Logging {
    sink: Arc<LogSink>,
}

impl Logging {
    pub(crate) fn new(sink: Arc<LogSink>) -> Logging {
        Logging { sink }
    }
}

#[derive(Default)]
struct Fields {
    message: String,
    fields: Vec<(String, Value)>,
}

impl Fields {
    fn put(&mut self, field: &Field, value: Value) {
        if field.name() == "message" {
            self.message = match value {
                Value::Str(text) => text,
                other => other.repr(),
            };
        } else {
            self.fields.push((field.name().to_owned(), value));
        }
    }
}

impl Visit for Fields {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        self.put(field, Value::Str(format!("{value:?}")));
    }
    fn record_str(&mut self, field: &Field, value: &str) {
        self.put(field, Value::Str(value.to_owned()));
    }
    fn record_i64(&mut self, field: &Field, value: i64) {
        self.put(field, Value::Int(value));
    }
    fn record_u64(&mut self, field: &Field, value: u64) {
        self.put(field, i64::try_from(value).map_or_else(|_| Value::Str(value.to_string()), Value::Int));
    }
    fn record_f64(&mut self, field: &Field, value: f64) {
        self.put(field, Value::Float(value));
    }
    fn record_bool(&mut self, field: &Field, value: bool) {
        self.put(field, Value::Bool(value));
    }
    fn record_error(&mut self, field: &Field, value: &(dyn std::error::Error + 'static)) {
        self.put(field, Value::Str(value.to_string()));
    }
}

impl Subscriber for Logging {
    fn enabled(&self, metadata: &Metadata<'_>) -> bool {
        self.sink.enabled(*metadata.level())
    }

    fn max_level_hint(&self) -> Option<LevelFilter> {
        Some(self.sink.level)
    }

    fn new_span(&self, _span: &Attributes<'_>) -> Id {
        Id::from_u64(1)
    }

    fn record(&self, _span: &Id, _values: &Record<'_>) {}

    fn record_follows_from(&self, _span: &Id, _follows: &Id) {}

    fn event(&self, event: &Event<'_>) {
        let metadata = event.metadata();
        let mut fields = Fields::default();
        event.record(&mut fields);
        let name = metadata.target().replace("::", ".");
        let exported = !name.starts_with("lakehouse_core.telemetry");
        self.sink.emit(*metadata.level(), &name, &fields.message, &fields.fields, exported);
    }

    fn enter(&self, _span: &Id) {}

    fn exit(&self, _span: &Id) {}
}
