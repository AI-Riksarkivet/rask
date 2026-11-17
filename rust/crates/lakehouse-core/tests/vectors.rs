//! The core functions answer what the Python ones answer (`rust/vectors/lineage_functions.json`, ADR 0217 §5).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::{events, naming, pywire};
use serde_json::Value;

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/lineage_functions.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

#[test]
fn a_dataset_is_external_where_python_says_so() {
    for case in vectors("external") {
        let text = |key: &str| case[key].as_str().unwrap();
        assert_eq!(
            naming::is_external_source_namespace(text("namespace"), text("name"), text("catalog")),
            case["external"],
            "{case}"
        );
    }
}

#[test]
fn a_static_event_has_the_id_python_gives_it() {
    for case in vectors("static_event_id") {
        let parts: Vec<&str> = case["parts"].as_array().unwrap().iter().map(|part| part.as_str().unwrap()).collect();
        assert_eq!(events::static_event_id(parts[0], parts[1], parts[2], parts[3], parts[4]), case["id"], "{case}");
    }
}

#[test]
fn json_text_is_written_as_python_writes_it() {
    for case in vectors("pyjson") {
        assert_eq!(pywire::dumps(&case["value"], pywire::COMPACT), case["text"].as_str().unwrap(), "{case}");
    }
}
