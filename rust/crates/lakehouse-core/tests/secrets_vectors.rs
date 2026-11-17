//! The session with the secret store, and the published keys, do what the Python ones do (`rust/vectors/secrets.json`,
//! ADR 0230): the pure decisions case by case, and every scripted scenario step by step, through a local server that
//! gives the recorded answers in order and records what it was asked.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::auth::issuer::Clock;
use lakehouse_core::secrets::decide::{self, SecretStoreSettings};
use lakehouse_core::secrets::published::{Fetch, PublishedKeys};
use lakehouse_core::secrets::{Budget, Refused, SecretStore, Unanswered, read_required_secret, signing_public_secret};
use lakehouse_core::signing::PublicKeySource;
use serde_json::{Value, json};
use std::collections::{BTreeMap, VecDeque};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/secrets.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

/// A clock the test moves, starting at the Python script's 1000 s.
fn manual_clock() -> (Clock, Arc<Mutex<f64>>) {
    let base = Instant::now();
    let now = Arc::new(Mutex::new(1000.0));
    let read = Arc::clone(&now);
    (Arc::new(move || base + Duration::from_secs_f64(*read.lock().unwrap())), now)
}

#[test]
fn the_settings_are_taken_and_refused_as_python_takes_them() {
    for case in vectors("settings") {
        let env: Vec<(String, String)> = case["env"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
            .collect();
        let taken = SecretStoreSettings::from_environment(env);
        assert_eq!(taken.is_ok(), case["taken"], "{case}");
        let Ok(settings) = taken else { continue };
        let fields = json!({"address": settings.address, "role": settings.role, "auth_mount": settings.auth_mount, "kv_mount": settings.kv_mount});
        assert_eq!(fields, case["fields"], "{case}");
        assert_eq!(settings.configured(), case["configured"], "{case}");
        for (name, path) in [("token_file", &settings.token_file), ("ca_file", &settings.ca_file)] {
            let recorded = &case["paths"][name];
            assert_eq!(path.is_some(), !recorded.is_null(), "{name} {case}");
            if let (Some(path), Some(normal)) = (path, recorded["normal"].as_str()) {
                assert_eq!(path, &PathBuf::from(normal), "{case}");
            }
        }
    }
}

#[test]
fn a_login_answer_and_a_read_answer_are_taken_as_python_takes_them() {
    for case in vectors("logins") {
        let issued = decide::parse_login(case["answer"].as_str().unwrap().as_bytes());
        assert_eq!(issued.is_some(), case["taken"], "{case}");
        if let Some(issued) = issued {
            assert_eq!(issued.lease_seconds, case["lease"], "{case}");
            assert_eq!(issued.token, decide::StoreToken::new(case["token"].as_str().unwrap()), "{case}");
        }
    }
    for case in vectors("reads") {
        let fields = decide::parse_read(case["answer"].as_str().unwrap().as_bytes());
        assert_eq!(fields.map(|fields| json!(fields)).unwrap_or(Value::Null), case["fields"], "{case}");
    }
}

#[test]
fn the_lease_names_and_backoff_are_python_s() {
    for case in vectors("leases") {
        let (renew_after, expires) = decide::lease_offsets(case["lease"].as_i64().unwrap());
        let asked = case["asked"].as_f64().unwrap();
        assert!(
            (asked + renew_after.as_secs_f64() - case["renew_after"].as_f64().unwrap()).abs() < 1e-9,
            "{case}"
        );
        assert!((asked + expires.as_secs_f64() - case["expires"].as_f64().unwrap()).abs() < 1e-9, "{case}");
    }
    for case in vectors("secret_names") {
        assert_eq!(decide::is_secret_name(case["name"].as_str().unwrap()), case["taken"], "{case}");
    }
    for case in vectors("fits_a_header") {
        assert_eq!(decide::fits_a_header(case["text"].as_str().unwrap()), case["fits"], "{case}");
    }
    for case in vectors("backoff") {
        let wait = decide::backoff_seconds(
            case["attempt"].as_u64().unwrap() as u32,
            case["initial"].as_f64().unwrap(),
            case["draw"].as_f64().unwrap(),
        );
        assert!((wait - case["wait"].as_f64().unwrap()).abs() < 1e-12, "{wait} {case}");
    }
    for case in vectors("public_secrets") {
        assert_eq!(signing_public_secret(case["identity"].as_str().unwrap()), case["secret"], "{case}");
    }
}

/// A store that gives the scripted answers in order, one per connection, and records each request. An answer marked
/// down, or one past the script, closes the connection unanswered.
struct Scripted {
    answers: Mutex<VecDeque<Value>>,
    asked: Mutex<Vec<Value>>,
}

async fn serve(scripted: Arc<Scripted>) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        loop {
            let (stream, _) = listener.accept().await.unwrap();
            answer(stream, &scripted).await;
        }
    });
    port
}

