//! A service's object-store session, as `lakehouse.store_session` keeps one: the projected ServiceAccount token
//! exchanged at the store's STS (`AssumeRoleWithWebIdentity`, unsigned, since the token is the credential) once at boot,
//! failing the boot closed, then again once half the session is used. The session is held for the duration asked,
//! counted on this process's clock from the moment it was asked for, so a pod whose clock disagrees with the store's
//! still renews in time; within a minute of its end it is handed out no more.

use super::sigv4::encode;
use super::{KeyPair, StorageError, error_code};
use crate::auth::issuer::Clock;
use crate::secrets::store::python_whitespace;
use serde::Deserialize;
use std::collections::HashMap;
use std::fmt;
use std::ops::RangeInclusive;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};
use tokio::task::JoinHandle;

/// Between two looks of the loop at whether the session is due; a due exchange that failed is asked again at this pace.
pub const WAKE: Duration = Duration::from_secs(15);
/// A session this close to its end is not handed out: a request signed with it could outlive it.
pub const EXPIRY_MARGIN: Duration = Duration::from_secs(60);
/// The boot waits out a store, an issuer and a provisioning hook still starting (the hook writes the policy the
/// token's subject names), then fails: a pod that never gets a session must not serve.
pub const BOOT_ATTEMPTS: u32 = 20;
pub const BOOT_DELAY: Duration = Duration::from_secs(3);
/// How long a session may be asked for: RustFS grants 900 to 43,200 s (`rustfs@1.0.3:rustfs/src/admin/handlers/sts.rs:64-83`).
pub const SESSION_SECONDS: RangeInclusive<u64> = 900..=43_200;
const DEFAULT_SESSION_SECONDS: u64 = 3600;
/// The role AWS assumes; a store that grants the policy the token's subject names (RustFS) ignores it.
const DEFAULT_ROLE_ARN: &str = "arn:aws:iam::000000000000:role/rask";
/// The region RustFS serves, never the ambient `AWS_REGION`.
const DEFAULT_REGION: &str = "us-east-1";
/// One small round trip on the boot's and the loop's path: an STS that takes a connection and never answers fails the
/// exchange rather than holding the loop.
const EXCHANGE_CONNECT: Duration = Duration::from_secs(5);
const EXCHANGE_TIMEOUT: Duration = Duration::from_secs(15);

/// Where the token is projected, the STS it is exchanged at, the role and region it is asked for, and for how long.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WebIdentity {
    pub token_file: PathBuf,
    pub sts_endpoint: String,
    pub region: String,
    pub role_arn: String,
    pub session_seconds: u64,
}

/// A service's identity at the store, and where the store is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoreSettings {
    pub endpoint: String,
    pub identity: WebIdentity,
    /// The only CA the store's and its STS's certificates are checked against, where they are under a private CA.
    pub ca_file: Option<PathBuf>,
}

impl StoreSettings {
    /// The settings `environment` holds under the service's `prefix` (`LINEAGE`, `LANCE`, `MAINTENANCE`), names read
    /// without regard to case, or why they cannot be taken.
    pub fn from_environment(prefix: &str, environment: impl IntoIterator<Item = (String, String)>) -> Result<Self, String> {
        let environment: HashMap<String, String> = environment.into_iter().map(|(name, value)| (name.to_uppercase(), value)).collect();
        let named = |field: &str| format!("{prefix}_S3_{field}");
        let get = |field: &str| environment.get(&named(field)).filter(|value| !value.is_empty()).cloned();
        let token = named("WEB_IDENTITY_TOKEN_FILE");
        let Some(token_file) = get("WEB_IDENTITY_TOKEN_FILE") else {
            return Err(format!(
                "{token} is required: this service signs only with the sessions its ServiceAccount token is exchanged for"
            ));
        };
        let Some(sts_endpoint) = get("STS_ENDPOINT") else {
            return Err(format!(
                "{} is required with {token} (else the token would be sent to the public AWS STS endpoint)",
                named("STS_ENDPOINT")
            ));
        };
        if get("ACCESS_KEY_ID").is_some() || get("SECRET_ACCESS_KEY").is_some() {
            return Err(format!(
                "a key pair beside {token} is a second identity: this service signs only with sessions its token is exchanged for"
            ));
        }
        let session_seconds = match environment.get(&named("SESSION_SECONDS")) {
            None => DEFAULT_SESSION_SECONDS,
            Some(text) => text
                .parse()
                .ok()
                .filter(|seconds| SESSION_SECONDS.contains(seconds))
                .ok_or_else(|| format!("{} must be a whole number of seconds from 900 to 43200", named("SESSION_SECONDS")))?,
        };
        let region = get("REGION").unwrap_or_else(|| DEFAULT_REGION.to_owned());
        let endpoint = get("ENDPOINT")
            .or_else(|| environment.get("RASK_S3_ENDPOINT_URL").cloned())
            .unwrap_or_else(|| format!("https://s3.{region}.amazonaws.com"));
        Ok(StoreSettings {
            endpoint,
            ca_file: environment.get("RASK_S3_CA_BUNDLE").map(PathBuf::from),
            identity: WebIdentity {
                token_file: PathBuf::from(token_file),
                sts_endpoint,
                role_arn: get("ASSUME_ROLE_ARN").unwrap_or_else(|| DEFAULT_ROLE_ARN.to_owned()),
                region,
                session_seconds,
            },
        })
    }
}

