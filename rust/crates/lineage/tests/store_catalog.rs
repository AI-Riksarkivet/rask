//! The store against the real Python catalog app on real pylance (`scripts/port_catalog_fixture.py`, ADR 0228).
//!
//! Each test keeps its tables in a namespace of its own under the fixture's, so a run never meets an earlier one's
//! rows. Skipped, with a message, when `RASK_PORT_CATALOG_URL` names no catalog. Run:
//!
//! ```text
//! uv run scripts/port_catalog_fixture.py --port 8765
//! cd rust && RASK_PORT_CATALOG_URL=http://127.0.0.1:8765 cargo test -p lineage --test store_catalog
//! ```
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use arrow_schema::{DataType, Field, Schema};
use lakehouse_tables::{Catalog, Rows, Scan, bearer};
use lineage::intake::Recording;
use lineage::model::{Event, parse_event};
use lineage::store::commit::{admitted, plan};
use lineage::store::{Store, tables};
use serde_json::{Value, json};
use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::time::Duration;

const CATALOG: &str = "lance-namespace://localhost/api/catalog";

fn url() -> Option<String> {
    let url = std::env::var("RASK_PORT_CATALOG_URL").ok();
    if url.is_none() {
        eprintln!("skipped: RASK_PORT_CATALOG_URL names no catalog; start one with `uv run scripts/port_catalog_fixture.py`");
    }
    url
}

/// A namespace of the test's own under the fixture's, created through the catalog's namespace door.
async fn namespace(url: &str) -> Vec<String> {
    let name = format!("s{}", uuid::Uuid::new_v4().simple());
    let created = reqwest::Client::new()
        .post(format!("{url}/v1/namespace/lance_catalog%24port%24{name}/create?delimiter=%24"))
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    assert!(created.status().is_success(), "the namespace was created: {}", created.status());
    vec!["lance_catalog".into(), "port".into(), name]
}

fn catalog(url: &str) -> Catalog {
    Catalog::new(url, bearer::anonymous(), Duration::from_secs(60)).unwrap()
}

async fn store(url: &str, refuse: bool) -> Store {
    let store = Store::new(catalog(url), namespace(url).await, CATALOG, refuse).unwrap();
    store.ensure().await.unwrap();
    store
}

/// The events `events` recorded at once, each caller's outcome in order.
async fn concurrently(store: &Store, events: Vec<Event>) -> Vec<Recording> {
    let mut recording = tokio::task::JoinSet::new();
    for (at, event) in events.into_iter().enumerate() {
        let store = store.clone();
        recording.spawn(async move { (at, store.record(&event, &[]).await) });
    }
    let mut outcomes = recording.join_all().await;
    outcomes.sort_by_key(|(at, _)| *at);
    outcomes.into_iter().map(|(_, outcome)| outcome).collect()
}

fn dataset(name: &str, uuid: &str) -> Value {
    json!({"namespace": CATALOG, "name": name, "facets": {"rask_lance": {"tableUuid": uuid}}})
}

/// A run event of `run` by `author`, reading `inputs` and writing `outputs`, each a `(name, uuid)`.
fn run(run: &str, kind: &str, author: &str, inputs: &[(&str, &str)], outputs: &[(&str, &str)]) -> Event {
    let datasets = |named: &[(&str, &str)]| named.iter().map(|(name, uuid)| dataset(name, uuid)).collect::<Vec<_>>();
    parse_event(&json!({
        "eventType": kind,
        "eventTime": "2026-11-17T10:00:00Z",
        "run": {"runId": run, "facets": {"rask_author": {"sub": author}}},
        "job": {"namespace": "jobs", "name": "copy"},
        "inputs": datasets(inputs),
        "outputs": datasets(outputs),
    }))
    .unwrap()
}

const A: (&str, &str) = ("wh$raw$a", "0190a1b2-0000-7000-8000-00000000000a");
const B: (&str, &str) = ("wh$clean$b", "0190a1b2-0000-7000-8000-00000000000b");
const C: (&str, &str) = ("wh$gold$c", "0190a1b2-0000-7000-8000-00000000000c");

