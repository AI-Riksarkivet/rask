//! What lineage runs without a request answers as the Python does (`rust/vectors/lanes.json`, ADR 0217 §5): the
//! settings, the timer's turns, the relay tick, the forwarder and the park.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use chrono::DateTime;
use lakehouse_core::delivery::Delivery;
use lakehouse_core::outbox::Staged;
use lakehouse_core::storage::records::{Found, StoreRead, Unless};
use lakehouse_core::storage::{Listed, StorageError};
use lakehouse_core::telemetry::instruments::LineageOutcome as Counted;
use lakehouse_core::timers::{Claims, Outcome, Timer};
use lakehouse_tables::CatalogError;
use lineage::forward::{Followed, Forwarded, forward};
use lineage::intake::{Authorizing, Recording, Refusal};
use lineage::lane::parking;
use lineage::model::Event;
use lineage::relay::Effects;
use lineage::settings::Settings;
use lineage::store::rows::EventRecord;
use lineage::store::window::{FeedReader, FeedWindow};
use lineage::tick::{Tick, tick};
use serde_json::{Map, Value, json};
use std::cell::RefCell;
use std::sync::Mutex;
use std::time::Duration;

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/lanes.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

fn environment(env: &Value) -> Vec<(String, String)> {
    env.as_object()
        .unwrap()
        .iter()
        .map(|(name, value)| (name.clone(), value.as_str().unwrap().to_owned()))
        .collect()
}

#[test]
fn the_settings_are_read_as_python_reads_them() {
    let cases = vectors("settings");
    for case in &cases {
        let read = Settings::from_environment(environment(&case["env"]));
        let label = case["env"].to_string();
        if let Some(refused) = case["refused"].as_array() {
            let error = read.expect_err(&label);
            assert!(error.contains(refused[0].as_str().unwrap()), "{label}: {error} names {refused:?}");
            continue;
        }
        let read = read.unwrap_or_else(|error| panic!("{label}: {error}"));
        let expected = &case["accepted"];
        let seconds = |duration: Duration| json!(duration.as_secs_f64());
        let got = json!({
            "catalog_url": read.catalog_url,
            "catalog_identity_token_file": read.catalog_identity_token_file,
            "provenance_namespace": read.provenance_namespace,
            "catalog_dataset_namespace": read.catalog_dataset_namespace,
            "catalog_timeout_seconds": seconds(read.catalog_timeout),
            "forward_url": read.forward_url,
            "forward_endpoint": read.forward_endpoint,
            "forward_api_key_secret": read.forward_api_key_secret,
            "forward_api_key_field": read.forward_api_key_field,
            "forward_batch": read.forward_batch,
            "forward_timeout_seconds": seconds(read.forward_timeout),
            "events_topic": read.events_topic,
            "deliveries_in_flight": read.deliveries_in_flight,
            "delivery_attempts": read.delivery_attempts,
            "delivery_retry_seconds": seconds(read.delivery_retry),
            "delivery_ack_wait_seconds": seconds(read.delivery_ack_wait),
            "publish_timeout_seconds": seconds(read.publish_timeout),
            "relay_period_seconds": read.relay_period_seconds,
            "timer_root": read.timer_root,
            "read_only": read.read_only,
            "outbox_uri": read.outbox_uri,
            "outbox_drain_limit": read.outbox_drain_limit,
            "events_retention_days": read.events_retention_days,
            "audit_enabled": read.audit_enabled,
            "docs_enabled": read.docs_enabled,
            "max_body_bytes": read.max_body_bytes,
            "request_deadline_seconds": seconds(read.request_deadline),
            "signers": read.signing.signers,
            "delegators": read.signing.delegators,
            "on_the_store": read.store.is_some(),
        });
        for (field, value) in expected.as_object().unwrap() {
            let matches = match (value.as_f64(), got[field].as_f64()) {
                (Some(python), Some(rust)) => (python - rust).abs() < 1e-9,
                _ => *value == got[field],
            };
            assert!(matches, "{label}: {field} is {} where Python reads {value}", got[field]);
        }
        read.lane().unwrap();
    }
    assert!(cases.len() >= 70);
}

fn micros(iso: &str) -> i64 {
    DateTime::parse_from_rfc3339(iso).unwrap().timestamp_micros()
}

