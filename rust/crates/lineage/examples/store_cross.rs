//! The Rust store's half of the cross-implementation check (`scripts/port_store_cross.py`, ADR 0228): it records
//! events into, and reads back from, tables the Python store also writes.
//!
//! ```text
//! store_cross record <catalog url> <namespace, $-joined> <dataset namespace> <events.json>
//! store_cross read   <catalog url> <namespace, $-joined> <dataset namespace> <queries.json>
//! ```
//!
//! `record` ensures the tables, records each event of the file (`[{"data": ..., "aliased": [...]}]`) in order and prints
//! each outcome; `read` answers the reads the queries file names and prints them as JSON.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_tables::{Catalog, bearer};
use lineage::intake::Recording;
use lineage::model::parse_event;
use lineage::store::Store;
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::time::Duration;

fn arguments() -> (String, Store, Value) {
    let arguments: Vec<String> = std::env::args().collect();
    let [_, _, url, namespace, dataset_namespace, file] = arguments.as_slice() else {
        panic!("usage: store_cross record|read <catalog url> <namespace> <dataset namespace> <file>");
    };
    let catalog = Catalog::new(url, bearer::anonymous(), Duration::from_secs(60)).unwrap();
    let store = Store::new(catalog, namespace.split('$').map(str::to_owned).collect(), dataset_namespace, true).unwrap();
    let input = serde_json::from_str(&std::fs::read_to_string(file).unwrap()).unwrap();
    (arguments[1].clone(), store, input)
}

/// The most pages a paging read takes, well above what the corpus fills.
const PAGES: usize = 20;

/// Every page of the feed `limit` rows at a time, from the start forward or from the newest back, as seqs.
async fn pages(store: &Store, limit: usize, forward: bool) -> Vec<Vec<i64>> {
    let mut pages = Vec::new();
    let mut cursor = if forward { Some(0) } else { None };
    // A cursor that does not move would page for ever: the bound turns that defect into a difference.
    while pages.len() < PAGES {
        let (after, since) = if forward { (None, cursor) } else { (cursor, None) };
        let page: Vec<i64> = store
            .list_events(limit, after, since, true)
            .await
            .unwrap()
            .iter()
            .map(|record| record.seq)
            .collect();
        let Some(last) = page.last().copied() else { break };
        pages.push(page);
        cursor = Some(last);
    }
    pages
}

#[tokio::main]
async fn main() {
    let (command, store, input) = arguments();
    if command == "record" {
        store.ensure().await.unwrap();
        let mut outcomes = Vec::new();
        for event in input.as_array().unwrap() {
            let aliased: Vec<String> = serde_json::from_value(event["aliased"].clone()).unwrap();
            let outcome = store.record(&parse_event(&event["data"]).unwrap(), &aliased).await;
            outcomes.push(match outcome {
                Recording::Landed => "landed".to_owned(),
                Recording::ForeignRun => "foreign_run".to_owned(),
                Recording::Failed(error) => format!("failed: {error}"),
            });
        }
        println!("{}", json!(outcomes));
        return;
    }
    let strings = |key: &str| -> Vec<String> { serde_json::from_value(input[key].clone()).unwrap() };
    let mut run_authors = serde_json::Map::new();
    for run in strings("runs") {
        run_authors.insert(run.clone(), json!(store.run_author(&run).await.unwrap()));
    }
    let (derived, complete) = store.derived(&strings("sources").into_iter().collect::<BTreeSet<_>>(), 20).await.unwrap();
    let answers = json!({
        "events": store.list_events(500, None, Some(0), false).await.unwrap(),
        "newest": store.list_events(3, None, None, false).await.unwrap(),
        "forward_pages": pages(&store, 2, true).await,
        "backward_pages": pages(&store, 3, false).await,
        "bounds": store.feed_bounds().await.unwrap().map(|(lo, hi)| [lo, hi]),
        "derived": derived,
        "complete": complete,
        "run_authors": run_authors,
        "recorded": store.events_recorded(strings("keys")).await.unwrap(),
        "parked": store.parked(None).await.unwrap().iter().map(|parked| json!([parked.key, parked.run_id, parked.feed_key])).collect::<Vec<_>>(),
        "cursor": store.cursor("forwarder").await.unwrap(),
        "incarnation": store.feed_incarnation().await.unwrap(),
    });
    println!("{answers}");
}
