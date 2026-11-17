//! The store decides what the Python store decides (`rust/vectors/store.json`, ADR 0228): the tables' schemas, the
//! rows a group commit sends, which events it refuses, the window's reads and bounds, the walk, and the predicates.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use arrow_array::RecordBatch;
use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use lakehouse_tables::CatalogError;
use lakehouse_tables::arrow::decode_answer;
use lineage::model::parse_event;
use lineage::store::commit::{Admitted, admitted, first_of_each, plan};
use lineage::store::rows::EventRecord;
use lineage::store::walk::{Edge, Walk, derived_tables, is_table_uuid};
use lineage::store::window::{FeedReader, FeedWindow};
use lineage::store::{delivery_feed_key, parked_filter, prune_predicate, tables};
use serde_json::{Map, Value, json};
use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap};

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/store.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

/// The one batch an IPC stream Python wrote holds, its chunks joined.
fn batch_of(ipc: &str) -> RecordBatch {
    let rows = decode_answer(&STANDARD.decode(ipc).unwrap()).unwrap();
    arrow_select::concat::concat_batches(&rows.schema, &rows.batches).unwrap()
}

#[test]
fn each_table_is_created_with_the_schema_python_creates_it_with() {
    let ours = tables::all();
    let cases = vectors("schemas");
    assert_eq!(cases.len(), ours.len());
    for (case, (name, schema)) in cases.iter().zip(ours) {
        assert_eq!(case["table"], name, "the tables are created in Python's order");
        let theirs = decode_answer(&STANDARD.decode(case["ipc"].as_str().unwrap()).unwrap()).unwrap();
        assert_eq!(theirs.schema, schema, "{name}");
        assert_eq!(theirs.num_rows(), 0);
    }
}

#[test]
fn a_batch_commits_the_rows_python_commits_and_refuses_what_it_refuses() {
    let catalog = vectors("catalog")[0].as_str().unwrap().to_owned();
    for case in vectors("commits") {
        let name = case["name"].as_str().unwrap();
        let batch: Vec<Admitted> = case["events"]
            .as_array()
            .unwrap()
            .iter()
            .map(|event| {
                let aliased: Vec<String> = serde_json::from_value(event["aliased"].clone()).unwrap();
                admitted(
                    &parse_event(&event["data"]).unwrap(),
                    &aliased,
                    &catalog,
                    event["received_at"].as_i64().unwrap(),
                )
            })
            .collect();
        let bound: HashMap<String, Option<String>> = serde_json::from_value(case["bound"].clone()).unwrap();
        let planned = plan(&batch, &bound, case["refuse"].as_bool().unwrap()).unwrap();
        let written = [
            ("runs", planned.runs, "run_id"),
            ("edges", planned.edges, "edge_key"),
            ("feed", planned.feed, "event_key"),
        ];
        for (table, ours, key) in written {
            match &case[table] {
                Value::Null => assert!(ours.is_none(), "{name}: Python writes no {table}"),
                theirs => {
                    assert_eq!(theirs["on"], key, "{name}");
                    assert_eq!(
                        ours.expect("Python writes rows here"),
                        batch_of(theirs["ipc"].as_str().unwrap()),
                        "{name}: {table}"
                    );
                }
            }
        }
        assert_eq!(json!(planned.foreign), case["foreign"], "{name}");
    }
}

#[test]
fn a_repeated_key_keeps_the_row_python_keeps() {
    for case in vectors("first_of_each") {
        let keys: Vec<&str> = case["keys"].as_array().unwrap().iter().map(|key| key.as_str().unwrap()).collect();
        assert_eq!(json!(first_of_each(keys)), case["kept"], "{case}");
    }
}

/// A feed of the case's row ids, answering in the case's order and keeping each read, as the Python reader does.
struct Feed {
    order: Vec<i64>,
    summary: bool,
    calls: RefCell<Vec<Value>>,
}

impl Feed {
    fn new(case: &Value, summary: bool) -> Self {
        let mut order: Vec<i64> = case["ids"].as_array().unwrap().iter().map(|id| id.as_i64().unwrap()).collect();
        order.sort_unstable();
        if case["order"] == "descending" {
            order.reverse();
        }
        Feed {
            order,
            summary,
            calls: RefCell::default(),
        }
    }

    fn record(&self, id: i64) -> EventRecord {
        let text = |column: &str| Some(format!("{column}-{id}"));
        let list = |column: &str| vec![format!("{column}-{id}")];
        EventRecord {
            seq: id + 1,
            event_type: text("event_type"),
            event_time: text("event_time"),
            job: text("job"),
            author: text("author"),
            inputs: list("inputs"),
            outputs: list("outputs"),
            table_uuids: list("table_uuids"),
            namespace_uuids: list("namespace_uuids"),
            unresolved_tables: list("unresolved_tables"),
            event: if self.summary {
                Map::new()
            } else {
                Map::from_iter([("row".to_owned(), json!(id))])
            },
        }
    }

    fn calls(&self) -> Vec<String> {
        let mut calls: Vec<String> = self.calls.borrow().iter().map(Value::to_string).collect();
        calls.sort();
        calls
    }
}

