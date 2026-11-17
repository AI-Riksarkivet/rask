//! Lineage's lanes against real collaborators (ADR 0235): the pinned nats-server with the stream and consumer declared
//! as the chart's stream Job declares them, the real Python catalog on real pylance, and the pinned RustFS under the
//! identities and policies the chart renders. The Python producer and the Python lane are the real ones
//! (`scripts/port_lanes_fixture.py`).
//!
//! Each test starts its own nats-server and keeps its tables in a namespace of its own and its outbox under a prefix of
//! its own. The tests named `way_*` replay the ways an event can go in `specs/lineage/EventPath.tla` and check the
//! model's NoLoss and AFeedRowIsWhole on the real tables.
//!
//! Skipped, with a message, unless `RASK_PORT_CATALOG_URL` and `RASK_PORT_STORE_ENDPOINT` name the fixtures;
//! `scripts/port_lanes_check.sh` starts them and runs this file.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod support;

use axum::body::Bytes;
use axum::http::{HeaderMap, Method, StatusCode, Uri};
use lakehouse_bus::{Bus, BusSettings, CloudEvent, lineage_envelope, publish_event};
use lakehouse_core::authz::{AccessDecider, Answer, TableDecision};
use lakehouse_core::delivery::Lane;
use lakehouse_core::outbox::Outbox;
use lakehouse_core::secrets::published::PublishedKeys;
use lakehouse_core::storage::records::Records;
use lakehouse_core::storage::{S3, StoreSession, StoreSettings};
use lakehouse_core::timers::{Outcome, Timer};
use lakehouse_tables::{Catalog, bearer};
use lineage::feed::feed_key;
use lineage::gate::Gate;
use lineage::lane::IngestLane;
use lineage::model::parse_event;
use lineage::settings::{CONSUMER, STREAM, Settings};
use lineage::signature::Signing;
use lineage::store::{Store, tables};
use lineage::tick::{RELAY_TIMER, Relay};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use support::counted::Counted;
use tokio::sync::oneshot;

const CATALOG: &str = "lance-namespace://localhost/api/catalog";
const TOPIC: &str = "lineage.events.v1";
const PRODUCER: &str = "system:serviceaccount:rask:rask-sa-catalog";
const RELAY: &str = "system:serviceaccount:rask:rask-sa-lineage";
const SOURCE: &str = "0190a1b2-0000-7000-8000-00000000000a";
const TARGET: &str = "0190a1b2-0000-7000-8000-00000000000b";
const ROOT: &str = env!("CARGO_MANIFEST_DIR");

struct Fixtures {
    catalog: String,
    endpoint: String,
    bucket: String,
    issuer: String,
}

fn fixtures() -> Option<Fixtures> {
    let (catalog, endpoint) = (std::env::var("RASK_PORT_CATALOG_URL").ok(), std::env::var("RASK_PORT_STORE_ENDPOINT").ok());
    if catalog.is_none() || endpoint.is_none() {
        eprintln!("skipped: RASK_PORT_CATALOG_URL and RASK_PORT_STORE_ENDPOINT name no fixtures; run scripts/port_lanes_check.sh");
    }
    Some(Fixtures {
        catalog: catalog?,
        endpoint: endpoint?,
        bucket: std::env::var("RASK_PORT_STORE_BUCKET").unwrap(),
        issuer: std::env::var("RASK_PORT_STORE_ISSUER").unwrap(),
    })
}

fn unique() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

