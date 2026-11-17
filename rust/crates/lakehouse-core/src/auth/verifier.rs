//! Verifying a bearer: its signature against its issuer's keys, then its claims, then who it names.

use crate::auth::claims::{self, Expected, IdToken};
use crate::auth::identity::{self, Caller, invalid_token};
use crate::auth::settings::{AuthSettings, Provider};
use crate::service::problem::{ErrorCode, NsError};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use jsonwebtoken::{Algorithm, DecodingKey, Validation};
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::pin::Pin;
use std::sync::Arc;

/// A key an issuer signs with, the algorithms it may be used with, and the issuer as it names itself.
#[derive(Clone)]
pub struct SigningKey {
    pub key: DecodingKey,
    pub algorithms: Vec<Algorithm>,
    pub issuer: String,
}

/// The issuer could not be asked for its keys, so nothing can be said about a token of its.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unavailable(pub String);

type Lookup<'a> = Pin<Box<dyn Future<Output = Result<Option<SigningKey>, Unavailable>> + Send + 'a>>;

/// Where a verifier gets an issuer's keys. `Ok(None)` is an issuer that answered and has no key of that id.
pub trait KeySource: Send + Sync {
    fn signing_key<'a>(&'a self, issuer: &'a str, kid: &'a str) -> Lookup<'a>;
}

/// Why a token was not verified.
#[derive(Debug, Clone, PartialEq)]
pub enum Refusal {
    Refused(NsError),
    Unavailable(Unavailable),
}

impl From<NsError> for Refusal {
    fn from(refusal: NsError) -> Self {
        Refusal::Refused(refusal)
    }
}

fn trimmed(issuer: &str) -> &str {
    issuer.trim_end_matches('/')
}

/// The issuer a token claims, read before its signature is checked: it only chooses whose keys to check it with.
fn claimed_issuer(token: &str) -> Option<String> {
    let mut parts = token.split('.');
    let (_, payload, _) = (parts.next()?, parts.next()?, parts.next().filter(|_| parts.next().is_none())?);
    let claims: Map<String, Value> = serde_json::from_slice(&URL_SAFE_NO_PAD.decode(payload.trim_end_matches('=')).ok()?).ok()?;
    Some(claims.get("iss").and_then(Value::as_str).unwrap_or_default().to_owned())
}

/// Verifies the tokens of a set of issuers for one audience.
pub struct TokenVerifier {
    issuers: Vec<String>,
    audience: String,
    leeway: i64,
    keys: Arc<dyn KeySource>,
}

impl TokenVerifier {
    pub fn new(issuers: impl IntoIterator<Item = String>, audience: &str, leeway: i64, keys: Arc<dyn KeySource>) -> Self {
        let issuers = issuers.into_iter().map(|issuer| trimmed(&issuer).to_owned()).collect();
        TokenVerifier {
            issuers,
            audience: audience.to_owned(),
            leeway,
            keys,
        }
    }

    /// Whether `token` claims to be from one of this verifier's issuers. Nothing is verified.
    pub fn issued(&self, token: &str) -> bool {
        claimed_issuer(token).is_some_and(|claimed| self.issuers.iter().any(|issuer| trimmed(&claimed) == issuer))
    }

    /// The claims of `token`, where at `now` it is a token of one of the issuers, signed by it, for this audience.
    pub async fn verify(&self, token: &str, now: i64) -> Result<IdToken, Refusal> {
        let claimed = claimed_issuer(token).ok_or_else(invalid_token)?;
        let issuer = self
            .issuers
            .iter()
            .find(|issuer| trimmed(&claimed) == *issuer)
            .ok_or_else(|| NsError::new(ErrorCode::Unauthenticated, "Unrecognized token issuer"))?;
        let kid = jsonwebtoken::decode_header(token)
            .ok()
            .and_then(|header| header.kid)
            .ok_or_else(invalid_token)?;
        let signing = self
            .keys
            .signing_key(issuer, &kid)
            .await
            .map_err(Refusal::Unavailable)?
            .ok_or_else(invalid_token)?;

        // The library checks the signature and that the algorithm is one of the issuer's; the claims are judged below.
        let mut signature_only = Validation::new(*signing.algorithms.first().ok_or_else(invalid_token)?);
        signature_only.algorithms = signing.algorithms.clone();
        signature_only.required_spec_claims.clear();
        (signature_only.validate_exp, signature_only.validate_nbf, signature_only.validate_aud) = (false, false, false);
        let payload = jsonwebtoken::decode::<Map<String, Value>>(token, &signing.key, &signature_only)
            .map_err(|_| invalid_token())?
            .claims;

        let expected = Expected {
            audience: &self.audience,
            issuer: &signing.issuer,
            leeway: self.leeway,
        };
        Ok(claims::judge(&payload, now, expected).ok_or_else(invalid_token)?)
    }
}