/// A claim's store, answering what the case says and keeping each write.
struct Store<'c> {
    case: &'c Value,
    writes: Mutex<Vec<Value>>,
}

impl Claims for &Store<'_> {
    async fn read_at(&self, key: &str) -> Result<StoreRead, StorageError> {
        assert_eq!(key, "_timers/lineage-relay.json");
        let at = DateTime::parse_from_rfc3339(self.case["at"].as_str().unwrap()).unwrap().to_utc();
        let found = match self.case["found"].as_str().unwrap() {
            "fails" => return Err(StorageError::NoAnswer("the store did not answer".to_owned())),
            "denied" => Found::Denied(StorageError::Refused {
                status: 403,
                code: "AccessDenied".to_owned(),
            }),
            "absent" => Found::Absent,
            _ => Found::Held {
                body: self.case["record"].as_str().unwrap().as_bytes().to_vec(),
                etag: "etag-1".to_owned(),
            },
        };
        Ok(StoreRead { found, at })
    }

    async fn put(&self, key: &str, body: Vec<u8>, unless: Unless<'_>) -> Result<bool, StorageError> {
        let unless = match unless {
            Unless::Present => "present".to_owned(),
            Unless::Changed(etag) => etag.to_owned(),
        };
        let body = String::from_utf8(body).unwrap();
        self.writes.lock().unwrap().push(json!({"key": key, "unless": unless, "body": body}));
        match self.case["put"].as_str().unwrap() {
            "lost" => Ok(false),
            "fails" => Err(StorageError::NoAnswer("the store did not answer".to_owned())),
            _ => Ok(true),
        }
    }
}

#[tokio::test]
async fn a_timer_turns_as_pythons_does() {
    let cases = vectors("turns");
    for case in &cases {
        let label = format!("{} {} {} put {} work {}", case["found"], case["record"], case["at"], case["put"], case["work"]);
        let store = Store {
            case,
            writes: Mutex::default(),
        };
        let forgot = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));
        let counted = std::sync::Arc::clone(&forgot);
        let timer = Timer::new("lineage-relay", case["period"].as_f64().unwrap(), &store, "replica-a")
            .unwrap()
            .on_elsewhere(move || {
                counted.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            });
        let mut worked = false;
        let work = async {
            worked = true;
            if case["work"] == "fails" { Err("the tick failed".to_owned()) } else { Ok(()) }
        };
        let turn = timer.turn(case["held"].as_bool().unwrap(), work).await;
        let outcome = match turn.outcome {
            Outcome::Ran => "ran",
            Outcome::Failed => "failed",
            Outcome::Elsewhere => "elsewhere",
            Outcome::Held => "held",
            Outcome::Unread => "unread",
        };
        assert_eq!(outcome, case["outcome"], "{label}");
        assert_eq!(turn.window, case["window"].as_str().map(micros), "{label}: the window");
        let wait = case["wait"].as_f64().unwrap();
        assert!((turn.wait.as_secs_f64() - wait).abs() < 0.01, "{label}: waits {:?}, Python {wait}", turn.wait);
        assert_eq!(worked, case["worked"].as_bool().unwrap(), "{label}: whether it worked");
        let forgotten = forgot.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(
            json!(forgotten),
            case["forgot"],
            "{label}: how often what only the window's runner knows was forgotten"
        );
        assert_eq!(Value::Array(store.writes.lock().unwrap().clone()), case["writes"], "{label}: the writes");
    }
    assert!(cases.len() >= 20);
}

/// A claim's store that takes its time to answer a read.
struct Slow<'c>(Store<'c>);

impl Claims for &Slow<'_> {
    async fn read_at(&self, key: &str) -> Result<StoreRead, StorageError> {
        tokio::time::sleep(Duration::from_millis(300)).await;
        (&self.0).read_at(key).await
    }

    async fn put(&self, key: &str, body: Vec<u8>, unless: Unless<'_>) -> Result<bool, StorageError> {
        (&self.0).put(key, body, unless).await
    }
}