async fn eventually<F: AsyncFnMut() -> bool>(what: &str, mut holds: F) {
    for _ in 0..600 {
        if holds().await {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("{what} did not come to hold within 30 s");
}

/// One nats-server with JetStream, the lineage stream and its durable consumer declared on it.
struct Nats {
    child: Child,
    url: String,
    token_file: PathBuf,
}

impl Nats {
    async fn start() -> Nats {
        let script = format!("{ROOT}/../../../scripts/nats_server.sh");
        let binary = String::from_utf8(Command::new("bash").arg(&script).output().unwrap().stdout).unwrap();
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let dir = std::env::temp_dir().join(format!("lineage-lanes-{}", unique()));
        std::fs::create_dir_all(&dir).unwrap();
        let token_file = dir.join("token");
        std::fs::write(&token_file, "lanes-token\n").unwrap();
        let child = Command::new(binary.trim())
            .args(["-js", "-a", "127.0.0.1", "-p", &port.to_string(), "--auth", "lanes-token", "-sd"])
            .arg(dir.join("store"))
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        while TcpStream::connect(("127.0.0.1", port)).is_err() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        let nats = Nats {
            child,
            url: format!("nats://127.0.0.1:{port}"),
            token_file,
        };
        let stream = json!({"name": STREAM, "subjects": ["lineage.events.>"], "retention": "limits", "storage": "file", "discard": "old",
            "num_replicas": 1, "duplicate_window": 120_000_000_000_u64, "allow_direct": true});
        let consumer = json!({"durable_name": CONSUMER, "ack_policy": "explicit", "ack_wait": 30_000_000_000_u64, "max_deliver": -1,
            "filter_subject": TOPIC, "deliver_policy": "all", "replay_policy": "instant", "max_ack_pending": 8, "max_waiting": 512});
        let admin = async_nats::ConnectOptions::with_token("lanes-token".to_owned())
            .connect(&nats.url)
            .await
            .unwrap();
        for (subject, body) in [
            (format!("$JS.API.STREAM.CREATE.{STREAM}"), stream),
            (
                format!("$JS.API.CONSUMER.CREATE.{STREAM}.{CONSUMER}"),
                json!({"stream_name": STREAM, "config": consumer, "action": "create"}),
            ),
        ] {
            let answer: Value = serde_json::from_slice(&admin.request(subject.clone(), body.to_string().into()).await.unwrap().payload).unwrap();
            assert!(answer.get("error").is_none(), "{subject}: {answer}");
        }
        nats
    }

    async fn bus(&self) -> Arc<Bus> {
        let bus = Bus::start(BusSettings {
            url: self.url.clone(),
            user: "lineage".to_owned(),
            token_file: self.token_file.clone(),
            ca_file: None,
            session: Duration::from_secs(300),
        });
        eventually("the bus to connect", async || bus.answering().is_ok()).await;
        Arc::new(bus)
    }
}

impl Drop for Nats {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// A Python command of `scripts/port_lanes_fixture.py`, its answer read from the last line it prints.
async fn python(command: &str, spec: Value) -> Value {
    let file = std::env::temp_dir().join(format!("lanes-{}.json", unique()));
    std::fs::write(&file, spec.to_string()).unwrap();
    let command = command.to_owned();
    let output = tokio::task::spawn_blocking(move || {
        Command::new("uv")
            .args(["run", "--no-sync", "python", "scripts/port_lanes_fixture.py", &command])
            .arg(&file)
            .current_dir(format!("{ROOT}/../../.."))
            .output()
            .unwrap()
    })
    .await
    .unwrap();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    let stdout = String::from_utf8(output.stdout).unwrap();
    serde_json::from_str(stdout.lines().last().unwrap()).unwrap()
}

/// A session at the store for `subject`, exchanged from a token the stand-in issuer projects.
async fn store_session(fixtures: &Fixtures, subject: &str) -> (Arc<S3>, Vec<(String, String)>) {
    let token = reqwest::get(format!("{}/token?sub={subject}", fixtures.issuer))
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    let token_file = std::env::temp_dir().join(format!("lanes-token-{}", unique()));
    std::fs::write(&token_file, token).unwrap();
    let environment: Vec<(String, String)> = [
        ("LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE", token_file.to_str().unwrap()),
        ("LINEAGE_S3_STS_ENDPOINT", &fixtures.endpoint),
        ("LINEAGE_S3_ENDPOINT", &fixtures.endpoint),
        ("LINEAGE_S3_SESSION_SECONDS", "900"),
    ]
    .map(|(name, value)| (name.to_owned(), value.to_owned()))
    .to_vec();
    let settings = StoreSettings::from_environment("LINEAGE", environment.clone()).unwrap();
    let session = Arc::new(StoreSession::new(&settings, "port-lanes-check").unwrap());
    assert_eq!(session.exchange_at_boot().await, Ok(1));
    (Arc::new(S3::new(&settings, session).unwrap()), environment)
}

async fn store_at(url: &str, namespace: Vec<String>) -> Store {
    let store = Store::new(
        Catalog::new(url, bearer::anonymous(), Duration::from_secs(60)).unwrap(),
        namespace,
        CATALOG,
        true,
    )
    .unwrap();
    store.ensure().await.unwrap();
    store
}

async fn namespace(url: &str) -> Vec<String> {
    let name = format!("l{}", unique());
    let created = reqwest::Client::new()
        .post(format!("{url}/v1/namespace/lance_catalog%24port%24{name}/create?delimiter=%24"))
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    assert!(created.status().is_success());
    vec!["lance_catalog".into(), "port".into(), name]
}

/// The decider that grants nothing on a table every tuple governs: a person with no grant, which a grant would lift.
struct Denies;

impl AccessDecider for Denies {
    fn resolves_names(&self) -> bool {
        false
    }
    fn check<'a>(&'a self, _: &'a str, _: &'a str, _: &'a str) -> Answer<'a, bool> {
        Box::pin(async { Ok(false) })
    }
    fn batch_check<'a>(&'a self, _: &'a str, _: &'a str, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>> {
        Box::pin(async move { Ok(objects.iter().map(|object| (object.clone(), false)).collect()) })
    }
    fn check_tables<'a>(&'a self, _: &'a str, _: &'a str, tables: &'a [String]) -> Answer<'a, BTreeMap<String, TableDecision>> {
        Box::pin(async move { Ok(tables.iter().map(|table| (table.clone(), TableDecision::default())).collect()) })
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

fn gate(store: &Store, denies: bool, counted: &Counted) -> Arc<Gate> {
    let keys = Arc::new(PublishedKeys::new(Arc::new(|_: &str| BTreeMap::new())));
    let decider: Option<Arc<dyn AccessDecider>> = denies.then(|| Arc::new(Denies) as Arc<dyn AccessDecider>);
    Arc::new(Gate {
        signing: Signing::default(),
        keys,
        fga_enabled: denies,
        decider,
        catalog: CATALOG.to_owned(),
        root: "lakehouse:estate".to_owned(),
        store: store.clone(),
        signals: Arc::clone(&counted.signals),
    })
}

/// A run's COMPLETE event, by a person, copying the source table into the target.
fn event(run: &str) -> Map<String, Value> {
    let table = |name: &str, uuid: &str| json!({"namespace": CATALOG, "name": name, "facets": {"rask_lance": {"tableUuid": uuid}}});
    let event = json!({
        "eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z", "producer": "rask/check",
        "run": {"runId": run, "facets": {"rask_author": {"sub": "dex~alice"}}},
        "job": {"namespace": "jobs", "name": "copy"},
        "inputs": [table("wh$raw$pages", SOURCE)], "outputs": [table("wh$clean$pages", TARGET)],
    });
    event.as_object().unwrap().clone()
}

/// One test's estate: a bus, the store's tables in a namespace of their own, an outbox of its own.
struct Estate {
    nats: Nats,
    bus: Arc<Bus>,
    store: Store,
    namespace: Vec<String>,
    outbox_uri: String,
    relay_s3: Arc<S3>,
    producer: Outbox,
    settings: Arc<Settings>,
    fixtures: Fixtures,
}

impl Estate {
    async fn new(fixtures: Fixtures, extra: &[(&str, &str)]) -> Estate {
        let nats = Nats::start().await;
        let bus = nats.bus().await;
        let namespace = namespace(&fixtures.catalog).await;
        let store = store_at(&fixtures.catalog, namespace.clone()).await;
        let outbox_uri = format!("s3://{}/_lineage_outbox/{}", fixtures.bucket, unique());
        let (relay_s3, mut environment) = store_session(&fixtures, RELAY).await;
        let (producer_s3, _) = store_session(&fixtures, PRODUCER).await;
        environment.push(("LINEAGE_OUTBOX_URI".to_owned(), outbox_uri.clone()));
        environment.push(("LINEAGE_PUBLISH_TIMEOUT_SECONDS".to_owned(), "5".to_owned()));
        environment.extend(extra.iter().map(|(name, value)| ((*name).to_owned(), (*value).to_owned())));
        Estate {
            producer: Outbox::new(&outbox_uri, producer_s3).unwrap(),
            settings: Arc::new(Settings::from_environment(environment).unwrap()),
            nats,
            bus,
            store,
            namespace,
            outbox_uri,
            relay_s3,
            fixtures,
        }
    }

    /// The ingest lane on `store`, taking deliveries until the returned sender is dropped or sent.
    fn lane(&self, store: Store, denies: bool, counted: &Counted) -> oneshot::Sender<()> {
        let (stop, stopped) = oneshot::channel::<()>();
        let lane = Arc::new(IngestLane::new(gate(&store, denies, counted)));
        let bus = Arc::clone(&self.bus);
        let settings = Lane::new(STREAM, CONSUMER, 5, Duration::from_secs(1), Duration::from_secs(30), 8).unwrap();
        tokio::spawn(async move {
            let _ = lane
                .run(&bus, &settings, async {
                    let _ = stopped.await;
                })
                .await;
        });
        stop
    }

    fn relay(&self, denies: bool, counted: &Counted) -> Relay {
        let (settings, store) = (Arc::clone(&self.settings), self.store.clone());
        Relay::new(
            settings,
            gate(&store, denies, counted),
            Some(Arc::clone(&self.bus)),
            Some(Arc::clone(&self.relay_s3)),
            None,
        )
        .unwrap()
    }

    /// Stage the event as the producer does, and publish it unless the bus is away.
    async fn produce(&self, data: &Map<String, Value>, publish: bool) -> CloudEvent {
        let envelope = lineage_envelope(data);
        self.producer.stage(&envelope.id, &envelope.staged_text()).await.unwrap();
        if publish {
            publish_event(&self.bus, &envelope, TOPIC, Duration::from_secs(5), None).await.unwrap();
        }
        envelope
    }

    async fn staged(&self) -> usize {
        Outbox::new(&self.outbox_uri, Arc::clone(&self.relay_s3))
            .unwrap()
            .listing()
            .await
            .unwrap()
            .len()
    }

    async fn feed_rows(&self) -> usize {
        self.store.list_events(100, None, Some(0), true).await.unwrap().len()
    }

    async fn refusals(&self) -> u64 {
        self.store.table(tables::REFUSALS).count_rows(None, None).await.unwrap()
    }

    /// The model's AFeedRowIsWhole for `data`: its feed row, and with it its run's binding and its edge.
    async fn whole(&self, data: &Map<String, Value>) -> bool {
        let key = feed_key(&parse_event(&Value::Object(data.clone())).unwrap());
        if !self.store.events_recorded([&key]).await.unwrap().contains(&key) {
            return false;
        }
        let run = data["run"]["runId"].as_str().unwrap();
        let bound = self.store.run_author(run).await.unwrap() == Some("dex~alice".to_owned());
        let (derived, _) = self.store.derived(&BTreeSet::from([SOURCE.to_owned()]), 3).await.unwrap();
        bound && derived.iter().any(|table| table.uuid.as_deref() == Some(TARGET))
    }
}

/// A proxy to the catalog that answers 503 to every write of the feed while `failing` holds, counting each.
struct Failing {
    url: String,
    failing: Arc<AtomicBool>,
    failed: Arc<AtomicUsize>,
}

async fn failing_proxy(catalog: &str) -> Failing {
    let (failing, failed) = (Arc::new(AtomicBool::new(true)), Arc::new(AtomicUsize::new(0)));
    let (upstream, client) = (catalog.to_owned(), reqwest::Client::new());
    let (fail, count) = (Arc::clone(&failing), Arc::clone(&failed));
    let forward = move |method: Method, uri: Uri, headers: HeaderMap, body: Bytes| {
        let (upstream, client, fail, count) = (upstream.clone(), client.clone(), Arc::clone(&fail), Arc::clone(&count));
        async move {
            if uri.path().ends_with("%24events/merge_insert") && fail.load(Ordering::SeqCst) {
                count.fetch_add(1, Ordering::SeqCst);
                return (
                    StatusCode::SERVICE_UNAVAILABLE,
                    HeaderMap::new(),
                    Bytes::from_static(b"{\"error\": \"failed\", \"code\": 21}"),
                );
            }
            let mut request = client.request(method, format!("{upstream}{uri}")).body(body);
            for name in ["content-type", "accept"] {
                if let Some(value) = headers.get(name) {
                    request = request.header(name, value.clone());
                }
            }
            let answer = request.send().await.unwrap();
            let status = StatusCode::from_u16(answer.status().as_u16()).unwrap();
            let mut kept = HeaderMap::new();
            if let Some(value) = answer.headers().get("content-type") {
                kept.insert("content-type", value.clone());
            }
            (status, kept, answer.bytes().await.unwrap())
        }
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, axum::Router::new().fallback(forward)).await });
    Failing { url, failing, failed }
}

/// The model's way "recorded by the lane", with the real Python producer: the Rust lane records the event the Python
/// producer published, and writes the rows the Python lane writes for it.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn way_recorded_by_the_lane_from_the_python_producer_as_the_python_lane_records_it() {
    let Some(fixtures) = fixtures() else { return };
    let estate = Estate::new(fixtures, &[]).await;
    let counted = Counted::new();
    let _lane = estate.lane(estate.store.clone(), false, &counted);
    let data = event(&format!("python-{}", unique()));
    let spec = json!({"bus_url": estate.nats.url, "token_file": estate.nats.token_file, "outbox_uri": estate.outbox_uri,
        "endpoint": estate.fixtures.endpoint, "issuer": estate.fixtures.issuer, "event": data});
    let produced = python("produce", spec).await;
    assert_eq!(produced["left_staged"], false);
    eventually("the Rust lane to record the event", async || estate.whole(&data).await).await;
    assert_eq!(estate.feed_rows().await, 1);
    assert_eq!(estate.staged().await, 0, "the producer dropped its staged copy once the bus took it");
    assert_eq!(counted.counted("subscriber", "ingested"), 1);

    let theirs = namespace(&estate.fixtures.catalog).await;
    let envelope = Value::Object(lineage_envelope(&data).wire());
    let answered = python(
        "lane",
        json!({"catalog_url": estate.fixtures.catalog, "namespace": theirs, "envelope": envelope}),
    )
    .await;
    assert_eq!(answered["verdict"], "taken");
    let rows = async |namespace: &[String]| python("rows", json!({"catalog_url": estate.fixtures.catalog, "namespace": namespace})).await;
    let ours = rows(&estate.namespace).await;
    let theirs = rows(&theirs).await;
    assert_eq!(ours, theirs, "the Rust lane's feed row, edges and run binding are the Python lane's");
    assert_eq!(ours["edges"].as_array().unwrap().len(), 1);
}

/// The model's way "refused and parked": a writer with no grant, which a grant would lift, is kept and counted.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn way_refused_and_parked() {
    let Some(fixtures) = fixtures() else { return };
    let estate = Estate::new(fixtures, &[]).await;
    let counted = Counted::new();
    let _lane = estate.lane(estate.store.clone(), true, &counted);
    let data = event(&format!("refused-{}", unique()));
    let envelope = estate.produce(&data, true).await;
    eventually("the delivery to be parked", async || counted.counted("dead_letter", "dead_lettered") == 1).await;
    let parked = estate.store.parked(None).await.unwrap();
    assert_eq!(parked.len(), 1);
    assert_eq!(parked[0].key, format!("{}{}", tables::PARKED_KEY_PREFIX, envelope.id));
    assert_eq!(parked[0].run_id.as_deref(), data["run"]["runId"].as_str());
    assert_eq!(counted.counted("subscriber", "refused"), 1);
    assert_eq!(estate.feed_rows().await, 0, "nothing is recorded for a refused writer");
}

/// A store that fails mid-record: the feed row, written last, is not there, the delivery is asked for again, and the
/// next delivery lands what was missing and nothing twice.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_store_failing_mid_record_leaves_the_delivery_asked_for_again_and_nothing_half_written() {
    let Some(fixtures) = fixtures() else { return };
    let estate = Estate::new(fixtures, &[]).await;
    let proxy = failing_proxy(&estate.fixtures.catalog).await;
    let through_proxy = store_at(&proxy.url, estate.namespace.clone()).await;
    let counted = Counted::new();
    let _lane = estate.lane(through_proxy, false, &counted);
    let data = event(&format!("failing-{}", unique()));
    estate.produce(&data, true).await;
    eventually("the feed's write to fail", async || proxy.failed.load(Ordering::SeqCst) >= 1).await;
    eventually("the lane to ask for the delivery again", async || counted.counted("subscriber", "retried") >= 1).await;
    assert_eq!(
        estate.feed_rows().await,
        0,
        "no feed row says the event is recorded while its writes are not all there"
    );
    assert!(
        estate.store.run_author(data["run"]["runId"].as_str().unwrap()).await.unwrap().is_some(),
        "the earlier write landed"
    );
    proxy.failing.store(false, Ordering::SeqCst);
    eventually("a later delivery to record the event", async || estate.whole(&data).await).await;
    assert_eq!(estate.feed_rows().await, 1);
    assert_eq!(counted.counted("subscriber", "ingested"), 1);
}

