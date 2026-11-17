//! The authentication settings, read from the environment and refused as a whole where two of them cannot both hold.
//!
//! One name each, the same for every service (`RASK_OIDC_*`, `RASK_SA_*`), so no two services can disagree about
//! who is signed in.

use crate::auth::principal::{CLIENT_IDP_SUFFIX, is_idp_id, is_machine_idp};
use serde::Deserialize;
use std::collections::{BTreeMap, HashMap, HashSet};

/// The claims every token carries, which therefore mark no token as a client's.
const CLAIMS_EVERY_TOKEN_CARRIES: [&str; 5] = ["iss", "sub", "aud", "exp", "iat"];

/// Names the settings had before they were unified. One left set would authenticate some services and not others.
const RETIRED_PREFIXES: [&str; 3] = ["LANCE", "LINEAGE", "MAINTENANCE"];
const RETIRED_SUFFIXES: [&str; 13] = [
    "OIDC_ENABLED",
    "OIDC_ISSUER",
    "OIDC_AUDIENCE",
    "OIDC_DISCOVERY_URL",
    "OIDC_CACHE_TTL",
    "OIDC_LEEWAY",
    "OIDC_ALLOW_INSECURE",
    "FGA_ENABLED",
    "FGA_API_URL",
    "FGA_STORE_ID",
    "FGA_MODEL_ID",
    "FGA_TIMEOUT_SECONDS",
    "FGA_ROOT_OBJECT",
];

/// The claim that marks a token as a client's, and the value it must have where one is named.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct ClientMarker {
    pub claim: String,
    #[serde(default)]
    pub value: Option<String>,
}

/// How an issuer's tokens name a person.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Naming {
    /// By the first of these claims the token carries.
    Subject(Vec<String>),
    /// By the user id of the upstream connector the person signed in through, each connector an idp of its own.
    Connectors(BTreeMap<String, String>),
}