/// `until_next` is what was left of the window when the store answered, so a read the store was slow to answer shortens
/// no wait: counted in, the loop woke before its window ended and found its own claim as another replica's.
#[tokio::test]
async fn a_slow_read_of_the_claim_shortens_no_wait() {
    let cases = vectors("turns");
    let case = cases
        .iter()
        .find(|case| case["outcome"] == "ran" && case["wait"].as_f64().unwrap() > 1.0)
        .unwrap();
    let store = Slow(Store {
        case,
        writes: Mutex::default(),
    });
    let timer = Timer::new("lineage-relay", case["period"].as_f64().unwrap(), &store, "replica-a").unwrap();
    let turn = timer.turn(false, async { Ok(()) }).await;
    let wait = case["wait"].as_f64().unwrap();
    assert!((turn.wait.as_secs_f64() - wait).abs() < 0.05, "waits {:?}, Python {wait}", turn.wait);
}

/// A tick's effects, each noted and answered as the case's staged keys say.
struct Ticker<'c> {
    case: &'c Value,
    effects: Vec<String>,
    counted: Vec<String>,
    draining: String,
}

fn kind(name: &str) -> &str {
    name.split('-').next().unwrap()
}

fn run_of(event: &Event) -> String {
    match event {
        Event::Run(run) => run.run.run_id.clone(),
        Event::Dataset(_) => String::new(),
    }
}

impl Effects for Ticker<'_> {
    async fn authorize(&mut self, event: &Event, _: &Map<String, Value>) -> Authorizing {
        self.effects.push("authorize".to_owned());
        match kind(&run_of(event)) {
            "denied" | "unkept" => Authorizing::Refused(Refusal::Denied),
            "unverified" => Authorizing::Refused(Refusal::Unverified),
            "unavailable" => Authorizing::Unavailable("OpenFGA did not answer".to_owned()),
            _ => Authorizing::Allowed(Vec::new()),
        }
    }

    async fn record(&mut self, event: &Event, _: &[String]) -> Recording {
        self.effects.push("record".to_owned());
        match kind(&run_of(event)) {
            "foreign" => Recording::ForeignRun,
            "unrecorded" => Recording::Failed("the catalog did not answer".to_owned()),
            _ => Recording::Landed,
        }
    }

    async fn republish(&mut self) -> bool {
        self.effects.push("republish".to_owned());
        kind(&self.draining) != "unpublished"
    }

    async fn drop_staged(&mut self, key: &str) -> bool {
        self.effects.push(format!("drop {key}"));
        kind(key) != "undropped"
    }

    async fn keep_refusal(&mut self, key: &str, _: &Event) -> bool {
        self.effects.push(format!("keep {key}"));
        kind(key) != "unkept"
    }
}

fn answer<T>(case: &Value, step: &str, value: T) -> Result<T, String> {
    if case[step] == "fails" {
        Err("the catalog did not answer".to_owned())
    } else {
        Ok(value)
    }
}

impl Tick for Ticker<'_> {
    async fn listing(&mut self) -> Result<Vec<Listed>, String> {
        self.effects.push("list".to_owned());
        let staged = self.case["staged"].as_array().unwrap().iter().enumerate();
        let listing = staged.map(|(at, key)| Listed {
            key: format!("_lineage_outbox/{}.json", key.as_str().unwrap()),
            modified_ns: i64::try_from(at).unwrap(),
        });
        answer(self.case, "listing", listing.collect())
    }

    async fn read_staged(&mut self, listing: &[Listed], limit: Option<usize>) -> Result<Vec<Staged>, String> {
        let staged = lakehouse_core::outbox::in_order(listing, limit).into_iter().map(|(key, _)| Staged {
            body: self.case["texts"][&key].as_str().unwrap().as_bytes().to_vec(),
            key,
        });
        Ok(staged.collect())
    }

    fn draining(&mut self, staged: &Staged, _: Option<&lakehouse_bus::CloudEvent>) {
        self.draining.clone_from(&staged.key);
    }

    fn counted(&mut self, counted: Counted) {
        self.counted.push(format!("outbox {}", counted.label()));
    }

    async fn retire_parks(&mut self) -> Result<u64, String> {
        self.effects.push("retire".to_owned());
        answer(self.case, "retire", self.case["retire"].as_u64().unwrap_or(0))
    }

    async fn forward(&mut self) -> Result<Forwarded, String> {
        self.effects.push("forward".to_owned());
        let forwarded = &self.case["forward"];
        if forwarded == "raises" {
            return Err("ConnectionError: forward did not answer".to_owned());
        }
        Ok(Forwarded {
            forwarded: forwarded["forwarded"].as_u64().unwrap(),
            refused: forwarded["refused"].as_u64().unwrap(),
            stalled: forwarded["stalled"].as_str().map(str::to_owned),
        })
    }

    async fn forwarded_through(&mut self) -> Result<i64, String> {
        self.effects.push("cursor".to_owned());
        answer(self.case, "cursor", self.case["cursor"].as_i64().unwrap_or(0))
    }

    async fn prune(&mut self, retention_days: u32, delivered_through: Option<i64>) -> Result<u64, String> {
        let through = delivered_through.map_or_else(|| "None".to_owned(), |seq| seq.to_string());
        self.effects.push(format!("prune {retention_days} {through}"));
        answer(self.case, "prune", self.case["prune"].as_u64().unwrap_or(0))
    }
}