/// The relay drains an event the real Python producer staged while the bus was away: recorded, published again, dropped.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_event_staged_by_the_python_producer_with_the_bus_away_is_drained_by_the_rust_tick() {
    let Some(fixtures) = fixtures() else { return };
    let estate = Estate::new(fixtures, &[]).await;
    let data = event(&format!("away-{}", unique()));
    let spec = json!({"bus_url": "", "token_file": estate.nats.token_file, "outbox_uri": estate.outbox_uri,
        "endpoint": estate.fixtures.endpoint, "issuer": estate.fixtures.issuer, "event": data});
    assert_eq!(python("produce", spec).await["left_staged"], true);
    assert_eq!(estate.staged().await, 1);
    let report = estate.relay(false, &Counted::new()).tick().await;
    assert_eq!((report.outbox_drained, report.outbox_stranded, report.outbox_refused), (1, 0, 0), "{report:?}");
    assert!(estate.whole(&data).await);
    assert_eq!(estate.staged().await, 0, "the drained event is dropped");
    let counted = Counted::new();
    let _lane = estate.lane(estate.store.clone(), false, &counted);
    eventually("the copy published again to reach the lane", async || {
        counted.counted("subscriber", "ingested") == 1
    })
    .await;
    assert_eq!(estate.feed_rows().await, 1, "the copy lands no second row");
}

