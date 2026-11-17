//! The outbox and its store session against a REAL RustFS (ADR 0234): the pinned 1.0.3, trusting a stand-in
//! ServiceAccount issuer, with the bucket and two identities' canned policies provisioned by the hook's own tool. The
//! producer's policy puts into the outbox; the relay's is the one the chart renders for lineage (list, get and delete
//! on the outbox). Every session is exchanged at the store's STS from a projected token, and every request is signed by
//! this crate and verified by the store.
//!
//! Skipped, with a message, unless `RASK_PORT_STORE_ENDPOINT` names a store. `scripts/port_outbox_check.sh` starts one,
//! runs this test and then the Python-and-Rust check; `KEEP_UP=1` leaves it running for a run of its own.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::auth::issuer::Clock;
use lakehouse_core::outbox::{Outbox, backlog, in_order};
use lakehouse_core::storage::{S3, Said, StorageError, StoreSession, StoreSettings};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const PRODUCER: &str = "system:serviceaccount:rask:rask-sa-catalog";
const RELAY: &str = "system:serviceaccount:rask:rask-sa-lineage";

struct Store {
    endpoint: String,
    bucket: String,
    issuer: String,
}

fn store() -> Option<Store> {
    let endpoint = std::env::var("RASK_PORT_STORE_ENDPOINT").ok();
    if endpoint.is_none() {
        eprintln!("skipped: RASK_PORT_STORE_ENDPOINT names no store; run scripts/port_outbox_check.sh");
    }
    Some(Store {
        endpoint: endpoint?,
        bucket: std::env::var("RASK_PORT_STORE_BUCKET").unwrap(),
        issuer: std::env::var("RASK_PORT_STORE_ISSUER").unwrap(),
    })
}

/// What a run names its objects with, so a store kept up between runs meets no earlier run's.
fn run_id() -> String {
    format!("{}-{}", std::process::id(), SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos())
}

/// A projected token for `subject`, written where a pod's kubelet would put it.
async fn project(store: &Store, subject: &str, file: &PathBuf) {
    let token = reqwest::get(format!("{}/token?sub={subject}", store.issuer))
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    std::fs::write(file, token).unwrap();
}

struct Identity {
    session: Arc<StoreSession>,
    outbox: Outbox,
    s3: Arc<S3>,
    token_file: PathBuf,
    offset: Arc<Mutex<Duration>>,
}

async fn identity(store: &Store, subject: &str) -> Identity {
    let directory = std::env::temp_dir().join(format!("rask-outbox-rustfs-{}", run_id()));
    std::fs::create_dir_all(&directory).unwrap();
    let token_file = directory.join("token");
    project(store, subject, &token_file).await;
    let environment = [
        ("LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE", token_file.to_str().unwrap().to_owned()),
        ("LINEAGE_S3_STS_ENDPOINT", store.endpoint.clone()),
        ("LINEAGE_S3_ENDPOINT", store.endpoint.clone()),
        ("LINEAGE_S3_SESSION_SECONDS", "900".to_owned()),
    ];
    let settings = StoreSettings::from_environment("LINEAGE", environment.map(|(name, value)| (name.to_owned(), value))).unwrap();
    let base = Instant::now();
    let offset = Arc::new(Mutex::new(Duration::ZERO));
    let read = Arc::clone(&offset);
    let clock: Clock = Arc::new(move || base + *read.lock().unwrap());
    let session = Arc::new(StoreSession::new(&settings, "port-outbox-check").unwrap().with_clock(clock));
    assert_eq!(session.exchange_at_boot().await, Ok(1), "the store exchanges {subject}'s token at once");
    let s3 = Arc::new(S3::new(&settings, Arc::clone(&session)).unwrap());
    let outbox = Outbox::new(&format!("s3://{}/_lineage_outbox", store.bucket), Arc::clone(&s3)).unwrap();
    Identity {
        session,
        outbox,
        s3,
        token_file,
        offset,
    }
}

