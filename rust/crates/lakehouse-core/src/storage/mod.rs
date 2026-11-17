//! The object store as this process's own identity: a session its projected ServiceAccount token is exchanged for at
//! the store's STS, and an S3 client that signs with that session alone.
//!
//! No key is read from the environment. `session` exchanges the token (read from its file at every exchange) with an
//! unsigned `AssumeRoleWithWebIdentity`, holds the session for the duration it asked, on this process's own clock,
//! renews it once half is used and hands none out within a minute of its end. `s3` signs each request with SigV4
//! (`sigv4`) under whatever session is held then, and maps a store's refusal to a [`StorageError`].

pub mod records;
pub mod s3;
pub mod session;
pub mod sigv4;

pub use s3::{Listed, S3};
pub use session::{Said, StoreSession, StoreSettings, WebIdentity};

use std::fmt;

/// A temporary credential STS issued. Its secret and session token show nothing of themselves when printed.
#[derive(Clone, PartialEq, Eq)]
pub struct KeyPair {
    pub access_key_id: String,
    secret_access_key: String,
    session_token: Option<String>,
}

impl KeyPair {
    pub fn new(access_key_id: &str, secret_access_key: &str, session_token: Option<&str>) -> Self {
        KeyPair {
            access_key_id: access_key_id.to_owned(),
            secret_access_key: secret_access_key.to_owned(),
            session_token: session_token.map(str::to_owned),
        }
    }
}

impl fmt::Debug for KeyPair {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "KeyPair({}, ..)", self.access_key_id)
    }
}

/// What a call to the store came to when it did not succeed. None quotes a credential.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StorageError {
    /// The bucket does not exist: told apart from an empty one, which lists nothing as well.
    BucketNotFound { bucket: String },
    /// The bucket exists and holds no such key.
    ObjectNotFound { bucket: String, key: String },
    /// A create conditioned on the key's absence found it there: the store arbitrated, not a read before.
    Exists { key: String },
    /// The store refused the call for another reason (a policy, throttling, an error of its own).
    Refused { status: u16, code: String },
    /// The store did not answer.
    NoAnswer(String),
    /// This process holds no unexpired session, so nothing is signed.
    NoSession,
}

impl fmt::Display for StorageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StorageError::BucketNotFound { bucket } => write!(formatter, "bucket does not exist: {bucket}"),
            StorageError::ObjectNotFound { bucket, key } => write!(formatter, "object does not exist: {bucket}/{key}"),
            StorageError::Exists { key } => write!(formatter, "{key} already exists"),
            StorageError::Refused { status, code } => write!(formatter, "the store refused the call: {status} {code}"),
            StorageError::NoAnswer(reason) => write!(formatter, "the store did not answer: {reason}"),
            StorageError::NoSession => formatter.write_str("no unexpired object-store session is held, so nothing is signed with it"),
        }
    }
}

/// The failure a store's refusal is, by its S3 error code, as `lakehouse.storage.s3_errors` and
/// `lakehouse.records.create_json` read one. A HEAD carries no body, so its code is its status. A not-found with no
/// key (a listing) is about the bucket, the only thing it can be about.
pub fn classify(status: u16, code: &str, bucket: &str, key: Option<&str>) -> StorageError {
    match (code, key) {
        ("NoSuchBucket", _) | ("NoSuchKey" | "404" | "NotFound", None) => StorageError::BucketNotFound { bucket: bucket.to_owned() },
        ("NoSuchKey" | "404" | "NotFound", Some(key)) => StorageError::ObjectNotFound {
            bucket: bucket.to_owned(),
            key: key.to_owned(),
        },
        _ if status == 412 || code == "PreconditionFailed" => StorageError::Exists {
            key: key.unwrap_or_default().to_owned(),
        },
        _ => StorageError::Refused { status, code: code.to_owned() },
    }
}

/// The S3 error code an answer's XML body names (`<Error><Code>`, or STS's `<ErrorResponse><Error><Code>`).
pub(crate) fn error_code(body: &str) -> Option<String> {
    #[derive(serde::Deserialize)]
    struct Coded {
        #[serde(rename = "Code")]
        code: Option<String>,
        #[serde(rename = "Error")]
        error: Option<Box<Coded>>,
    }
    let coded: Coded = quick_xml::de::from_str(body).ok()?;
    coded.code.or_else(|| coded.error.and_then(|error| error.code))
}
