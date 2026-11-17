# 0225. The bus is spoken in Rust as in Python: one envelope, one credential, one settlement (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), the shell around the delivery rule of ADR 0224 and the steps of
`specs/lineage/EventPath.tla` (ADR 0221) that touch the bus. Nothing on the estate runs it yet. Source: the owner,
2026-11-17 ("try to port the code to rust following these principles, keep ADR of features").

## Context

Every event the lakehouse emits rides NATS JetStream in a CloudEvents envelope, and every service that takes events
pulls a durable consumer the chart declares (`lance.busConsumers`, `chart/templates/nats-stream-job.yaml`). In Python
that is `lakehouse.bus` and `lakehouse.cloud_events`. During the mixed period a Python producer and a Rust consumer
share one stream, so the envelope must be the same bytes, the message id the same text, and a settlement the same
call, or the stream's deduplication and the consumer's redelivery stop meaning what the model assumes.

## Decision

1. **A crate of its own, `rust/crates/lakehouse-bus`**, on `lakehouse-core` and `async-nats` (0.52, rustls with the
   aws-lc provider, no JetStream helpers). Every JetStream call is a request on a subject written out where it is
   made, the same subjects the permission table grants (`chart/values.yaml` § Bus authentication):

   | Rust | Decides or does | Python it answers for |
   | --- | --- | --- |
   | `envelope::CloudEvent::parse` | the envelope a staged text holds, or a refusal | `CloudEvent.model_validate_json` (`outbox.staged_envelope`) |
   | `envelope::CloudEvent::wire`, `staged_text` | the JSON object the bus carries, and the text an outbox stages | `CloudEvent.as_wire`, `outbox.stage_envelope` |
   | `envelope::CloudEvent::body` | the bytes a publish sends, the claim-check caps (64 KiB said, 900 KiB refused) | `bus._body` |
   | `envelope::lineage_envelope` | an OpenLineage event's id, source, type, subject and time | `cloud_events.lineage_envelope` |
   | `envelope::is_traceparent`, `is_tracestate`, `TraceContext::continuing` | which trace context is adopted | `context.is_traceparent`, `is_tracestate`, `continuing` |
   | `instant` | an `eventTime` read as `datetime.fromisoformat` reads it; a staged `time` read and written as pydantic does | the standard library and pydantic |
   | `lakehouse_core::pywire` | JSON text as `json.dumps` writes it, a float as `repr` | the standard library |
   | `pystr` | a value as `str()` writes it, and whether Python reads it as false | the standard library |
   | `page::StreamPage` | where a read by sequence begins and whether it found a gap | `bus.StreamPage` |
   | `jetstream` | what a settlement is sent as (`+ACK`, `-NAK {"delay": ns}`, `+TERM`), what a reply subject says of a delivery, what the API answered | `bus._tell`, nats-py's `Msg` |
   | `bus::BusSettings` | the bus, user, token file, CA and session the chart names in `RASK_BUS_*` | `bus.BusSettings` |
   | `bus::Bus::start` and its keeper | one connection as the service's own user, its session asked for again before the bus ends it, another opened when it closes for good | `Bus.keep_connected`, `_open`, `_kept` |
   | `bus::Bus::send`, `publish_event` | a publish under a timeout, its duplicate acknowledgement believed only once the stored message carries the event | `Bus.send`, `publish_event`, `_stored_carries` |
   | `bus::Bus::consume` | pulls of the chart's consumer, at most `in_flight` handlings, each settled by `lakehouse_core::delivery::judged` | `Bus.consume`, `_pulled`, `pull`, `_settled` |
   | `bus::Bus::read` | a stream read by sequence after a cursor | `Bus.read` |
   | `bus::Bus::answering` | the readiness verdict for the bus | `Bus.answering` |

