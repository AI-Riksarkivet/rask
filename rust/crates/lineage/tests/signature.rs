//! Lineage takes an arrived event on its signature as the Python door does (`rust/vectors/signing.json`, ADR 0217 §5).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::signing::json;
use lineage::signature::{Admission, Signing, attested, verify_arrived};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/signing.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

fn texts(list: &Value) -> Vec<String> {
    list.as_array().unwrap().iter().map(|text| text.as_str().unwrap().to_owned()).collect()
}

#[test]
fn an_arrived_event_is_admitted_as_python_admits_it() {
    let sets: BTreeMap<String, BTreeMap<String, Vec<String>>> = vectors("key_sets")
        .into_iter()
        .map(|set| {
            let published = set["published"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(identity, keys)| (identity.clone(), texts(keys)))
                .collect();
            (set["name"].as_str().unwrap().to_owned(), published)
        })
        .collect();
    let cases = vectors("lineage");
    assert!(!cases.is_empty());
    for case in cases {
        let signing = Signing {
            signers: texts(&case["signers"]).into_iter().collect::<BTreeSet<_>>(),
            delegators: texts(&case["delegators"]).into_iter().collect(),
        };
        let arrived = json::parse(case["arrived"].as_str().unwrap()).unwrap();
        let admission = verify_arrived(&arrived, &sets[case["keys"].as_str().unwrap()], &signing);
        let mut answer = match &admission {
            Admission::Unenforced => json!({"unenforced": true}),
            Admission::Verified(verified) => {
                json!({"verified": {"identity": verified.identity, "kid": verified.kid, "on_behalf_of": verified.on_behalf_of}})
            }
            Admission::Refused(refusal) => json!({"refused": refusal.reason.label()}),
            Admission::Unavailable(_) => json!({"unavailable": true}),
        };
        if matches!(admission, Admission::Unenforced | Admission::Verified(_)) {
            answer["attested"] = Value::Bool(attested(&admission, &signing));
        }
        assert_eq!(answer, case["answer"], "{} {}", case["name"], case["signers"]);
    }
}
