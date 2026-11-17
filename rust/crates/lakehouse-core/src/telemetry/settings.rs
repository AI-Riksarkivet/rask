//! What the chart tells a service about its telemetry, read from the environment `lance.otelEnv` renders.

use percent_encoding::percent_decode_str;
use std::path::PathBuf;
use std::time::Duration;

/// Where and as whom a service exports; `None` from [`Settings::from_lookup`] when the chart turned export off.
#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    pub service_name: String,
    /// Each signal's full OTLP/HTTP URL, or `None` where its exporter is `none`.
    pub traces: Option<String>,
    pub metrics: Option<String>,
    pub logs: Option<String>,
    /// The pod's projected ServiceAccount token for the telemetry audience, read again for every export.
    pub token_file: PathBuf,
    pub timeout: Duration,
    pub metric_interval: Duration,
    /// `OTEL_RESOURCE_ATTRIBUTES` as pairs, `service.name` last where the service names itself.
    pub resource: Vec<(String, String)>,
}

const DEFAULT_TIMEOUT_SECONDS: f64 = 10.0;
const DEFAULT_INTERVAL_MS: u64 = 60_000;
const SIGNALS: [(&str, &str); 3] = [("TRACES", "v1/traces"), ("METRICS", "v1/metrics"), ("LOGS", "v1/logs")];

impl Settings {
    /// The settings `get` names, refusing what this exporter cannot honour rather than exporting otherwise than told.
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Option<Settings>, String> {
        let set = |name: &str| get(name).filter(|value| !value.is_empty());
        let Some(base) = set("OTEL_EXPORTER_OTLP_ENDPOINT") else {
            return Ok(None);
        };
        let mut endpoints = Vec::new();
        for (signal, path) in SIGNALS {
            let protocol = set(&format!("OTEL_EXPORTER_OTLP_{signal}_PROTOCOL")).or_else(|| set("OTEL_EXPORTER_OTLP_PROTOCOL"));
            if protocol.as_deref().is_some_and(|protocol| protocol.trim() != "http/protobuf") {
                return Err("only OTLP over HTTP with protobuf bodies is exported (OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf)".to_owned());
            }
            let exporter = get(&format!("OTEL_{signal}_EXPORTER")).unwrap_or_default().trim().to_lowercase();
            endpoints.push(match exporter.as_str() {
                "none" => None,
                "" | "otlp" => Some(set(&format!("OTEL_EXPORTER_OTLP_{signal}_ENDPOINT")).unwrap_or_else(|| format!("{}/{path}", base.trim_end_matches('/')))),
                _ => return Err(format!("OTEL_{signal}_EXPORTER names an exporter other than otlp or none")),
            });
        }
        let token_file = set("RASK_OTLP_TOKEN_FILE").ok_or("RASK_OTLP_TOKEN_FILE names no token file, so an export could not say who sent it")?;
        let timeout = get("OTEL_EXPORTER_OTLP_TIMEOUT")
            .and_then(|raw| raw.trim().parse::<f64>().ok())
            .filter(|seconds| seconds.is_finite() && *seconds > 0.0)
            .unwrap_or(DEFAULT_TIMEOUT_SECONDS);
        let interval = get("OTEL_METRIC_EXPORT_INTERVAL")
            .and_then(|raw| raw.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_INTERVAL_MS);
        let mut resource: Vec<(String, String)> = Vec::new();
        for item in get("OTEL_RESOURCE_ATTRIBUTES").unwrap_or_default().split(',').filter(|item| !item.is_empty()) {
            if let Some((key, value)) = item.split_once('=') {
                let value = percent_decode_str(value.trim()).decode_utf8_lossy().into_owned();
                resource.retain(|(kept, _)| kept != key.trim());
                resource.push((key.trim().to_owned(), value));
            }
        }
        let service_name = set("OTEL_SERVICE_NAME")
            .or_else(|| resource.iter().find(|(key, _)| key == "service.name").map(|(_, value)| value.clone()))
            .unwrap_or_else(|| "unknown_service".to_owned());
        resource.retain(|(key, _)| key != "service.name");
        resource.push(("service.name".to_owned(), service_name.clone()));
        let [traces, metrics, logs]: [Option<String>; 3] = endpoints.try_into().map_err(|_| "three signals".to_owned())?;
        Ok(Some(Settings {
            service_name,
            traces,
            metrics,
            logs,
            token_file: PathBuf::from(token_file),
            timeout: Duration::from_secs_f64(timeout),
            metric_interval: Duration::from_millis(interval),
            resource,
        }))
    }

    /// The settings this process's environment names.
    pub fn from_env() -> Result<Option<Settings>, String> {
        Settings::from_lookup(|name| std::env::var(name).ok())
    }
}
