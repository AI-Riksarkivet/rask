//! The outbox and the store session do what the Python ones do (`rust/vectors/outbox.json`, ADR 0234): the pure
//! decisions case by case, and every exchange and session scenario step by step through a local STS that gives the
//! recorded answers in order and records each request's form.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::Router;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::post;
use chrono::DateTime;
use lakehouse_core::auth::issuer::Clock;
use lakehouse_core::outbox::{Location, Producing, Staging, backlog, in_order, publish_with_outbox};
use lakehouse_core::storage::session::{BOOT_ATTEMPTS, BOOT_DELAY, Refreshed, Unissued};
use lakehouse_core::storage::sigv4::{Request, encode_key, sign};
use lakehouse_core::storage::{KeyPair, Listed, Said, StorageError, StoreSession, StoreSettings, classify};
use percent_encoding::percent_decode_str;
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/outbox.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap()
}

#[test]
fn an_event_is_staged_and_dropped_at_the_object_python_names() {
    let cases = vectors("object_keys");
    for case in &cases {
        let location = Location::parse(text(&case["uri"])).unwrap();
        let object = location.object_key(text(&case["key"]));
        for call in case["calls"].as_array().unwrap() {
            assert_eq!((location.bucket.as_str(), object.as_str()), (text(&call[1]), text(&call[2])), "{case}");
        }
    }
    assert_eq!(cases.len(), 12);
    assert!(Location::parse("/var/outbox").is_err(), "a local outbox is no object store's");
}

#[test]
fn a_key_stands_in_a_path_as_boto3_puts_it() {
    for case in vectors("urls") {
        let url = format!("http://store.test:9000/{}/{}", text(&case["bucket"]), encode_key(text(&case["key"])));
        assert_eq!(url, text(&case["url"]), "{case}");
    }
}

#[test]
fn the_relay_lists_the_staged_events_oldest_first_as_python_does() {
    let cases = vectors("listings");
    for case in &cases {
        // A directory pyarrow lists is a common prefix to S3, which a listing's contents never hold.
        let listing: Vec<Listed> = case["objects"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|object| object["directory"] == false)
            .map(|object| Listed {
                key: text(&object["key"]).to_owned(),
                modified_ns: object["modified_ns"].as_i64().unwrap(),
            })
            .collect();
        let limit = case["limit"].as_u64().map(|limit| usize::try_from(limit).unwrap());
        let keys: Vec<String> = in_order(&listing, limit).into_iter().map(|(key, _)| key).collect();
        let listed: Vec<&str> = case["listed"].as_array().unwrap().iter().map(|pair| text(&pair[0])).collect();
        assert_eq!(keys, listed, "{}", case["name"]);
        let (depth, age) = backlog(&listing, case["now_ns"].as_i64().unwrap());
        assert_eq!(
            (json!(depth), json!(age)),
            (case["depth"].clone(), case["oldest_age_seconds"].clone()),
            "{}",
            case["name"]
        );
    }
    assert_eq!(cases.len(), 7);
}

struct Producer<'a> {
    case: &'a Value,
    effects: Vec<&'static str>,
}

impl Producing for Producer<'_> {
    async fn stage(&mut self) -> Result<(), String> {
        self.effects.push("stage");
        if self.case["staging"] == "fails" {
            Err("the store did not answer".to_owned())
        } else {
            Ok(())
        }
    }

    async fn publish(&mut self) -> Result<(), String> {
        self.effects.push("publish");
        if self.case["publishing"] == "fails" {
            Err("TimeoutError".to_owned())
        } else {
            Ok(())
        }
    }

    async fn drop_staged(&mut self) -> Result<(), String> {
        self.effects.push("drop");
        if self.case["dropping"] == "fails" {
            Err("the store did not answer".to_owned())
        } else {
            Ok(())
        }
    }
}

#[tokio::test]
async fn the_producer_stages_publishes_and_drops_in_python_s_order() {
    let cases = vectors("publishes");
    for case in &cases {
        let mut producer = Producer { case, effects: Vec::new() };
        let outcome = publish_with_outbox(case["configured"] == true, &mut producer).await;
        assert_eq!(json!(producer.effects), case["effects"], "{case}");
        let staging = match &outcome {
            Ok(published) => published.staging,
            Err(unpublished) => unpublished.staging,
        };
        // What Python counts is what the outcome says: the stage's result, the publish's, and a drop that failed.
        let mut counted = match staging {
            Staging::Off => vec![],
            Staging::Staged => vec!["record_staged"],
            Staging::Failed => vec!["record_stage_failed"],
        };
        match &outcome {
            Ok(published) => {
                assert!(case["raised"].is_null(), "{case}");
                counted.push("record_published");
                if published.dropped == Some(false) {
                    counted.push("record_drop_failed");
                }
            }
            Err(unpublished) => {
                assert_eq!(json!(unpublished.staging == Staging::Staged), case["raised"]["staged"], "{case}");
                assert_eq!(json!(unpublished.left_staged()), case["raised"]["left_staged"], "{case}");
                if unpublished.staging == Staging::Staged {
                    counted.push("record_publish_failed");
                }
            }
        }
        assert_eq!(json!(counted), case["counted"], "{case}");
    }
    assert_eq!(cases.len(), 16);
}

