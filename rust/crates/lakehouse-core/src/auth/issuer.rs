//! An issuer's signing keys, fetched over HTTP: its OIDC discovery document, then its key set, held and refreshed.
//!
//! What is decided (which algorithms, where the keys are fetched from, whether a fetch is admitted now, which keys a
//! set holds) is a function of values. The fetches are the thin shell around those functions, at the end of the file.

use crate::auth::settings::AuthSettings;
use crate::auth::verifier::{Authenticator, KeySource, SigningKey, Unavailable};
use jsonwebtoken::jwk::{AlgorithmParameters, EllipticCurve, Jwk, PublicKeyUse};
use jsonwebtoken::{Algorithm, DecodingKey};
use reqwest::header::{AUTHORIZATION, HeaderValue};
use serde::Deserialize;
use serde_json::Value;
use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

const DISCOVERY_SUFFIX: &str = "/.well-known/openid-configuration";

/// The longest one fetch of a discovery document or a key set may hold the request or refresh that made it.
pub const FETCH_TIMEOUT: Duration = Duration::from_secs(15);

/// The least time between two fetches a request may cause for one issuer, counted from when the earlier one began.
///
/// A key id is the caller's own unverified text, so without a pace every anonymous request naming a novel one would buy
/// a live fetch from the issuer. Paced, a flood costs one fetch per interval, and an honest rotation's first token is
/// found by the first re-read after the interval.
pub const UNKNOWN_KID_REFRESH_INTERVAL: Duration = Duration::from_secs(30);

/// The asymmetric algorithms a token may be signed with, in order of preference. Symmetric algorithms and `none` are
/// absent, because a public key taken as an HMAC secret forges any token. ES512 is absent because the JWT library
/// verifies P-256 and P-384 only.
pub const ALLOWED_ALGORITHMS: [Algorithm; 5] = [Algorithm::RS256, Algorithm::RS384, Algorithm::RS512, Algorithm::ES256, Algorithm::ES384];

/// The allowlist's algorithms that the issuer advertises, in the allowlist's order. An issuer that advertises none is
/// taken to sign with any of them, since discovery makes the field optional; the token's own algorithm is still held
/// to the key's.
pub fn safe_algorithms(advertised: &[String]) -> Vec<Algorithm> {
    let advertised: Vec<String> = advertised.iter().map(|name| name.to_uppercase()).collect();
    ALLOWED_ALGORITHMS
        .into_iter()
        .filter(|algorithm| advertised.is_empty() || advertised.contains(&format!("{algorithm:?}")))
        .collect()
}

/// `url`, where it may be fetched: HTTPS, or anything where the deployment allowed a development issuer.
pub fn require_https(url: &str, what: &str, allow_insecure: bool) -> Result<(), String> {
    if allow_insecure || url.get(..8).is_some_and(|scheme| scheme.eq_ignore_ascii_case("https://")) {
        return Ok(());
    }
    Err(format!("OIDC {what} must use HTTPS (set RASK_OIDC_ALLOW_INSECURE=true for dev IdPs)"))
}

/// Where a key set is fetched from: a `jwks_uri` under the issuer moves under the discovery override with it, so the
/// fetch reaches the same place discovery did; one elsewhere is the issuer hosting its keys there, and is kept.
pub fn jwks_location(jwks_uri: &str, issuer: &str, discovery_base: Option<&str>) -> String {
    match (discovery_base, jwks_uri.strip_prefix(issuer)) {
        (Some(base), Some(rest)) if rest.is_empty() || rest.starts_with('/') => format!("{base}{rest}"),
        _ => jwks_uri.to_owned(),
    }
}

/// What an issuer's discovery document says, once it agrees with the configuration.
#[derive(Debug, Clone, PartialEq)]
pub struct Discovered {
    /// The issuer as the document names it, which a token must name exactly.
    pub issuer: String,
    pub jwks_uri: String,
    pub algorithms: Vec<Algorithm>,
}

#[derive(Deserialize)]
struct DiscoveryDocument {
    issuer: String,
    jwks_uri: String,
    #[serde(default)]
    id_token_signing_alg_values_supported: Vec<String>,
}

