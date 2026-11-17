//! What a token's claims must say before anything is read from them.

use serde_json::{Map, Value};

/// The claims of a token whose signature and claims both held.
#[derive(Debug, Clone, PartialEq)]
pub struct IdToken {
    pub iss: String,
    pub sub: String,
    pub exp: i64,
    pub iat: i64,
    /// Every claim the token carries, these among them.
    pub claims: Map<String, Value>,
}

impl IdToken {
    /// The claim `name`, where it is a non-empty string.
    pub fn text(&self, name: &str) -> Option<&str> {
        self.claims.get(name).and_then(Value::as_str).filter(|value| !value.is_empty())
    }
}

/// What a verifier holds a token's claims to.
#[derive(Debug, Clone, Copy)]
pub struct Expected<'a> {
    pub audience: &'a str,
    /// The issuer as it names itself, which the token must name exactly.
    pub issuer: &'a str,
    /// How far two clocks may disagree, in seconds.
    pub leeway: i64,
}

/// A time claim: a whole number of seconds, as a JSON number.
fn seconds(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_f64().filter(|seconds| seconds.fract() == 0.0).map(|seconds| seconds as i64))
}

/// The claims of `payload`, where at `now` they are those of a token to take: unexpired, already valid, for this
/// audience, from this issuer, and naming a subject.
pub fn judge(payload: &Map<String, Value>, now: i64, expected: Expected) -> Option<IdToken> {
    let iat = seconds(payload.get("iat")?)?;
    if iat > now + expected.leeway {
        return None;
    }
    if let Some(not_before) = payload.get("nbf") {
        // Where the others are whole seconds, a start time may carry a fraction: it is read as the second it falls in.
        if not_before.as_f64()?.trunc() as i64 > now + expected.leeway {
            return None;
        }
    }
    let exp = seconds(payload.get("exp")?)?;
    if exp <= now - expected.leeway {
        return None;
    }
    let iss = payload.get("iss")?.as_str().filter(|issuer| *issuer == expected.issuer)?;
    let audiences = match payload.get("aud")? {
        Value::String(audience) => vec![audience.as_str()],
        Value::Array(audiences) => audiences.iter().map(Value::as_str).collect::<Option<Vec<_>>>()?,
        _ => return None,
    };
    if !audiences.contains(&expected.audience) {
        return None;
    }
    let sub = payload.get("sub")?.as_str()?;
    if payload.get("jti").is_some_and(|id| !id.is_string()) {
        return None;
    }
    Some(IdToken {
        iss: iss.to_owned(),
        sub: sub.to_owned(),
        exp,
        iat,
        claims: payload.clone(),
    })
}
