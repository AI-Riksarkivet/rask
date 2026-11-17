//! Who a verified token names: a person, a client of the identity provider, or a service account.

use crate::auth::claims::IdToken;
use crate::auth::principal::Principal;
use crate::auth::settings::{ClientMarker, Naming, Provider};
use crate::service::problem::{ErrorCode, NsError};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde_json::Value;

/// The claims a client's token names its client in, in the order they are read.
const CLIENT_ID_CLAIMS: [&str; 3] = ["client_id", "azp", "appid"];

/// A caller whose token was verified.
#[derive(Debug, Clone, PartialEq)]
pub enum Caller {
    Person {
        principal: Principal,
        claims: IdToken,
    },
    /// A machine: an identity provider's client, or a workload's service account.
    Service {
        principal: Principal,
    },
}

impl Caller {
    pub fn principal(&self) -> &Principal {
        match self {
            Caller::Person { principal, .. } | Caller::Service { principal } => principal,
        }
    }

    /// The caller as grants, events and the audit trail name it.
    pub fn subject(&self) -> String {
        self.principal().key()
    }
}

fn unauthenticated(message: &str) -> NsError {
    NsError::new(ErrorCode::Unauthenticated, message)
}

/// What every token that fails a check is told: nothing that helps forge the next one.
pub fn invalid_token() -> NsError {
    unauthenticated("Invalid or expired token")
}

/// The `sub` Dex signs into a client-credentials token: the client id as one length-prefixed protobuf field.
fn dex_client_subject(client_id: &str) -> String {
    let mut message = vec![0x0a];
    let mut size = client_id.len();
    loop {
        let low = (size & 0x7f) as u8;
        size >>= 7;
        message.push(if size == 0 { low } else { low | 0x80 });
        if size == 0 {
            break;
        }
    }
    message.extend_from_slice(client_id.as_bytes());
    URL_SAFE_NO_PAD.encode(message)
}

/// The client a token is its own subject for: it signed in as itself, where a person's token names the person.
fn signed_in_client(claims: &IdToken) -> Option<&str> {
    let by_id = claims.text("client_id").filter(|client_id| *client_id == claims.sub);
    by_id.or_else(|| claims.text("azp").filter(|client| dex_client_subject(client) == claims.sub))
}

/// The client of a token the issuer marked as a client's, where it is marked.
fn marked_client<'a>(claims: &'a IdToken, marker: &ClientMarker) -> Result<Option<&'a str>, NsError> {
    let marked = match (claims.claims.get(&marker.claim), &marker.value) {
        (None | Some(Value::Null), _) => false,
        (Some(mark), Some(value)) => mark.as_str() == Some(value.as_str()),
        (Some(_), None) => true,
    };
    if !marked {
        return Ok(None);
    }
    let client = CLIENT_ID_CLAIMS.iter().find_map(|name| claims.text(name));
    client.map(Some).ok_or_else(|| unauthenticated("The token is a client's and names no client"))
}

/// The person `claims` names under `naming`.
fn person(claims: &IdToken, provider: &Provider) -> Result<Principal, NsError> {
    let (idp_id, subject) = match &provider.naming {
        Naming::Subject(names) => {
            let subject = names
                .iter()
                .find_map(|name| claims.text(name))
                .ok_or_else(|| unauthenticated("The token names no subject this deployment reads"))?;
            (provider.idp_id.as_str(), subject)
        }
        Naming::Connectors(connectors) => {
            let federated = claims.claims.get("federated_claims").filter(|federated| !federated.is_null());
            let federated = federated.ok_or_else(|| unauthenticated("The token names no connector: a client asks for the federated:id scope"))?;
            let text = |name: &str| federated.get(name).and_then(Value::as_str).ok_or_else(invalid_token);
            let (connector, user) = (text("connector_id")?, text("user_id")?);
            let idp_id = connectors
                .get(connector)
                .ok_or_else(|| unauthenticated("The token's connector names no identity provider this deployment knows"))?;
            (idp_id.as_str(), user)
        }
    };
    Principal::new(idp_id, subject).map_err(|_| invalid_token())
}

/// Who the verified `claims` of one of `provider`'s tokens name.
pub fn caller(claims: IdToken, provider: &Provider) -> Result<Caller, NsError> {
    let client = match &provider.client_marker {
        None => signed_in_client(&claims),
        Some(marker) => marked_client(&claims, marker)?,
    };
    if let Some(client_id) = client {
        return Principal::client(&provider.idp_id, client_id)
            .map(|principal| Caller::Service { principal })
            .map_err(|_| invalid_token());
    }
    let principal = person(&claims, provider)?;
    Ok(Caller::Person { principal, claims })
}

/// The service account the verified `claims` name, unless the token was minted for longer than `max_lifetime` seconds.
pub fn service_account(claims: &IdToken, max_lifetime: i64) -> Result<Caller, NsError> {
    if claims.exp - claims.iat > max_lifetime {
        tracing::warn!(account = %claims.sub, lifetime_seconds = claims.exp - claims.iat, max_seconds = max_lifetime, "service_account_token_minted_too_long");
        return Err(invalid_token());
    }
    Principal::service_account(&claims.sub)
        .map(|principal| Caller::Service { principal })
        .ok_or_else(invalid_token)
}
