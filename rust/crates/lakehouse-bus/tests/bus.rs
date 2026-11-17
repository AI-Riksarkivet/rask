//! The bus against a real nats-server with JetStream: the steps of `specs/lineage/EventPath.tla` a bus takes (Deliver,
//! Again, Park, ReplicaDies), the in-flight bound, the read by sequence, publishing and the health read (ADR 0225).
//!
//! Each test starts its own server on a free port with a store of its own, fetching the pinned release with
//! `scripts/nats_server.sh` when `.localbin/` does not hold it. The stream and its consumer are declared here as the
//! chart's stream Job declares them; the bus under test creates neither.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_bus::{Bus, BusSettings, CloudEvent, Handler, PublishError, lineage_envelope, publish_event};
use lakehouse_core::delivery::{Delivery, Lane, Verdict};
use serde_json::{Value, json};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tokio::sync::{Notify, Semaphore, oneshot};
use tokio::task::JoinHandle;
use tokio::time::{Instant, sleep};

const STREAM: &str = "LINEAGE";
const CONSUMER: &str = "lineage-ingest";
const TOPIC: &str = "lineage.events.v1";
/// The ack wait the consumer is declared with here: short, so a delivery left unsettled comes again within a test.
const ACK_WAIT: Duration = Duration::from_secs(2);
/// The lane's own ack wait must leave its settle margin; it only bounds a handling's time, which these never reach.
const LANE_ACK_WAIT: Duration = Duration::from_secs(11);
const WITHIN: Duration = Duration::from_secs(5);

fn nats_server() -> &'static Path {
    static BINARY: OnceLock<PathBuf> = OnceLock::new();
    BINARY.get_or_init(|| {
        let script = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../scripts/nats_server.sh");
        let output = Command::new("bash")
            .arg(script)
            .stderr(Stdio::inherit())
            .output()
            .unwrap_or_else(|error| panic!("could not run {script} to fetch the pinned nats-server: {error}"));
        assert!(
            output.status.success(),
            "{script} could not fetch the pinned nats-server (scripts/toolchain.sha256); these tests need it and no docker"
        );
        PathBuf::from(String::from_utf8(output.stdout).unwrap().trim())
    })
}

/// One nats-server with JetStream, on a free port, with a store and a token file of its own.
struct Server {
    child: Option<Child>,
    port: u16,
    dir: PathBuf,
    token: String,
}

