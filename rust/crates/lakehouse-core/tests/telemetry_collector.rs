//! A service's telemetry against a real OpenTelemetry Collector (contrib 0.157.0, the version the development gateway
//! runs), pinned in `scripts/toolchain.sha256` and fetched into `.localbin/otelcol-contrib`. Its configuration is the
//! development gateway's receiver, token check and audit routing (`deploy/fixtures/observability`, `collectorConfig`)
//! with file exporters in place of the store and no Kubernetes metadata: the OTLP/HTTP receiver admits a token of the
//! telemetry audience signed by the issuer's key, stamps the record with the token's subject, and keeps a `lance.audit`
//! record on its own path only when that subject is one of the lakehouse's accounts. The issuer's key is generated when
//! the test runs.
//!
//! Run with `cargo test -p lakehouse-core --test telemetry_collector -- --ignored` after fetching the binary:
//! `scripts/fetch_pinned.sh https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.157.0/otelcol-contrib_0.157.0_linux_amd64.tar.gz <tar>`
//! and `tar -xzf <tar> -C .localbin otelcol-contrib`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use jsonwebtoken::{Algorithm, EncodingKey, Header};
use lakehouse_core::telemetry::audit::{AuditAction, AuditOutcome};
use lakehouse_core::telemetry::{LevelFilter, Settings, Telemetry};
use rcgen::{KeyPair, PKCS_ECDSA_P256_SHA256};
use serde_json::json;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const ISSUER: &str = "https://kubernetes.default.svc";
const AUDIENCE: &str = "rask-otlp";
const LINEAGE: &str = "system:serviceaccount:rask:rask-sa-lineage";
const OTHER: &str = "system:serviceaccount:rask:someone-else";

/// The collector, stopped when the test ends however it ends.
struct Collector(Child, PathBuf);

impl Drop for Collector {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
        let _ = std::fs::remove_dir_all(&self.1);
    }
}

fn config(home: &Path, port: u16) -> String {
    let at = |name: &str| home.join(name).display().to_string();
    format!(
        r#"extensions:
  oidc:
    providers:
      - issuer_url: "{ISSUER}"
        audience: "{AUDIENCE}"
        public_keys_file: {keys}
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:{port}
        auth: {{ authenticator: oidc }}
processors:
  attributes/sender:
    actions:
      - {{ key: rask.sender, from_context: auth.subject, action: upsert }}
  filter/audit_only:
    error_mode: propagate
    logs:
      log_record:
        - 'instrumentation_scope.name != "lance.audit"'
        - 'attributes["rask.sender"] == nil or not IsMatch(attributes["rask.sender"], "^system:serviceaccount:rask:rask-sa-(catalog|lineage|maintenance)$")'
  filter/drop_audit:
    error_mode: ignore
    logs:
      log_record:
        - 'instrumentation_scope.name == "lance.audit"'
exporters:
  file/audit: {{ path: {audit} }}
  file/logs: {{ path: {logs} }}
  file/metrics: {{ path: {metrics} }}
  file/traces: {{ path: {traces} }}
service:
  extensions: [oidc]
  telemetry: {{ metrics: {{ level: none }} }}
  pipelines:
    traces: {{ receivers: [otlp], exporters: [file/traces] }}
    metrics: {{ receivers: [otlp], exporters: [file/metrics] }}
    logs: {{ receivers: [otlp], processors: [attributes/sender, filter/drop_audit], exporters: [file/logs] }}
    logs/audit: {{ receivers: [otlp], processors: [attributes/sender, filter/audit_only], exporters: [file/audit] }}
"#,
        keys = at("jwks.json"),
        audit = at("audit.json"),
        logs = at("logs.json"),
        metrics = at("metrics.json"),
        traces = at("traces.json"),
    )
}

fn token(key: &KeyPair, subject: &str, audience: &str) -> String {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
    let claims = json!({"iss": ISSUER, "sub": subject, "aud": [audience], "iat": now, "nbf": now, "exp": now + 600});
    jsonwebtoken::encode(
        &Header::new(Algorithm::ES256),
        &claims,
        &EncodingKey::from_ec_pem(key.serialize_pem().as_bytes()).unwrap(),
    )
    .unwrap()
}

