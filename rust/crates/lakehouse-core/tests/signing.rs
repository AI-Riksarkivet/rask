//! An event's signature is judged as the Python verifier judges it (`rust/vectors/signing.json`, ADR 0217 §5).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::pywire::json::{self, Json};
use lakehouse_core::signing::{NotVerified, VerifiedSignature, canon, nkey, signed_control_document, signed_document, verify_control_event, verify_event};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/signing.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

fn texts(list: &Value) -> BTreeSet<String> {
    list.as_array().unwrap().iter().map(|text| text.as_str().unwrap().to_owned()).collect()
}

/// Each recorded key set, its lists in the order published, since the current key comes first.
fn key_sets() -> BTreeMap<String, BTreeMap<String, Vec<String>>> {
    let list = |keys: &Value| keys.as_array().unwrap().iter().map(|key| key.as_str().unwrap().to_owned()).collect();
    vectors("key_sets")
        .into_iter()
        .map(|set| {
            let published = set["published"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(identity, keys)| (identity.clone(), list(keys)))
                .collect();
            (set["name"].as_str().unwrap().to_owned(), published)
        })
        .collect()
}

fn verdict(outcome: Result<VerifiedSignature, NotVerified>) -> Value {
    match outcome {
        Ok(verified) => json!({"verified": {"identity": verified.identity, "kid": verified.kid, "on_behalf_of": verified.on_behalf_of}}),
        Err(NotVerified::Refused(refusal)) => json!({"refused": refusal.reason.label()}),
        Err(NotVerified::Unavailable(_)) => json!({"unavailable": true}),
    }
}

#[test]
fn an_event_is_judged_as_python_judges_it() {
    let sets = key_sets();
    let cases = vectors("events");
    assert!(cases.len() > 90);
    for case in cases {
        let arrived = json::parse(case["arrived"].as_str().unwrap()).unwrap();
        let (signers, delegators) = (texts(&case["signers"]), texts(&case["delegators"]));
        let keys = &sets[case["keys"].as_str().unwrap()];
        let control = case["kind"] == "control";
        let outcome = if control {
            verify_control_event(&arrived, keys, &signers, &delegators)
        } else {
            verify_event(&arrived, keys, &signers, &delegators)
        };
        assert_eq!(verdict(outcome), case["verdict"], "{}", case["name"]);
        let document = if control {
            signed_control_document(&arrived)
        } else {
            signed_document(&arrived)
        };
        let bytes = document
            .and_then(|document| canon::canon_1(&document).ok())
            .map(|bytes| String::from_utf8(bytes).unwrap());
        assert_eq!(bytes.as_deref(), case["signed_bytes"].as_str(), "{}", case["name"]);
    }
}

#[test]
fn a_value_has_the_canonical_bytes_python_gives_it() {
    let cases = vectors("canon");
    assert!(cases.len() > 600);
    for case in cases {
        let value: Json = json::parse(case["text"].as_str().unwrap()).unwrap();
        let bytes = canon::canon_1(&value).ok().map(|bytes| String::from_utf8(bytes).unwrap());
        assert_eq!(bytes.as_deref(), case["canon"].as_str(), "{}", case["text"]);
    }
}

#[test]
fn a_published_entry_names_the_key_id_python_derives() {
    for case in vectors("public_keys") {
        let kid = nkey::raw_public_key(case["entry"].as_str().unwrap()).map(|raw| nkey::kid_of(&raw));
        assert_eq!(kid.as_deref(), case["kid"].as_str(), "{}", case["entry"]);
    }
}

#[test]
fn a_published_list_is_split_as_python_splits_it() {
    for case in vectors("published_fields") {
        let keys: Vec<Value> = nkey::parse_published_keys(case["field"].as_str().unwrap())
            .into_iter()
            .map(Value::String)
            .collect();
        assert_eq!(Value::Array(keys), case["keys"], "{}", case["field"]);
    }
}

#[test]
fn text_python_does_not_read_is_not_read() {
    for text in [
        "",
        "{",
        "[1,]",
        "{\"a\" 1}",
        "01",
        "1.",
        "1e",
        "+1",
        "\"\\x\"",
        "\"\\u12\"",
        "\"a\u{1}\"",
        "nul",
        "[1] 2",
        "\u{feff}{}",
    ] {
        assert!(json::parse(text).is_err(), "{text:?}");
    }
    assert!(json::parse(&"[".repeat(json::MAX_PARSE_DEPTH + 1)).is_err());
    assert!(json::parse(&"1".repeat(4301)).is_err());
}