2. **The credential is the Python's.** Each connect calls back for the token and reads it from `RASK_BUS_TOKEN_FILE`
   as the file holds it then (the kubelet's projected ServiceAccount token); the environment names only where it is.
   Nothing read from the file is logged: a failed read says its kind. A `tls://` bus is dialled with the handshake
   before the greeting and verified against `RASK_BUS_CA_FILE` or the system's store. Replies come under the user's own
   inbox prefix `_INBOX_<user>`.
3. **The service creates nothing on the bus.** It binds to the consumer the stream Job declares by publishing its pull
   on `$JS.API.CONSUMER.MSG.NEXT.<stream>.<consumer>`; a consumer the bus does not hold yet is asked for again.
4. **A settlement is the Python's call:** taken is `+ACK`, again is `-NAK {"delay": <ns>}` (plain `-NAK` for no delay),
   parked is `+TERM`, each published on the delivery's reply subject. A delivery is told from a status by its reply
   subject, never by its headers, which are its publisher's.
5. **A stop stops pulling and lets every handling in flight finish and be settled** (each is bounded by the lane's
   handling time, ADR 0224); the pull under way is waited out, so no message is spent on an inbox nobody reads.

## How parity is shown

- *Vectors through the real Python* (`scripts/_port_bus_vectors.py` → `rust/vectors/bus.json`): 81 OpenLineage events
  put to `lineage_envelope` and `as_wire` (every state, terminal or not, 40 `eventTime` spellings, one output, several
  or none, a uuid that is no string, every falsy run id, a dataset change, floats and non-ASCII in `data`); 98 staged
  texts put to `model_validate_json` (each attribute missing, empty, of the wrong type; 42 `time` spellings; 8
  `traceparent` and 17 `tracestate` spellings; duplicate keys); 12 publish bodies put to `_body` under four trace
  contexts; 1 relayed envelope put to `with_trace_of`; 54 starts and 90 pages put to `StreamPage`. The Rust gives the
  same bytes for every envelope and body, and refuses exactly the texts Python refuses. Two runs of the generator
  write the same file.
- *A real bus* (`rust/crates/lakehouse-bus/tests/bus.rs`, nats-server 2.15.2 fetched by `scripts/nats_server.sh` and
  held to its digest in `scripts/toolchain.sha256`, one server per test on a free port with its own store, the stream
  and consumer declared as the stream Job declares them, `max_deliver -1`). Each case of the model the bus takes:

  | Test | Model step |
  | --- | --- |
  | `a_published_envelope_is_delivered_and_once_taken_is_not_delivered_again` | `Publish`, `Deliver`, `Judge` taken, `Settled` |
  | `a_delivery_asked_for_again_comes_back_after_the_delay_as_attempt_two` | `Again`, `Redelivered` |
  | `a_delivery_that_fails_every_attempt_is_parked_on_the_last_and_ended` | `JudgeFails` on every attempt, then `Park` |
  | `a_delivery_whose_replica_dies_mid_handling_is_handed_out_again_after_the_ack_wait` | `ReplicaDies`, `Redelivered` |
  | `no_more_deliveries_are_handled_at_once_than_the_lane_allows` | the lane's in-flight bound (`deliveriesInFlight`) |
  | `a_stop_finishes_and_settles_what_is_in_flight` | a stop is no `ReplicaDies`: what was begun is settled |
  | `a_duplicate_is_delivered_once_and_a_message_another_stored_under_its_id_is_not_believed` | `Publish`, `PublishFails` |
  | `a_read_by_sequence_returns_what_follows_a_cursor_and_reports_one_older_than_retention` | A4: a cursor older than what the stream keeps is a gap |
  | `the_health_read_fails_while_the_server_is_away_and_recovers_on_the_token_the_file_holds_then` | the credential read at each connect |

- *Both implementations on one stream* (`scripts/bus_interop.py`, run by hand: `uv run --no-sync python
  scripts/bus_interop.py`): Python's `publish_event` publishes two events that the Rust bus takes, and the Rust bus
  publishes two that Python's `Bus.consume` takes, each under the id it was published with, on one server with token
  authentication.

## What differs from the Python, on purpose

- **A stop finishes what is in flight.** Python cancels each handling and asks for its delivery again at once
  (`-NAK`); Rust lets it finish and settles it. Both hand nothing to a second replica beneath a running handling.
- **A handling that panics** is not settled (its delivery comes again after the ack wait); the consumer finishes the
  others and returns `HandlingPanicked`, where Python's loop raises and ends the process.
- **The inbox grant is judged by the bus's refusal, whenever it arrives,** not by two round trips before the
  connection is held: the Rust client exposes no round trip. A refused inbox closes the connection, and another is
  opened after a pause.
- **No spans and no metrics yet.** The publish stamps the trace context its caller passes, or the one the envelope was
  staged under, where Python stamps a new producer span's; there is no consumer span, and the counters of
  `bus_metrics` are log lines. The Rust plane has no OpenTelemetry yet.
- **Inputs nothing produces:** an `eventTime` in ISO week or basic form is read as no time; a staged `time` that is a
  unix timestamp is refused (Python's `as_wire` always writes text); a JSON integer `-0` is read as the float `-0.0`;
  an integer past 64 bits is read as a float; a `runId`, `eventTime` or `producer` that is an array or an object is
  spelled as its JSON text where Python spells its `repr`. Each is outside the corpus, as ADR 0217 §5 requires.

## The four facts

- **GitOps:** the service declares nothing on the bus; the stream and consumer are the chart's, and the tests declare
  them as the chart's Job does.
- **Cloud native:** one connection per process, kept by a task; the pod's readiness reads `answering`.
- **Lance-idiomatic:** not applicable; the bus carries pointers to Lance tables, never their data.
- **Zero trust:** the only credential is the projected token, read from its file at each connect and never logged;
  TLS first on a `tls://` bus; replies under an inbox no other user holds; a duplicate acknowledgement is not believed
  until the stored message is read back.

## Line budget

`rust/budget.json` gives `lakehouse-bus` 642 lines: the executing lines of `bus.py` (538) and `cloud_events.py` (104),
counted without blanks, comments and docstrings. The crate is 1,047 lines, over it: the Python's `str()` spelling,
datetime grammars and pull protocol are its libraries' (the standard library, pydantic, nats-py) and count nothing,
where the Rust carries each. Its JSON text and float `repr` are `lakehouse_core::pywire`'s, which the edge key and
canon-1 share.

## Not ported yet

`control_envelope` (it needs the catalog's control event model), `CloudEvent.under_current_trace`, `Bus.follow` (the
catalog's follow of its control topic), the processing and publish spans and the bus metrics, and the park's write to
lineage's refusals table.

## Consequences

- A change to the envelope, a settlement or the subjects the bus uses lands in both implementations in one commit, and
  the vectors or `scripts/bus_interop.py` show it.
- The integration tests need the pinned nats-server; `scripts/nats_server.sh` fetches it into `.localbin/`, and no
  container runs.