fn read(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

async fn exported(settings: &Settings, subject_token: &str, home: &Path) -> u64 {
    std::fs::write(home.join("token"), subject_token).unwrap();
    let out: Arc<Mutex<Vec<u8>>> = Arc::new(Mutex::new(Vec::new()));
    let telemetry = Telemetry::new(Some(settings), LevelFilter::INFO, out).unwrap();
    let _logging = tracing::subscriber::set_default(telemetry.subscriber());
    tracing::info!(target: "lineage::relay", "lineage_relay_tick");
    telemetry
        .audit(true)
        .record(AuditAction::AUTHZ, AuditOutcome::Allow, Some("user:ada"), Some("table:wh$a$b"), &[]);
    lakehouse_core::telemetry::instruments::LineageMetrics::new(&telemetry.meter("lance.lineage")).record_ingest_duration(0.1);
    telemetry.flush().await;
    let failures = telemetry.export_failures();
    telemetry.shutdown().await;
    failures
}

#[tokio::test]
#[ignore = "needs the pinned collector in .localbin/otelcol-contrib; see the module comment"]
async fn the_development_gateway_admits_the_token_and_routes_the_audit_trail_by_subject() {
    let binary = PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/../../../.localbin/otelcol-contrib"));
    let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    let home = std::env::temp_dir().join(format!("rask-otelcol-{}-{port}", std::process::id()));
    std::fs::create_dir_all(&home).unwrap();
    let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap();
    let point = key.public_key_raw();
    let coordinate = |range: std::ops::Range<usize>| URL_SAFE_NO_PAD.encode(&point[range]);
    let jwk = json!({"kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig", "kid": "issuer", "x": coordinate(1..33), "y": coordinate(33..65)});
    std::fs::write(home.join("jwks.json"), json!({"keys": [jwk]}).to_string()).unwrap();
    std::fs::write(home.join("config.yaml"), config(&home, port)).unwrap();
    let _collector = Collector(
        Command::new(&binary)
            .arg(format!("--config={}", home.join("config.yaml").display()))
            .env_clear()
            .stdout(Stdio::null())
            .stderr(Stdio::from(std::fs::File::create(home.join("collector.log")).unwrap()))
            .spawn()
            .unwrap(),
        home.clone(),
    );
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(("127.0.0.1", port)).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(
        tokio::net::TcpStream::connect(("127.0.0.1", port)).await.is_ok(),
        "{}",
        read(&home.join("collector.log"))
    );
    let base = format!("http://127.0.0.1:{port}");
    let settings = Settings {
        service_name: "lineage".to_owned(),
        traces: Some(format!("{base}/v1/traces")),
        metrics: Some(format!("{base}/v1/metrics")),
        logs: Some(format!("{base}/v1/logs")),
        token_file: home.join("token"),
        timeout: Duration::from_secs(5),
        metric_interval: Duration::from_secs(3600),
        resource: vec![("service.name".to_owned(), "lineage".to_owned())],
    };
    let (audit, logs, metrics) = (home.join("audit.json"), home.join("logs.json"), home.join("metrics.json"));

    let refused = exported(&settings, &token(&key, LINEAGE, "openbao"), &home).await;
    assert!(
        refused >= 2,
        "a token of another audience is refused: logs and metrics: {refused} failures\n{}",
        read(&home.join("collector.log"))
    );
    assert!(read(&logs).is_empty() && read(&audit).is_empty() && read(&metrics).is_empty());

    let other = exported(&settings, &token(&key, OTHER, AUDIENCE), &home).await;
    assert_eq!(other, 0, "{}", read(&home.join("collector.log")));
    let admitted = exported(&settings, &token(&key, LINEAGE, AUDIENCE), &home).await;
    assert_eq!(admitted, 0, "{}", read(&home.join("collector.log")));
    tokio::time::sleep(Duration::from_secs(2)).await;

    let kept = read(&audit);
    assert_eq!(
        kept.matches("\"audit.action\"").count(),
        1,
        "only the lakehouse's own account writes the trail: {kept}"
    );
    assert!(kept.contains("lance.audit") && kept.contains(LINEAGE) && !kept.contains(OTHER), "{kept}");
    let general = read(&logs);
    assert_eq!(general.matches("lineage_relay_tick").count(), 2, "{general}");
    assert!(!general.contains("audit.action"), "the trail never reaches the general logs: {general}");
    assert!(read(&metrics).contains("lineage.ingest.duration"));
    assert!(read(&home.join("traces.json")).is_empty(), "no span was started");
}