/// The model's way "lane fails, then the relay records": the relay records the staged copy while the lane's writes fail,
/// and the lane's later delivery lands nothing new.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn way_the_lane_fails_then_the_relay_records() {
    let Some(fixtures) = fixtures() else { return };
    let estate = Estate::new(fixtures, &[]).await;
    let proxy = failing_proxy(&estate.fixtures.catalog).await;
    let counted = Counted::new();
    let _lane = estate.lane(store_at(&proxy.url, estate.namespace.clone()).await, false, &counted);
    let data = event(&format!("relayed-{}", unique()));
    estate.produce(&data, true).await;
    eventually("the lane's write to fail", async || counted.counted("subscriber", "retried") >= 1).await;
    let report = estate.relay(false, &Counted::new()).tick().await;
    assert_eq!(report.outbox_drained, 1, "{report:?}");
    assert!(estate.whole(&data).await, "NoLoss, and the feed row is whole");
    assert_eq!(estate.staged().await, 0);
    proxy.failing.store(false, Ordering::SeqCst);
    eventually("the lane to take the delivery", async || counted.counted("subscriber", "ingested") >= 1).await;
    assert_eq!(estate.feed_rows().await, 1);
}

/// The model's way "the relay refuses": a staged event whose writer has no grant is kept as refused, then dropped.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn way_the_relay_refuses() {
    let Some(fixtures) = fixtures() else { return };
    let estate = Estate::new(fixtures, &[]).await;
    let data = event(&format!("relay-refused-{}", unique()));
    estate.produce(&data, false).await;
    let counted = Counted::new();
    let report = estate.relay(true, &counted).tick().await;
    assert_eq!(
        (report.outbox_refused, report.outbox_refusals_recorded, report.outbox_drained),
        (1, 1, 0),
        "{report:?}"
    );
    assert_eq!(counted.counted("outbox", "refused"), 1);
    assert_eq!(estate.refusals().await, 1, "NoLoss: the refusal is kept where an admin finds it");
    assert_eq!(estate.staged().await, 0);
    assert_eq!(estate.feed_rows().await, 0, "OnlyAllowedEventsAreRecorded");
}