#[tokio::test]
async fn a_tick_takes_its_effects_in_pythons_order_and_reports_what_it_reports() {
    let cases = vectors("ticks");
    for case in &cases {
        let label = case.to_string();
        let mut env = vec![
            ("LINEAGE_OUTBOX_DRAIN_LIMIT".to_owned(), case["limit"].to_string()),
            ("LINEAGE_EVENTS_RETENTION_DAYS".to_owned(), case["retention"].to_string()),
            ("LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE".to_owned(), "/token".to_owned()),
            ("LINEAGE_S3_STS_ENDPOINT".to_owned(), "http://sts".to_owned()),
        ];
        if case["outbox"] == true {
            env.push(("LINEAGE_OUTBOX_URI".to_owned(), "s3://b/_lineage_outbox".to_owned()));
        }
        if !case["forward"].is_null() {
            env.push(("LINEAGE_FORWARD_URL".to_owned(), "http://graph".to_owned()));
        }
        let settings = Settings::from_environment(env).unwrap();
        let mut ticker = Ticker {
            case,
            effects: Vec::new(),
            counted: Vec::new(),
            draining: String::new(),
        };
        let report = tick(&mut ticker, &settings).await;
        assert_eq!(json!(ticker.effects), case["effects"], "{label}: the effects");
        assert_eq!(json!(ticker.counted), case["counted"], "{label}: the counts");
        let got = json!({
            "outbox_drained": report.outbox_drained,
            "outbox_stranded": report.outbox_stranded,
            "outbox_refused": report.outbox_refused,
            "outbox_refusals_recorded": report.outbox_refusals_recorded,
            "pruned_events": report.pruned_events,
            "parks_retired": report.parks_retired,
            "forwarded": report.forwarded,
            "forward_refused": report.forward_refused,
            "forward_stalled": report.forward_stalled,
        });
        assert_eq!(got, case["report"], "{label}: the report");
    }
    assert!(cases.len() >= 15);
}

/// The feed and its cursors as the case gives them, every call noted.
struct Feed<'c> {
    case: &'c Value,
    calls: RefCell<Vec<String>>,
}

/// The feed's rows by row id, a seq less one.
struct Rows(Vec<i64>);

impl FeedReader for Rows {
    async fn count(&self, outside: Option<(i64, i64)>) -> Result<u64, CatalogError> {
        let counted = self.0.iter().filter(|id| outside.is_none_or(|(lo, hi)| **id < lo || **id > hi));
        Ok(counted.count() as u64)
    }

    async fn ids(&self, limit: Option<usize>, offset: Option<usize>) -> Result<Vec<i64>, CatalogError> {
        Ok(self.0.iter().skip(offset.unwrap_or(0)).take(limit.unwrap_or(usize::MAX)).copied().collect())
    }

    async fn range(&self, lo: i64, hi: i64, limit: usize) -> Result<Vec<EventRecord>, CatalogError> {
        let rows = self.0.iter().filter(|id| (lo..=hi).contains(*id)).take(limit);
        Ok(rows
            .map(|id| EventRecord {
                seq: id + 1,
                event_type: None,
                event_time: None,
                job: None,
                author: None,
                inputs: Vec::new(),
                outputs: Vec::new(),
                table_uuids: Vec::new(),
                namespace_uuids: Vec::new(),
                unresolved_tables: Vec::new(),
                event: Map::from_iter([("seq".to_owned(), json!(id + 1))]),
            })
            .collect())
    }
}

