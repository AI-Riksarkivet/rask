# 0231. The relay drains a staged event in Rust by the model's steps (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), written to the model of ADR 0221. It decides what the relay
does with one event still staged in the outbox. Nothing lists the outbox or runs a tick with it yet. Source: the
owner, 2026-11-17 ("try to port the code to rust following these principles, keep ADR of features").

## Context

`specs/lineage/EventPath.tla` proves no event is lost provided the relay takes its steps in one order: judge, write,
publish again, drop, and keep a refusal before dropping a refused event (RelayJudge, RelayWrite, RelayRepublish,
RelayRefuse, RelayDrop). In Python the order is the body of `lineage.api.relay._drain_outbox`.

## Decision

1. **`lineage::relay::drain_one` drains one staged event:**

   | The event | What the relay does, in order | It answers |
   | --- | --- | --- |
   | cannot be read as an event | drop | `Poison` |
   | allowed | record, publish again, drop | `Drained`; `Stranded` where a step failed |
   | refused (no grant, signature, author, another subject's run, ungoverned output) | keep the refusal, drop | `Refused { kept }` with how it is counted; `kept: None` where the refusal could not be written |
   | undecided (the authority or the store did not answer) | nothing | `Stranded` |

   A stranded event stays staged for the next tick. A refused event that could not be kept stays staged too, and is
   kept under the same row the next time.
2. **The effects are a trait** (`relay::Effects`: authorize, record, publish again, drop, keep a refusal), so the
   order is tested with no outbox, no bus, no OpenFGA and no catalog. The judging is `lineage::intake`'s
   (ADR 0224): the relay and the bus lane refuse the same events.
3. **Parity is shown by vectors through the real Python** (`scripts/_port_relay_vectors.py` →
   `rust/vectors/relay.json`): 100 drains of one staged event put to `_drain_outbox`, over what is staged (an event,
   text that is no envelope, an envelope that is no event), every answer of the authorization and of the record, and
   a publish, a drop and a keep that each succeed or fail. A vector is the effects in their order and what the drain
   counted. The Rust takes the same effects in the same order and counts the same for all 100.
4. **The test was shown to bite.** Four planted defects were each reported: dropping before publishing again (8 of
   100 drains differ), dropping a refused event that was not kept (28), stranding another subject's run where Python
   keeps it as refused (8), and leaving a poison event staged (4).

## What differs from the Python, on purpose

Nothing in the order or the counts. Python counts into four totals of a tick; the Rust answers one value per event
and the tick sums them.

## Not ported yet

Listing the outbox on the object store and reading a staged text, the tick and the window it claims (`lakehouse.timers`), the publish itself,
and the refusals table's write.

## Consequences

- A change to the order of the relay's effects changes `EventPath.tla` first, then a vector.
