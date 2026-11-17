//! Lineage's doors answer what the Python app answers (`rust/vectors/doors.json`, ADR 0236).
//!
//! As in the recording, the collaborators are stand-ins: one recorded key for the identity provider, a decider answering
//! from the recorded grants, the catalog's names from the recorded table, no outbox or bus, and a provenance store whose
//! catalog does not answer.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::body::{Body, Bytes};
use axum::http::Request;
use http_body_util::{BodyExt, Full};
use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header};
use lakehouse_core::auth::settings::AuthSettings;
use lakehouse_core::auth::verifier::{Authenticator, KeySource, SigningKey, Unavailable as KeysDown};
use lakehouse_core::authz::{AccessDecider, Answer, TableDecision};
use lakehouse_core::events::DatasetUuids;
use lakehouse_core::service::health::HealthWatch;
use lakehouse_core::service::lifecycle::Lifecycle;
use lakehouse_core::service::shell::Shell;
use lakehouse_tables::Catalog;
use lakehouse_tables::bearer::anonymous;
use lineage::app::router;
use lineage::authz::gates::Names;
use lineage::signature::Signing;
use lineage::state::{Resolver, State};
use lineage::store::Store;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::pin::Pin;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tower::ServiceExt;

fn vectors() -> Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/doors.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

struct Recorded(String);

impl KeySource for Recorded {
    fn signing_key<'a>(&'a self, issuer: &'a str, _: &'a str) -> Pin<Box<dyn Future<Output = Result<Option<SigningKey>, KeysDown>> + Send + 'a>> {
        let key = SigningKey {
            key: DecodingKey::from_secret(self.0.as_bytes()).unwrap(),
            algorithms: vec![Algorithm::HS256],
            issuer: issuer.to_owned(),
        };
        Box::pin(async move { Ok(Some(key)) })
    }
}

/// Answers from the recorded grants: `<relation> <object>` per subject.
struct Grants(Value);

impl Grants {
    fn allowed(&self, user: &str, relation: &str, object: &str) -> bool {
        let pair = format!("{relation} {object}");
        self.0[user].as_array().is_some_and(|held| held.iter().any(|held| held == &json!(pair)))
    }
}

impl AccessDecider for Grants {
    fn resolves_names(&self) -> bool {
        false
    }
    fn check<'a>(&'a self, user: &'a str, relation: &'a str, object: &'a str) -> Answer<'a, bool> {
        Box::pin(async move { Ok(self.allowed(user, relation, object)) })
    }
    fn batch_check<'a>(&'a self, user: &'a str, relation: &'a str, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>> {
        Box::pin(async move { Ok(objects.iter().map(|object| (object.clone(), self.allowed(user, relation, object))).collect()) })
    }
    fn check_tables<'a>(&'a self, _: &'a str, _: &'a str, _: &'a [String]) -> Answer<'a, BTreeMap<String, TableDecision>> {
        panic!("names are described, never decided, here")
    }
    fn list_users<'a>(&'a self, _: &'a str, _: &'a str) -> Answer<'a, Vec<String>> {
        Box::pin(async { Ok(Vec::new()) })
    }
    fn governed<'a>(&'a self, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>> {
        Box::pin(async move { Ok(objects.iter().map(|object| (object.clone(), true)).collect()) })
    }
    fn locate<'a>(&'a self, locations: &'a [String]) -> Answer<'a, BTreeMap<String, Option<String>>> {
        Box::pin(async move { Ok(locations.iter().map(|location| (location.clone(), None)).collect()) })
    }
}

/// The catalog's names, from the recorded tables and namespaces.
struct Catalogued(Value);

impl Names for Catalogued {
    fn table<'a>(&'a self, name: &'a str, _: Option<&'a str>) -> Answer<'a, DatasetUuids> {
        let held = &self.0["tables"][name];
        let uuids = DatasetUuids {
            table_uuid: held[0].as_str().map(str::to_owned),
            namespace_uuid: held[1].as_str().map(str::to_owned),
        };
        Box::pin(async move { Ok(uuids) })
    }
}

impl Resolver for Catalogued {
    fn namespace<'a>(&'a self, name: &'a str, _: Option<&'a str>) -> Answer<'a, Option<String>> {
        Box::pin(async move { Ok(self.0["namespaces"][name].as_str().map(str::to_owned)) })
    }
}