impl Server {
    fn start() -> Server {
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let dir = std::env::temp_dir().join(format!("lakehouse-bus-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut server = Server {
            child: None,
            port,
            dir,
            token: String::new(),
        };
        server.run("first-token");
        server
    }

    /// Run the server (again) on its port and store, taking `token` from now on, and write it where the pod reads it.
    fn run(&mut self, token: &str) {
        self.token = token.to_owned();
        std::fs::write(self.token_file(), format!("{token}\n")).unwrap();
        let child = Command::new(nats_server())
            .args(["-js", "-a", "127.0.0.1", "-p", &self.port.to_string(), "--auth", token])
            .arg("-sd")
            .arg(self.dir.join("store"))
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        self.child = Some(child);
        let until = std::time::Instant::now() + Duration::from_secs(10);
        while TcpStream::connect(("127.0.0.1", self.port)).is_err() {
            assert!(std::time::Instant::now() < until, "nats-server did not listen on {}", self.port);
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    fn url(&self) -> String {
        format!("nats://127.0.0.1:{}", self.port)
    }

    fn token_file(&self) -> PathBuf {
        self.dir.join("token")
    }

    fn settings(&self) -> BusSettings {
        BusSettings {
            url: self.url(),
            user: "lineage".to_owned(),
            token_file: self.token_file(),
            ca_file: None,
            session: Duration::from_secs(300),
        }
        .validated()
        .unwrap()
    }

    async fn admin(&self) -> async_nats::Client {
        async_nats::ConnectOptions::with_token(self.token.clone()).connect(self.url()).await.unwrap()
    }

    /// Declare a stream as the chart's stream Job does (`declare_stream`), with `extra` members on top.
    async fn declare_stream(&self, name: &str, subjects: &str, extra: Value) {
        let mut config = json!({
            "name": name, "subjects": [subjects], "retention": "limits", "max_consumers": -1, "max_msgs": -1, "max_bytes": -1,
            "max_msgs_per_subject": -1, "max_msg_size": -1, "max_age": 604_800_000_000_000_u64, "storage": "file", "discard": "old",
            "num_replicas": 1, "duplicate_window": 120_000_000_000_u64, "deny_delete": true, "deny_purge": true, "allow_rollup_hdrs": false,
            "allow_direct": true
        });
        config.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        self.api(&format!("$JS.API.STREAM.CREATE.{name}"), config).await;
    }

    /// Declare the durable consumer as `lance.busConsumers` declares lineage's: explicit acks, no delivery limit.
    async fn declare_consumer(&self, ack_wait: Duration, max_ack_pending: u64) {
        let config = json!({
            "durable_name": CONSUMER, "ack_policy": "explicit", "ack_wait": ack_wait.as_nanos() as u64, "max_deliver": -1,
            "filter_subject": TOPIC, "deliver_policy": "all", "replay_policy": "instant", "max_ack_pending": max_ack_pending,
            "max_waiting": 512
        });
        self.api(
            &format!("$JS.API.CONSUMER.CREATE.{STREAM}.{CONSUMER}"),
            json!({"stream_name": STREAM, "config": config, "action": "create"}),
        )
        .await;
    }

    async fn api(&self, subject: &str, body: Value) {
        let reply = self.admin().await.request(subject.to_owned(), body.to_string().into()).await.unwrap();
        let answer: Value = serde_json::from_slice(&reply.payload).unwrap();
        assert!(answer.get("error").is_none(), "{subject}: {answer}");
    }

    /// The lineage stream and its consumer, declared, and a bus connected to them.
    async fn lineage(ack_wait: Duration, max_ack_pending: u64) -> (Server, Arc<Bus>) {
        let server = Server::start();
        server.declare_stream(STREAM, "lineage.events.>", json!({})).await;
        server.declare_consumer(ack_wait, max_ack_pending).await;
        let bus = server.connected().await;
        (server, bus)
    }

    async fn connected(&self) -> Arc<Bus> {
        let bus = Bus::start(self.settings());
        eventually("the bus to connect", || bus.answering().is_ok()).await;
        Arc::new(bus)
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.stop();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

async fn eventually(what: &str, mut holds: impl FnMut() -> bool) {
    let until = Instant::now() + Duration::from_secs(30);
    while !holds() {
        assert!(Instant::now() < until, "timed out waiting for {what}");
        sleep(Duration::from_millis(20)).await;
    }
}

fn event(run: &str) -> CloudEvent {
    let event = json!({"eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z", "run": {"runId": run}, "producer": "rask/catalog"});
    lineage_envelope(event.as_object().unwrap())
}

/// A delivery a handling saw: its sequence, its attempt, when, and its envelope's id.
type Seen = (u64, u32, Instant, Option<String>);

#[derive(Clone, Copy)]
enum Behaviour {
    Take,
    AgainOnFirst,
    Fail,
    Hang,
    Gated,
}

/// A service's handling that does what its behaviour says and records every delivery and park.
struct Recorder {
    behaviour: Behaviour,
    seen: Mutex<Vec<Seen>>,
    parked: Mutex<Vec<(u32, String)>>,
    started: Notify,
    gate: Semaphore,
}

impl Recorder {
    fn new(behaviour: Behaviour) -> Arc<Self> {
        Arc::new(Recorder {
            behaviour,
            seen: Mutex::new(Vec::new()),
            parked: Mutex::new(Vec::new()),
            started: Notify::new(),
            gate: Semaphore::new(0),
        })
    }

    fn seen(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().clone()
    }
}

impl Handler for Recorder {
    async fn handle(&self, delivery: &Delivery) -> Result<Verdict, String> {
        let id = delivery
            .envelope()
            .and_then(|envelope| envelope.get("id").and_then(Value::as_str).map(str::to_owned));
        self.seen.lock().unwrap().push((delivery.sequence, delivery.attempt, Instant::now(), id));
        self.started.notify_one();
        match self.behaviour {
            Behaviour::Take => Ok(Verdict::Taken),
            Behaviour::AgainOnFirst if delivery.attempt == 1 => Ok(Verdict::Again),
            Behaviour::AgainOnFirst => Ok(Verdict::Taken),
            Behaviour::Fail => Err("ConnectionError: the catalog did not answer".to_owned()),
            Behaviour::Hang => std::future::pending().await,
            Behaviour::Gated => {
                let _permit = self.gate.acquire().await.map_err(|error| error.to_string())?;
                Ok(Verdict::Taken)
            }
        }
    }

    async fn park(&self, delivery: &Delivery, reason: &str) -> Result<(), String> {
        self.parked.lock().unwrap().push((delivery.attempt, reason.to_owned()));
        Ok(())
    }
}

/// `bus` consuming `lane` into `recorder` until the returned sender is dropped or sent on.
fn consuming(bus: &Arc<Bus>, lane: Lane, recorder: &Arc<Recorder>) -> (oneshot::Sender<()>, JoinHandle<()>) {
    let (stop, stopped) = oneshot::channel::<()>();
    let (bus, recorder) = (Arc::clone(bus), Arc::clone(recorder));
    let task = tokio::spawn(async move {
        let stop = async {
            let _ = stopped.await;
        };
        bus.consume(&lane, recorder, stop).await.unwrap();
    });
    (stop, task)
}

fn lane(attempts: u32, retry: Duration, in_flight: usize) -> Lane {
    Lane::new(STREAM, CONSUMER, attempts, retry, LANE_ACK_WAIT, in_flight).unwrap()
}

/// Deliver, then Judge taking it: the envelope published arrives whole, and once taken it is not delivered again.
#[tokio::test(flavor = "multi_thread")]
async fn a_published_envelope_is_delivered_and_once_taken_is_not_delivered_again() {
    let (_server, bus) = Server::lineage(ACK_WAIT, 1000).await;
    let envelope = event("run-1");
    publish_event(&bus, &envelope, TOPIC, WITHIN, None).await.unwrap();
    let recorder = Recorder::new(Behaviour::Take);
    let (stop, task) = consuming(&bus, lane(3, Duration::from_secs(1), 4), &recorder);
    eventually("the delivery", || !recorder.seen().is_empty()).await;
    sleep(ACK_WAIT * 2).await;
    let seen = recorder.seen();
    assert_eq!(seen.len(), 1, "a taken delivery came again: {seen:?}");
    assert_eq!((seen[0].0, seen[0].1, seen[0].3.as_deref()), (1, 1, Some(envelope.id.as_str())));
    stop.send(()).unwrap();
    task.await.unwrap();
}

/// Again: a delivery asked for again comes back after the lane's delay as its second attempt, and is then taken.
#[tokio::test(flavor = "multi_thread")]
async fn a_delivery_asked_for_again_comes_back_after_the_delay_as_attempt_two() {
    let (_server, bus) = Server::lineage(ACK_WAIT, 1000).await;
    publish_event(&bus, &event("run-1"), TOPIC, WITHIN, None).await.unwrap();
    let recorder = Recorder::new(Behaviour::AgainOnFirst);
    let retry = Duration::from_millis(1500);
    let (stop, task) = consuming(&bus, lane(3, retry, 4), &recorder);
    eventually("the second delivery", || recorder.seen().len() == 2).await;
    sleep(ACK_WAIT * 2).await;
    let seen = recorder.seen();
    assert_eq!(
        seen.iter().map(|(sequence, attempt, ..)| (*sequence, *attempt)).collect::<Vec<_>>(),
        [(1, 1), (1, 2)]
    );
    assert!(
        seen[1].2 - seen[0].2 >= retry,
        "asked for again sooner than its delay: {:?}",
        seen[1].2 - seen[0].2
    );
    stop.send(()).unwrap();
    task.await.unwrap();
}

/// Park: a handling that fails on every attempt is parked on the lane's last and ended, so the bus delivers it no more.
#[tokio::test(flavor = "multi_thread")]
async fn a_delivery_that_fails_every_attempt_is_parked_on_the_last_and_ended() {
    let (_server, bus) = Server::lineage(ACK_WAIT, 1000).await;
    publish_event(&bus, &event("run-1"), TOPIC, WITHIN, None).await.unwrap();
    let recorder = Recorder::new(Behaviour::Fail);
    let (stop, task) = consuming(&bus, lane(3, Duration::from_millis(200), 4), &recorder);
    eventually("the park", || !recorder.parked.lock().unwrap().is_empty()).await;
    sleep(ACK_WAIT * 2).await;
    assert_eq!(recorder.seen().iter().map(|(_, attempt, ..)| *attempt).collect::<Vec<_>>(), [1, 2, 3]);
    assert_eq!(
        *recorder.parked.lock().unwrap(),
        [(3, "its last delivery (3 of 3) failed: ConnectionError: the catalog did not answer".to_owned())]
    );
    stop.send(()).unwrap();
    task.await.unwrap();
}

/// ReplicaDies: a replica gone mid-handling settles nothing, and the bus hands the delivery to the next after the ack wait.
#[tokio::test(flavor = "multi_thread")]
async fn a_delivery_whose_replica_dies_mid_handling_is_handed_out_again_after_the_ack_wait() {
    let (server, dying) = Server::lineage(ACK_WAIT, 1000).await;
    publish_event(&dying, &event("run-1"), TOPIC, WITHIN, None).await.unwrap();
    let hanging = Recorder::new(Behaviour::Hang);
    let (_stop, task) = consuming(&dying, lane(3, Duration::from_secs(1), 4), &hanging);
    hanging.started.notified().await;
    let died = Instant::now();
    task.abort();
    let _ = task.await;
    drop(dying);
    let next = server.connected().await;
    let recorder = Recorder::new(Behaviour::Take);
    let (stop, task) = consuming(&next, lane(3, Duration::from_secs(1), 4), &recorder);
    eventually("the delivery handed out again", || !recorder.seen().is_empty()).await;
    let seen = recorder.seen();
    assert_eq!((seen[0].0, seen[0].1), (1, 2));
    assert!(
        seen[0].2 - died >= ACK_WAIT - Duration::from_millis(200),
        "handed out before the ack wait ran out"
    );
    stop.send(()).unwrap();
    task.await.unwrap();
}

/// The in-flight bound: with the lane's bound in hand, the next delivery waits until a handling ends, however many the
/// consumer would hand out.
#[tokio::test(flavor = "multi_thread")]
async fn no_more_deliveries_are_handled_at_once_than_the_lane_allows() {
    let (_server, bus) = Server::lineage(Duration::from_secs(30), 100).await;
    for run in ["run-1", "run-2", "run-3"] {
        publish_event(&bus, &event(run), TOPIC, WITHIN, None).await.unwrap();
    }
    let recorder = Recorder::new(Behaviour::Gated);
    // The lane's ack wait is the consumer's here, so a handling held at the gate is not cut short and asked for again.
    let bounded = Lane::new(STREAM, CONSUMER, 3, Duration::from_secs(1), Duration::from_secs(30), 2).unwrap();
    let (stop, task) = consuming(&bus, bounded, &recorder);
    eventually("two handlings", || recorder.seen().len() == 2).await;
    sleep(Duration::from_secs(2)).await;
    assert_eq!(recorder.seen().len(), 2, "a third delivery was handled beside two in flight");
    recorder.gate.add_permits(1);
    eventually("the third handling", || recorder.seen().len() == 3).await;
    recorder.gate.add_permits(2);
    stop.send(()).unwrap();
    task.await.unwrap();
    assert_eq!(
        recorder.seen().iter().map(|(sequence, attempt, ..)| (*sequence, *attempt)).collect::<Vec<_>>(),
        [(1, 1), (2, 1), (3, 1)]
    );
}

/// A stop lets a handling already begun finish and settle it: nothing is handed out again.
#[tokio::test(flavor = "multi_thread")]
async fn a_stop_finishes_and_settles_what_is_in_flight() {
    let (_server, bus) = Server::lineage(ACK_WAIT, 1000).await;
    publish_event(&bus, &event("run-1"), TOPIC, WITHIN, None).await.unwrap();
    let recorder = Recorder::new(Behaviour::Gated);
    let (stop, task) = consuming(&bus, lane(3, Duration::from_secs(1), 4), &recorder);
    recorder.started.notified().await;
    stop.send(()).unwrap();
    sleep(Duration::from_millis(200)).await;
    assert!(!task.is_finished(), "the consumer returned with a handling in flight");
    recorder.gate.add_permits(1);
    task.await.unwrap();
    let again = Recorder::new(Behaviour::Take);
    let (stop, task) = consuming(&bus, lane(3, Duration::from_secs(1), 4), &again);
    sleep(ACK_WAIT * 2).await;
    assert!(again.seen().is_empty(), "a delivery finished at the stop came again");
    stop.send(()).unwrap();
    task.await.unwrap();
}

/// A read by sequence returns what follows a cursor, the last messages for a new reader, and says when a cursor is
/// older than what the stream keeps.
#[tokio::test(flavor = "multi_thread")]
async fn a_read_by_sequence_returns_what_follows_a_cursor_and_reports_one_older_than_retention() {
    let server = Server::start();
    server.declare_stream("CATALOG_CONTROL", "catalog.control.v1", json!({})).await;
    server.declare_stream("KEPT", "kept.v1", json!({"max_msgs": 3})).await;
    let bus = server.connected().await;
    for run in 1..=6 {
        publish_event(&bus, &event(&format!("run-{run}")), "catalog.control.v1", WITHIN, None)
            .await
            .unwrap();
        publish_event(&bus, &event(&format!("run-{run}")), "kept.v1", WITHIN, None).await.unwrap();
    }
    let sequences = |page: &lakehouse_bus::StreamPage| page.messages.iter().map(|message| message.sequence).collect::<Vec<_>>();
    let newest = bus.read("CATALOG_CONTROL", "catalog.control.v1", 0, 3).await.unwrap();
    assert_eq!((sequences(&newest), newest.end, newest.gap), (vec![4, 5, 6], 6, false));
    assert_eq!(newest.messages[0].envelope().unwrap()["id"], "run-4@COMPLETE");
    let after = bus.read("CATALOG_CONTROL", "catalog.control.v1", 2, 2).await.unwrap();
    assert_eq!((sequences(&after), after.gap), (vec![3, 4], false));
    let caught_up = bus.read("CATALOG_CONTROL", "catalog.control.v1", 6, 10).await.unwrap();
    assert_eq!((sequences(&caught_up), caught_up.gap), (vec![], false));
    let past_the_end = bus.read("CATALOG_CONTROL", "catalog.control.v1", 9, 10).await.unwrap();
    assert!(past_the_end.gap && past_the_end.messages.is_empty());
    let dropped = bus.read("KEPT", "kept.v1", 1, 10).await.unwrap();
    assert_eq!((sequences(&dropped), dropped.gap), (vec![4, 5, 6], true));
    assert!(
        bus.read("MISSING", "missing.v1", 0, 10).await.is_err(),
        "a stream the bus does not hold read as empty"
    );
}

/// The stream's acknowledgement of a duplicate is believed only when the message kept under the id carries the event.
#[tokio::test(flavor = "multi_thread")]
async fn a_duplicate_is_delivered_once_and_a_message_another_stored_under_its_id_is_not_believed() {
    let (server, bus) = Server::lineage(ACK_WAIT, 1000).await;
    let envelope = event("run-1");
    publish_event(&bus, &envelope, TOPIC, WITHIN, None).await.unwrap();
    publish_event(&bus, &envelope, TOPIC, WITHIN, None).await.unwrap();
    assert_eq!(bus.read(STREAM, TOPIC, 0, 10).await.unwrap().end, 1);
    let forged = event("run-2");
    let mut headers = async_nats::HeaderMap::new();
    headers.insert("Nats-Msg-Id", forged.id.as_str());
    let admin = server.admin().await;
    admin
        .request_with_headers(TOPIC, headers, br#"{"data": {"forged": true}}"#.to_vec().into())
        .await
        .unwrap();
    let refused = publish_event(&bus, &forged, TOPIC, WITHIN, None).await;
    assert!(matches!(refused, Err(PublishError::Unavailable(_))), "{refused:?}");
}

/// The health read fails while the server is away and answers again once it is back, on the token the file holds then.
#[tokio::test(flavor = "multi_thread")]
async fn the_health_read_fails_while_the_server_is_away_and_recovers_on_the_token_the_file_holds_then() {
    let (mut server, bus) = Server::lineage(ACK_WAIT, 1000).await;
    publish_event(&bus, &event("run-1"), TOPIC, WITHIN, None).await.unwrap();
    server.stop();
    eventually("the health read to fail", || bus.answering().is_err()).await;
    let refused = publish_event(&bus, &event("run-2"), TOPIC, WITHIN, None).await;
    assert!(matches!(refused, Err(PublishError::Unavailable(_))), "{refused:?}");
    server.run("second-token");
    eventually("the health read to recover", || bus.answering().is_ok()).await;
    publish_event(&bus, &event("run-2"), TOPIC, WITHIN, None).await.unwrap();
    assert_eq!(bus.read(STREAM, TOPIC, 0, 10).await.unwrap().end, 2);
}
