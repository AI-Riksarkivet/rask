//! Lineage's doors, one module per group of endpoints, and what every door shares: who the caller is, the query's
//! validation as FastAPI answers it, and a refusal's body.

pub mod dlq;
pub mod events;
pub mod ingest;
pub mod tables;

use crate::authz::gates::Denial;
use crate::state::State;
use axum::http::header::{AUTHORIZATION, CONTENT_TYPE};
use axum::http::{HeaderMap, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use lakehouse_core::authz::Unavailable;
use lakehouse_core::service::problem::{ErrorCode, NsError, Problem};
use lakehouse_tables::CatalogError;
use percent_encoding::percent_decode_str;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

/// Why a door did not answer as asked: a spec refusal, or a request that failed validation, with each field's error.
#[derive(Debug)]
pub enum Refusal {
    Ns(NsError),
    Invalid(Vec<Value>),
    /// What the router answers a path it serves no door at.
    NotFound,
    /// An answer already made, by the framework's own rules.
    Answered(Box<Response>),
}

impl From<NsError> for Refusal {
    fn from(error: NsError) -> Self {
        Refusal::Ns(error)
    }
}

impl From<Denial> for Refusal {
    fn from(denial: Denial) -> Self {
        Refusal::Ns(denial.ns_error())
    }
}

impl From<CatalogError> for Refusal {
    fn from(error: CatalogError) -> Self {
        Refusal::Ns(error.error)
    }
}

impl From<Unavailable> for Refusal {
    fn from(unavailable: Unavailable) -> Self {
        Refusal::Ns(NsError::new(ErrorCode::ServiceUnavailable, unavailable.0))
    }
}

impl IntoResponse for Refusal {
    fn into_response(self) -> Response {
        match self {
            Refusal::Ns(error) => error.into_response(),
            Refusal::NotFound => Problem::for_status(StatusCode::NOT_FOUND).into_response(),
            Refusal::Answered(response) => *response,
            Refusal::Invalid(errors) => {
                let mut problem = Problem::new(ErrorCode::InvalidInput, 422, "Validation Error", "validation", "Validation Error");
                problem.extra.insert("errors".to_owned(), Value::Array(errors));
                problem.into_response()
            }
        }
    }
}

pub type Answer = Result<Response, Refusal>;

/// A JSON answer.
pub fn answer(status: StatusCode, body: Value) -> Answer {
    Ok((status, [(CONTENT_TYPE, "application/json")], body.to_string()).into_response())
}

/// One field's validation error, as FastAPI lists it.
fn field_error(field: &str, message: &str, kind: &str) -> Value {
    json!({"field": field, "message": message, "type": kind})
}

/// Who sent a request, as the service itself verified it, and the bearer it sent, which a name is resolved under.
pub struct Caller {
    pub subject: Option<String>,
    pub bearer: Option<String>,
}

/// The bearer of an `Authorization` header: its first value, whose scheme is `bearer` in any case, trimmed.
fn bearer(headers: &HeaderMap) -> Option<String> {
    let (scheme, token) = headers.get(AUTHORIZATION)?.to_str().ok()?.split_once(' ')?;
    let token = token.trim();
    (scheme.eq_ignore_ascii_case("bearer") && !token.is_empty()).then(|| token.to_owned())
}

/// The caller: refused before anything else is read, the request's body included. `None` with authentication off.
pub async fn caller(state: &State, headers: &HeaderMap) -> Result<Caller, Refusal> {
    let bearer = bearer(headers);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |since| i64::try_from(since.as_secs()).unwrap_or(i64::MAX));
    let verified = state.authenticator.caller_of(bearer.as_deref(), now).await?;
    Ok(Caller {
        subject: verified.map(|caller| caller.subject()),
        bearer,
    })
}

/// A path parameter as the router of the Python service matches it: a segment that decodes to a `/` is no segment.
pub fn segment(raw: String) -> Result<String, Refusal> {
    if raw.contains('/') { Err(Refusal::NotFound) } else { Ok(raw) }
}

/// What a door audits on the governance trail, under the `lance.audit` target its export routes.
pub fn audit(action: &str, outcome: &str, subject: &str, resource: &str) {
    tracing::info!(target: "lance.audit", action, outcome, subject, resource, "audit");
}