fn shell_and_state(stand_ins: &Value) -> (Shell, State) {
    let env = stand_ins["env"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(name, value)| (name.clone(), value.as_str().unwrap().to_owned()));
    let settings = AuthSettings::from_environment(env).unwrap();
    let authenticator = Authenticator::new(&settings, Arc::new(Recorded(stand_ins["secret"].as_str().unwrap().to_owned())));
    let catalog = stand_ins["env"]["LINEAGE_CATALOG_DATASET_NAMESPACE"].as_str().unwrap().to_owned();
    let unanswered = Catalog::new("http://127.0.0.1:9", anonymous(), Duration::from_secs(5)).unwrap();
    let store = Store::new(unanswered, vec!["lance_catalog".to_owned(), "provenance".to_owned()], &catalog, true).unwrap();
    let state = State {
        authenticator: Arc::new(authenticator),
        fga_enabled: true,
        decider: Some(Arc::new(Grants(stand_ins["grants"].clone()))),
        catalog,
        root: stand_ins["env"]["RASK_FGA_ROOT_OBJECT"].as_str().unwrap().to_owned(),
        store,
        names: Arc::new(Catalogued(stand_ins.clone())),
        outbox: None,
        publisher: None,
        keys: Arc::new(BTreeMap::<String, Vec<String>>::new()),
        signing: Signing::default(),
    };
    let lifecycle = Lifecycle::default();
    lifecycle.mark_started();
    let shell = Shell {
        lifecycle: Arc::new(lifecycle),
        health: Arc::new(HealthWatch::new(Vec::new())),
        max_body_bytes: stand_ins["env"]["LINEAGE_MAX_BODY_BYTES"].as_str().unwrap().parse().unwrap(),
        request_deadline: Duration::from_secs(300),
    };
    (shell, state)
}

/// A header's value as sent: `@<subject>` is a bearer the recorded key signs for the subject, valid now or expired.
fn sent(value: &str, secret: &str) -> String {
    let Some(subject) = value.strip_prefix('@') else { return value.to_owned() };
    let (subject, expired) = subject.split_once(':').map_or((subject, false), |(subject, _)| (subject, true));
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
    let claims = json!({"iss": "https://idp.example", "sub": subject, "aud": "lance", "exp": now + if expired { -300 } else { 300 }, "iat": now - 10});
    let mut header = Header::new(Algorithm::HS256);
    header.kid = Some("k1".to_owned());
    format!(
        "Bearer {}",
        jsonwebtoken::encode(&header, &claims, &EncodingKey::from_secret(secret.as_bytes()).unwrap()).unwrap()
    )
}

/// What differs between the Rust doors' answer to `case` and the Python app's, as lines to print.
async fn differences(case: &Value, shell: &Shell, state: &State, secret: &str) -> Vec<String> {
    let mut request = Request::builder().method(case["method"].as_str().unwrap()).uri(case["path"].as_str().unwrap());
    for (name, value) in case["headers"].as_object().unwrap() {
        request = request.header(name, sent(value.as_str().unwrap(), secret));
    }
    let bytes = match &case["body"] {
        Value::String(text) => {
            request = request.header("content-length", text.len());
            text.as_bytes().to_vec()
        }
        Value::Object(sized) => {
            let size = sized["size"].as_u64().unwrap() as usize;
            if sized["declared"].as_bool().unwrap() {
                request = request.header("content-length", size);
            }
            vec![b'x'; size]
        }
        _ => Vec::new(),
    };
    let body = Body::new(Full::new(Bytes::from(bytes)));
    let answer = shell.router(router(state.clone())).oneshot(request.body(body).unwrap()).await.unwrap();
    let mut differing = Vec::new();
    let mut check = |what: &str, ours: String, theirs: String| {
        if ours != theirs {
            differing.push(format!("{what}\n    rust:   {ours}\n    python: {theirs}"));
        }
    };
    check("status", answer.status().as_u16().to_string(), case["status"].to_string());
    // A GET door answers HEAD here and its `Allow` says so; the Python doors refuse HEAD (ADR 0219).
    let header = |name: &str| Some(answer.headers().get(name)?.to_str().unwrap().replace(",HEAD", ""));
    let answered: Map<String, Value> = ["content-type", "cache-control", "vary", "allow", "location"]
        .iter()
        .filter_map(|name| Some(((*name).to_owned(), json!(header(name)?))))
        .collect();
    check("headers", Value::Object(answered).to_string(), case["answered"].to_string());
    let mut ours: Value = serde_json::from_slice(&answer.into_body().collect().await.unwrap().to_bytes()).unwrap_or(Value::Null);
    let instance = ours.as_object_mut().and_then(|body| body.remove("instance"));
    check("an instance is named", instance.is_some().to_string(), case["instance"].to_string());
    // Members in the order each is answered: the text of each, not the equality of two maps.
    check("body", ours.to_string(), case["answer"].to_string());
    differing
}

#[tokio::test]
async fn the_doors_answer_as_the_python_app_does() {
    let vectors = vectors();
    let stand_ins = &vectors["stand_ins"][0];
    let (shell, state) = shell_and_state(stand_ins);
    let secret = stand_ins["secret"].as_str().unwrap();
    let cases = vectors["exchanges"].as_array().unwrap();
    let mut failures = Vec::new();
    for case in cases {
        let differing = differences(case, &shell, &state, secret).await;
        if !differing.is_empty() {
            failures.push(format!("{}:\n  {}", case["name"], differing.join("\n  ")));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} exchanges differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}