#[test]
fn a_store_s_refusal_is_classified_as_python_classifies_it() {
    let cases = vectors("errors");
    for case in &cases {
        let status = u16::try_from(case["status"].as_u64().unwrap()).unwrap();
        let classified = classify(status, text(&case["code"]), "bucket", case["key"].as_str());
        let expected = match (text(&case["created"]), text(&case["found"])) {
            ("exists", _) => "exists",
            (_, found) => found,
        };
        let got = match classified {
            StorageError::BucketNotFound { .. } => "bucket_not_found",
            StorageError::ObjectNotFound { .. } => "object_not_found",
            StorageError::Exists { .. } => "exists",
            _ => "refused",
        };
        assert_eq!(got, expected, "{case}");
    }
    assert_eq!(cases.len(), 24);
}

#[test]
fn a_request_is_signed_as_botocore_signs_it() {
    let cases = vectors("signatures");
    for case in &cases {
        let url = reqwest::Url::parse(text(&case["url"])).unwrap();
        let host = url
            .port()
            .map_or_else(|| url.host_str().unwrap().to_owned(), |port| format!("{}:{port}", url.host_str().unwrap()));
        let query: Vec<(&str, &str)> = case["query"].as_array().unwrap().iter().map(|pair| (text(&pair[0]), text(&pair[1]))).collect();
        let headers: Vec<(String, &str)> = case["headers"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(name, value)| (name.to_lowercase(), text(value)))
            .collect();
        let headers: Vec<(&str, &str)> = headers.iter().map(|(name, value)| (name.as_str(), *value)).collect();
        let request = Request {
            method: text(&case["method"]),
            host: &host,
            path: url.path(),
            query: &query,
            headers: &headers,
            body: text(&case["body"]).as_bytes(),
        };
        let key_pair = KeyPair::new("fixture-access", "fixture-secret", case["token"].as_str());
        let at = DateTime::parse_from_rfc3339(text(&case["at"])).unwrap().to_utc();
        let signed: Map<String, Value> = sign(&request, &key_pair, text(&case["region"]), at)
            .into_iter()
            .map(|(name, value)| (name.to_owned(), json!(value)))
            .chain(headers.iter().map(|(name, value)| ((*name).to_owned(), json!(value))))
            .collect();
        assert_eq!(Value::Object(signed), case["signed"], "{case}");
    }
    assert_eq!(cases.len(), 7);
}

#[test]
fn the_store_identity_is_read_and_refused_as_python_reads_it() {
    let cases = vectors("settings");
    for case in &cases {
        let environment = case["environment"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(name, value)| (name.clone(), text(value).to_owned()));
        let read = StoreSettings::from_environment("LINEAGE", environment);
        assert_eq!(read.is_err(), case["refused"] == true, "{case}: {read:?}");
        let Ok(settings) = read else { continue };
        let identity = &settings.identity;
        let seen = json!({
            "token_file": identity.token_file.to_str().unwrap(), "sts_endpoint": identity.sts_endpoint, "region": identity.region,
            "role_arn": identity.role_arn, "session_seconds": identity.session_seconds,
        });
        let python = json!({
            "token_file": case["token_file"], "sts_endpoint": case["sts_endpoint"], "region": case["region"],
            "role_arn": case["role_arn"], "session_seconds": case["session_seconds"],
        });
        assert_eq!(seen, python, "{case}");
        // Where Python names no endpoint, boto3 resolves the region's own; the Rust names it.
        let endpoint = case["endpoint"]
            .as_str()
            .map_or_else(|| format!("https://s3.{}.amazonaws.com", identity.region), str::to_owned);
        assert_eq!(settings.endpoint, endpoint, "{case}");
    }
    assert_eq!(cases.len(), 13);
}

/// A local STS: answers in order, each request's form recorded.
#[derive(Clone, Default)]
struct Sts {
    answers: Arc<Mutex<VecDeque<(u16, String)>>>,
    asked: Arc<Mutex<Vec<BTreeMap<String, String>>>>,
}

async fn answer(State(sts): State<Sts>, body: String) -> (StatusCode, String) {
    let form = body
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .map(|(name, value)| (name.to_owned(), percent_decode_str(value).decode_utf8().unwrap().into_owned()))
        .collect();
    sts.asked.lock().unwrap().push(form);
    let (status, body) = sts.answers.lock().unwrap().pop_front().expect("an answer for every request");
    (StatusCode::from_u16(status).unwrap(), body)
}