async fn count(store: &Store, table: &str) -> u64 {
    store.table(table).count_rows(None, None).await.unwrap()
}

async fn versions(store: &Store) -> Vec<u64> {
    let mut versions = Vec::new();
    for (name, _) in tables::all() {
        versions.push(store.table(name).version().await.unwrap());
    }
    versions
}

async fn seqs(store: &Store, limit: usize, after: Option<i64>, since: Option<i64>) -> Vec<i64> {
    store
        .list_events(limit, after, since, true)
        .await
        .unwrap()
        .iter()
        .map(|record| record.seq)
        .collect()
}

#[tokio::test]
async fn ensure_twice_keeps_every_table_as_it_is() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    assert_eq!(store.record(&run("r1", "COMPLETE", "ada", &[A], &[B]), &[]).await, Recording::Landed);
    let (before, incarnation) = (versions(&store).await, store.feed_incarnation().await.unwrap());

    store.ensure().await.unwrap();

    assert_eq!(versions(&store).await, before, "a second ensure commits nothing");
    assert_eq!(store.feed_incarnation().await.unwrap(), incarnation, "and keeps the feed it found");
    assert_eq!(count(&store, tables::FEED).await, 1);
    store.ready().await.unwrap();
}

#[tokio::test]
async fn an_event_recorded_twice_is_one_feed_row_and_one_set_of_edges() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    let event = run("r1", "COMPLETE", "ada", &[A, C], &[B]);

    assert_eq!(store.record(&event, &[]).await, Recording::Landed);
    assert_eq!(store.record(&event, &[]).await, Recording::Landed, "a redelivery is answered as landed");
    let (one, other) = tokio::join!(store.record(&event, &[]), store.record(&event, &[]));

    assert_eq!((one, other), (Recording::Landed, Recording::Landed), "two deliveries in one batch");
    assert_eq!(count(&store, tables::FEED).await, 1);
    assert_eq!(count(&store, tables::EDGES).await, 2, "one edge per input of the output");
    assert_eq!(count(&store, tables::RUNS).await, 1);
    let key = lineage::feed::feed_key(&event);
    assert_eq!(
        store.events_recorded([key.as_str(), "r9@COMPLETE"]).await.unwrap(),
        BTreeSet::from([key.clone()])
    );
    let stored = store.recorded_event(&key).await.unwrap().unwrap();
    assert_eq!(Value::Object(stored), serde_json::from_str::<Value>(&event.to_json()).unwrap());
    assert_eq!(store.recorded_event("r9@COMPLETE").await.unwrap(), None);

    let resent = parse_event(&json!({
        "eventType": "COMPLETE",
        "eventTime": "2026-11-17T11:00:00Z",
        "run": {"runId": "r1", "facets": {"rask_author": {"sub": "ada"}}},
        "job": {"namespace": "jobs", "name": "copy"},
    }))
    .unwrap();
    assert_eq!(store.record(&resent, &[]).await, Recording::Landed);
    let kept = store.recorded_event(&key).await.unwrap().unwrap();
    assert_eq!(
        kept.get("eventTime"),
        Some(&json!("2026-11-17T10:00:00Z")),
        "a terminal state re-sent keeps its first row"
    );
}

#[tokio::test]
async fn thirty_concurrent_events_land_in_far_fewer_commits_each_caller_told_its_own() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    assert_eq!(store.record(&run("owned", "START", "ada", &[], &[]), &[]).await, Recording::Landed);
    let before = store.table(tables::FEED).version().await.unwrap();

    // Every sixth event extends the run ada founded, as bob.
    let events = (0..30)
        .map(|at| {
            if at % 6 == 0 {
                run("owned", "COMPLETE", "bob", &[A], &[B])
            } else {
                run(&format!("r{at}"), "COMPLETE", "ada", &[A], &[B])
            }
        })
        .collect();

    let outcomes = concurrently(&store, events).await;

    for (at, outcome) in outcomes.iter().enumerate() {
        let expected = if at % 6 == 0 { Recording::ForeignRun } else { Recording::Landed };
        assert_eq!(outcome, &expected, "event {at}");
    }
    let commits = store.table(tables::FEED).version().await.unwrap() - before;
    eprintln!("30 concurrent events landed in {commits} commits of the feed");
    assert!(commits <= 5, "{commits} commits for 30 events");
    assert_eq!(count(&store, tables::FEED).await, 1 + 25);
    assert_eq!(store.run_author("owned").await.unwrap().as_deref(), Some("ada"));
}

