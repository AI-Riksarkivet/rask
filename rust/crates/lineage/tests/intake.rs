//! An event is judged as the Python consumer judges it (`rust/vectors/delivery.json`, ADR 0217 §5).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::delivery::Verdict;
use lineage::intake::{Authorizing, Recording, Refusal, handle};
use lineage::model::Event;
use serde_json::{Map, Value, json};

#[tokio::test]
async fn an_event_is_judged_as_python_judges_it() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/delivery.json");
    let vectors: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let cases = vectors["handlings"].as_array().unwrap();
    let mut failures = Vec::new();
    for case in cases {
        let authorizing = case["authorizing"].as_str().unwrap();
        let asked = match authorizing {
            "allowed" => Authorizing::Allowed(Vec::new()),
            "denied" => Authorizing::Refused(Refusal::Denied),
            "unverified" => Authorizing::Refused(Refusal::Unverified),
            "unauthored" => Authorizing::Refused(Refusal::Unauthored),
            "foreign" => Authorizing::Refused(Refusal::ForeignRun),
            "ungoverned" => Authorizing::Refused(Refusal::Ungoverned),
            _ => Authorizing::Unavailable("OpenFGA did not answer".to_owned()),
        };
        let written = match case["recording"].as_str().unwrap() {
            "lands" => Recording::Landed,
            "foreign" => Recording::ForeignRun,
            _ => Recording::Failed("the catalog did not answer".to_owned()),
        };
        let authorize = (authorizing != "no check").then_some(async |_: &Event, _: &Map<String, Value>| asked);
        let (verdict, counted) = handle(case["body"].as_object(), authorize, async |_: &Event, _: &[String]| written).await;
        let verdict = match verdict {
            Verdict::Taken => "taken",
            Verdict::Again => "again",
            Verdict::Park => "park",
        };
        let ours = json!({"verdict": verdict, "counted": [counted.label()]});
        let theirs = json!({"verdict": case["verdict"], "counted": case["counted"]});
        if ours != theirs {
            failures.push(format!(
                "{} / {} / {}\n    rust:   {ours}\n    python: {theirs}",
                case["body"], case["authorizing"], case["recording"]
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} handlings differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}
