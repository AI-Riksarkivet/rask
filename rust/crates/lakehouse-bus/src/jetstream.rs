//! The JetStream wire protocol the bus speaks, as values: what a settlement is sent as, what a delivery's reply subject
//! says of it, and what the API answers. No connection is touched here.

use base64::Engine;
use lakehouse_core::delivery::{Outcome, Settlement};
use serde_json::{Value, json};
use std::time::Duration;

/// What every delivery's reply subject begins with, and no status the bus sends has: a status has no reply subject.
pub const ACK_PREFIX: &str = "$JS.ACK.";
/// JetStream's error code for a read by sequence that found no message (`JSNoMessageFoundErr`).
pub const NO_MESSAGE_FOUND: u64 = 10037;
/// The status the bus ends a pull with once its wait ran out with no message.
pub const PULL_EXPIRED: u16 = 408;
/// What a delivery cut short by the process stopping is sent as: asked for again at once.
pub const HANDED_BACK: &[u8] = b"-NAK";

/// What the bus is told of a settled delivery, published on its reply subject.
pub fn settle_payload(settlement: &Settlement) -> Vec<u8> {
    match settlement.outcome {
        Outcome::Taken => b"+ACK".to_vec(),
        Outcome::Again if settlement.delay.is_zero() => HANDED_BACK.to_vec(),
        Outcome::Again => format!("-NAK {{\"delay\": {}}}", nanos(settlement.delay)).into_bytes(),
        Outcome::Parked => b"+TERM".to_vec(),
    }
}

fn nanos(duration: Duration) -> u128 {
    duration.as_nanos()
}

/// The pull request for one message, which the bus holds open for `wait`.
pub fn pull_request(wait: Duration) -> Vec<u8> {
    json!({"batch": 1, "expires": nanos(wait)}).to_string().into_bytes()
}

/// The subject a pull on `consumer` of `stream` is published on.
pub fn pull_subject(stream: &str, consumer: &str) -> String {
    format!("$JS.API.CONSUMER.MSG.NEXT.{stream}.{consumer}")
}

/// The subject a read of `stream` by sequence is asked on.
pub fn get_subject(stream: &str) -> String {
    format!("$JS.API.STREAM.MSG.GET.{stream}")
}

/// The read of the last message `stream` holds on `subject`.
pub fn last_request(subject: &str) -> Vec<u8> {
    json!({"last_by_subj": subject}).to_string().into_bytes()
}

/// The read of the first message on `subject` at or after `sequence`.
pub fn next_request(sequence: u64, subject: &str) -> Vec<u8> {
    json!({"seq": sequence, "next_by_subj": subject}).to_string().into_bytes()
}

/// The read of the message at `sequence`.
pub fn at_request(sequence: u64) -> Vec<u8> {
    json!({"seq": sequence}).to_string().into_bytes()
}

/// Which delivery of which stream message a reply subject names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Delivered {
    pub sequence: u64,
    pub attempt: u32,
}

/// What a delivery's reply subject says of it: `$JS.ACK.<stream>.<consumer>.<delivered>.<stream seq>.…` (nine tokens),
/// or the same after a domain and an account hash (eleven or more).
pub fn delivered(reply: &str) -> Option<Delivered> {
    let tokens: Vec<&str> = reply.split('.').collect();
    if tokens.first() != Some(&"$JS") || tokens.get(1) != Some(&"ACK") {
        return None;
    }
    let first = match tokens.len() {
        9 => 4,
        length if length >= 11 => 6,
        _ => return None,
    };
    let attempt = tokens.get(first)?.parse::<u32>().ok()?;
    Some(Delivered {
        sequence: tokens.get(first + 1)?.parse().ok()?,
        attempt: attempt.max(1),
    })
}

/// What the JetStream API answered: its value, or the error it named.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{description} ({code}/{err_code})")]
pub struct ApiError {
    pub code: u64,
    pub err_code: u64,
    pub description: String,
}

/// The answer of an API request: the JSON object, or the error it carries.
pub fn answer(payload: &[u8]) -> Result<Value, ApiError> {
    let value: Value = serde_json::from_slice(payload).map_err(|error| ApiError {
        code: 0,
        err_code: 0,
        description: format!("the bus answered with no JSON: {error}"),
    })?;
    if let Some(error) = value.get("error") {
        let number = |key: &str| error.get(key).and_then(Value::as_u64).unwrap_or(0);
        return Err(ApiError {
            code: number("code"),
            err_code: number("err_code"),
            description: error.get("description").and_then(Value::as_str).unwrap_or("").to_owned(),
        });
    }
    Ok(value)
}

/// A message read from a stream by sequence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stored {
    pub subject: String,
    pub sequence: u64,
    pub data: Vec<u8>,
}

/// The message a read by sequence answered with.
pub fn stored(answer: &Value) -> Option<Stored> {
    let message = answer.get("message")?;
    let data = match message.get("data").and_then(Value::as_str) {
        Some(encoded) => base64::engine::general_purpose::STANDARD.decode(encoded).ok()?,
        None => Vec::new(),
    };
    Some(Stored {
        subject: message.get("subject").and_then(Value::as_str).unwrap_or("").to_owned(),
        sequence: message.get("seq").and_then(Value::as_u64).unwrap_or(0),
        data,
    })
}

/// What a stream said it kept of a publish.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishAck {
    pub stream: String,
    pub sequence: u64,
    pub duplicate: bool,
}

/// The acknowledgement a publish was answered with.
pub fn publish_ack(answer: &Value) -> Option<PublishAck> {
    Some(PublishAck {
        stream: answer.get("stream")?.as_str()?.to_owned(),
        sequence: answer.get("seq")?.as_u64()?,
        duplicate: answer.get("duplicate").and_then(Value::as_bool).unwrap_or(false),
    })
}

/// The event a structured envelope carries, or None for bytes that are no envelope: two publishes of one event differ in
/// their trace, so their events are what is compared.
pub fn event_of(body: &[u8]) -> Option<Value> {
    lakehouse_core::delivery::envelope_of(body)?.get("data").cloned()
}