#[tokio::test]
async fn a_run_extended_by_another_author_is_refused_only_when_refusal_is_on() {
    let Some(url) = url() else { return };
    let shared = namespace(&url).await;
    let refusing = Store::new(catalog(&url), shared.clone(), CATALOG, true).unwrap();
    refusing.ensure().await.unwrap();
    let lenient = Store::new(catalog(&url), shared, CATALOG, false).unwrap();
    assert_eq!(refusing.record(&run("r1", "START", "ada", &[], &[]), &[]).await, Recording::Landed);
    let extension = run("r1", "COMPLETE", "bob", &[A], &[B]);

    assert_eq!(refusing.record(&extension, &[]).await, Recording::ForeignRun);
    assert_eq!(count(&refusing, tables::FEED).await, 1, "a refused event writes no feed row");
    assert_eq!(count(&refusing, tables::EDGES).await, 0, "and no edge");
    assert_eq!(lenient.record(&extension, &[]).await, Recording::Landed);
    assert_eq!(count(&refusing, tables::FEED).await, 2);
    assert_eq!(refusing.run_author("r1").await.unwrap().as_deref(), Some("ada"), "the binding is its founder's");
    assert_eq!(refusing.run_author("r9").await.unwrap(), None);
}

#[tokio::test]
async fn a_page_is_read_at_the_version_its_window_opened_on() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    for at in 0..3 {
        store.record(&run(&format!("r{at}"), "COMPLETE", "ada", &[A], &[B]), &[]).await;
    }
    let mut window = store.window(None, Some(0), true).await.unwrap();

    store.record(&run("late", "COMPLETE", "ada", &[A], &[B]), &[]).await;
    assert_eq!(store.prune_events(0, Some(2)).await.unwrap(), 2);

    let pinned: Vec<i64> = window.take(10).await.unwrap().iter().map(|record| record.seq).collect();
    assert_eq!(
        pinned,
        vec![1, 2, 3],
        "the window reads the version it opened on, before the commit and the prune"
    );
    assert_eq!(seqs(&store, 10, None, Some(0)).await, vec![3, 4], "a window opened later reads what they left");
    assert_eq!(window.bounds().await.unwrap(), Some((1, 3)));
}

#[tokio::test]
async fn since_and_after_page_through_the_feed_in_order() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    assert_eq!(store.feed_bounds().await.unwrap(), None, "an empty feed has no bounds");
    for at in 0..5 {
        store.record(&run(&format!("r{at}"), "COMPLETE", "ada", &[A], &[B]), &[]).await;
    }

    assert_eq!(store.feed_bounds().await.unwrap(), Some((1, 5)));
    assert_eq!(seqs(&store, 2, None, Some(0)).await, vec![1, 2]);
    assert_eq!(seqs(&store, 2, None, Some(2)).await, vec![3, 4]);
    assert_eq!(seqs(&store, 2, None, Some(4)).await, vec![5]);
    assert_eq!(seqs(&store, 2, None, Some(5)).await, Vec::<i64>::new());
    assert_eq!(seqs(&store, 2, None, None).await, vec![5, 4], "newest first");
    assert_eq!(seqs(&store, 2, Some(4), None).await, vec![3, 2]);
    assert_eq!(seqs(&store, 2, Some(2), None).await, vec![1]);
    let full = store.list_events(1, None, Some(0), false).await.unwrap();
    assert_eq!(
        full[0].event.get("run").and_then(|run| run.get("runId")),
        Some(&json!("r0")),
        "a full read carries the payload"
    );
    assert!(store.window(Some(1), Some(1), true).await.is_err(), "a read takes one cursor");
}