/// The model's way "both race": the lane and the relay record one event at once; one feed row, whole, and nothing staged.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn way_the_lane_and_the_relay_race() {
    let Some(fixtures) = fixtures() else { return };
    let estate = Estate::new(fixtures, &[]).await;
    let data = event(&format!("race-{}", unique()));
    estate.produce(&data, true).await;
    let counted = Counted::new();
    let _lane = estate.lane(estate.store.clone(), false, &counted);
    let relay = estate.relay(false, &Counted::new());
    let (report, ()) = tokio::join!(
        relay.tick(),
        eventually("the lane to take it", async || counted.counted("subscriber", "ingested") >= 1)
    );
    assert_eq!(report.outbox_drained + report.outbox_stranded, 1, "{report:?}");
    if estate.staged().await > 0 {
        assert_eq!(relay.tick().await.outbox_drained, 1, "a stranded copy drains on the next tick");
    }
    assert_eq!(estate.staged().await, 0);
    assert!(estate.whole(&data).await);
    assert_eq!(estate.feed_rows().await, 1);
    assert_eq!(estate.refusals().await, 0);
}

/// Two replicas' ticks in one window run once, on the real store's conditional writes, under the relay's own policy.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn two_rust_ticks_in_one_window_run_once() {
    let Some(fixtures) = fixtures() else { return };
    let root = format!("s3://{}", fixtures.bucket);
    let (relay_s3, _) = store_session(&fixtures, RELAY).await;
    let replicas = ["replica-a", "replica-b"].map(|holder| Timer::new(RELAY_TIMER, 4.0, Records::new(&root, Arc::clone(&relay_s3)).unwrap(), holder).unwrap());
    for window in 0..2 {
        let ran = Arc::new(AtomicUsize::new(0));
        let work = || async {
            ran.fetch_add(1, Ordering::SeqCst);
            Ok(())
        };
        let (a, b) = tokio::join!(replicas[0].turn(false, work()), replicas[1].turn(false, work()));
        let outcomes = BTreeSet::from([format!("{:?}", a.outcome), format!("{:?}", b.outcome)]);
        assert_eq!(ran.load(Ordering::SeqCst), 1, "window {window}: {outcomes:?}");
        assert_eq!(outcomes, BTreeSet::from(["Elsewhere".to_owned(), "Ran".to_owned()]));
        assert_eq!(a.window, b.window, "both name one window");
        let again = replicas[0].turn(false, async { Err("a window runs once".to_owned()) }).await;
        assert_eq!(again.outcome, Outcome::Elsewhere);
        tokio::time::sleep(a.wait.max(b.wait) + Duration::from_millis(1100)).await;
    }
}

