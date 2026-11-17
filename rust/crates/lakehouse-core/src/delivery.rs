//! How a delivery from the bus is settled: taken, asked for again, or parked.
//!
//! The steps are those of `specs/lineage/EventPath.tla` (Deliver, Judge, Again, Park): a delivery is handled on a
//! bounded number of attempts, and what could not be handled by the last is parked, never acknowledged unseen.

use serde_json::{Map, Value};
use std::time::Duration;
use tokio::time::Instant;

/// The deepest an envelope may nest. One nested deeper is not read at all.
pub const MAX_ENVELOPE_DEPTH: usize = 300;
const MAX_FAILURE_CHARS: usize = 300;

/// What a handling says of a delivery.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    /// Done with it: recorded, or dropped on purpose.
    Taken,
    /// Could not be handled now; a later attempt may.
    Again,
    /// Will never be handled as it is; keep it for a person.
    Park,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Taken,
    Again,
    Parked,
}

/// How the bus is told to settle a delivery, and how long it waits before it delivers one asked for again.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Settlement {
    pub outcome: Outcome,
    pub delay: Duration,
}

/// A durable consumer's lane: how many attempts a delivery gets and how long each may take.
#[derive(Debug, Clone, PartialEq)]
pub struct Lane {
    pub stream: String,
    pub consumer: String,
    pub attempts: u32,
    pub retry: Duration,
    pub ack_wait: Duration,
    pub in_flight: usize,
}

impl Lane {
    /// What is kept of the ack wait for the settle, so the bus never hands a delivery out beneath a handling still running.
    pub const SETTLE: Duration = Duration::from_secs(10);

    pub fn new(stream: &str, consumer: &str, attempts: u32, retry: Duration, ack_wait: Duration, in_flight: usize) -> Result<Self, String> {
        if attempts < 1 || in_flight < 1 {
            return Err("a lane handles a delivery on at least one attempt, at least one at a time".to_owned());
        }
        if ack_wait <= Self::SETTLE {
            return Err(format!(
                "an ack wait of {} s leaves no time to handle a delivery ({} s is kept for its settle)",
                ack_wait.as_secs_f64(),
                Self::SETTLE.as_secs()
            ));
        }
        Ok(Lane {
            stream: stream.to_owned(),
            consumer: consumer.to_owned(),
            attempts,
            retry,
            ack_wait,
            in_flight,
        })
    }

    /// How long a handling and its park may take together.
    pub fn handling_time(&self) -> Duration {
        self.ack_wait - Self::SETTLE
    }