#[tokio::test]
async fn a_cursor_only_moves_forward_unless_reset() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    assert_eq!(store.cursor("forwarder").await.unwrap(), 0);

    store.advance_cursor("forwarder", 5).await.unwrap();
    store.advance_cursor("forwarder", 3).await.unwrap();
    assert_eq!(store.cursor("forwarder").await.unwrap(), 5, "a slower tick cannot move it back");
    store.advance_cursor("forwarder", 7).await.unwrap();
    assert_eq!(store.cursor("forwarder").await.unwrap(), 7);
    store.reset_cursor("forwarder", 2).await.unwrap();
    assert_eq!(store.cursor("forwarder").await.unwrap(), 2, "a reset moves it either way");
    assert_eq!(store.cursor("other").await.unwrap(), 0);
}

#[tokio::test]
async fn a_parked_delivery_is_retired_once_its_event_is_recorded() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    let event = run("r1", "COMPLETE", "ada", &[A], &[B]);
    let delivery = json!({"id": "d1", "data": serde_json::from_str::<Value>(&event.to_json()).unwrap()});
    store
        .record_refusal("dead_letter:d1", Some("r1"), Some("ada"), "denied", &delivery.to_string())
        .await
        .unwrap();
    store.record_refusal("outbox/r1.json", Some("r1"), Some("ada"), "denied", "{}").await.unwrap();

    let parked = store.parked(Some("r1")).await.unwrap();
    assert_eq!(parked.len(), 1, "only the lane's parks are parked deliveries");
    assert_eq!(parked[0].feed_key.as_deref(), Some(lineage::feed::feed_key(&event).as_str()));
    assert_eq!(
        store.retire_recovered_parks().await.unwrap(),
        0,
        "nothing is retired before its event is recorded"
    );

    assert_eq!(store.record(&event, &[]).await, Recording::Landed);

    assert_eq!(store.retire_recovered_parks().await.unwrap(), 1);
    assert!(store.parked(None).await.unwrap().is_empty());
    assert_eq!(count(&store, tables::REFUSALS).await, 1, "an outbox refusal is kept");
}

#[tokio::test]
async fn a_prune_removes_only_rows_old_enough_and_already_forwarded() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    for at in 0..4 {
        store.record(&run(&format!("r{at}"), "COMPLETE", "ada", &[A], &[B]), &[]).await;
    }

    assert_eq!(store.prune_events(1, None).await.unwrap(), 0, "no row is a day old");
    assert_eq!(store.prune_events(0, Some(2)).await.unwrap(), 2, "every row is old, two forwarded");

    assert_eq!(seqs(&store, 10, None, Some(0)).await, vec![3, 4]);
    assert_eq!(store.feed_bounds().await.unwrap(), Some((3, 4)));
    assert_eq!(count(&store, tables::EDGES).await, 4, "the edge list is not pruned");
    assert_eq!(count(&store, tables::RUNS).await, 4, "nor are the run bindings");
}

#[tokio::test]
async fn a_write_that_lands_late_lands_nothing_twice() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    let event = run("r1", "COMPLETE", "ada", &[A, C], &[B]);
    let planned = plan(&[admitted(&event, &[], CATALOG, 0)], &HashMap::new(), true).unwrap();
    // An earlier attempt bound the run and wrote the edges, and was given up on before its feed row.
    store
        .table(tables::RUNS)
        .merge_insert_only(&Rows::from(planned.runs.clone().unwrap()), "run_id")
        .await
        .unwrap();
    store
        .table(tables::EDGES)
        .merge_insert_only(&Rows::from(planned.edges.clone().unwrap()), "edge_key")
        .await
        .unwrap();

    assert_eq!(store.record(&event, &[]).await, Recording::Landed, "the retry lands what is missing");
    for table in [tables::RUNS, tables::EDGES] {
        let read = store.table(table).read(&Scan::default()).await.unwrap();
        let stamps = lineage::store::rows::int_column(&read, "received_at").unwrap();
        assert!(
            stamps.iter().all(|stamp| *stamp == Some(0)),
            "the retry rewrote nothing the earlier attempt landed in {table}"
        );
    }
    // The given-up attempt's feed write lands after all.
    store
        .table(tables::FEED)
        .merge_insert_only(&Rows::from(planned.feed.unwrap()), "event_key")
        .await
        .unwrap();

    assert_eq!(
        (
            count(&store, tables::RUNS).await,
            count(&store, tables::EDGES).await,
            count(&store, tables::FEED).await
        ),
        (1, 2, 1)
    );
    assert_eq!(
        store.prune_events(1, None).await.unwrap(),
        0,
        "the late write, stamped long ago, replaced no row"
    );
}