/// The backend the forwarder sends to: every event it took, and how many answers it is to fail.
#[derive(Default)]
struct Sink {
    taken: Mutex<Vec<Value>>,
    failures: AtomicUsize,
}

async fn sink() -> (Arc<Sink>, String) {
    let sink = Arc::new(Sink::default());
    let held = Arc::clone(&sink);
    let take = move |body: Bytes| {
        let sink = Arc::clone(&held);
        async move {
            if sink.failures.load(Ordering::SeqCst) > 0 {
                sink.failures.fetch_sub(1, Ordering::SeqCst);
                return StatusCode::SERVICE_UNAVAILABLE;
            }
            sink.taken.lock().unwrap().push(serde_json::from_slice(&body).unwrap());
            StatusCode::CREATED
        }
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, axum::Router::new().route("/api/v1/lineage", axum::routing::post(take))).await });
    (sink, url)
}

fn runs(sink: &Sink) -> Vec<String> {
    sink.taken
        .lock()
        .unwrap()
        .iter()
        .map(|event| event["run"]["runId"].as_str().unwrap().to_owned())
        .collect()
}

/// Every recorded event reaches the backend at least once, in order, and the cursor survives a restart.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn the_forwarder_sends_every_recorded_event_and_its_cursor_survives_a_restart() {
    let Some(fixtures) = fixtures() else { return };
    let (sink, url) = sink().await;
    let estate = Estate::new(fixtures, &[("LINEAGE_FORWARD_URL", &url)]).await;
    let record = async |runs: &[&str]| {
        for run in runs {
            let event = parse_event(&Value::Object(event(run))).unwrap();
            assert_eq!(estate.store.record(&event, &[]).await, lineage::intake::Recording::Landed);
        }
    };
    record(&["f1", "f2", "f3"]).await;
    let report = estate.relay(false, &Counted::new()).tick().await;
    assert_eq!((report.forwarded, report.forward_stalled.clone()), (3, None), "{report:?}");
    assert_eq!(runs(&sink), ["f1", "f2", "f3"]);

    let restarted = || async {
        let store = store_at(&estate.fixtures.catalog, estate.namespace.clone()).await;
        Relay::new(
            Arc::clone(&estate.settings),
            gate(&store, false, &Counted::new()),
            None,
            Some(Arc::clone(&estate.relay_s3)),
            None,
        )
        .unwrap()
    };
    assert_eq!(restarted().await.tick().await.forwarded, 0, "a restarted relay sends nothing its cursor passed");
    record(&["f4", "f5"]).await;
    sink.failures.store(1, Ordering::SeqCst);
    let stalled = restarted().await.tick().await;
    assert_eq!(stalled.forward_stalled.as_deref(), Some("HTTP 503"), "{stalled:?}");
    let resumed = restarted().await.tick().await;
    assert_eq!((resumed.forwarded, resumed.forward_stalled), (2, None));
    assert_eq!(runs(&sink), ["f1", "f2", "f3", "f4", "f5"]);
    let feed = estate.store.feed_incarnation().await.unwrap();
    assert_eq!(estate.store.cursor(&estate.settings.follower(&feed)).await.unwrap(), 5);
}
