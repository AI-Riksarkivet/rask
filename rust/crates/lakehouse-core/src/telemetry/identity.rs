//! The identity a service presents on every OTLP export: its pod's projected ServiceAccount token, read for each one.
//!
//! The kubelet replaces the file at 80% of the token's lifetime, so a token is never kept between exports. An export
//! whose token cannot be read is not sent rather than sent anonymously. The token is never logged and no error names it.

use super::logging::{LogSink, Value};
use bytes::Bytes;
use http::header::{AUTHORIZATION, HeaderValue};
use opentelemetry_http::{HttpClient, HttpError, Request, Response};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

/// The `Authorization` value for a token file's contents, or why none can be presented.
pub fn bearer_of(contents: &str) -> Result<HeaderValue, &'static str> {
    let token = contents.trim();
    if token.is_empty() {
        return Err("the projected token is empty");
    }
    let mut value = HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| "the projected token cannot be carried in a header")?;
    value.set_sensitive(true);
    Ok(value)
}

/// The one client every exporter of the process sends with.
#[derive(Debug, Clone)]
pub struct ProjectedTokenClient {
    client: reqwest::Client,
    token_file: PathBuf,
    runtime: tokio::runtime::Handle,
    failures: Arc<AtomicU64>,
    said: Arc<LogSink>,
}

impl ProjectedTokenClient {
    /// A client that sends on `runtime`, whichever thread the SDK exports from, counting each export the gateway did
    /// not take in `failures` and saying it in `said`'s lines. It follows no redirect and uses no proxy, so the token
    /// reaches only the declared gateway.
    pub fn new(token_file: PathBuf, timeout: Duration, runtime: tokio::runtime::Handle, failures: Arc<AtomicU64>, said: Arc<LogSink>) -> Result<Self, String> {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .build()
            .map_err(|error| format!("the telemetry client could not be built: {error}"))?;
        Ok(ProjectedTokenClient {
            client,
            token_file,
            runtime,
            failures,
            said,
        })
    }

    async fn send(&self, request: Request<Bytes>) -> Result<Response<Bytes>, HttpError> {
        // A file of a few hundred bytes on the kubelet's tmpfs: read in place, as the secret store's login reads it.
        let contents = std::fs::read_to_string(&self.token_file).map_err(|_| "the projected token could not be read")?;
        let (mut parts, body) = request.into_parts();
        parts.headers.insert(AUTHORIZATION, bearer_of(&contents)?);
        let request = reqwest::Request::try_from(Request::from_parts(parts, body))?;
        let answer = self.client.execute(request).await?;
        let mut response = Response::builder().status(answer.status());
        if let Some(headers) = response.headers_mut() {
            headers.extend(answer.headers().iter().map(|(name, value)| (name.clone(), value.clone())));
        }
        Ok(response.body(answer.bytes().await?)?)
    }
}

#[async_trait::async_trait]
impl HttpClient for ProjectedTokenClient {
    async fn send_bytes(&self, request: Request<Bytes>) -> Result<Response<Bytes>, HttpError> {
        let this = self.clone();
        let answer = match self.runtime.spawn(async move { this.send(request).await }).await {
            Ok(answer) => answer,
            Err(_) => Err("the export was cancelled".into()),
        };
        if !answer.as_ref().is_ok_and(|response| response.status().is_success()) {
            let failed = self.failures.fetch_add(1, Ordering::Relaxed) + 1;
            let why = match &answer {
                Ok(response) => format!("the gateway answered {}", response.status().as_u16()),
                Err(error) => error.to_string(),
            };
            // Said on stdout only: an export's failure exported would be one more export to fail.
            if self.said.enabled(tracing::Level::WARN) {
                let fields = [
                    ("failed".to_owned(), Value::Int(i64::try_from(failed).unwrap_or(i64::MAX))),
                    ("error".to_owned(), Value::Str(why)),
                ];
                self.said
                    .emit(tracing::Level::WARN, "lakehouse_core.telemetry", "telemetry_export_failed", &fields, false);
            }
        }
        answer
    }
}
