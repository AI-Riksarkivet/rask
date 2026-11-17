//! Whose identity a call to the catalog carries: a bearer answered afresh for every request.

use lakehouse_core::service::problem::{ErrorCode, NsError};
use std::path::PathBuf;
use std::sync::Arc;

/// Answers the bearer for the next request, or none for a catalog with authentication off.
///
/// Asked on every request, never kept: the kubelet rotates a projected token well inside its life, so a token read once
/// at start goes stale under a long-running service.
pub type Bearer = Arc<dyn Fn() -> Result<Option<String>, NsError> + Send + Sync>;

/// No bearer: only a catalog with authentication off accepts the calls.
pub fn anonymous() -> Bearer {
    Arc::new(|| Ok(None))
}

/// The token in the file at `path`, read at every request; a file that cannot be read or is empty stops the call unsent.
pub fn token_file(path: impl Into<PathBuf>) -> Bearer {
    let path = path.into();
    Arc::new(move || read_identity_token(&path).map(Some))
}

/// The token in the file at `path`, trimmed. A call is not sent without the identity it was meant to carry.
pub fn read_identity_token(path: &std::path::Path) -> Result<String, NsError> {
    let unavailable = |what: &str| NsError::new(ErrorCode::ServiceUnavailable, format!("the identity token at {} {what}", path.display()));
    let token = std::fs::read_to_string(path).map_err(|_| unavailable("could not be read"))?;
    let token = token.trim();
    if token.is_empty() {
        return Err(unavailable("is empty"));
    }
    Ok(token.to_owned())
}