#[tokio::test]
async fn the_producer_stages_and_the_relay_lists_reads_and_drops_on_a_real_store() {
    let Some(store) = store() else { return };
    let (producer, relay) = (identity(&store, PRODUCER).await, identity(&store, RELAY).await);
    let run = run_id();
    let keys: Vec<String> = (0..3).map(|n| format!("{run}-{n}@COMPLETE")).collect();
    for key in &keys {
        producer.outbox.stage(key, &format!("{{\"id\": \"{key}\"}}")).await.unwrap();
        // A store keeps a modification time to the millisecond: apart, the three are listed in the order staged.
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    producer
        .outbox
        .stage(&keys[0], &format!("{{\"id\": \"{}\"}}", keys[0]))
        .await
        .expect("staging an event again writes the same object");

    let listing = relay.outbox.listing().await.unwrap();
    let ours: Vec<_> = listing.iter().filter(|listed| listed.key.contains(&run)).cloned().collect();
    let order: Vec<String> = in_order(&ours, None).into_iter().map(|(key, _)| key).collect();
    assert_eq!(
        order,
        [keys[1].clone(), keys[2].clone(), keys[0].clone()],
        "oldest first: the event staged again is the newest"
    );
    let now = i64::try_from(SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()).unwrap();
    let (depth, age) = backlog(&listing, now);
    assert!(depth >= 3 && age >= 0.0, "{depth} {age}");
    let staged = relay.outbox.read_staged(&ours, Some(2)).await.unwrap();
    assert_eq!(
        staged.iter().map(|staged| staged.key.as_str()).collect::<Vec<_>>(),
        [keys[1].as_str(), keys[2].as_str()],
        "bounded by the limit"
    );
    assert_eq!(staged[0].body, format!("{{\"id\": \"{}\"}}", keys[1]).into_bytes());

    for key in &keys {
        relay.outbox.drop_staged(key).await.unwrap();
        relay.outbox.drop_staged(key).await.expect("a drop of what is gone is what was asked for");
        assert_eq!(relay.outbox.read(key).await.unwrap(), None);
    }
    let after = relay.outbox.listing().await.unwrap();
    assert!(after.iter().all(|listed| !listed.key.contains(&run)), "{after:?}");
    assert!(
        relay.outbox.read_staged(&ours, None).await.unwrap().is_empty(),
        "an event dropped since the listing is passed over"
    );
}

#[tokio::test]
async fn the_store_refuses_what_each_identity_was_not_granted() {
    let Some(store) = store() else { return };
    let (producer, relay) = (identity(&store, PRODUCER).await, identity(&store, RELAY).await);
    let refused = relay.outbox.stage(&run_id(), "{}").await;
    assert!(
        matches!(&refused, Err(StorageError::Refused { status: 403, code }) if code == "AccessDenied"),
        "{refused:?}"
    );
    let listed = producer.outbox.listing().await;
    assert!(matches!(&listed, Err(StorageError::Refused { status: 403, .. })), "{listed:?}");
    let elsewhere = relay.s3.get(&store.bucket, "_timers/another-timer.json").await;
    assert!(matches!(&elsewhere, Err(StorageError::Refused { status: 403, .. })), "{elsewhere:?}");
}

#[tokio::test]
async fn a_conditional_create_is_refused_by_the_store_once_the_key_exists() {
    let Some(store) = store() else { return };
    let (producer, relay) = (identity(&store, PRODUCER).await, identity(&store, RELAY).await);
    let key = format!("_lineage_outbox/claim-{}.json", run_id());
    producer.s3.put(&store.bucket, &key, b"first".to_vec(), true).await.unwrap();
    let second = producer.s3.put(&store.bucket, &key, b"second".to_vec(), true).await;
    assert_eq!(second, Err(StorageError::Exists { key: key.clone() }));
    assert_eq!(
        relay.s3.get(&store.bucket, &key).await.unwrap(),
        Some(b"first".to_vec()),
        "the loser wrote nothing"
    );
    relay.s3.delete(&store.bucket, &key).await.unwrap();
}

#[tokio::test]
async fn a_listing_of_more_than_a_page_is_read_to_its_end() {
    let Some(store) = store() else { return };
    let (producer, relay) = (identity(&store, PRODUCER).await, identity(&store, RELAY).await);
    let run = run_id();
    let producer = Arc::new(producer);
    let mut staging = tokio::task::JoinSet::new();
    for n in 0..1005 {
        let (producer, key) = (Arc::clone(&producer), format!("{run}-page-{n:04}"));
        staging.spawn(async move { producer.outbox.stage(&key, "{}").await });
    }
    while let Some(staged) = staging.join_next().await {
        staged.unwrap().unwrap();
    }
    let ours: Vec<_> = relay
        .outbox
        .listing()
        .await
        .unwrap()
        .into_iter()
        .filter(|listed| listed.key.contains(&run))
        .collect();
    assert_eq!(ours.len(), 1005, "a store lists at most 1000 keys a page");
    let relay = Arc::new(relay);
    let mut dropping = tokio::task::JoinSet::new();
    for (key, _) in in_order(&ours, None) {
        let relay = Arc::clone(&relay);
        dropping.spawn(async move { relay.outbox.drop_staged(&key).await });
    }
    while let Some(dropped) = dropping.join_next().await {
        dropped.unwrap().unwrap();
    }
}

#[tokio::test]
async fn the_session_is_renewed_from_the_token_read_again_and_signs_nothing_past_its_end() {
    let Some(store) = store() else { return };
    let relay = identity(&store, RELAY).await;
    std::fs::write(&relay.token_file, "a token no issuer signed").unwrap();
    *relay.offset.lock().unwrap() = Duration::from_secs(450);
    let refused = relay.session.refresh_if_due().await;
    assert_eq!(
        (refused.renewed, refused.said),
        (false, Some(Said::RenewalFailed)),
        "the store refuses a forged token"
    );
    relay.outbox.listing().await.expect("the session held still signs");

    project(&store, RELAY, &relay.token_file).await;
    assert!(relay.session.refresh_if_due().await.renewed, "the rotated token is read and exchanged");
    relay.outbox.listing().await.unwrap();

    *relay.offset.lock().unwrap() = Duration::from_secs(450 + 841);
    assert_eq!(relay.outbox.listing().await, Err(StorageError::NoSession));
}
