//! Error bodies: RFC 9457 problem details carrying the Lance Namespace spec's error code, which a client dispatches on.

use crate::service::trace::current_trace_id;
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde::Serialize;
use serde_json::{Map, Value};
use std::borrow::Cow;

pub const PROBLEM_JSON: &str = "application/problem+json";

macro_rules! error_codes {
    ($($variant:ident = $code:literal, $status:literal, $name:literal;)*) => {
        /// The Lance Namespace spec's error codes.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub enum ErrorCode { $($variant = $code,)* }

        impl ErrorCode {
            pub const ALL: &[ErrorCode] = &[$(ErrorCode::$variant,)*];

            /// The HTTP status a refusal with this code is answered with.
            pub const fn status(self) -> u16 {
                match self { $(ErrorCode::$variant => $status,)* }
            }

            /// The name the spec's clients know an error of this code by.
            pub const fn name(self) -> &'static str {
                match self { $(ErrorCode::$variant => $name,)* }
            }
        }
    };
}

error_codes! {
    Unsupported = 0, 406, "UnsupportedOperationError";
    NamespaceNotFound = 1, 404, "NamespaceNotFoundError";
    NamespaceAlreadyExists = 2, 409, "NamespaceAlreadyExistsError";
    NamespaceNotEmpty = 3, 409, "NamespaceNotEmptyError";
    TableNotFound = 4, 404, "TableNotFoundError";
    TableAlreadyExists = 5, 409, "TableAlreadyExistsError";
    TableIndexNotFound = 6, 404, "TableIndexNotFoundError";
    TableIndexAlreadyExists = 7, 409, "TableIndexAlreadyExistsError";
    TableTagNotFound = 8, 404, "TableTagNotFoundError";
    TableTagAlreadyExists = 9, 409, "TableTagAlreadyExistsError";
    TransactionNotFound = 10, 404, "TransactionNotFoundError";
    TableVersionNotFound = 11, 404, "TableVersionNotFoundError";
    TableColumnNotFound = 12, 404, "TableColumnNotFoundError";
    InvalidInput = 13, 400, "InvalidInputError";
    ConcurrentModification = 14, 409, "ConcurrentModificationError";
    PermissionDenied = 15, 403, "PermissionDeniedError";
    Unauthenticated = 16, 401, "UnauthenticatedError";
    ServiceUnavailable = 17, 503, "ServiceUnavailableError";
    Internal = 18, 500, "InternalError";
    InvalidTableState = 19, 409, "InvalidTableStateError";
    TableSchemaValidation = 20, 400, "TableSchemaValidationError";
    Throttling = 21, 429, "ThrottlingError";
    TableBranchNotFound = 22, 404, "TableBranchNotFoundError";
    TableBranchAlreadyExists = 23, 409, "TableBranchAlreadyExistsError";
}

/// A problem details body.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Problem {
    #[serde(rename = "type")]
    pub kind: String,
    pub title: String,
    pub status: u16,
    pub detail: String,
    pub code: u8,
    pub error: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub instance: Option<String>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Problem {
    /// A problem the shell itself answers with: `slug` names its type, and the request's trace is its instance.
    pub fn new(code: ErrorCode, status: u16, title: &str, slug: &str, detail: impl Into<String>) -> Self {
        let detail = detail.into();
        Problem {
            kind: format!("https://lance.org/problems/{slug}"),
            title: title.to_owned(),
            status,
            error: detail.clone(),
            detail,
            code: code as u8,
            instance: current_trace_id(),
            extra: Map::new(),
        }
    }

    /// The answer to a request the router has no door for: an HTTP status with no spec error of its own.
    pub fn for_status(status: StatusCode) -> Self {
        let code = match status.as_u16() {
            400 => ErrorCode::InvalidInput,
            401 => ErrorCode::Unauthenticated,
            403 => ErrorCode::PermissionDenied,
            429 => ErrorCode::Throttling,
            500 => ErrorCode::Internal,
            503 => ErrorCode::ServiceUnavailable,
            _ => ErrorCode::Unsupported,
        };
        let phrase = status.canonical_reason().unwrap_or("Error");
        Problem::new(code, status.as_u16(), phrase, &phrase.to_lowercase().replace(' ', "-"), phrase)
    }
}

impl IntoResponse for Problem {
    fn into_response(self) -> Response {
        let status = StatusCode::from_u16(self.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        // A problem's members are strings, numbers and JSON values, so writing it cannot fail.
        let body = serde_json::to_string(&self).unwrap_or_default();
        (status, [(header::CONTENT_TYPE, PROBLEM_JSON)], body).into_response()
    }
}

/// A door's refusal: a spec error code, the name it is known by, and what to tell the caller.
#[derive(Debug, Clone, PartialEq)]
pub struct NsError {
    pub code: ErrorCode,
    pub name: Cow<'static, str>,
    pub message: String,
}

impl NsError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        NsError {
            code,
            name: Cow::Borrowed(code.name()),
            message: message.into(),
        }
    }

    /// The same refusal under a name of its own, for a caller that tells refusals of one code apart.
    pub fn named(mut self, name: &'static str) -> Self {
        self.name = Cow::Borrowed(name);
        self
    }

    /// The body the refusal is answered with. What went wrong inside the service is not told to the caller.
    pub fn problem(&self) -> Problem {
        let status = self.code.status();
        let detail = if status < 500 { self.message.as_str() } else { "Internal Server Error" };
        Problem::new(self.code, status, &self.name, &self.name.to_lowercase(), detail)
    }
}

impl IntoResponse for NsError {
    fn into_response(self) -> Response {
        if self.code.status() >= 500 {
            tracing::error!(code = self.code as u8, name = %self.name, error = %self.message, "domain_error");
        }
        self.problem().into_response()
    }
}
