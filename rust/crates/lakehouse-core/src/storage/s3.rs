//! An S3 client for any S3-compatible store, signed with the session its [`StoreSession`] holds when each request is
//! made, path-style (RustFS refuses virtual-hosted signing), and carrying only the calls the outbox and a timer's claim
//! make: put (plain, or conditioned on the key's absence), get, delete, and a listing one level deep.

use super::session::{StoreSession, StoreSettings, client};
use super::sigv4::{self, Request, encode, encode_key};
use super::{StorageError, classify, error_code};
use chrono::{DateTime, Utc};
use reqwest::header::HeaderValue;
use reqwest::{Method, StatusCode};
use serde::Deserialize;
use std::sync::Arc;
use std::time::Duration;

/// A connect and read bound, so a store that takes a connection and never answers fails the call.
const CONNECT: Duration = Duration::from_secs(10);
const TIMEOUT: Duration = Duration::from_secs(60);

/// One object a listing names: its key and when it was last written, in nanoseconds since the epoch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Listed {
    pub key: String,
    pub modified_ns: i64,
}

pub struct S3 {
    /// The endpoint's scheme and authority, as the URL names them.
    origin: String,
    /// The `host` the signature covers: the authority, its port only where it is not the scheme's own.
    host: String,
    /// A path the endpoint names before the bucket, without its trailing slash.
    base: String,
    region: String,
    client: reqwest::Client,
    session: Arc<StoreSession>,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct Page {
    #[serde(default)]
    contents: Vec<Entry>,
    #[serde(default)]
    is_truncated: bool,
    next_continuation_token: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct Entry {
    key: String,
    last_modified: String,
}

impl S3 {
    pub fn new(settings: &StoreSettings, session: Arc<StoreSession>) -> Result<Self, String> {
        let endpoint = reqwest::Url::parse(&settings.endpoint).map_err(|error| format!("the store endpoint is no URL: {error}"))?;
        let host = endpoint.host_str().ok_or("the store endpoint names no host")?;
        let host = endpoint.port().map_or_else(|| host.to_owned(), |port| format!("{host}:{port}"));
        Ok(S3 {
            origin: format!("{}://{host}", endpoint.scheme()),
            host,
            base: endpoint.path().trim_end_matches('/').to_owned(),
            region: settings.identity.region.clone(),
            client: client(settings.ca_file.as_deref(), CONNECT, TIMEOUT)?,
            session,
        })
    }

    async fn send(
        &self,
        method: Method,
        bucket: &str,
        key: Option<&str>,
        query: &[(&str, &str)],
        headers: &[(&str, &str)],
        body: Vec<u8>,
    ) -> Result<reqwest::Response, StorageError> {
        let key_pair = self.session.held()?;
        let path = match key {
            Some(key) => format!("{}/{}/{}", self.base, encode_key(bucket), encode_key(key)),
            None => format!("{}/{}", self.base, encode_key(bucket)),
        };
        let mut url = format!("{}{path}", self.origin);
        if !query.is_empty() {
            url.push('?');
            url.push_str(
                &query
                    .iter()
                    .map(|(name, value)| format!("{}={}", encode(name), encode(value)))
                    .collect::<Vec<_>>()
                    .join("&"),
            );
        }
        let request = Request {
            method: method.as_str(),
            host: &self.host,
            path: &path,
            query,
            headers,
            body: &body,
        };
        let signed = sigv4::sign(&request, &key_pair, &self.region, DateTime::<Utc>::from(std::time::SystemTime::now()));
        let mut request = self.client.request(method, url);
        for (name, value) in headers.iter().copied().chain(signed.iter().map(|(name, value)| (*name, value.as_str()))) {
            let mut value = HeaderValue::from_str(value).map_err(|_| StorageError::NoAnswer(format!("{name} cannot be sent as a header")))?;
            value.set_sensitive(matches!(name, "authorization" | "x-amz-security-token"));
            request = request.header(name, value);
        }
        let answer = request
            .body(body)
            .send()
            .await
            .map_err(|error| StorageError::NoAnswer(error.without_url().to_string()))?;
        let status = answer.status();
        if status.is_success() {
            return Ok(answer);
        }
        let text = answer.text().await.unwrap_or_default();
        let code = error_code(&text).unwrap_or_else(|| status.as_str().to_owned());
        Err(classify(status.as_u16(), &code, bucket, key))
    }

    /// Put `body` at `key`; with `only_if_absent` the store refuses it ([`StorageError::Exists`]) where the key exists.
    pub async fn put(&self, bucket: &str, key: &str, body: Vec<u8>, only_if_absent: bool) -> Result<(), StorageError> {
        let headers: &[(&str, &str)] = if only_if_absent { &[("if-none-match", "*")] } else { &[] };
        self.send(Method::PUT, bucket, Some(key), &[], headers, body).await.map(drop)
    }

    /// The object at `key`, or `None` where the bucket holds none.
    pub async fn get(&self, bucket: &str, key: &str) -> Result<Option<Vec<u8>>, StorageError> {
        match self.send(Method::GET, bucket, Some(key), &[], &[], Vec::new()).await {
            Ok(answer) => Ok(Some(
                answer
                    .bytes()
                    .await
                    .map_err(|error| StorageError::NoAnswer(error.without_url().to_string()))?
                    .to_vec(),
            )),
            Err(StorageError::ObjectNotFound { .. }) => Ok(None),
            Err(error) => Err(error),
        }
    }

    /// Delete `key`. A key already absent is what was asked for.
    pub async fn delete(&self, bucket: &str, key: &str) -> Result<(), StorageError> {
        match self.send(Method::DELETE, bucket, Some(key), &[], &[], Vec::new()).await {
            Ok(_) | Err(StorageError::ObjectNotFound { .. }) => Ok(()),
            Err(error) => Err(error),
        }
    }

    /// Every object directly under `prefix` (a deeper key is a common prefix, which is not listed), in the store's
    /// order, every page of it.
    pub async fn list(&self, bucket: &str, prefix: &str) -> Result<Vec<Listed>, StorageError> {
        let mut listed = Vec::new();
        let mut token: Option<String> = None;
        loop {
            let mut query = vec![("list-type", "2"), ("prefix", prefix), ("delimiter", "/")];
            if let Some(token) = &token {
                query.push(("continuation-token", token));
            }
            let answer = self.send(Method::GET, bucket, None, &query, &[], Vec::new()).await?;
            let text = answer.text().await.map_err(|error| StorageError::NoAnswer(error.without_url().to_string()))?;
            let malformed = || StorageError::Refused {
                status: StatusCode::OK.as_u16(),
                code: "MalformedListing".to_owned(),
            };
            let page: Page = quick_xml::de::from_str(&text).map_err(|_| malformed())?;
            for entry in page.contents {
                let modified = DateTime::parse_from_rfc3339(&entry.last_modified).map_err(|_| malformed())?;
                listed.push(Listed {
                    key: entry.key,
                    modified_ns: modified.timestamp_nanos_opt().ok_or_else(malformed)?,
                });
            }
            match page.next_continuation_token.filter(|_| page.is_truncated) {
                Some(next) => token = Some(next),
                None => return Ok(listed),
            }
        }
    }
}