/// The discovery `document` fetched for `issuer`, or why it cannot be used. Every reason is the issuer's or this
/// deployment's, never a token's.
pub fn discovered(document: &[u8], issuer: &str, discovery_base: Option<&str>, allow_insecure: bool) -> Result<Discovered, String> {
    let document: DiscoveryDocument = serde_json::from_slice(document).map_err(|_| "is not a discovery document".to_owned())?;
    if !matches!(
        reqwest::Url::parse(&document.jwks_uri).map(|url| url.scheme().to_owned()).as_deref(),
        Ok("http" | "https")
    ) {
        return Err("is not a discovery document: its jwks_uri is no http or https URL".to_owned());
    }
    // The document's own issuer is what tokens are checked against, so one naming another issuer is not trusted.
    if document.issuer.trim_end_matches('/') != issuer {
        return Err("advertises a different issuer than this deployment is configured for".to_owned());
    }
    let jwks_uri = jwks_location(&document.jwks_uri, issuer, discovery_base);
    require_https(&jwks_uri, "jwks_uri", allow_insecure).map_err(|reason| format!("names a key set it cannot be fetched from: {reason}"))?;
    let algorithms = safe_algorithms(&document.id_token_signing_alg_values_supported);
    if algorithms.is_empty() {
        return Err("advertises no mutually-supported OIDC signing algorithm".to_owned());
    }
    Ok(Discovered {
        issuer: document.issuer,
        jwks_uri,
        algorithms,
    })
}

/// The one algorithm a key verifies with: the one it names, or the one its type implies.
fn key_algorithm(jwk: &Jwk) -> Option<Algorithm> {
    let implied = match &jwk.algorithm {
        AlgorithmParameters::RSA(_) => Algorithm::RS256,
        AlgorithmParameters::EllipticCurve(params) if params.curve == EllipticCurve::P256 => Algorithm::ES256,
        AlgorithmParameters::EllipticCurve(params) if params.curve == EllipticCurve::P384 => Algorithm::ES384,
        AlgorithmParameters::OctetKey(_) => Algorithm::HS256,
        AlgorithmParameters::OctetKeyPair(params) if params.curve == EllipticCurve::Ed25519 => Algorithm::EdDSA,
        _ => return None,
    };
    jwk.common.key_algorithm.map_or(Some(implied), |named| Algorithm::try_from(named).ok())
}

#[derive(Deserialize)]
struct KeySetDocument {
    keys: Vec<Value>,
}

/// The signing keys of key set `body`, by key id: each marked for signing or for nothing, naming a key id, and one the
/// JWT library can read; the first of a key id wins. A key verifies only with its own algorithm, and only where that is
/// one `discovered` allows, so a key of any other algorithm is held and verifies nothing. A set holding no signing key
/// cannot be used.
pub fn signing_keys(body: &[u8], discovered: &Discovered) -> Result<HashMap<String, SigningKey>, String> {
    let set: KeySetDocument = serde_json::from_slice(body).map_err(|_| "is not a key set".to_owned())?;
    let mut keys = HashMap::new();
    for jwk in set.keys.into_iter().filter_map(|key| serde_json::from_value::<Jwk>(key).ok()) {
        let signs = matches!(jwk.common.public_key_use, None | Some(PublicKeyUse::Signature));
        let (Some(kid), true, Some(algorithm)) = (jwk.common.key_id.clone(), signs, key_algorithm(&jwk)) else {
            continue;
        };
        let Ok(key) = DecodingKey::from_jwk(&jwk) else { continue };
        keys.entry(kid).or_insert_with(|| SigningKey {
            key,
            algorithms: discovered.algorithms.iter().copied().filter(|allowed| *allowed == algorithm).collect(),
            issuer: discovered.issuer.clone(),
        });
    }
    if keys.is_empty() {
        return Err("holds no usable signing key".to_owned());
    }
    Ok(keys)
}

/// Admits one fetch per `UNKNOWN_KID_REFRESH_INTERVAL`. The moment is claimed before the fetch runs, so a burst
/// arriving while one is in flight is declined at once, and a fetch that failed counts like one that landed.
#[derive(Debug, Default)]
pub struct Pace {
    began: Option<Instant>,
}

impl Pace {
    pub fn admit(&mut self, now: Instant) -> bool {
        if self
            .began
            .is_some_and(|began| now.saturating_duration_since(began) < UNKNOWN_KID_REFRESH_INTERVAL)
        {
            return false;
        }
        self.began = Some(now);
        true
    }
}

/// How an issuer's documents are fetched: from where, with which trust, and with which bearer.
struct Fetch {
    issuer: String,
    discovery_base: Option<String>,
    allow_insecure: bool,
    /// A projected token, read on every fetch because the kubelet rotates it under the running process.
    token_file: Option<PathBuf>,
    client: reqwest::Client,
}