/// `future` boxed as a `Send` future where it is called, so the borrows it holds are checked at the lifetimes they have
/// there: a handler's future is checked at every lifetime, which the compiler cannot prove for one holding a borrowed
/// `&str` across an await, as the metadata gate does.
pub fn sendable<'a, T>(future: impl Future<Output = T> + Send + 'a) -> std::pin::Pin<Box<dyn Future<Output = T> + Send + 'a>> {
    Box::pin(future)
}

/// A query string read as Starlette reads it (the last value of a name wins) and validated as pydantic validates it,
/// each failing field listed in the order the door declares it.
pub struct Query {
    values: HashMap<String, String>,
    errors: Vec<Value>,
}

/// The digits CPython reads an integer from at most.
const MOST_DIGITS: usize = 4300;

/// An integer as pydantic reads one from text: trimmed, signed, digits with single underscores between them, and a
/// fraction of zeros only.
fn integer(text: &str) -> Result<i128, (&'static str, &'static str)> {
    const UNPARSED: (&str, &str) = ("Input should be a valid integer, unable to parse string as an integer", "int_parsing");
    let text = text.trim();
    let (negative, unsigned) = match text.as_bytes().first() {
        Some(b'-') => (true, &text[1..]),
        Some(b'+') => (false, &text[1..]),
        _ => (false, text),
    };
    let (whole, fraction) = unsigned.split_once('.').unwrap_or((unsigned, "0"));
    let groups: Vec<&str> = whole.split('_').collect();
    let well_formed = groups.iter().all(|group| !group.is_empty() && group.bytes().all(|byte| byte.is_ascii_digit()))
        && !fraction.is_empty()
        && fraction.bytes().all(|byte| byte == b'0');
    if !well_formed {
        return Err(UNPARSED);
    }
    let digits = groups.concat();
    if digits.len() + usize::from(text.starts_with(['-', '+'])) > MOST_DIGITS {
        return Err(("Unable to parse input string as an integer, exceeded maximum size", "int_parsing_size"));
    }
    let magnitude = digits.parse::<i128>().unwrap_or(i128::MAX);
    Ok(if negative { -magnitude } else { magnitude })
}

impl Query {
    pub fn of(uri: &Uri) -> Self {
        let decoded = |text: &str| percent_decode_str(&text.replace('+', " ")).decode_utf8_lossy().into_owned();
        let pairs = uri.query().unwrap_or_default().split('&').filter(|pair| !pair.is_empty());
        let values = pairs
            .map(|pair| {
                let (name, value) = pair.split_once('=').unwrap_or((pair, ""));
                (decoded(name), decoded(value))
            })
            .collect();
        Query { values, errors: Vec::new() }
    }

    /// An integer at least `ge` and at most `le`, saturated to 64 bits; `None` where absent or invalid.
    pub fn int(&mut self, name: &str, ge: i128, le: Option<i128>) -> Option<i64> {
        let field = format!("query.{name}");
        let number = match integer(self.values.get(name)?) {
            Ok(number) => number,
            Err((message, kind)) => {
                self.errors.push(field_error(&field, message, kind));
                return None;
            }
        };
        if number < ge {
            let message = format!("Input should be greater than or equal to {ge}");
            self.errors.push(field_error(&field, &message, "greater_than_equal"));
            return None;
        }
        if let Some(le) = le.filter(|le| number > *le) {
            let message = format!("Input should be less than or equal to {le}");
            self.errors.push(field_error(&field, &message, "less_than_equal"));
            return None;
        }
        Some(i64::try_from(number).unwrap_or(i64::MAX))
    }

    /// A text of at least one character.
    pub fn text(&mut self, name: &str) -> Option<String> {
        let value = self.values.get(name)?;
        if value.is_empty() {
            let error = field_error(&format!("query.{name}"), "String should have at least 1 character", "string_too_short");
            self.errors.push(error);
            return None;
        }
        Some(value.clone())
    }

    /// A boolean, false where absent.
    pub fn flag(&mut self, name: &str) -> bool {
        let Some(value) = self.values.get(name) else { return false };
        match value.to_ascii_lowercase().as_str() {
            "1" | "on" | "t" | "true" | "y" | "yes" => true,
            "0" | "off" | "f" | "false" | "n" | "no" => false,
            _ => {
                let error = field_error(
                    &format!("query.{name}"),
                    "Input should be a valid boolean, unable to interpret input",
                    "bool_parsing",
                );
                self.errors.push(error);
                false
            }
        }
    }

    /// Refused when any field failed.
    pub fn checked(self) -> Result<(), Refusal> {
        if self.errors.is_empty() { Ok(()) } else { Err(Refusal::Invalid(self.errors)) }
    }
}