/// A client that sends a request only to the address it names: no proxy and no redirect, since what it sends is a
/// projected token or a signed request.
pub(crate) fn client(ca_file: Option<&Path>, connect: Duration, timeout: Duration) -> Result<reqwest::Client, String> {
    let mut client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .connect_timeout(connect)
        .timeout(timeout);
    if let Some(ca_file) = ca_file {
        let shown = ca_file.display();
        let pem = std::fs::read(ca_file).map_err(|error| format!("the CA file {shown} cannot be read: {error}"))?;
        let certificates = reqwest::Certificate::from_pem_bundle(&pem).map_err(|error| format!("the CA file {shown} holds no certificate: {error}"))?;
        client = client.tls_certs_only(certificates);
    }
    client
        .build()
        .map_err(|error| format!("the object store's HTTP client cannot be built: {error}"))
}

/// Why an exchange issued no session. None quotes the token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Unissued {
    /// The projected token could not be read, or is empty: STS was not asked.
    Token(&'static str),
    Refused {
        status: u16,
        code: String,
    },
    NoAnswer(String),
    /// STS answered success with no whole credential.
    Malformed,
}

impl fmt::Display for Unissued {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Unissued::Token(reason) => formatter.write_str(reason),
            Unissued::Refused { status, code } => write!(formatter, "STS refused the exchange: {status} {code}"),
            Unissued::NoAnswer(reason) => write!(formatter, "STS did not answer: {reason}"),
            Unissued::Malformed => formatter.write_str("STS answered with no whole credential"),
        }
    }
}

/// What the loop says of a renewal that failed: a warning while the held session lasts, then once that it expired.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Said {
    RenewalFailed,
    Expired,
}

/// What one pass of the loop did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Refreshed {
    pub renewed: bool,
    pub said: Option<Said>,
}

struct Held {
    key_pair: KeyPair,
    issued_at: Instant,
    expires_at: Instant,
}

