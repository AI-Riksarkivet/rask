//! The envelope and the stream page answer what the Python ones answer (`rust/vectors/bus.json`, ADR 0225).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_bus::envelope::{self, MAX_PAYLOAD_BYTES, Oversize, WARN_PAYLOAD_BYTES};
use lakehouse_bus::{CloudEvent, StreamPage, TraceContext, lineage_envelope};
use lakehouse_core::delivery::Delivery;
use lakehouse_core::pywire;
use serde_json::{Map, Value};

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/bus.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

fn text<'a>(case: &'a Value, key: &str) -> &'a str {
    case[key].as_str().unwrap()
}

#[test]
fn an_openlineage_event_gets_the_envelope_python_gives_it() {
    let cases = vectors("lineage");
    for case in &cases {
        let event: Map<String, Value> = serde_json::from_str(text(case, "event")).unwrap();
        assert_eq!(lineage_envelope(&event).staged_text(), text(case, "wire"), "{}", text(case, "event"));
    }
    assert_eq!(cases.len(), 81);
}

#[test]
fn a_staged_envelope_is_read_or_refused_as_python_reads_it() {
    let cases = vectors("parses");
    for case in &cases {
        let parsed = CloudEvent::parse(text(case, "text").as_bytes());
        match case["wire"].as_str() {
            Some(wire) => assert_eq!(parsed.map(|envelope| envelope.staged_text()).as_deref(), Ok(wire), "{}", text(case, "text")),
            None => assert!(parsed.is_err(), "{} was read as {parsed:?}", text(case, "text")),
        }
    }
    assert_eq!(cases.len(), 98);
}

#[test]
fn a_publish_sends_the_bytes_python_sends() {
    let cases = vectors("bodies");
    for case in &cases {
        let envelope = CloudEvent::parse(text(case, "envelope").as_bytes()).unwrap();
        let trace = TraceContext::continuing(case["traceparent"].as_str(), case["tracestate"].as_str());
        let body = envelope.body(trace.as_ref()).unwrap();
        assert_eq!(String::from_utf8(body).unwrap(), text(case, "body"), "{case}");
    }
    assert_eq!(cases.len(), 12);
}

#[test]
fn a_relayed_envelope_keeps_the_trace_it_was_staged_under() {
    for case in vectors("trace_of") {
        let fresh = CloudEvent::parse(text(&case, "envelope").as_bytes()).unwrap();
        let staged = CloudEvent::parse(text(&case, "staged").as_bytes()).unwrap();
        assert_eq!(fresh.with_trace_of(&staged).staged_text(), text(&case, "wire"));
    }
}

#[test]
fn a_read_begins_and_finds_a_gap_where_python_says() {
    let limits = &vectors("limits")[0];
    assert_eq!(limits["max_payload_bytes"], MAX_PAYLOAD_BYTES);
    assert_eq!(limits["warn_payload_bytes"], WARN_PAYLOAD_BYTES);
    let places = vectors("places");
    for case in &places {
        let number = |key: &str| case[key].as_u64().unwrap();
        assert_eq!(
            StreamPage::first_place(number("after"), number("end"), number("limit")),
            case["first"].as_u64(),
            "{case}"
        );
    }
    let pages = vectors("pages");
    for case in &pages {
        let messages = case["sequences"]
            .as_array()
            .unwrap()
            .iter()
            .map(|sequence| Delivery {
                subject: "s".to_owned(),
                body: Vec::new(),
                sequence: sequence.as_u64().unwrap(),
                attempt: 1,
            })
            .collect();
        let page = StreamPage::of(case["after"].as_u64().unwrap(), case["end"].as_u64().unwrap(), messages);
        assert_eq!(Value::Bool(page.gap), case["gap"], "{case}");
    }
    assert_eq!((places.len(), pages.len()), (54, 90));
}

#[test]
fn an_envelope_past_the_claim_check_cap_is_refused_before_anything_is_sent() {
    let mut envelope = CloudEvent::parse(br#"{"id": "e", "source": "s", "type": "t", "data": {}}"#).unwrap();
    let wrapping = envelope.body(None).unwrap().len() + r#""x": """#.len();
    envelope.data.insert("x".to_owned(), Value::String("a".repeat(MAX_PAYLOAD_BYTES - wrapping)));
    assert_eq!(envelope.body(None).unwrap().len(), MAX_PAYLOAD_BYTES);
    envelope
        .data
        .insert("x".to_owned(), Value::String("a".repeat(MAX_PAYLOAD_BYTES - wrapping + 1)));
    assert_eq!(envelope.body(None), Err(Oversize { bytes: MAX_PAYLOAD_BYTES + 1 }));
}

#[test]
fn a_float_is_spelled_as_python_spells_it() {
    for (float, spelled) in [
        (1e16, "1e+16"),
        (1e15, "1000000000000000.0"),
        (1e-5, "1e-05"),
        (0.0001, "0.0001"),
        (-1.5e-7, "-1.5e-07"),
        (123.456, "123.456"),
    ] {
        assert_eq!(pywire::float_repr(float), spelled);
    }
    assert!(envelope::is_traceparent("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"));
}
