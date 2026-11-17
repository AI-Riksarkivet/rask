//! The lane settles a delivery as the Python lane does (`rust/vectors/delivery.json`, ADR 0217 §5).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::delivery::{Delivery, Lane, Outcome, Verdict, envelope_of, judged};
use serde_json::{Value, json};
use std::cell::RefCell;
use std::time::Duration;

fn vectors() -> Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/delivery.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

#[tokio::test(start_paused = true)]
async fn a_delivery_is_settled_as_python_settles_it() {
    let vectors = vectors();
    let limits = &vectors["limits"][0];
    let seconds = |name: &str| Duration::from_secs_f64(limits[name].as_f64().unwrap());
    let cases = vectors["settlements"].as_array().unwrap();
    let mut failures = Vec::new();
    for case in cases {
        let lane = Lane::new(
            "S",
            "c",
            case["attempts"].as_u64().unwrap() as u32,
            seconds("retry_seconds"),
            seconds("ack_wait_seconds"),
            1,
        )
        .unwrap();
        let delivery = Delivery {
            subject: "s".to_owned(),
            body: b"{}".to_vec(),
            sequence: 1,
            attempt: case["attempt"].as_u64().unwrap() as u32,
        };
        let (handling, parking) = (case["handling"].as_str().unwrap(), case["parking"].as_str().unwrap());
        let reason = RefCell::new(None);
        let handle = async |_: &Delivery| match handling {
            "fails" => Err("ConnectionError: the catalog did not answer".to_owned()),
            "outlasts" => {
                tokio::time::sleep(Duration::from_secs(1)).await;
                Ok(Verdict::Taken)
            }
            "again" => Ok(Verdict::Again),
            "park" => Ok(Verdict::Park),
            _ => Ok(Verdict::Taken),
        };
        let park = async |_: &Delivery, given: &str| {
            reason.replace(Some(given.to_owned()));
            match parking {
                "fails" => Err("ConnectionError: the refusals table did not answer".to_owned()),
                "outlasts" => {
                    tokio::time::sleep(Duration::from_secs(1)).await;
                    Ok(())
                }
                _ => Ok(()),
            }
        };
        let settlement = judged(&delivery, &lane, handle, park).await;
        let outcome = match settlement.outcome {
            Outcome::Taken => "taken",
            Outcome::Again => "again",
            Outcome::Parked => "parked",
        };
        let ours = json!({"outcome": outcome, "delay_seconds": settlement.delay.as_secs_f64(), "park_reason": reason.into_inner()});
        let theirs = json!({"outcome": case["outcome"], "delay_seconds": case["delay_seconds"], "park_reason": case["park_reason"]});
        if ours != theirs {
            failures.push(format!("{case}\n    rust:   {ours}\n    python: {theirs}"));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} deliveries differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}

#[test]
fn an_envelope_is_read_where_python_reads_it() {
    for case in vectors()["envelopes"].as_array().unwrap() {
        let body = case["body"].as_str().unwrap();
        assert_eq!(envelope_of(body.as_bytes()).is_some(), case["read"].as_bool().unwrap(), "{:.80}", body);
    }
}