/// A table of the name `name` in the store's namespace, created before the store with a shape its rows cannot fill.
async fn broken(url: &str, name: &str) -> Store {
    let namespace = namespace(url).await;
    let mut id = namespace.clone();
    id.push(name.to_owned());
    let key = Field::new("other", DataType::Int64, false);
    catalog(url).table(id).create(Arc::new(Schema::new(vec![key]))).await.unwrap();
    let store = Store::new(catalog(url), namespace, CATALOG, true).unwrap();
    store.ensure().await.unwrap();
    store
}

#[tokio::test]
async fn no_feed_row_is_written_when_an_earlier_write_fails() {
    let Some(url) = url() else { return };
    let without_edges = broken(&url, tables::EDGES).await;
    let without_runs = broken(&url, tables::RUNS).await;
    let event = run("r1", "COMPLETE", "ada", &[A], &[B]);

    assert!(matches!(without_edges.record(&event, &[]).await, Recording::Failed(_)));
    assert!(matches!(without_runs.record(&event, &[]).await, Recording::Failed(_)));

    assert_eq!(count(&without_edges, tables::FEED).await, 0, "the feed row waits for its edges");
    assert_eq!(count(&without_runs, tables::FEED).await, 0, "and for its run's binding");
    assert_eq!(count(&without_runs, tables::EDGES).await, 0, "and the edges for the binding");
}

#[tokio::test]
async fn a_refused_batch_lands_in_parts_and_fails_only_the_refused_event() {
    let Some(url) = url() else { return };
    let store = broken(&url, tables::EDGES).await;
    let edgeless: Vec<Event> = (0..3).map(|at| run(&format!("s{at}"), "START", "ada", &[], &[])).collect();
    let with_edges = run("r1", "COMPLETE", "ada", &[A], &[B]);

    let mut events = edgeless;
    events.insert(1, with_edges);

    let outcomes = concurrently(&store, events).await;

    assert!(matches!(outcomes[1], Recording::Failed(_)), "{outcomes:?}");
    assert!([0, 2, 3].iter().all(|at| outcomes[*at] == Recording::Landed), "{outcomes:?}");
    assert_eq!(count(&store, tables::FEED).await, 3);
}

#[tokio::test]
async fn derived_walks_the_edges_the_store_recorded() {
    let Some(url) = url() else { return };
    let store = store(&url, true).await;
    store.record(&run("r1", "COMPLETE", "ada", &[A], &[B]), &[]).await;
    store.record(&run("r2", "COMPLETE", "ada", &[B], &[C]), &[]).await;

    let (found, complete) = store.derived(&BTreeSet::from([A.1.to_owned()]), 20).await.unwrap();
    let found: Vec<(String, u32)> = found.into_iter().map(|table| (table.table, table.depth)).collect();
    assert_eq!(found, vec![(B.0.to_owned(), 1), (C.0.to_owned(), 2)]);
    assert!(complete);
    let (_, complete) = store.derived(&BTreeSet::from([A.1.to_owned()]), 1).await.unwrap();
    assert!(!complete, "a walk cut short says so");
    let scan = Scan::default();
    assert_eq!(store.table(tables::EDGES).read(&scan).await.unwrap().num_rows(), 2);
}