/// An identity provider whose tokens name people, and clients under `<idp_id>-client`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Provider {
    pub issuer: String,
    pub idp_id: String,
    /// Where the issuer's discovery document is fetched from, where that is not the issuer itself.
    pub discovery_url: Option<String>,
    pub naming: Naming,
    pub client_marker: Option<ClientMarker>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceAccounts {
    pub issuer: String,
    pub audience: String,
    pub fetch_token_file: Option<String>,
    pub ca_file: Option<String>,
    /// The longest a service account's token may have been minted for.
    pub max_token_seconds: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthSettings {
    pub enabled: bool,
    pub audience: Option<String>,
    pub providers: Vec<Provider>,
    /// How often an issuer's keys are read again, in seconds.
    pub cache_ttl: u64,
    pub leeway: i64,
    pub allow_insecure: bool,
    /// The operator's word that a service with no authentication is what was meant.
    pub insecure_allow_unauthenticated: bool,
    pub service_accounts: Option<ServiceAccounts>,
}

#[derive(Deserialize)]
struct ProviderEntry {
    issuer: String,
    idp_id: String,
    #[serde(default)]
    discovery_url: Option<String>,
    #[serde(default)]
    subject_claims: Option<Vec<String>>,
    #[serde(default)]
    connectors: BTreeMap<String, String>,
    #[serde(default)]
    client_marker: Option<ClientMarker>,
}

fn trimmed(issuer: &str) -> &str {
    issuer.trim_end_matches('/')
}

fn persons_idp_id(what: &str, idp_id: &str) -> Result<(), String> {
    if is_machine_idp(idp_id) {
        return Err(format!("{what} {idp_id:?}, a machine identity's idp id"));
    }
    if !is_idp_id(idp_id) {
        return Err(format!("{what} {idp_id:?}, which is not lower-case letters, digits and '-'"));
    }
    Ok(())
}

fn marker(marker: Option<ClientMarker>) -> Result<Option<ClientMarker>, String> {
    match marker {
        Some(marker) if marker.claim.is_empty() => Err("the client marker names no claim".to_owned()),
        Some(marker) if CLAIMS_EVERY_TOKEN_CARRIES.contains(&marker.claim.as_str()) => Err(format!(
            "the client marker names {:?}, a claim every token carries, so it marks nothing",
            marker.claim
        )),
        marker => Ok(marker),
    }
}

impl ProviderEntry {
    fn provider(self) -> Result<Provider, String> {
        if self.issuer.is_empty() {
            return Err("an issuer is empty".to_owned());
        }
        if let Some(claims) = &self.subject_claims {
            if !self.connectors.is_empty() {
                return Err("subject claims are never read beside a connector map, which reports its user id in federated_claims".to_owned());
            }
            if claims.is_empty() || claims.iter().any(String::is_empty) {
                return Err("the subject claims name no claim, or an empty one".to_owned());
            }
        }
        if self.connectors.contains_key("") {
            return Err("the connector map names an empty connector id".to_owned());
        }
        persons_idp_id("the idp id is", &self.idp_id)?;
        for (connector, idp_id) in &self.connectors {
            persons_idp_id(&format!("the connector map maps {connector:?} to"), idp_id)?;
        }
        let naming = if self.connectors.is_empty() {
            Naming::Subject(self.subject_claims.unwrap_or_else(|| vec!["sub".to_owned()]))
        } else {
            Naming::Connectors(self.connectors)
        };
        Ok(Provider {
            issuer: self.issuer,
            idp_id: self.idp_id,
            discovery_url: self.discovery_url,
            naming,
            client_marker: marker(self.client_marker)?,
        })
    }
}

impl Provider {
    /// Every idp id the provider's tokens can name: its people's, its clients' and its connectors'.
    fn idp_ids(&self) -> Vec<String> {
        let mut ids = vec![self.idp_id.clone(), format!("{}{CLIENT_IDP_SUFFIX}", self.idp_id)];
        if let Naming::Connectors(connectors) = &self.naming {
            ids.extend(connectors.values().cloned());
        }
        ids
    }
}

/// The environment as the settings read it: names without regard to case.
struct Environment(HashMap<String, String>);

impl Environment {
    fn text(&self, name: &str) -> Option<&str> {
        self.0.get(name).map(String::as_str)
    }

    fn flag(&self, name: &str) -> Result<bool, String> {
        match self.text(name).map(|value| value.trim().to_lowercase()).as_deref() {
            None => Ok(false),
            Some("1" | "true" | "t" | "yes" | "y" | "on") => Ok(true),
            Some("0" | "false" | "f" | "no" | "n" | "off") => Ok(false),
            Some(other) => Err(format!("{name} is {other:?}, which is neither true nor false")),
        }
    }

    fn number(&self, name: &str, default: i64) -> Result<i64, String> {
        self.text(name).map_or(Ok(default), |value| {
            value.trim().parse().map_err(|_| format!("{name} is {value:?}, which is no whole number"))
        })
    }

    fn json<T: serde::de::DeserializeOwned + Default>(&self, name: &str) -> Result<T, String> {
        self.text(name).map_or_else(
            || Ok(T::default()),
            |value| serde_json::from_str(value).map_err(|error| format!("{name} is not the JSON it must be: {error}")),
        )
    }
}

impl AuthSettings {
    /// The settings `environment` holds, or the first reason they cannot be taken together.
    pub fn from_environment(environment: impl IntoIterator<Item = (String, String)>) -> Result<Self, String> {
        let environment = Environment(environment.into_iter().map(|(name, value)| (name.to_uppercase(), value)).collect());
        let retired = RETIRED_PREFIXES
            .iter()
            .flat_map(|prefix| RETIRED_SUFFIXES.iter().map(move |suffix| format!("{prefix}_{suffix}")));
        if let Some(name) = retired.into_iter().find(|name| environment.0.contains_key(name)) {
            return Err(format!(
                "{name} no longer exists: the auth settings are one RASK_* name each (RASK_OIDC_*, RASK_FGA_*), with no alias and no fallback"
            ));
        }

        let enabled = environment.flag("RASK_OIDC_ENABLED")?;
        let allow_insecure = environment.flag("RASK_OIDC_ALLOW_INSECURE")?;
        let issuer = environment.text("RASK_OIDC_ISSUER").filter(|issuer| !issuer.is_empty());
        let audience = environment.text("RASK_OIDC_AUDIENCE").filter(|audience| !audience.is_empty());
        let idp_id = environment.text("RASK_OIDC_IDP_ID").filter(|idp_id| !idp_id.is_empty());
        let cache_ttl = environment.number("RASK_OIDC_CACHE_TTL", 300)?;
        if cache_ttl <= 0 {
            return Err("RASK_OIDC_CACHE_TTL must be greater than 0".to_owned());
        }

        let client_marker = marker(environment.json::<Option<ClientMarker>>("RASK_OIDC_CLIENT_MARKER")?)?;
        let extra = environment
            .json::<Vec<ProviderEntry>>("RASK_OIDC_PROVIDERS")?
            .into_iter()
            .map(ProviderEntry::provider)
            .collect::<Result<Vec<_>, _>>()?;
        let subject_claims = environment.json::<Option<Vec<String>>>("RASK_OIDC_SUBJECT_CLAIMS")?;
        let connectors = environment.json::<BTreeMap<String, String>>("RASK_OIDC_CONNECTORS")?;

        let mut providers = Vec::new();
        if enabled {
            let (Some(issuer), Some(_)) = (issuer, audience) else {
                return Err("RASK_OIDC_ISSUER and RASK_OIDC_AUDIENCE are required when OIDC is enabled".to_owned());
            };
            let idp_id =
                idp_id.ok_or("RASK_OIDC_IDP_ID is required when OIDC is enabled: it names the issuer's people, or under RASK_OIDC_CONNECTORS its clients")?;
            let discovery_url = environment.text("RASK_OIDC_DISCOVERY_URL").filter(|url| !url.is_empty()).map(str::to_owned);
            let primary = ProviderEntry {
                issuer: issuer.to_owned(),
                idp_id: idp_id.to_owned(),
                discovery_url,
                subject_claims,
                connectors,
                client_marker,
            };
            providers.push(
                primary
                    .provider()
                    .map_err(|refusal| format!("RASK_OIDC_ISSUER's settings are refused: {refusal}"))?,
            );
            providers.extend(extra);
            let (mut issuers, mut claimed) = (HashSet::new(), HashMap::new());
            for provider in &providers {
                let issuer = trimmed(&provider.issuer);
                if !issuers.insert(issuer) {
                    return Err(format!("{issuer} is named as an issuer twice: one token would be named by two rules"));
                }
                for idp_id in provider.idp_ids() {
                    if *claimed.entry(idp_id.clone()).or_insert(issuer) != issuer {
                        return Err(format!("the idp id {idp_id:?} is named by two issuers: their people would share one namespace"));
                    }
                }
                if !allow_insecure && !issuer.starts_with("https://") {
                    return Err(format!("the issuer {issuer} must use HTTPS (set RASK_OIDC_ALLOW_INSECURE=true for a dev IdP)"));
                }
            }
        }

        let max_token_seconds = environment.number("RASK_SA_MAX_TOKEN_SECONDS", 3600)?;
        if max_token_seconds <= 0 {
            return Err("RASK_SA_MAX_TOKEN_SECONDS must be greater than 0".to_owned());
        }
        let service_accounts = match environment.text("RASK_SA_ISSUER").filter(|issuer| !issuer.is_empty()) {
            None => None,
            Some(issuer) => {
                let audience = environment
                    .text("RASK_SA_AUDIENCE")
                    .filter(|audience| !audience.is_empty())
                    .ok_or("RASK_SA_AUDIENCE is required with RASK_SA_ISSUER")?;
                if !issuer.starts_with("https://") {
                    return Err("RASK_SA_ISSUER must use HTTPS".to_owned());
                }
                if providers.iter().any(|provider| trimmed(&provider.issuer) == trimmed(issuer)) {
                    return Err(
                        "RASK_SA_ISSUER and a person's issuer name one issuer: its tokens would be routed as service accounts' and never as a person's"
                            .to_owned(),
                    );
                }
                let file = |name: &str| environment.text(name).filter(|path| !path.is_empty()).map(str::to_owned);
                Some(ServiceAccounts {
                    issuer: issuer.to_owned(),
                    audience: audience.to_owned(),
                    fetch_token_file: file("RASK_SA_FETCH_TOKEN_FILE"),
                    ca_file: file("RASK_SA_CA_FILE"),
                    max_token_seconds,
                })
            }
        };

        if environment.flag("RASK_FGA_ENABLED")? && !enabled {
            return Err("RASK_OIDC_ENABLED is required when RASK_FGA_ENABLED is set (authz needs a verified subject)".to_owned());
        }
        Ok(AuthSettings {
            enabled,
            audience: audience.map(str::to_owned),
            providers,
            cache_ttl: cache_ttl as u64,
            leeway: environment.number("RASK_OIDC_LEEWAY", 60)?,
            allow_insecure,
            insecure_allow_unauthenticated: environment.flag("RASK_INSECURE_ALLOW_UNAUTHENTICATED")?,
            service_accounts,
        })
    }

    /// Refuse to start a service with no authentication unless its operator said that is what was meant.
    pub fn require_authentication_or_its_waiver(&self) -> Result<(), String> {
        if self.enabled || self.insecure_allow_unauthenticated {
            return Ok(());
        }
        Err(
            "this service would start with NO authentication: set RASK_OIDC_ENABLED=true (with RASK_OIDC_ISSUER and RASK_OIDC_AUDIENCE), \
             or RASK_INSECURE_ALLOW_UNAUTHENTICATED=true to declare that an open service is what you meant"
                .to_owned(),
        )
    }
}
