//! Lineage's decisions answer what the Python ones answer (`rust/vectors/`, ADR 0217 §5).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lineage::feed::{edges_of, feed_row};
use lineage::model::{author_sub_from_payload, parse_event, run_id_from_payload, without_signature_facet};
use lineage::paging::{forward_cursor, next_chunk};
use lineage::targeting::{column_lineage_datasets, targeting_view};
use serde_json::{Value, json};

fn vectors(file: &str) -> Value {
    let path = format!("{}/../../vectors/{file}.json", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// What differs between the port's answer and Python's for one case, as lines to print.
fn differences(case: &Value, catalog: &str) -> Vec<String> {
    let mut differing = Vec::new();
    let mut check = |what: &str, ours: Value, theirs: &Value| {
        if &ours != theirs {
            differing.push(format!("{what}\n    rust:   {ours}\n    python: {theirs}"));
        }
    };
    let input = &case["input"];
    let parsed = parse_event(input);
    check("taken", json!(parsed.is_ok()), &case["ok"]);
    if case.get("aliased").is_none() {
        return differing;
    }
    check("author_sub", json!(author_sub_from_payload(input)), &case["author_sub"]);
    check("run_id", json!(run_id_from_payload(input)), &case["run_id"]);
    if input.is_object() {
        check("without_signature", without_signature_facet(input), &case["without_signature"]);
    }
    let Ok(event) = parsed else { return differing };
    let aliased: Vec<String> = serde_json::from_value(case["aliased"].clone()).unwrap();
    let stored: Value = serde_json::from_str(&event.to_json()).unwrap();
    check("stored", json!(event.to_json()), &case["stored"]);
    check("row", serde_json::to_value(feed_row(&event, &aliased, catalog)).unwrap(), &case["row"]);
    check("edges", serde_json::to_value(edges_of(&event, catalog)).unwrap(), &case["edges"]);
    check(
        "targeting_view",
        Value::Object(targeting_view(stored.as_object().unwrap())),
        &case["targeting_view"],
    );
    check(
        "column_lineage",
        json!(column_lineage_datasets(stored.as_object().unwrap(), catalog)),
        &case["column_lineage"],
    );
    differing
}

#[test]
fn an_event_is_judged_and_recorded_as_python_does() {
    let vectors = vectors("lineage_events");
    let catalog = vectors["catalog"][0].as_str().unwrap();
    let cases = vectors["cases"].as_array().unwrap();
    let failures: Vec<String> = cases
        .iter()
        .filter_map(|case| {
            let differing = differences(case, catalog);
            (!differing.is_empty()).then(|| format!("{}:\n  {}", case["name"], differing.join("\n  ")))
        })
        .collect();
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}

#[test]
fn the_next_read_takes_what_python_takes() {
    for case in vectors("lineage_functions")["next_chunk"].as_array().unwrap() {
        let args: Vec<u64> = serde_json::from_value(case["args"].clone()).unwrap();
        assert_eq!(
            next_chunk(args[0], args[1], args[2], args[3], args[4]),
            case["chunk"].as_u64().unwrap(),
            "{case}"
        );
    }
}

#[test]
fn a_follower_continues_where_python_continues() {
    for case in vectors("lineage_functions")["forward_cursor"].as_array().unwrap() {
        let seqs = |key: &str| -> Vec<u64> { serde_json::from_value(case[key].clone()).unwrap() };
        let number = |key: &str| case[key].as_u64().unwrap();
        let (returned, read) = (seqs("returned"), seqs("read"));
        let cursor = forward_cursor(
            returned.last().copied(),
            returned.len(),
            read.last().copied(),
            number("limit") as usize,
            number("since"),
            number("head"),
        );
        assert_eq!(cursor, number("cursor"), "{case}");
    }
}
