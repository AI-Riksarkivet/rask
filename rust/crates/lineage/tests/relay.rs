//! The relay drains a staged event as the Python relay does (`rust/vectors/relay.json`, ADR 0217 §5).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lineage::intake::{Authorizing, Recording, Refusal};
use lineage::model::{Event, parse_event};
use lineage::relay::{Drained, Effects, drain_one};
use serde_json::{Map, Value, json};

/// The stand-ins a recorded case names: each notes that it was called and answers what the case says.
struct Scripted<'a> {
    case: &'a Value,
    called: Vec<String>,
}

impl Scripted<'_> {
    fn says(&self, step: &str, answer: &str) -> bool {
        self.case[step] == answer
    }
}

impl Effects for Scripted<'_> {
    async fn authorize(&mut self, _: &Event, _: &Map<String, Value>) -> Authorizing {
        self.called.push("authorize".to_owned());
        match self.case["authorizing"].as_str().unwrap() {
            "allowed" => Authorizing::Allowed(Vec::new()),
            "denied" => Authorizing::Refused(Refusal::Denied),
            "unverified" => Authorizing::Refused(Refusal::Unverified),
            _ => Authorizing::Unavailable("OpenFGA did not answer".to_owned()),
        }
    }

    async fn record(&mut self, _: &Event, _: &[String]) -> Recording {
        self.called.push("record".to_owned());
        match self.case["recording"].as_str().unwrap() {
            "lands" => Recording::Landed,
            "foreign" => Recording::ForeignRun,
            _ => Recording::Failed("the catalog did not answer".to_owned()),
        }
    }

    async fn republish(&mut self) -> bool {
        self.called.push("republish".to_owned());
        self.says("publishing", "sent")
    }

    async fn drop_staged(&mut self, key: &str) -> bool {
        self.called.push(format!("drop {key}"));
        self.says("dropping", "dropped")
    }

    async fn keep_refusal(&mut self, key: &str, _: &Event) -> bool {
        self.called.push(format!("keep {key}"));
        self.says("keeping", "kept")
    }
}

#[tokio::test]
async fn a_staged_event_is_drained_as_python_drains_it() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/relay.json");
    let vectors: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let key = vectors["key"][0].as_str().unwrap();
    let data = json!({"eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z", "run": {"runId": "r"}, "job": {"namespace": "j", "name": "n"}});
    let event = parse_event(&data).unwrap();
    let cases = vectors["drains"].as_array().unwrap();
    let mut failures = Vec::new();
    for case in cases {
        let staged = (case["staged"] == "an event").then(|| (&event, data.as_object().unwrap()));
        let mut effects = Scripted { case, called: Vec::new() };
        let drained = drain_one(key, staged, &mut effects).await;
        let count = |was: bool| if was { 1 } else { 0 };
        let kept = match drained {
            Drained::Refused { kept } => kept,
            _ => None,
        };
        let outcome = json!({
            "drained": count(drained == Drained::Drained),
            "stranded": count(drained == Drained::Stranded),
            "refused": count(matches!(drained, Drained::Refused { .. })),
            "recorded": count(kept.is_some()),
        });
        let ours = json!({"effects": effects.called, "outcome": outcome, "counted": kept.map(|counted| counted.label()).into_iter().collect::<Vec<_>>()});
        let theirs = json!({"effects": case["effects"], "outcome": case["outcome"], "counted": case["counted"]});
        if ours != theirs {
            failures.push(format!("{case}\n    rust:   {ours}\n    python: {theirs}"));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} drains differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}