impl Fetch {
    fn new(issuer: &str, discovery_base: Option<&str>, allow_insecure: bool, token_file: Option<&str>, ca_file: Option<&str>) -> Result<Self, String> {
        // No redirect is followed, so the bearer reaches no host but the one the URL names.
        let mut client = reqwest::Client::builder().timeout(FETCH_TIMEOUT).redirect(reqwest::redirect::Policy::none());
        if let Some(ca_file) = ca_file {
            let pem = std::fs::read(ca_file).map_err(|error| format!("the CA file {ca_file} cannot be read: {error}"))?;
            let certificates = reqwest::Certificate::from_pem_bundle(&pem).map_err(|error| format!("the CA file {ca_file} holds no certificate: {error}"))?;
            client = client.tls_certs_only(certificates);
        }
        Ok(Fetch {
            issuer: issuer.trim_end_matches('/').to_owned(),
            discovery_base: discovery_base.map(|base| base.trim_end_matches('/').to_owned()),
            allow_insecure,
            token_file: token_file.map(PathBuf::from),
            client: client
                .build()
                .map_err(|error| format!("the HTTP client for {issuer} cannot be built: {error}"))?,
        })
    }

    fn bearer(&self, path: &PathBuf) -> Result<HeaderValue, String> {
        let unreadable = |reason: &str| format!("the fetch credential at {} {reason}", path.display());
        let token = std::fs::read_to_string(path).map_err(|_| unreadable("cannot be read"))?;
        let token = token.trim();
        if token.is_empty() {
            return Err(unreadable("is empty"));
        }
        let mut header = HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| unreadable("is not a header value"))?;
        header.set_sensitive(true);
        Ok(header)
    }

    async fn get(&self, url: &str) -> Result<Vec<u8>, String> {
        let mut request = self.client.get(url);
        if let Some(path) = &self.token_file {
            request = request.header(AUTHORIZATION, self.bearer(path)?);
        }
        let response = request.send().await.map_err(|error| error.without_url().to_string())?;
        if !response.status().is_success() {
            return Err(format!("it answered {}", response.status()));
        }
        Ok(response.bytes().await.map_err(|error| error.without_url().to_string())?.to_vec())
    }

    async fn key_set(&self, discovered: &Discovered) -> Result<HashMap<String, SigningKey>, String> {
        let uri = &discovered.jwks_uri;
        let body = self
            .get(uri)
            .await
            .map_err(|reason| format!("The OIDC key set could not be reached at {uri}: {reason}"))?;
        signing_keys(&body, discovered).map_err(|reason| format!("The OIDC key set at {uri} {reason}"))
    }

    /// The issuer's discovery document and the key set it names, both fetched now.
    async fn provider(&self) -> Result<Held, String> {
        let base = self.discovery_base.as_deref().unwrap_or(&self.issuer);
        let url = format!("{base}{DISCOVERY_SUFFIX}");
        let what = if self.discovery_base.is_some() { "discovery override" } else { "issuer" };
        require_https(&url, what, self.allow_insecure)?;
        let document = self
            .get(&url)
            .await
            .map_err(|reason| format!("The OIDC issuer could not be reached at {url}: {reason}"))?;
        let discovered = discovered(&document, &self.issuer, self.discovery_base.as_deref(), self.allow_insecure)
            .map_err(|reason| format!("The OIDC discovery document at {url} {reason}"))?;
        let keys = self.key_set(&discovered).await?;
        Ok(Held { discovered, keys })
    }
}

/// An issuer's discovery document and key set, held only once both arrived whole.
struct Held {
    discovered: Discovered,
    keys: HashMap<String, SigningKey>,
}

#[derive(Default)]
struct State {
    held: Option<Arc<Held>>,
    /// Two paces, so a request fetching an issuer nothing holds yet and one re-reading a held set never delay each other.
    discovery: Pace,
    unknown_kid: Pace,
}

struct Remote {
    fetch: Fetch,
    state: Mutex<State>,
}

impl Remote {
    fn state(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn hold(&self, held: Held) {
        self.state().held = Some(Arc::new(held));
    }
}

enum Step {
    Discover,
    Reread(Arc<Held>),
}

/// The instant a pace is counted at; injectable, so a test can move it.
pub type Clock = Arc<dyn Fn() -> Instant + Send + Sync>;

/// The signing keys of a deployment's issuers, fetched over HTTP. A request reads the keys held; the background
/// refresh replaces them only with a set that arrived whole, so an issuer's outage serves the last good keys.
pub struct IssuerKeys {
    issuers: BTreeMap<String, Remote>,
    every: Duration,
    clock: Clock,
}

impl IssuerKeys {
    /// The key source for the issuers `settings` names: the people's, fetched where and as the deployment says, and
    /// the service-account issuer's, always over HTTPS with its own bearer and CA.
    pub fn from_settings(settings: &AuthSettings) -> Result<Self, String> {
        let mut fetches = Vec::new();
        if settings.enabled && settings.audience.is_some() {
            for provider in &settings.providers {
                fetches.push(Fetch::new(
                    &provider.issuer,
                    provider.discovery_url.as_deref(),
                    settings.allow_insecure,
                    None,
                    None,
                )?);
            }
        }
        if let Some(accounts) = &settings.service_accounts {
            fetches.push(Fetch::new(
                &accounts.issuer,
                None,
                false,
                accounts.fetch_token_file.as_deref(),
                accounts.ca_file.as_deref(),
            )?);
        }
        let issuers = fetches
            .into_iter()
            .map(|fetch| {
                let remote = Remote {
                    fetch,
                    state: Mutex::default(),
                };
                (remote.fetch.issuer.clone(), remote)
            })
            .collect();
        Ok(IssuerKeys {
            issuers,
            every: Duration::from_secs(settings.cache_ttl),
            clock: Arc::new(Instant::now),
        })
    }

