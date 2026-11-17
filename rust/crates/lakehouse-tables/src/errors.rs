//! What a caller is told when the catalog did not do what it was asked: a spec refusal, and the status it answered.

use lakehouse_core::service::problem::{ErrorCode, NsError};

/// A catalog call that did not succeed.
///
/// `status` is the HTTP status the catalog answered, or `None` when nothing answered; a caller that tells refusals of
/// one class apart (a write whose rows were refused, say) reads it, since a status outside the spec's classes is
/// reported as the catalog being unavailable.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
#[error("{}: {}", error.name, error.message)]
pub struct CatalogError {
    pub status: Option<u16>,
    pub error: NsError,
}

const UNAVAILABLE: &str = "the catalog is unavailable";

impl CatalogError {
    /// The refusal for a catalog's answer of `status` with `body`.
    ///
    /// A 4xx of a class the spec names relays the catalog's own body, which is its redacted problem: that is what keeps a
    /// refusal's meaning through the caller. Any other answer is told as a fixed sentence, because what answered may not
    /// be the catalog (a proxy's error page, say), and its body is logged rather than relayed.
    pub fn answered(status: u16, body: &str) -> Self {
        let error = match refusal_class(status) {
            Some((default, prefix)) => NsError::new(code_in(body, status).unwrap_or(default), format!("{prefix}: {body}")),
            None => {
                tracing::error!(status, body, "catalog_unavailable");
                NsError::new(ErrorCode::ServiceUnavailable, UNAVAILABLE)
            }
        };
        CatalogError { status: Some(status), error }
    }

    /// The refusal for a call nothing answered: the catalog could not be reached, or its answer was cut off.
    pub fn unanswered(reason: &dyn std::fmt::Display) -> Self {
        tracing::error!(reason = %reason, "catalog_unavailable");
        Self::local(ErrorCode::ServiceUnavailable, UNAVAILABLE)
    }

    /// A refusal decided on this side of the exchange: no status rides with it.
    pub fn local(code: ErrorCode, message: impl Into<String>) -> Self {
        CatalogError {
            status: None,
            error: NsError::new(code, message),
        }
    }
}

/// The spec's refusal classes by status, with the default code and what the message says the catalog did.
fn refusal_class(status: u16) -> Option<(ErrorCode, &'static str)> {
    match status {
        400 | 422 => Some((ErrorCode::InvalidInput, "catalog rejected the request")),
        401 => Some((ErrorCode::Unauthenticated, "catalog rejected the credential")),
        403 => Some((ErrorCode::PermissionDenied, "catalog denied the request")),
        404 => Some((ErrorCode::TableNotFound, "catalog table not found")),
        409 => Some((ErrorCode::ConcurrentModification, "catalog write conflict")),
        _ => None,
    }
}

/// The spec code the catalog's problem body names, when it names one answered with this very status.
fn code_in(body: &str, status: u16) -> Option<ErrorCode> {
    let named = serde_json::from_str::<serde_json::Value>(body).ok()?.get("code")?.as_u64()?;
    ErrorCode::ALL.iter().copied().find(|code| *code as u64 == named && code.status() == status)
}
