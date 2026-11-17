//! The session with the secret store end to end, through its public API, against an in-process store that speaks the
//! three endpoints a service uses: the Kubernetes auth login, `auth/token/lookup-self` and a KV v2 read.
//!
//! The signing key the published-keys test verifies with is generated when the test runs.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::Json as Body;
use axum::Router;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use ed25519_dalek::{Signer, SigningKey};
use lakehouse_core::auth::issuer::Clock;
use lakehouse_core::pywire::json::{self, Json};
use lakehouse_core::secrets::published::store_fetch;
use lakehouse_core::secrets::{Budget, PublishedKeys, Refused, SecretStore, SecretStoreSettings, Session, Unanswered};
use lakehouse_core::signing::{NotVerified, RefusalReason, canon, nkey, signed_document, verify_event};
use serde_json::{Value, json};
use std::collections::hash_map::RandomState;
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::hash::BuildHasher;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::net::TcpListener;

const ROLE: &str = "lineage";
const SIGNER: &str = "kubernetes~system:serviceaccount:rask:svc-ingest";

/// What the store holds and what it was asked.
#[derive(Default)]
struct Served {
    /// The projected tokens the store's token review admits.
    admitted: HashSet<String>,
    lease: u64,
    issued: HashSet<String>,
    secrets: BTreeMap<String, BTreeMap<String, String>>,
    down: bool,
    login_delay: Duration,
    /// The projected token each login presented, in order.
    presented: Vec<String>,
    tokens_used: Vec<String>,
}

type Shared = Arc<Mutex<Served>>;