/// One identity's session with the store.
pub struct StoreSession {
    identity: WebIdentity,
    session_name: String,
    client: reqwest::Client,
    clock: Clock,
    boot_delay: Duration,
    held: Mutex<Option<Held>>,
    said_expired: Mutex<bool>,
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct Answer {
    assume_role_with_web_identity_result: Issued,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct Issued {
    credentials: Credentials,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct Credentials {
    access_key_id: String,
    secret_access_key: String,
    session_token: String,
}

impl StoreSession {
    /// A session that holds nothing yet. `session_name` names it at the store (two characters at least).
    pub fn new(settings: &StoreSettings, session_name: &str) -> Result<Self, String> {
        Ok(StoreSession {
            identity: settings.identity.clone(),
            session_name: session_name.to_owned(),
            client: client(settings.ca_file.as_deref(), EXCHANGE_CONNECT, EXCHANGE_TIMEOUT)?,
            clock: Arc::new(Instant::now),
            boot_delay: BOOT_DELAY,
            held: Mutex::default(),
            said_expired: Mutex::default(),
        })
    }

    /// The clock a session's life is counted on.
    pub fn with_clock(self, clock: Clock) -> Self {
        StoreSession { clock, ..self }
    }

    pub fn with_boot_delay(self, boot_delay: Duration) -> Self {
        StoreSession { boot_delay, ..self }
    }

    /// Exchange the token its file holds now for a session.
    pub async fn exchange(&self) -> Result<KeyPair, Unissued> {
        let read = std::fs::read_to_string(&self.identity.token_file).map_err(|_| Unissued::Token("the projected token could not be read"))?;
        let token = read.trim_matches(python_whitespace);
        if token.is_empty() {
            return Err(Unissued::Token("the projected token is empty"));
        }
        let duration = self.identity.session_seconds.to_string();
        let form = [
            ("Action", "AssumeRoleWithWebIdentity"),
            ("Version", "2011-06-15"),
            ("RoleArn", self.identity.role_arn.as_str()),
            ("RoleSessionName", self.session_name.as_str()),
            ("WebIdentityToken", token),
            ("DurationSeconds", duration.as_str()),
        ];
        let body = form
            .iter()
            .map(|(name, value)| format!("{name}={}", encode(value)))
            .collect::<Vec<_>>()
            .join("&");
        let answer = self
            .client
            .post(&self.identity.sts_endpoint)
            .header(reqwest::header::CONTENT_TYPE, "application/x-www-form-urlencoded; charset=utf-8")
            .body(body)
            .send()
            .await
            .map_err(|error| Unissued::NoAnswer(error.without_url().to_string()))?;
        let status = answer.status();
        let text = answer.text().await.map_err(|error| Unissued::NoAnswer(error.without_url().to_string()))?;
        if !status.is_success() {
            let code = error_code(&text).unwrap_or_else(|| status.as_str().to_owned());
            return Err(Unissued::Refused { status: status.as_u16(), code });
        }
        let answer: Answer = quick_xml::de::from_str(&text).map_err(|_| Unissued::Malformed)?;
        let credentials = answer.assume_role_with_web_identity_result.credentials;
        Ok(KeyPair::new(
            &credentials.access_key_id,
            &credentials.secret_access_key,
            Some(&credentials.session_token),
        ))
    }

    async fn exchange_and_hold(&self) -> Result<(), Unissued> {
        let asked_at = (self.clock)();
        let key_pair = self.exchange().await?;
        let held = Held {
            key_pair,
            issued_at: asked_at,
            expires_at: asked_at + Duration::from_secs(self.identity.session_seconds),
        };
        *locked(&self.held) = Some(held);
        Ok(())
    }

    /// Exchange the token, waiting out a store or a policy still being provisioned; the attempts it took.
    pub async fn exchange_at_boot(&self) -> Result<u32, String> {
        for attempt in 1..=BOOT_ATTEMPTS {
            match self.exchange_and_hold().await {
                Ok(()) => {
                    tracing::info!(session_name = %self.session_name, "store_session_issued");
                    return Ok(attempt);
                }
                Err(error) if attempt == BOOT_ATTEMPTS => {
                    return Err(format!("the object store exchanged no session for this service's token: {error}"));
                }
                Err(error) => {
                    tracing::warn!(attempt, %error, "store_session_boot_retry");
                    tokio::time::sleep(self.boot_delay).await;
                }
            }
        }
        Err("the boot made no attempt".to_owned())
    }

    /// The session held while it is more than a minute from its end. Never asks STS.
    pub fn held(&self) -> Result<KeyPair, StorageError> {
        let now = (self.clock)();
        locked(&self.held)
            .as_ref()
            .filter(|held| now + EXPIRY_MARGIN < held.expires_at)
            .map(|held| held.key_pair.clone())
            .ok_or(StorageError::NoSession)
    }

    fn due(&self) -> bool {
        let now = (self.clock)();
        locked(&self.held)
            .as_ref()
            .is_none_or(|held| now >= held.issued_at + (held.expires_at - held.issued_at) / 2)
    }

    /// Exchange again when no session is held or half of the held one is used. A failure leaves the held session to
    /// its end; it is said once that it ended.
    pub async fn refresh_if_due(&self) -> Refreshed {
        if !self.due() {
            return Refreshed { renewed: false, said: None };
        }
        let Err(error) = self.exchange_and_hold().await else {
            *locked(&self.said_expired) = false;
            return Refreshed { renewed: true, said: None };
        };
        let said = if self.held().is_ok() {
            tracing::warn!(%error, "store_session_renewal_failed");
            Some(Said::RenewalFailed)
        } else if !std::mem::replace(&mut *locked(&self.said_expired), true) {
            tracing::error!(%error, "store_session_expired");
            Some(Said::Expired)
        } else {
            None
        };
        Refreshed { renewed: false, said }
    }

    /// The loop that keeps the session renewed, until the handle is aborted.
    pub fn keep_renewed(self: &Arc<Self>) -> JoinHandle<()> {
        let session = Arc::clone(self);
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(WAKE).await;
                session.refresh_if_due().await;
            }
        })
    }
}
