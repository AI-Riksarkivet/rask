//! Small JSON records under a control root, written only where the store arbitrates, as `lakehouse.records` keeps them:
//! a read that answers the record's ETag and the store's own clock, and a put that lands only if the key is absent or
//! still holds the ETag that was read.

use super::{S3, StorageError, classify, error_code};
use chrono::{DateTime, Utc};
use reqwest::Method;
use std::sync::Arc;

/// What a read found at a key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Found {
    Held {
        body: Vec<u8>,
        etag: String,
    },
    Absent,
    /// The store refused the read as forbidden: S3 answers a missing key so to an identity that may read the key and not
    /// list its bucket, so the caller decides what it means.
    Denied(StorageError),
}

/// One read: what it found and the store's clock when it answered (its `Date`), so no two pods' clocks are compared.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoreRead {
    pub found: Found,
    pub at: DateTime<Utc>,
}

/// What a conditional put asks the store to hold true.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Unless<'a> {
    /// The key exists.
    Present,
    /// The key no longer holds the record this ETag names.
    Changed(&'a str),
}

/// The records under one root (`s3://<bucket>[/<prefix>]`) of the store `s3` reaches.
pub struct Records {
    s3: Arc<S3>,
    bucket: String,
    prefix: String,
}

fn store_clock(answer: &reqwest::Response) -> DateTime<Utc> {
    let date = answer.headers().get("date").and_then(|date| date.to_str().ok());
    match date.and_then(|date| DateTime::parse_from_rfc2822(date).ok()) {
        Some(at) => at.with_timezone(&Utc),
        None => {
            tracing::warn!(
                reason = "this pod's clock stands in for the store's, which sent no Date",
                "store_answered_no_date"
            );
            DateTime::<Utc>::from(std::time::SystemTime::now())
        }
    }
}

impl Records {
    pub fn new(root: &str, s3: Arc<S3>) -> Result<Self, String> {
        let rest = root
            .strip_prefix("s3://")
            .ok_or_else(|| format!("the control root {root:?} is not on the object store (s3://)"))?
            .trim_end_matches('/');
        let (bucket, prefix) = rest.split_once('/').unwrap_or((rest, ""));
        Ok(Records {
            s3,
            bucket: bucket.to_owned(),
            prefix: prefix.to_owned(),
        })
    }

    fn object(&self, key: &str) -> String {
        if self.prefix.is_empty() {
            key.to_owned()
        } else {
            format!("{}/{key}", self.prefix)
        }
    }

    /// The record at `key` with its ETag, or none, or a refusal to say, and the store's clock at the read.
    pub async fn read_at(&self, key: &str) -> Result<StoreRead, StorageError> {
        let object = self.object(key);
        let answer = self.s3.signed(Method::GET, &self.bucket, Some(&object), &[], &[], Vec::new()).await?;
        let (status, at) = (answer.status(), store_clock(&answer));
        if status.is_success() {
            let etag = answer.headers().get("etag").and_then(|etag| etag.to_str().ok()).unwrap_or_default();
            let etag = etag.trim_matches('"').to_owned();
            let body = answer.bytes().await.map_err(|error| StorageError::NoAnswer(error.without_url().to_string()))?;
            return Ok(StoreRead {
                found: Found::Held { body: body.to_vec(), etag },
                at,
            });
        }
        let code = error_code(&answer.text().await.unwrap_or_default()).unwrap_or_else(|| status.as_str().to_owned());
        let found = match code.as_str() {
            "NoSuchKey" | "404" => Found::Absent,
            _ if status.as_u16() == 403 || code == "AccessDenied" => Found::Denied(classify(status.as_u16(), &code, &self.bucket, Some(&object))),
            _ => return Err(classify(status.as_u16(), &code, &self.bucket, Some(&object))),
        };
        Ok(StoreRead { found, at })
    }

    /// Put `body` at `key` unless the store finds `unless` true; `false` when it did, and nothing was written.
    pub async fn put(&self, key: &str, body: Vec<u8>, unless: Unless<'_>) -> Result<bool, StorageError> {
        let object = self.object(key);
        let condition = match unless {
            Unless::Present => ("if-none-match", "*"),
            Unless::Changed(etag) => ("if-match", etag),
        };
        let answer = self.s3.signed(Method::PUT, &self.bucket, Some(&object), &[], &[condition], body).await?;
        let status = answer.status().as_u16();
        if answer.status().is_success() {
            return Ok(true);
        }
        let code = error_code(&answer.text().await.unwrap_or_default()).unwrap_or_else(|| status.to_string());
        // A record deleted since the read answers 404 to a put naming its ETag: it changed as surely as one written over.
        let lost = match unless {
            Unless::Present => status == 412 || code == "PreconditionFailed",
            Unless::Changed(_) => {
                matches!(status, 404 | 409 | 412) || matches!(code.as_str(), "PreconditionFailed" | "ConditionalRequestConflict" | "NoSuchKey")
            }
        };
        if lost {
            Ok(false)
        } else {
            Err(classify(status, &code, &self.bucket, Some(&object)))
        }
    }
}