    pub fn with_clock(self, clock: Clock) -> Self {
        IssuerKeys { clock, ..self }
    }

    /// Fetch every issuer's discovery document and key set now, hold each that arrived whole, and report each that did
    /// not as `(issuer, reason)`. It reports and never decides: an unreachable issuer is the door's 503, not the pod's.
    pub async fn refresh(&self) -> Vec<(String, String)> {
        let mut failures = Vec::new();
        for (issuer, remote) in &self.issuers {
            match remote.fetch.provider().await {
                Ok(held) => remote.hold(held),
                Err(reason) => failures.push((issuer.clone(), reason)),
            }
        }
        failures
    }

    /// `refresh` every `cache_ttl` seconds, until the task is dropped.
    pub async fn refresh_forever(self: Arc<Self>) {
        loop {
            tokio::time::sleep(self.every).await;
            for (issuer, reason) in self.refresh().await {
                tracing::warn!(issuer, reason, "oidc_refresh_failed_serving_held_keys");
            }
        }
    }

    async fn lookup(&self, issuer: &str, kid: &str) -> Result<Option<SigningKey>, Unavailable> {
        let remote = self
            .issuers
            .get(issuer)
            .ok_or_else(|| Unavailable(format!("no key source is configured for {issuer}")))?;
        let now = (self.clock)();
        let step = {
            let mut state = remote.state();
            match state.held.clone() {
                Some(held) => match held.keys.get(kid) {
                    Some(key) => return Ok(Some(key.clone())),
                    // An unknown key id may be a key the issuer rotated in since the set was read.
                    None if state.unknown_kid.admit(now) => Step::Reread(Arc::clone(&held)),
                    None => return Ok(None),
                },
                None if state.discovery.admit(now) => Step::Discover,
                None => return Err(Unavailable(format!("The OIDC issuer {issuer} has not answered yet"))),
            }
        };
        let held = match step {
            Step::Discover => remote.fetch.provider().await.map_err(Unavailable)?,
            Step::Reread(held) => Held {
                keys: remote.fetch.key_set(&held.discovered).await.map_err(Unavailable)?,
                discovered: held.discovered.clone(),
            },
        };
        let found = held.keys.get(kid).cloned();
        remote.hold(held);
        Ok(found)
    }
}

impl KeySource for IssuerKeys {
    fn signing_key<'a>(&'a self, issuer: &'a str, kid: &'a str) -> Pin<Box<dyn Future<Output = Result<Option<SigningKey>, Unavailable>> + Send + 'a>> {
        Box::pin(self.lookup(issuer, kid))
    }
}

/// The authenticator `settings` describe, reading every issuer's keys over HTTP, and the key source to start.
pub fn authenticator(settings: &AuthSettings) -> Result<(Authenticator, Arc<IssuerKeys>), String> {
    let keys = Arc::new(IssuerKeys::from_settings(settings)?);
    Ok((Authenticator::new(settings, Arc::clone(&keys) as Arc<dyn KeySource>), keys))
}

/// Fetch every issuer's keys now, then keep them fresh in the background. Called once at a service's startup.
///
/// A failed first fetch is logged and is not fatal: failing the boot or readiness on an issuer would take every pod
/// out of its Service at once, where the door answers 503 on its own until the issuer answers.
pub async fn start(keys: &Arc<IssuerKeys>) -> tokio::task::JoinHandle<()> {
    let failures = keys.refresh().await;
    for (issuer, reason) in &failures {
        tracing::error!(issuer, reason, "oidc_issuer_unusable");
    }
    if failures.is_empty() {
        tracing::info!(issuers = keys.issuers.len(), "oidc_keys_ready");
    }
    tokio::spawn(Arc::clone(keys).refresh_forever())
}
