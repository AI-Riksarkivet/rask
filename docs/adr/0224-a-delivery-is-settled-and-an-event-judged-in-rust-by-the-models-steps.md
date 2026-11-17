# 0224. A delivery is settled and an event judged in Rust by the model's steps (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), and the first written to a model (ADR 0221). It decides what
happens to a delivery and to the event it carries. Nothing takes deliveries from the bus with it yet. Source: the
owner, 2026-11-17 ("try to port the code to rust following these principles, keep ADR of features").

## Context

`specs/lineage/EventPath.tla` proves that no event is lost provided the lane takes four steps as modelled: deliver,
judge, ask again, park. In Python those steps are `lakehouse.bus_delivery.judged` and
`lineage.services.consumer.handle_cloud_event`.

## Decision

1. **`lakehouse_core::delivery` settles a delivery** (the model's Deliver, Again and Park):

   | Rust | Decides | Python it answers for |
   | --- | --- | --- |
   | `before_handling` | a delivery past the lane's attempts is parked unhandled | the first branch of `judged` |
   | `after_handling` | taken ends it; a park is asked for, or forced on the last attempt; anything else is asked for again | the rest of `judged` |
   | `after_parking` | a park that kept the delivery ends it; one that could not is asked for again | `_parked` |
   | `envelope_of` | an envelope is a JSON object nested no deeper than 300 | `envelope_of` |
   | `judged` | the shell: one deadline shared by the handling and the park | `judged`, `_handled` |

2. **`lineage::intake` judges the event** (the model's Judge): an event that cannot be read is taken; a writer with
   no grant is parked, because a grant would lift it; an unverified signature, a missing author, another subject's
   run and an ungoverned output are taken and counted, because no later delivery changes them; an authority or a
   store that did not answer is asked for again.
3. **Asking and recording are arguments.** `intake::handle` takes the two effects as functions and returns the
   verdict, so the decision is tested with no bus, no OpenFGA and no catalog.
4. **Parity is shown by vectors through the real Python** (`scripts/_port_delivery_vectors.py` →
   `rust/vectors/delivery.json`): 90 deliveries put to `judged` (every attempt up to one past the lane's, a handling
   that answers each verdict, fails or outlasts its time, a park that keeps, fails or outlasts), 29 events put to
   `handle_cloud_event` (every refusal, an authority and a store that fail), and 10 envelopes. The Rust gives the
   same outcome, delay and park reason for every delivery, and the same verdict and count for every event.

## What differs from the Python, on purpose

Nothing in the decisions. The envelope's nesting is counted on the text before it is parsed, where Python counts the
parsed value: the same envelopes are read.

## Not ported yet

Taking deliveries from the bus and settling them on it, the park's write to the refusals table, and what asks who
may write an event (the signature check and OpenFGA).

## Consequences

- A change to the order or the outcome of these steps changes `EventPath.tla` first, then a vector.