impl Followed for Feed<'_> {
    type Reader = Rows;

    async fn incarnation(&self) -> Result<String, CatalogError> {
        Ok("feed-1".to_owned())
    }

    async fn cursor(&self, follower: &str) -> Result<i64, CatalogError> {
        self.calls.borrow_mut().push(format!("cursor {follower}"));
        Ok(self.case["mark"].as_i64().unwrap())
    }

    async fn window(&self, since: i64) -> Result<FeedWindow<Rows>, CatalogError> {
        self.calls.borrow_mut().push(format!("window {since}"));
        let ids = self.case["seqs"].as_array().unwrap().iter().map(|seq| seq.as_i64().unwrap() - 1).collect();
        FeedWindow::open(Rows(ids), Some(since), None).await
    }

    async fn reset_cursor(&self, follower: &str, seq: i64) -> Result<(), CatalogError> {
        self.calls.borrow_mut().push(format!("reset {follower} {seq}"));
        Ok(())
    }

    async fn advance_cursor(&self, follower: &str, seq: i64) -> Result<(), CatalogError> {
        self.calls.borrow_mut().push(format!("advance {follower} {seq}"));
        Ok(())
    }
}

#[tokio::test]
async fn the_forwarder_sends_and_moves_its_cursor_as_pythons_does() {
    let cases = vectors("forwards");
    for case in &cases {
        let label = case.to_string();
        let env = [
            ("LINEAGE_FORWARD_URL", "http://graph/"),
            ("LINEAGE_FORWARD_ENDPOINT", "/api/v1/lineage"),
            ("LINEAGE_FORWARD_BATCH", &case["batch"].to_string()),
        ];
        let settings = Settings::from_environment(env.map(|(name, value)| (name.to_owned(), value.to_owned()))).unwrap();
        let feed = Feed {
            case,
            calls: RefCell::default(),
        };
        let authorization = async {
            feed.calls.borrow_mut().push("authorization".to_owned());
            match case["authorization"].as_str().unwrap() {
                "fails" => Err("RuntimeError: the secret store did not serve the key".to_owned()),
                "bearer" => Ok(Some("Bearer k".to_owned())),
                _ => Ok(None),
            }
        };
        let url = &settings.endpoint_url();
        let feed = &feed;
        let post = |event: Map<String, Value>, bearer: Option<String>| async move {
            let seq = event["seq"].as_i64().unwrap();
            feed.calls
                .borrow_mut()
                .push(format!("post {url} {seq} {}", bearer.as_deref().unwrap_or("None")));
            match &case["answers"][seq.to_string()] {
                Value::Null => Ok(200),
                Value::Number(status) => Ok(u16::try_from(status.as_u64().unwrap()).unwrap()),
                _ => Err("ConnectError: the backend did not answer".to_owned()),
            }
        };
        let outcome = match forward(feed, &settings, authorization, post).await {
            Ok(sent) => json!({"forwarded": sent.forwarded, "refused": sent.refused, "stalled": sent.stalled}),
            Err(_) => json!({"raised": true}),
        };
        assert_eq!(json!(feed.calls.take()), case["calls"], "{label}: the calls");
        assert_eq!(outcome, case["outcome"], "{label}: the outcome");
    }
    assert!(cases.len() >= 15);
}

#[test]
fn a_park_keeps_what_pythons_keeps() {
    let cases = vectors("parks");
    for case in &cases {
        let label = case["name"].as_str().unwrap();
        let delivery = Delivery {
            subject: "lineage.events.v1".to_owned(),
            body: case["body"].as_str().unwrap().as_bytes().to_vec(),
            sequence: 1,
            attempt: 5,
        };
        let Some(parked) = delivery.envelope().map(|envelope| parking(&envelope)) else {
            assert_eq!(case["asked"], json!([]), "{label}");
            assert_eq!(case["kept"], json!([]), "{label}");
            continue;
        };
        let asked: Vec<Vec<String>> = parked.feed_key.iter().map(|key| vec![key.clone()]).collect();
        assert_eq!(json!(asked), case["asked"], "{label}: the feed row asked about");
        let kept = match (&parked.key, case["recorded"].as_bool().unwrap()) {
            (Some(key), false) => json!([{
                "outbox_key": key,
                "run_id": parked.run_id,
                "author": parked.author,
                "reason": "its handling asked for it to be parked",
                "event_json": parked.text,
            }]),
            _ => json!([]),
        };
        assert_eq!(kept, case["kept"], "{label}: what is kept");
    }
    assert!(cases.len() >= 15);
}