impl Sts {
    async fn start(answers: &Value) -> (Sts, String) {
        let sts = Sts::default();
        sts.answers.lock().unwrap().extend(
            answers
                .as_array()
                .unwrap()
                .iter()
                .map(|given| (u16::try_from(given["status"].as_u64().unwrap()).unwrap(), text(&given["body"]).to_owned())),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = Router::new().route("/", post(answer)).with_state(sts.clone());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        (sts, format!("http://{address}"))
    }

    fn take_asked(&self) -> Value {
        json!(std::mem::take(&mut *self.asked.lock().unwrap()))
    }
}

fn settings(endpoint: &str, token_file: &std::path::Path, seconds: u64) -> StoreSettings {
    let environment = [
        ("LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE", token_file.to_str().unwrap().to_owned()),
        ("LINEAGE_S3_STS_ENDPOINT", endpoint.to_owned()),
        ("LINEAGE_S3_SESSION_SECONDS", seconds.to_string()),
    ];
    StoreSettings::from_environment("LINEAGE", environment.map(|(name, value)| (name.to_owned(), value))).unwrap()
}

fn scratch(name: &str) -> std::path::PathBuf {
    let directory = std::env::temp_dir().join(format!("rask-outbox-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    directory.join("token")
}

#[tokio::test]
async fn an_exchange_asks_and_takes_as_python_s() {
    let cases = vectors("exchanges");
    for (n, case) in cases.iter().enumerate() {
        let answers = if case["answer"].is_null() { json!([]) } else { json!([case["answer"]]) };
        let (sts, endpoint) = Sts::start(&answers).await;
        let token_file = scratch(&format!("exchange-{n}"));
        let _ = std::fs::remove_file(&token_file);
        if let Some(token) = case["token"].as_str() {
            std::fs::write(&token_file, token).unwrap();
        }
        let session = StoreSession::new(&settings(&endpoint, &token_file, 3600), "lineage").unwrap();
        let outcome = match session.exchange().await {
            Ok(key_pair) => json!({"access_key_id": key_pair.access_key_id}),
            Err(Unissued::Token(_)) => json!({"failed": "token"}),
            Err(Unissued::Refused { .. }) => json!({"failed": "ClientError"}),
            Err(other) => panic!("{other:?}"),
        };
        let mut python = case["outcome"].clone();
        python.as_object_mut().unwrap().remove("expiration");
        assert_eq!(outcome, python, "{}", case["name"]);
        assert_eq!(sts.take_asked(), case["asked"], "{}", case["name"]);
    }
    assert_eq!(cases.len(), 6);
}

#[tokio::test]
async fn a_session_is_held_renewed_and_said_as_python_s() {
    let cases = vectors("sessions");
    for (n, case) in cases.iter().enumerate() {
        let (sts, endpoint) = Sts::start(&case["answers"]).await;
        let token_file = scratch(&format!("session-{n}"));
        std::fs::write(&token_file, "projected-1").unwrap();
        let base = Instant::now();
        let offset = Arc::new(Mutex::new(Duration::ZERO));
        let read = Arc::clone(&offset);
        let clock: Clock = Arc::new(move || base + *read.lock().unwrap());
        let seconds = case["seconds"].as_u64().unwrap();
        let session = StoreSession::new(&settings(&endpoint, &token_file, seconds), "lineage")
            .unwrap()
            .with_clock(clock)
            .with_boot_delay(Duration::ZERO);
        for step in case["steps"].as_array().unwrap() {
            let said: Vec<&str> = step["said"].as_array().unwrap().iter().map(text).collect();
            match text(&step["op"]) {
                "boot" => {
                    let slept = step["slept"].as_array().unwrap();
                    assert!(slept.iter().all(|wait| wait.as_f64() == Some(BOOT_DELAY.as_secs_f64())), "{step}");
                    match session.exchange_at_boot().await {
                        Ok(attempts) => {
                            assert_eq!(step["booted"], true, "{step}");
                            assert_eq!(attempts as usize, slept.len() + 1, "{step}");
                        }
                        Err(_) => {
                            assert_eq!(step["booted"], false, "{step}");
                            assert_eq!(BOOT_ATTEMPTS as usize, slept.len() + 1, "{step}");
                        }
                    }
                    assert!(
                        said.iter().all(|event| *event == "store_session_boot_retry") && said.len() == slept.len(),
                        "{step}"
                    );
                }
                "pass" => {
                    let Refreshed { renewed, said: rust_said } = session.refresh_if_due().await;
                    assert_eq!(json!(renewed), step["renewed"], "{}: {step}", case["name"]);
                    let rust_said: Vec<&str> = rust_said
                        .map(|said| match said {
                            Said::RenewalFailed => "store_session_renewal_failed",
                            Said::Expired => "store_session_expired",
                        })
                        .into_iter()
                        .collect();
                    assert_eq!(rust_said, said, "{}: {step}", case["name"]);
                }
                "advance" => *offset.lock().unwrap() += Duration::from_secs(step["seconds"].as_u64().unwrap()),
                "token" => match step["text"].as_str() {
                    Some(token) => std::fs::write(&token_file, token).unwrap(),
                    None => std::fs::remove_file(&token_file).unwrap(),
                },
                "held" => {
                    let held = session.held().map(|key_pair| key_pair.access_key_id).ok();
                    assert_eq!(json!(held), step["held"], "{}: {step}", case["name"]);
                }
                other => panic!("no step {other}"),
            }
            assert_eq!(sts.take_asked(), step["asked"], "{}: {step}", case["name"]);
        }
    }
    assert_eq!(cases.len(), 8);
}
