//! The auth plane names a caller as the Python one does (`rust/vectors/auth.json`, ADR 0217 §5).
//!
//! As in the recording, each verifier is handed one key where it would have fetched its issuer's.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header};
use lakehouse_core::auth::identity::Caller;
use lakehouse_core::auth::settings::AuthSettings;
use lakehouse_core::auth::verifier::{Authenticator, KeySource, SigningKey, Unavailable};
use serde_json::{Map, Value, json};
use std::pin::Pin;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

fn vectors() -> Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/auth.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn environment(env: &Value) -> Vec<(String, String)> {
    env.as_object()
        .unwrap()
        .iter()
        .map(|(name, value)| (name.clone(), value.as_str().unwrap().to_owned()))
        .collect()
}

/// The recorded key, for every issuer, under the recorded key id.
struct Recorded {
    secret: String,
    kid: String,
}

impl KeySource for Recorded {
    fn signing_key<'a>(&'a self, issuer: &'a str, kid: &'a str) -> Pin<Box<dyn Future<Output = Result<Option<SigningKey>, Unavailable>> + Send + 'a>> {
        let key = (kid == self.kid).then(|| SigningKey {
            key: DecodingKey::from_secret(self.secret.as_bytes()).unwrap(),
            algorithms: vec![Algorithm::HS256],
            issuer: issuer.to_owned(),
        });
        Box::pin(async move { Ok(key) })
    }
}

/// What the door answers `case`, in the shape the recording keeps.
async fn outcome(case: &Value, env: &Value, secret: &Value) -> Value {
    let settings = AuthSettings::from_environment(environment(env)).unwrap();
    let keys = Arc::new(Recorded {
        secret: secret["secret"].as_str().unwrap().to_owned(),
        kid: secret["kid"].as_str().unwrap().to_owned(),
    });
    let mut authenticator = Authenticator::new(&settings, keys.clone());
    let up = |verifier: &str| case["verifiers"].as_array().unwrap().iter().any(|name| name == verifier);
    if !up("idp") {
        authenticator.people = None;
    }
    if !up("accounts") {
        authenticator.accounts = None;
    }

    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
    let bearer = match case["claims"].as_object() {
        None => case["bearer"].as_str().map(str::to_owned),
        Some(claims) => {
            let at = |value: &Value| {
                value
                    .get("$now")
                    .and_then(Value::as_i64)
                    .map_or_else(|| value.clone(), |offset| json!(now + offset))
            };
            let resolved: Map<String, Value> = claims.iter().map(|(name, value)| (name.clone(), at(value))).collect();
            let mut header = Header::new(Algorithm::HS256);
            header.kid = case["kid"].as_str().map(str::to_owned);
            Some(jsonwebtoken::encode(&header, &resolved, &EncodingKey::from_secret(keys.secret.as_bytes()).unwrap()).unwrap())
        }
    };
    match authenticator.caller_of(bearer.as_deref(), now).await {
        Ok(None) => json!({"caller": null}),
        Ok(Some(caller)) => {
            let kind = if matches!(caller, Caller::Person { .. }) { "person" } else { "service" };
            json!({"caller": {"kind": kind, "key": caller.subject(), "fga_user": caller.principal().fga_user()}})
        }
        Err(refusal) => json!({"refused": {"name": refusal.name, "code": refusal.code as u8, "message": refusal.message}}),
    }
}

#[tokio::test]
async fn a_bearer_names_whom_python_says_it_names() {
    let vectors = vectors();
    let environments: Map<String, Value> = vectors["environments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| (entry["name"].as_str().unwrap().to_owned(), entry["env"].clone()))
        .collect();
    let cases = vectors["callers"].as_array().unwrap();
    let mut failures = Vec::new();
    for case in cases {
        let ours = outcome(case, &environments[case["env"].as_str().unwrap()], &vectors["secret"][0]).await;
        let theirs: Map<String, Value> = case
            .as_object()
            .unwrap()
            .iter()
            .filter(|(name, _)| *name == "caller" || *name == "refused")
            .map(|(name, value)| (name.clone(), value.clone()))
            .collect();
        if ours != Value::Object(theirs.clone()) {
            failures.push(format!("{}:\n    rust:   {ours}\n    python: {}", case["name"], Value::Object(theirs)));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} callers differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}

#[test]
fn the_settings_are_taken_where_python_takes_them() {
    let vectors = vectors();
    let cases = vectors["settings"].as_array().unwrap();
    let differing: Vec<String> = cases
        .iter()
        .filter_map(|case| {
            let ours = AuthSettings::from_environment(environment(&case["env"]));
            (ours.is_ok() != case["taken"].as_bool().unwrap()).then(|| format!("{}: rust {:?}, python taken={}", case["name"], ours.err(), case["taken"]))
        })
        .collect();
    assert!(
        differing.is_empty(),
        "{} of {} environments differ:\n{}",
        differing.len(),
        cases.len(),
        differing.join("\n")
    );
}