async fn answer(mut stream: TcpStream, scripted: &Scripted) {
    let mut bytes = Vec::new();
    let mut chunk = [0u8; 4096];
    let head_end = loop {
        let read = stream.read(&mut chunk).await.unwrap();
        if read == 0 {
            return;
        }
        bytes.extend_from_slice(&chunk[..read]);
        if let Some(end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
            break end + 4;
        }
    };
    let head = String::from_utf8(bytes[..head_end].to_vec()).unwrap();
    let mut lines = head.split("\r\n");
    let mut request_line = lines.next().unwrap().split(' ');
    let (method, path) = (request_line.next().unwrap().to_owned(), request_line.next().unwrap().to_owned());
    let headers: BTreeMap<String, String> = lines
        .filter_map(|line| line.split_once(": "))
        .map(|(name, value)| (name.to_lowercase(), value.to_owned()))
        .collect();
    let length: usize = headers.get("content-length").map_or(0, |length| length.parse().unwrap());
    while bytes.len() < head_end + length {
        let read = stream.read(&mut chunk).await.unwrap();
        bytes.extend_from_slice(&chunk[..read]);
    }
    let body = (length > 0).then(|| serde_json::from_slice::<Value>(&bytes[head_end..]).unwrap());
    scripted
        .asked
        .lock()
        .unwrap()
        .push(json!({"method": method, "path": path, "token": headers.get("x-vault-token"), "body": body}));
    let next = scripted.answers.lock().unwrap().pop_front().unwrap_or(json!({"down": true}));
    if next.get("down").is_some() {
        return;
    }
    let content = next
        .get("text")
        .map_or_else(|| next["body"].to_string(), |text| text.as_str().unwrap().to_owned());
    let location = next
        .get("location")
        .map_or(String::new(), |location| format!("Location: {}\r\n", location.as_str().unwrap()));
    let status = next["status"].as_u64().unwrap();
    let response = format!(
        "HTTP/1.1 {status} X\r\nContent-Length: {}\r\nConnection: close\r\n{location}\r\n{content}",
        content.len()
    );
    stream.write_all(response.as_bytes()).await.unwrap();
    stream.shutdown().await.unwrap();
}

fn fields(fields: BTreeMap<String, String>) -> Value {
    json!({"fields": fields})
}

