//! The size bound an event is held to, at its edge.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lineage::model::{EventError, MAX_EVENT_BYTES, parse_event};
use serde_json::json;

fn event_weighing(bytes: usize) -> serde_json::Value {
    let bare = json!({"eventType": "START", "eventTime": "t", "run": {"runId": "r"}, "job": {"namespace": "j", "name": "n"}, "pad": ""});
    let weight = parse_event(&bare).unwrap().to_json().len();
    let mut padded = bare;
    padded["pad"] = json!("x".repeat(bytes - weight));
    padded
}

#[test]
fn an_event_at_the_weight_bound_is_taken_and_one_byte_over_is_refused() {
    assert_eq!(parse_event(&event_weighing(MAX_EVENT_BYTES)).unwrap().to_json().len(), MAX_EVENT_BYTES);
    assert_eq!(
        parse_event(&event_weighing(MAX_EVENT_BYTES + 1)),
        Err(EventError::TooHeavy(MAX_EVENT_BYTES + 1))
    );
}
