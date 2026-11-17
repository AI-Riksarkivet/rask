//! The catalog decider sends the requests the Python decider sends and makes the same of each answer
//! (`rust/vectors/authz.json`, ADR 0217 §5). The decision door is an in-process server on a loopback port that answers
//! as the Python case's mock transport did and records every request.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::Router;
use axum::body::Bytes;
use axum::http::{HeaderMap, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use lakehouse_core::authz::AccessDecider;
use lakehouse_core::authz::catalog::CatalogDecider;
use serde_json::{Map, Value, json};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const UA: &str = "0190a1b2-0000-7000-8000-0000000000a1";
const UD: &str = "0190a1b2-0000-7000-8000-0000000000d4";
const UNS: &str = "0190a1b2-0000-7000-8000-0000000000ff";
const LOCATION: &str = "s3://lake/wh/raw/pages.lance";

#[derive(Default)]
struct Door {
    mode: String,
    sent: Vec<Value>,
}

fn texts(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|list| list.iter().map(|text| text.as_str().unwrap().to_owned()).collect())
        .unwrap_or_default()
}

fn non_empty(body: &Value, key: &str) -> Option<Vec<String>> {
    Some(texts(&body[key])).filter(|list| !list.is_empty())
}

/// What the Python case's door answered: object `n` allowed where `n` is even, a table known where its name ends in `b`.
fn answer(route: &str, body: &Value, mode: &str) -> Value {
    let mut asked = non_empty(body, "objects")
        .or_else(|| non_empty(body, "tables"))
        .or_else(|| non_empty(body, "locations"))
        .unwrap_or_default();
    if mode == "leaves an object out" && !asked.is_empty() {
        asked.remove(0);
    } else if mode == "answers one nobody asked" {
        asked.push("table:nobody".to_owned());
    }
    let each = |decide: &dyn Fn(usize, &String) -> Value| {
        asked
            .iter()
            .enumerate()
            .map(|(index, name)| (name.clone(), decide(index, name)))
            .collect::<Map<_, _>>()
    };
    if route.ends_with("/list-users") {
        json!({"users": ["dex~ada", "dex~bob"]})
    } else if route.ends_with("/locate") {
        json!({"located": each(&|_, location| if location == LOCATION { json!(UD) } else { Value::Null })})
    } else if route.ends_with("/governed") {
        json!({"governed": each(&|_, object| json!(object.ends_with('a')))})
    } else if non_empty(body, "tables").is_some() {
        let known = json!({"allowed": true, "table_uuid": UA, "namespace_uuid": UNS});
        json!({"tables": each(&|_, name| if name.ends_with('b') { known.clone() } else { json!({"allowed": false}) })})
    } else {
        json!({"allowed": each(&|index, _| json!(index % 2 == 0))})
    }
}

async fn serve(door: Arc<Mutex<Door>>) -> String {
    let app = Router::new().fallback(move |uri: Uri, headers: HeaderMap, body: Bytes| {
        let door = Arc::clone(&door);
        async move {
            let body: Value = serde_json::from_slice(&body).unwrap();
            let route = uri.path().to_owned();
            let mut door = door.lock().unwrap();
            let authorization = headers.get("authorization").map(|value| value.to_str().unwrap().to_owned());
            door.sent.push(json!({"route": route, "authorization": authorization, "body": body}));
            let response: Response = match door.mode.as_str() {
                "refuses the service" => (StatusCode::FORBIDDEN, axum::Json(json!({"error": "no decision role"}))).into_response(),
                "fails" => (StatusCode::INTERNAL_SERVER_ERROR, "boom").into_response(),
                "answers malformed" => (StatusCode::OK, "{").into_response(),
                mode => axum::Json(answer(&route, &body, mode)).into_response(),
            };
            response
        }
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    format!("http://{address}/")
}

#[tokio::test]
async fn the_catalog_decider_asks_and_answers_as_python() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/authz.json");
    let vectors: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let cases = vectors["catalog_decider"].as_array().unwrap();
    let token = std::env::temp_dir().join(format!("authz-catalog-token-{}", std::process::id()));
    std::fs::write(&token, "service-token\n").unwrap();
    let door = Arc::new(Mutex::new(Door::default()));
    let url = serve(Arc::clone(&door)).await;
    let decider = CatalogDecider::new(&url, Some(token.clone()), Duration::from_secs(5)).unwrap();
    let mut failures = Vec::new();
    for case in cases {
        {
            let mut door = door.lock().unwrap();
            door.mode = case["mode"].as_str().unwrap().to_owned();
            door.sent.clear();
        }
        let arguments = &case["arguments"];
        let (user, relation) = (
            arguments["user"].as_str().unwrap_or_default(),
            arguments["relation"].as_str().unwrap_or_default(),
        );
        let answered = match case["method"].as_str().unwrap() {
            "batch_check" => decider
                .batch_check(user, relation, &texts(&arguments["objects"]))
                .await
                .map(|allowed| json!(allowed)),
            "check" => decider
                .check(user, relation, arguments["obj"].as_str().unwrap())
                .await
                .map(|allowed| json!(allowed)),
            "check_tables" => decider.check_tables(user, relation, &texts(&arguments["tables"])).await.map(|decided| {
                let decided: Map<String, Value> = decided
                    .into_iter()
                    .map(|(name, decision)| {
                        let whole = json!({"allowed": decision.allowed, "table_uuid": decision.table_uuid, "namespace_uuid": decision.namespace_uuid});
                        (name, whole)
                    })
                    .collect();
                Value::Object(decided)
            }),
            "locate" => decider.locate(&texts(&arguments["locations"])).await.map(|located| json!(located)),
            "governed" => decider.governed(&texts(&arguments["objects"])).await.map(|governed| json!(governed)),
            _ => decider.list_users(relation, arguments["obj"].as_str().unwrap()).await.map(|users| json!(users)),
        };
        let outcome = match answered {
            Ok(answered) => json!({"answered": answered}),
            Err(unavailable) => json!({"refused": "ServiceUnavailableError", "message": unavailable.0}),
        };
        let sent = door.lock().unwrap().sent.clone();
        // Requests in the order sent, each member in its order: the Python writes a body in its fields' order. An answer
        // is a map, compared whatever its order.
        let (ours, theirs) = (json!(sent).to_string(), case["sent"].to_string());
        if ours != theirs || outcome != case["outcome"] {
            let (ours, theirs) = (format!("{ours} {outcome}"), format!("{theirs} {}", case["outcome"]));
            failures.push(format!(
                "{} / {}\n    rust:   {ours:.600}\n    python: {theirs:.600}",
                case["method"], case["mode"]
            ));
        }
    }
    std::fs::remove_file(&token).unwrap();
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}