/// Verifies the people and clients of the configured identity providers.
pub struct PeopleVerifier {
    tokens: TokenVerifier,
    providers: HashMap<String, Provider>,
}

/// Verifies workloads by their Kubernetes service-account tokens.
pub struct AccountVerifier {
    tokens: TokenVerifier,
    max_lifetime: i64,
}

/// What a door asks who its caller is.
pub struct Authenticator {
    enabled: bool,
    /// Absent where the identity providers could not be set up: their callers are answered that authentication is down.
    pub people: Option<PeopleVerifier>,
    pub accounts: Option<AccountVerifier>,
    account_issuer: Option<String>,
}

fn audit(outcome: &str, detail: &str) {
    tracing::info!(target: "lance.audit", action = "authn", outcome, detail);
}

impl Authenticator {
    /// The authenticator `settings` describe, with each verifier reading its keys from `keys`.
    pub fn new(settings: &AuthSettings, keys: Arc<dyn KeySource>) -> Self {
        let people = settings
            .audience
            .as_ref()
            .filter(|_| settings.enabled && !settings.providers.is_empty())
            .map(|audience| PeopleVerifier {
                tokens: TokenVerifier::new(
                    settings.providers.iter().map(|provider| provider.issuer.clone()),
                    audience,
                    settings.leeway,
                    Arc::clone(&keys),
                ),
                providers: settings
                    .providers
                    .iter()
                    .map(|provider| (trimmed(&provider.issuer).to_owned(), provider.clone()))
                    .collect(),
            });
        let accounts = settings.service_accounts.as_ref().map(|accounts| AccountVerifier {
            tokens: TokenVerifier::new([accounts.issuer.clone()], &accounts.audience, settings.leeway, keys),
            max_lifetime: accounts.max_token_seconds,
        });
        let account_issuer = settings.service_accounts.as_ref().map(|accounts| trimmed(&accounts.issuer).to_owned());
        Authenticator {
            enabled: settings.enabled,
            people,
            accounts,
            account_issuer,
        }
    }

    /// Who sent `bearer`, at `now`. `None` where authentication is off.
    ///
    /// A token that claims the service-account issuer is only ever verified as a service account's, and any other
    /// only as a person's or a client's: which verifier a token meets is decided by its claim, never by which accepts it.
    pub async fn caller_of(&self, bearer: Option<&str>, now: i64) -> Result<Option<Caller>, NsError> {
        if !self.enabled {
            return Ok(None);
        }
        let bearer = bearer.filter(|bearer| !bearer.is_empty());
        let verified = match bearer {
            Some(bearer) if self.claims_the_account_issuer(bearer) => match &self.accounts {
                Some(accounts) => accounts
                    .tokens
                    .verify(bearer, now)
                    .await
                    .and_then(|claims| Ok(identity::service_account(&claims, accounts.max_lifetime)?)),
                None => Err(Refusal::Unavailable(Unavailable("the service-account verifier is not up".to_owned()))),
            },
            None if self.people.is_some() => {
                audit("failure", "missing_token");
                return Err(NsError::new(ErrorCode::Unauthenticated, "Missing bearer token"));
            }
            bearer => match &self.people {
                Some(people) => people.verify(bearer.unwrap_or_default(), now).await,
                None => Err(Refusal::Unavailable(Unavailable("the identity-provider verifier is not up".to_owned()))),
            },
        };
        match verified {
            Ok(caller) => {
                audit("success", &caller.subject());
                Ok(Some(caller))
            }
            Err(Refusal::Refused(refusal)) => {
                audit("failure", "invalid_token");
                Err(refusal)
            }
            Err(Refusal::Unavailable(Unavailable(reason))) => {
                audit("failure", "verifier_unavailable");
                tracing::warn!(reason, "authentication_unavailable");
                Err(NsError::new(ErrorCode::ServiceUnavailable, "Authentication is enabled but unavailable"))
            }
        }
    }

    fn claims_the_account_issuer(&self, bearer: &str) -> bool {
        match (&self.accounts, &self.account_issuer) {
            (Some(accounts), _) => accounts.tokens.issued(bearer),
            (None, Some(issuer)) => claimed_issuer(bearer).is_some_and(|claimed| trimmed(&claimed) == issuer),
            (None, None) => false,
        }
    }
}

impl PeopleVerifier {
    async fn verify(&self, token: &str, now: i64) -> Result<Caller, Refusal> {
        let claims = self.tokens.verify(token, now).await?;
        let provider = self
            .providers
            .get(trimmed(&claims.iss))
            .ok_or_else(|| NsError::new(ErrorCode::Unauthenticated, "Unrecognized token issuer"))?;
        Ok(identity::caller(claims, provider)?)
    }
}