async fn login(State(served): State<Shared>, Body(body): Body<Value>) -> Response {
    let delay = served.lock().unwrap().login_delay;
    tokio::time::sleep(delay).await;
    let mut served = served.lock().unwrap();
    if served.down {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let jwt = body["jwt"].as_str().unwrap_or_default().to_owned();
    served.presented.push(jwt.clone());
    if body["role"] != ROLE || !served.admitted.contains(&jwt) {
        return (StatusCode::FORBIDDEN, Body(json!({"errors": ["permission denied"]}))).into_response();
    }
    let token = format!("s.{}", served.presented.len());
    served.issued.insert(token.clone());
    Body(json!({"auth": {"client_token": token, "lease_duration": served.lease, "policies": ["default", ROLE]}})).into_response()
}

/// Whether the store serves the request's token: down is a 503, a token it does not hold a 403.
fn admitted(served: &mut Served, headers: &HeaderMap) -> Result<(), StatusCode> {
    if served.down {
        return Err(StatusCode::SERVICE_UNAVAILABLE);
    }
    let token = headers
        .get("x-vault-token")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_owned();
    served.tokens_used.push(token.clone());
    if served.issued.contains(&token) { Ok(()) } else { Err(StatusCode::FORBIDDEN) }
}

async fn lookup_self(State(served): State<Shared>, headers: HeaderMap) -> Response {
    match admitted(&mut served.lock().unwrap(), &headers) {
        Ok(()) => Body(json!({"data": {"ttl": 600}})).into_response(),
        Err(status) => status.into_response(),
    }
}

async fn kv(State(served): State<Shared>, Path(name): Path<String>, headers: HeaderMap) -> Response {
    let mut served = served.lock().unwrap();
    if let Err(status) = admitted(&mut served, &headers) {
        return status.into_response();
    }
    match served.secrets.get(&name) {
        Some(fields) => Body(json!({"data": {"data": fields, "metadata": {"version": 1}}})).into_response(),
        None => (StatusCode::NOT_FOUND, Body(json!({"errors": []}))).into_response(),
    }
}

struct Store {
    served: Shared,
    settings: SecretStoreSettings,
    token: PathBuf,
}

async fn store_up() -> Store {
    let served: Shared = Arc::new(Mutex::new(Served {
        admitted: HashSet::from(["projected-1".to_owned()]),
        lease: 900,
        ..Served::default()
    }));
    let app = Router::new()
        .route("/v1/auth/kubernetes/login", post(login))
        .route("/v1/auth/token/lookup-self", get(lookup_self))
        .route("/v1/secret/data/{name}", get(kv))
        .with_state(Arc::clone(&served));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let directory = std::env::temp_dir().join(format!("rask-secrets-e2e-{}-{port}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    let token = directory.join("token");
    std::fs::write(&token, "projected-1\n").unwrap();
    let settings = SecretStoreSettings::from_environment([
        ("RASK_SECRET_STORE_ADDR".to_owned(), format!("http://127.0.0.1:{port}")),
        ("RASK_SECRET_STORE_ROLE".to_owned(), ROLE.to_owned()),
        ("RASK_SECRET_STORE_TOKEN_FILE".to_owned(), token.display().to_string()),
    ])
    .unwrap();
    Store { served, settings, token }
}

fn manual_clock() -> (Clock, Arc<Mutex<Duration>>) {
    let base = Instant::now();
    let offset = Arc::new(Mutex::new(Duration::ZERO));
    let read = Arc::clone(&offset);
    (Arc::new(move || base + *read.lock().unwrap()), offset)
}

fn quick() -> Budget {
    Budget {
        timeout: Duration::from_secs(2),
        retries: 2,
        backoff: Duration::ZERO,
    }
}

fn fields(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs.iter().map(|(name, value)| ((*name).to_owned(), (*value).to_owned())).collect()
}

#[tokio::test]
async fn a_read_is_served_under_the_login_the_role_made() {
    let store = store_up().await;
    store
        .served
        .lock()
        .unwrap()
        .secrets
        .insert("signing-key-catalog".to_owned(), fields(&[("seed", "S")]));
    let session = Session::open(Arc::new(SecretStore::new(store.settings.clone()).unwrap()), Duration::from_secs(3600))
        .await
        .unwrap();

    let read = session.store.read_required("signing-key-catalog", "seed", quick()).await.unwrap();

    assert_eq!(read, fields(&[("seed", "S")]));
    assert_eq!(session.store.answers().await, Ok(()));
    let served = store.served.lock().unwrap();
    assert_eq!(
        served.presented,
        ["projected-1"],
        "one login, presenting the projected token as its file holds it"
    );
    assert!(
        served.tokens_used.iter().all(|token| token == "s.1"),
        "every call is made under the issued token"
    );
}

#[tokio::test]
async fn the_projected_token_is_read_again_at_the_next_login() {
    let store = store_up().await;
    store.served.lock().unwrap().lease = 60;
    store.served.lock().unwrap().secrets.insert("a".to_owned(), fields(&[("k", "v")]));
    let (clock, offset) = manual_clock();
    let session = SecretStore::new(store.settings.clone()).unwrap().with_clock(clock);
    assert_eq!(session.read("a", quick()).await.unwrap(), fields(&[("k", "v")]));

    // The kubelet rotates the token and the review admits only the new one.
    std::fs::write(&store.token, "projected-2").unwrap();
    store.served.lock().unwrap().admitted = HashSet::from(["projected-2".to_owned()]);
    *offset.lock().unwrap() = Duration::from_secs(54);

    assert_eq!(session.read("a", quick()).await.unwrap(), fields(&[("k", "v")]));
    assert_eq!(store.served.lock().unwrap().presented, ["projected-1", "projected-2"]);
}

#[tokio::test]
async fn a_token_the_store_dropped_is_replaced_by_the_loop_and_never_by_a_read() {
    let store = store_up().await;
    store.served.lock().unwrap().secrets.insert("a".to_owned(), fields(&[("k", "v")]));
    let session = SecretStore::new(store.settings.clone()).unwrap();
    session.log_in(quick()).await.unwrap();
    store.served.lock().unwrap().issued.clear();

    assert_eq!(session.read("a", Budget::one_attempt(Duration::from_secs(2))).await.unwrap(), BTreeMap::new());
    assert_eq!(session.answers().await, Err(Unanswered::TokenRefused));
    assert_eq!(store.served.lock().unwrap().presented.len(), 1, "a refused read logs in no further");

    let pass = session.keep_logged_in(Duration::from_secs(2)).await;

    assert!(pass.logged_in);
    assert_eq!(session.read("a", quick()).await.unwrap(), fields(&[("k", "v")]));
    assert_eq!(session.answers().await, Ok(()));
}

#[tokio::test]
async fn a_store_that_is_down_fails_closed_and_serves_again_once_it_is_up() {
    let store = store_up().await;
    store
        .served
        .lock()
        .unwrap()
        .secrets
        .insert("signing-key-catalog".to_owned(), fields(&[("seed", "S")]));
    store.served.lock().unwrap().down = true;
    let session = SecretStore::new(store.settings.clone()).unwrap();

    assert!(session.log_in(quick()).await.is_err());
    assert!(matches!(
        session.read_required("signing-key-catalog", "seed", quick()).await,
        Err(Refused::NotServed { .. })
    ));
    assert_eq!(session.answers().await, Err(Unanswered::Status(503)));

    store.served.lock().unwrap().down = false;

    assert_eq!(
        session.read_required("signing-key-catalog", "seed", quick()).await.unwrap(),
        fields(&[("seed", "S")])
    );
    assert_eq!(session.answers().await, Ok(()));
}

#[tokio::test]
async fn readers_that_find_no_login_share_one() {
    let store = store_up().await;
    store.served.lock().unwrap().secrets.insert("a".to_owned(), fields(&[("k", "v")]));
    store.served.lock().unwrap().login_delay = Duration::from_millis(200);
    let session = Arc::new(SecretStore::new(store.settings.clone()).unwrap());

    let readers: Vec<_> = (0..8)
        .map(|_| {
            let session = Arc::clone(&session);
            tokio::spawn(async move { session.read("a", Budget::one_attempt(Duration::from_secs(2))).await })
        })
        .collect();
    for reader in readers {
        assert_eq!(reader.await.unwrap().unwrap(), fields(&[("k", "v")]));
    }

    assert_eq!(store.served.lock().unwrap().presented.len(), 1);
}

/// An Ed25519 key generated now: its signing half, its published NKEY and its key id.
fn signing_key() -> (SigningKey, String, String) {
    let mut seed = [0u8; 32];
    for (index, chunk) in seed.chunks_mut(8).enumerate() {
        chunk.copy_from_slice(&RandomState::new().hash_one(index).to_le_bytes());
    }
    let key = SigningKey::from_bytes(&seed);
    let raw = key.verifying_key().to_bytes();
    let mut body = vec![0xa0];
    body.extend_from_slice(&raw);
    let mut crc: u16 = 0;
    for byte in &body {
        crc ^= u16::from(*byte) << 8;
        for _ in 0..8 {
            crc = if crc & 0x8000 != 0 { (crc << 1) ^ 0x1021 } else { crc << 1 };
        }
    }
    body.extend_from_slice(&crc.to_le_bytes());
    let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let (mut bits, mut held, mut text) = (0u32, 0u32, String::new());
    for byte in body {
        bits = (bits << 8) | u32::from(byte);
        held += 8;
        while held >= 5 {
            held -= 5;
            text.push(alphabet[((bits >> held) & 31) as usize] as char);
        }
        bits &= (1 << held) - 1;
    }
    assert_eq!(nkey::raw_public_key(&text), Some(raw));
    (key, text, nkey::kid_of(&raw))
}

/// A run event authored and signed by `SIGNER` with `key`.
fn signed_event(key: &SigningKey, kid: &str) -> Json {
    let mut event = json!({
        "eventType": "COMPLETE",
        "eventTime": "2026-11-17T10:00:00Z",
        "producer": "https://example.test/p",
        "run": {"runId": "0190a1b2-0000-7000-8000-000000000001", "facets": {
            "rask_author": {"sub": SIGNER},
            "rask_signature": {"identity": SIGNER, "kid": kid, "alg": "Ed25519", "canon": 1, "signature": ""},
        }},
        "job": {"namespace": "wh", "name": "pages"},
    });
    let unsigned = json::parse(&event.to_string()).unwrap();
    let message = canon::canon_1(&signed_document(&unsigned).unwrap()).unwrap();
    let signature = base64_url(&key.sign(&message).to_bytes());
    event["run"]["facets"]["rask_signature"]["signature"] = json!(signature);
    json::parse(&event.to_string()).unwrap()
}

fn base64_url(bytes: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_signed_event_verifies_against_the_keys_its_signer_publishes_in_the_store() {
    let store = store_up().await;
    let (first, first_public, first_kid) = signing_key();
    let (second, second_public, second_kid) = signing_key();
    let secret = "signing-public-svc-ingest";
    store
        .served
        .lock()
        .unwrap()
        .secrets
        .insert(secret.to_owned(), fields(&[("keys", &first_public)]));
    let session = Arc::new(SecretStore::new(store.settings.clone()).unwrap());
    session.log_in(quick()).await.unwrap();
    let (clock, offset) = manual_clock();
    let keys = Arc::new(PublishedKeys::new(store_fetch(session, tokio::runtime::Handle::current())).with_clock(clock));
    let verify = |event: Json, kid: String| {
        let keys = Arc::clone(&keys);
        tokio::task::spawn_blocking(move || {
            let signers = BTreeSet::from([SIGNER.to_owned()]);
            verify_event(&event, &keys.for_event(Some(&kid)), &signers, &BTreeSet::new())
        })
    };

    let verified = verify(signed_event(&first, &first_kid), first_kid.clone()).await.unwrap().unwrap();
    assert_eq!((verified.identity.as_str(), verified.kid.as_str()), (SIGNER, first_kid.as_str()));

    // The signer rotates: the store lists the new key first, which a re-read finds once the pace allows one.
    store
        .served
        .lock()
        .unwrap()
        .secrets
        .insert(secret.to_owned(), fields(&[("keys", &format!("{second_public},{first_public}"))]));
    *offset.lock().unwrap() = Duration::from_secs(10);
    let inside_the_pace = verify(signed_event(&second, &second_kid), second_kid.clone()).await.unwrap();
    assert!(matches!(inside_the_pace, Err(NotVerified::Unavailable(_))), "{inside_the_pace:?}");
    *offset.lock().unwrap() = Duration::from_secs(31);
    let rotated = verify(signed_event(&second, &second_kid), second_kid.clone()).await.unwrap().unwrap();
    assert_eq!(rotated.kid, second_kid);

    // A signature made by the old key under the new key's id does not verify.
    let tampered = signed_event(&first, &second_kid);
    let refused = verify(tampered, second_kid).await.unwrap();
    assert!(
        matches!(refused, Err(NotVerified::Refused(ref refusal)) if refusal.reason == RefusalReason::Signature),
        "{refused:?}"
    );
}