impl FeedReader for &Feed {
    async fn count(&self, outside: Option<(i64, i64)>) -> Result<u64, CatalogError> {
        let Some((lo, hi)) = outside else {
            self.calls.borrow_mut().push(json!(["count"]));
            return Ok(self.order.len() as u64);
        };
        self.calls.borrow_mut().push(json!(["outside", lo, hi]));
        Ok(self.order.iter().filter(|id| **id < lo || **id > hi).count() as u64)
    }

    async fn ids(&self, limit: Option<usize>, offset: Option<usize>) -> Result<Vec<i64>, CatalogError> {
        self.calls.borrow_mut().push(json!(["ids", limit, offset]));
        let from = self.order.iter().skip(offset.unwrap_or(0)).copied();
        Ok(from.take(limit.unwrap_or(usize::MAX)).collect())
    }

    async fn range(&self, lo: i64, hi: i64, limit: usize) -> Result<Vec<EventRecord>, CatalogError> {
        self.calls.borrow_mut().push(json!(["range", lo, hi, limit]));
        let ids = self.order.iter().filter(|id| (lo..=hi).contains(*id)).take(limit);
        Ok(ids.map(|id| self.record(*id)).collect())
    }
}

fn sorted_calls(calls: &Value) -> Vec<String> {
    let mut calls: Vec<String> = calls.as_array().unwrap().iter().map(Value::to_string).collect();
    calls.sort();
    calls
}

#[tokio::test]
async fn a_window_reads_and_hands_out_what_pythons_does() {
    let cases = vectors("windows");
    for case in &cases {
        let label = format!(
            "{} {} since={} after={} takes={}",
            case["name"], case["order"], case["since"], case["after"], case["takes"]
        );
        let bounded = Feed::new(case, true);
        let bounds = FeedWindow::open(&bounded, None, None).await.unwrap().bounds().await.unwrap();
        assert_eq!(json!(bounds.map(|(lo, hi)| [lo, hi])), case["bounds"], "{label}: bounds");
        assert_eq!(bounded.calls(), sorted_calls(&case["bounds_calls"]), "{label}: the reads naming the bounds");

        let summary = case["summary"].as_bool().unwrap();
        let feed = Feed::new(case, summary);
        let mut window = FeedWindow::open(&feed, case["since"].as_i64(), case["after"].as_i64()).await.unwrap();
        let mut pages = Vec::new();
        for n in case["takes"].as_array().unwrap() {
            pages.push(serde_json::to_value(window.take(usize::try_from(n.as_u64().unwrap()).unwrap()).await.unwrap()).unwrap());
        }
        assert_eq!(Value::Array(pages), case["pages"], "{label}: pages");
        assert_eq!(feed.calls(), sorted_calls(&case["calls"]), "{label}: the reads");
    }
    assert!(cases.len() >= 100);
}

#[tokio::test]
async fn the_walk_reaches_the_tables_pythons_reaches() {
    for case in vectors("walks") {
        let edges: Vec<(String, Edge)> = case["edges"]
            .as_array()
            .unwrap()
            .iter()
            .map(|edge| {
                let out = Edge {
                    out_table: edge["out_table"].as_str().unwrap().to_owned(),
                    out_name: edge["out_name"].as_str().map(str::to_owned),
                    received_at: edge["received_at"].as_i64().unwrap(),
                };
                (edge["in_table"].as_str().unwrap().to_owned(), out)
            })
            .collect();
        let sources: BTreeSet<String> = serde_json::from_value(case["sources"].clone()).unwrap();
        let depth = u32::try_from(case["max_depth"].as_u64().unwrap()).unwrap();
        let mut walk = Walk::new(&sources, Some(depth));
        while let Some(inputs) = walk.frontier() {
            let found = edges.iter().filter(|(input, _)| inputs.contains(input)).map(|(_, edge)| edge.clone());
            walk.take(found.collect());
        }
        let walked = walk.walked();
        assert_eq!(serde_json::to_value(derived_tables(&walked)).unwrap(), case["derived"], "{}", case["name"]);
        assert_eq!(json!(walked.complete), case["complete"], "{}", case["name"]);
    }
    for case in vectors("uuid_keys") {
        assert_eq!(json!(is_table_uuid(case["key"].as_str().unwrap())), case["uuid"], "{case}");
    }
}

#[test]
fn a_parked_delivery_names_the_feed_row_python_names() {
    for case in vectors("delivery_feed_keys") {
        assert_eq!(json!(delivery_feed_key(&case["cloud_event"])), case["feed_key"], "{case}");
    }
}

#[test]
fn the_prune_and_the_parked_read_send_pythons_predicates() {
    for case in vectors("prune_predicates") {
        let days = u32::try_from(case["retention_days"].as_u64().unwrap()).unwrap();
        let predicate = prune_predicate(case["now"].as_i64().unwrap(), days, case["delivered_through"].as_i64());
        assert_eq!(predicate, case["predicate"].as_str().unwrap(), "{case}");
    }
    for case in vectors("parked_filters") {
        assert_eq!(parked_filter(case["run_id"].as_str()), case["filter"].as_str().unwrap(), "{case}");
    }
}