    fn again(&self) -> Settlement {
        Settlement {
            outcome: Outcome::Again,
            delay: self.retry,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Delivery {
    pub subject: String,
    pub body: Vec<u8>,
    pub sequence: u64,
    /// Which delivery of the message this is, from 1.
    pub attempt: u32,
}

impl Delivery {
    pub fn envelope(&self) -> Option<Map<String, Value>> {
        envelope_of(&self.body)
    }
}

/// How deep the containers of the JSON text `body` nest, without parsing it.
fn nesting(body: &[u8]) -> usize {
    let (mut depth, mut deepest, mut in_string, mut escaped) = (0usize, 0usize, false, false);
    for byte in body {
        match (in_string, byte) {
            (true, _) if escaped => escaped = false,
            (true, b'\\') => escaped = true,
            (true, b'"') => in_string = false,
            (false, b'"') => in_string = true,
            (false, b'{' | b'[') => {
                depth += 1;
                deepest = deepest.max(depth);
            }
            (false, b'}' | b']') => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    deepest
}

/// The envelope `body` is: a JSON object nested no deeper than [`MAX_ENVELOPE_DEPTH`].
pub fn envelope_of(body: &[u8]) -> Option<Map<String, Value>> {
    if nesting(body) > MAX_ENVELOPE_DEPTH {
        return None;
    }
    let mut parser = serde_json::Deserializer::from_slice(body);
    // The count above bounds the nesting; the parser's own bound is lower than the envelope's.
    parser.disable_recursion_limit();
    let envelope: Map<String, Value> = serde::Deserialize::deserialize(&mut parser).ok()?;
    parser.end().ok()?;
    Some(envelope)
}

/// What a handling came to: its verdict, or what it said when it failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Handled {
    Answered(Verdict),
    Failed(String),
}

/// What is done with a delivery next: settle it, or park it for this reason and then settle it.
#[derive(Debug, Clone, PartialEq)]
pub enum Next {
    Settle(Settlement),
    Park(String),
}

/// A delivery past the lane's attempts is parked unhandled: each earlier one ended before its handling did.
pub fn before_handling(attempt: u32, lane: &Lane) -> Option<String> {
    (attempt > lane.attempts).then(|| format!("every one of its {} deliveries ended before its handling did", lane.attempts))
}

/// What follows a handling: taken ends it, a park is asked for or forced on the last attempt, and anything else is asked for again.
pub fn after_handling(attempt: u32, lane: &Lane, handled: &Handled) -> Next {
    let failure = match handled {
        Handled::Answered(Verdict::Taken) => {
            return Next::Settle(Settlement {
                outcome: Outcome::Taken,
                delay: Duration::ZERO,
            });
        }
        Handled::Answered(Verdict::Park) => return Next::Park("its handling asked for it to be parked".to_owned()),
        Handled::Answered(Verdict::Again) => None,
        Handled::Failed(failure) => Some(failure),
    };
    if attempt < lane.attempts {
        return Next::Settle(lane.again());
    }
    let last = format!("{attempt} of {}", lane.attempts);
    Next::Park(match failure {
        Some(failure) => format!("its last delivery ({last}) failed: {failure}"),
        None => format!("asked for again on its last delivery ({last})"),
    })
}

/// A park that kept the delivery ends it; one that could not is asked for again, so it is parked by a later attempt.
pub fn after_parking(lane: &Lane, kept: bool) -> Settlement {
    if kept {
        Settlement {
            outcome: Outcome::Parked,
            delay: Duration::ZERO,
        }
    } else {
        lane.again()
    }
}

/// Handle `delivery` and settle it. The handling and the park share one deadline, the lane's handling time.
pub async fn judged<H, P>(delivery: &Delivery, lane: &Lane, handle: H, park: P) -> Settlement
where
    H: AsyncFnOnce(&Delivery) -> Result<Verdict, String>,
    P: AsyncFnOnce(&Delivery, &str) -> Result<(), String>,
{
    let until = Instant::now() + lane.handling_time();
    let reason = match before_handling(delivery.attempt, lane) {
        Some(reason) => reason,
        None => {
            let handled = match tokio::time::timeout_at(until, handle(delivery)).await {
                Ok(Ok(verdict)) => Handled::Answered(verdict),
                Ok(Err(failure)) => Handled::Failed(failure.chars().take(MAX_FAILURE_CHARS).collect()),
                Err(_) => Handled::Failed(format!("TimeoutError: its handling outran {:.0} s", lane.handling_time().as_secs_f64())),
            };
            if let Handled::Failed(error) = &handled {
                tracing::warn!(
                    stream = lane.stream,
                    consumer = lane.consumer,
                    sequence = delivery.sequence,
                    attempt = delivery.attempt,
                    error,
                    "delivery_handling_failed"
                );
            }
            match after_handling(delivery.attempt, lane, &handled) {
                Next::Settle(settlement) => return settlement,
                Next::Park(reason) => reason,
            }
        }
    };
    let kept = matches!(tokio::time::timeout_at(until, park(delivery, &reason)).await, Ok(Ok(())));
    if kept {
        tracing::warn!(
            stream = lane.stream,
            consumer = lane.consumer,
            sequence = delivery.sequence,
            attempt = delivery.attempt,
            reason,
            "delivery_parked"
        );
    } else {
        tracing::error!(
            stream = lane.stream,
            consumer = lane.consumer,
            sequence = delivery.sequence,
            attempt = delivery.attempt,
            reason,
            "delivery_not_parked"
        );
    }
    after_parking(lane, kept)
}
