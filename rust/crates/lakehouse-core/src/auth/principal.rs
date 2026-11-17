//! A principal: an identity provider's id and the subject it names, which together are one caller everywhere.

use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};

/// What separates the idp id from the subject in a principal's key.
pub const SEPARATOR: char = '~';
/// The idp id of every Kubernetes service account.
pub const SERVICE_ACCOUNT_IDP: &str = "kubernetes";
/// What an identity provider's clients carry after its idp id, so a client is never one of its people.
pub const CLIENT_IDP_SUFFIX: &str = "-client";
/// The longest user OpenFGA takes.
const FGA_USER_MAX_BYTES: usize = 512;
/// Everything but the characters a URL never needs escaped.
const ESCAPED: &AsciiSet = &NON_ALPHANUMERIC.remove(b'_').remove(b'.').remove(b'-').remove(b'~');
const ACCOUNT_PREFIX: &str = "system:serviceaccount:";

/// Whether `idp_id` names machines: service accounts, or an identity provider's clients.
pub fn is_machine_idp(idp_id: &str) -> bool {
    idp_id == SERVICE_ACCOUNT_IDP || idp_id.ends_with(CLIENT_IDP_SUFFIX)
}

/// Whether `idp_id` is one a principal can carry: lower-case letters, digits and `-`.
pub fn is_idp_id(idp_id: &str) -> bool {
    !idp_id.is_empty() && idp_id.bytes().all(|byte| matches!(byte, b'a'..=b'z' | b'0'..=b'9' | b'-'))
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Principal {
    idp_id: String,
    subject: String,
}

impl Principal {
    /// The principal `subject` of `idp_id`, or why there is no such principal.
    pub fn new(idp_id: &str, subject: &str) -> Result<Self, String> {
        if !is_idp_id(idp_id) {
            return Err(format!("the idp id {idp_id:?} is not lower-case letters, digits and '-'"));
        }
        if subject.is_empty() {
            return Err("a principal's subject is empty".to_owned());
        }
        if subject.chars().any(char::is_control) {
            return Err("a principal's subject carries a control character".to_owned());
        }
        let principal = Principal {
            idp_id: idp_id.to_owned(),
            subject: subject.to_owned(),
        };
        if principal.fga_user().len() > FGA_USER_MAX_BYTES {
            return Err(format!("a principal's OpenFGA user is longer than {FGA_USER_MAX_BYTES} bytes"));
        }
        Ok(principal)
    }

    /// The principal a key names.
    pub fn parse(key: &str) -> Result<Self, String> {
        let (idp_id, subject) = key
            .split_once(SEPARATOR)
            .ok_or_else(|| format!("{key:?} names no identity provider: a principal is '<idp-id>{SEPARATOR}<subject>'"))?;
        Principal::new(idp_id, subject)
    }

    /// The service account `account` (`system:serviceaccount:<namespace>:<name>`), where it is one.
    pub fn service_account(account: &str) -> Option<Self> {
        let (namespace, name) = account.strip_prefix(ACCOUNT_PREFIX)?.split_once(':')?;
        let named = !namespace.is_empty() && !name.is_empty() && !name.contains(':');
        named.then(|| Principal::new(SERVICE_ACCOUNT_IDP, account).ok()).flatten()
    }

    /// The client `client_id` of the identity provider `idp_id`.
    pub fn client(idp_id: &str, client_id: &str) -> Result<Self, String> {
        Principal::new(&format!("{idp_id}{CLIENT_IDP_SUFFIX}"), client_id)
    }

    /// The one text that names the principal: in grants, in events and in the audit trail.
    pub fn key(&self) -> String {
        format!("{}{SEPARATOR}{}", self.idp_id, self.subject)
    }

    /// The principal as an OpenFGA user.
    pub fn fga_user(&self) -> String {
        format!("user:{}", utf8_percent_encode(&self.key(), ESCAPED))
    }

    pub fn is_machine(&self) -> bool {
        is_machine_idp(&self.idp_id)
    }
}