async fn replay(scenario: &Value) {
    let name = scenario["name"].as_str().unwrap();
    let scripted = Arc::new(Scripted {
        answers: Mutex::new(scenario["answers"].as_array().unwrap().iter().cloned().collect()),
        asked: Mutex::default(),
    });
    let port = serve(Arc::clone(&scripted)).await;
    let directory = std::env::temp_dir().join(format!("rask-secrets-{}-{port}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    let token = directory.join("token");
    std::fs::write(&token, "fixture-projected-1\n").unwrap();
    let overrides = &scenario["settings"];
    let address = overrides["RASK_SECRET_STORE_ADDR"].as_str().unwrap_or("http://store.test:8200/");
    let path = address.splitn(4, '/').nth(3).unwrap_or("");
    let mut env: BTreeMap<String, String> = [
        ("RASK_SECRET_STORE_ROLE", "lineage".to_owned()),
        ("RASK_SECRET_STORE_TOKEN_FILE", token.display().to_string()),
    ]
    .into_iter()
    .map(|(name, value)| (name.to_owned(), value))
    .collect();
    for (name, value) in overrides.as_object().unwrap() {
        env.insert(name.clone(), value.as_str().unwrap().to_owned());
    }
    env.insert("RASK_SECRET_STORE_ADDR".to_owned(), format!("http://127.0.0.1:{port}/{path}"));
    let (clock, now) = manual_clock();
    let store = SecretStore::new(SecretStoreSettings::from_environment(env).unwrap())
        .unwrap()
        .with_clock(clock)
        .with_jitter(Arc::new(|| 0.0));
    let budget = |step: &Value| Budget {
        timeout: Duration::from_secs(5),
        retries: step["retries"].as_u64().unwrap() as u32,
        backoff: Duration::ZERO,
    };
    for (index, step) in scenario["steps"].as_array().unwrap().iter().enumerate() {
        let asked_before = scripted.asked.lock().unwrap().len();
        let mut said = Vec::new();
        let outcome = match step["op"].as_str().unwrap() {
            "advance" => {
                *now.lock().unwrap() += step["seconds"].as_f64().unwrap();
                Value::Null
            }
            "rotate" => {
                std::fs::write(&token, step["token"].as_str().unwrap()).unwrap();
                Value::Null
            }
            "remove_token" => {
                std::fs::remove_file(&token).unwrap();
                Value::Null
            }
            "log_in" => json!(if store.log_in(budget(step)).await.is_ok() { "logged_in" } else { "refused" }),
            "pass" => {
                let pass = store.keep_logged_in(Duration::from_secs(5)).await;
                said = pass.said.iter().map(|said| said.event()).collect();
                json!(if pass.logged_in { "logged_in" } else { "held" })
            }
            "answers" => match store.answers().await {
                Ok(()) => json!("answers"),
                Err(Unanswered::Status(status)) => json!({"refused": format!("status {status}")}),
                Err(Unanswered::NoAnswer(_)) => json!({"refused": "no answer"}),
                Err(Unanswered::NoUsableLogin(_)) => json!({"refused": "no usable login"}),
                Err(Unanswered::TokenRefused) => json!({"refused": "token refused"}),
            },
            op => {
                let secret = step["name"].as_str().unwrap();
                let answer = if op == "read" {
                    store.read(secret, budget(step)).await
                } else {
                    read_required_secret(Some(&store), secret, step["require"].as_str().unwrap(), budget(step)).await
                };
                match answer {
                    Ok(answer) => fields(answer),
                    Err(Refused::NoSecretName(_)) => json!({"refused": "no_secret_name"}),
                    Err(_) => json!({"refused": "not_served"}),
                }
            }
        };
        let asked: Vec<Value> = scripted.asked.lock().unwrap()[asked_before..].to_vec();
        let at = format!("{name}, step {index}: {step}");
        assert_eq!(outcome, step["outcome"], "{at}");
        assert_eq!(Value::Array(asked), step["asked"], "{at}");
        assert_eq!(json!(said), step["said"], "{at}");
    }
    store.close();
    std::fs::remove_dir_all(&directory).unwrap();
}

#[tokio::test]
async fn the_session_does_what_python_s_does_step_by_step() {
    for scenario in vectors("sessions") {
        replay(&scenario).await;
    }
}

#[test]
fn the_published_keys_are_cached_and_paced_as_python_s() {
    for scenario in vectors("published_keys") {
        let name = scenario["name"].as_str().unwrap();
        let fetches: Arc<Mutex<VecDeque<Value>>> = Arc::new(Mutex::new(scenario["fetches"].as_array().unwrap().iter().cloned().collect()));
        let fetched: Arc<Mutex<Vec<String>>> = Arc::default();
        let (answers, asked) = (Arc::clone(&fetches), Arc::clone(&fetched));
        let fetch: Fetch = Arc::new(move |identity: &str| {
            asked.lock().unwrap().push(identity.to_owned());
            let answer = answers.lock().unwrap().pop_front().unwrap_or(json!({}));
            serde_json::from_value(answer).unwrap()
        });
        let (clock, now) = manual_clock();
        let keys = PublishedKeys::new(fetch).with_clock(clock);
        let mut source = None;
        for (index, step) in scenario["steps"].as_array().unwrap().iter().enumerate() {
            let before = fetched.lock().unwrap().len();
            let outcome = match step["op"].as_str().unwrap() {
                "advance" => {
                    *now.lock().unwrap() += step["seconds"].as_f64().unwrap();
                    Value::Null
                }
                "event" => {
                    source = Some(keys.for_event(step["kid"].as_str()));
                    Value::Null
                }
                op => {
                    let source = source.as_ref().unwrap();
                    let identity = step["identity"].as_str().unwrap();
                    let read = if op == "published" {
                        source.published(identity)
                    } else {
                        source.refresh(identity)
                    };
                    read.map_or(json!("unavailable"), |keys| json!({"keys": keys}))
                }
            };
            let at = format!("{name}, step {index}: {step}");
            assert_eq!(outcome, step["outcome"], "{at}");
            assert_eq!(json!(fetched.lock().unwrap()[before..]), step["fetched"], "{at}");
        }
    }
}
