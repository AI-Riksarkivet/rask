# 0235. What lineage runs without a request is ported to Rust by the model's steps: its settings, the ingest lane, the relay tick and the forwarder (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), assembled from ADR 0223 (signature), 0224 (delivery and intake),
0225 (the bus), 0226 (authorization), 0228 (the store), 0230 (secrets and published keys), 0231 (the relay's drain step)
and 0234 (the outbox and its store session), and written to the model of ADR 0221. Each component is a value built from
its dependencies with a `run` or `tick` the service's main calls; no `main.rs` wires them yet. Source: the owner,
2026-11-17 ("try to port the code to rust following these principles, keep ADR of features").

## Context

`specs/lineage/EventPath.tla` proves that no event is lost provided the bus lane takes the steps Deliver, Judge, Again
and Park, and the relay the steps Pick, RelayJudge, RelayWrite, RelayRepublish, RelayRefuse and RelayDrop, with the feed
row written last. The earlier features ported each step as a decision; nothing took deliveries from a consumer, listed the
outbox on a timer, or forwarded the feed. In Python these are `lineage.core.config.LineageSettings`,
`lineage.api.ingest_lane` with `lineage.services.consumer`, `lineage.api.relay` with `lineage.services.staged`,
`lakehouse.timers` with `lakehouse.records`, and `lineage.services.forward`; the gate both routes pass is
`lineage.api.fga_deps.enforce_bus_authz` with `lineage.services.signature.verify_arrived_event`.

## Decision

1. **Seven files, five in the lineage crate and two in `lakehouse-core`:**

   | Rust | Decides or does | Python it answers for |
   | --- | --- | --- |
   | `lineage::settings` | lineage's own settings from the environment under the chart's names, pydantic's lax forms, each refusal naming its variable; the lane, the drain limit, the endpoint and the forwarder's name | `LineageSettings`, `Signing`, `ingest_lane.lane`, `forward.endpoint_url`, `forward.follower` |
   | `lineage::gate` | the signature over the bytes that arrived (on a blocking thread, since published keys are read through a blocking fetch), then `bus_authz` as the stamped author, with the feed's two lookups read through the catalog | `enforce_bus_authz`, `verify_arrived_event`, `_is_replay` |
   | `lineage::lane` | `IngestLane`, the bus's `Handler`: a delivery judged by `intake::handle` through the gate and recorded by the store; a park that keeps only plain text of the delivery, under its CloudEvent id or its feed row, unless the feed holds that row; the counts by door | `IngestLane`, `_plain`, `handle_cloud_event`, `record_outcome` |
   | `lineage::tick` | the tick over a `Tick` trait (drain the outbox oldest first through `relay::drain_one`, retire recovered parks, forward, prune), the `RelayReport`, and `Relay`, which implements `Tick` and `relay::Effects` over the real store, outbox, bus, secret store and backend, and runs on its timer | `relay.tick`, `_drain_outbox`, `_prune_old_events`, `start_relay`, `republish_staged`, `DrainOutcome`, `RelayReport` |
   | `lineage::forward` | the forwarder over a `Followed` trait (the store implements it): the cursor judged against the feed's bounds, POSTs a page at a time, a refusal of one event passed, any other answer a stall, the cursor moved only forward | `forward`, `_send`, `_authorization`, `ForwardOutcome` |
   | `lakehouse_core::timers` | a fixed timer: the window on the store's clock in Python's float arithmetic, whether a claim is another replica's, the claim record as `json.dumps` writes it, a turn, the loop | `lakehouse.timers.Timer`, `_Claim`, `claim_key` |
   | `lakehouse_core::storage::records` | a read answering the record, its ETag and the store's `Date`, a 403 told apart; a put only if absent (`If-None-Match: *`) or still the ETag read (`If-Match`) | `records.read_json_at`, `create_json`, `replace_json`, `store_clock`, `timers._denied_at` |

2. **Each component is constructed from its dependencies, and nothing reads the process.** The service's main calls:
   - `Settings::from_environment(std::env::vars())`, then `settings.lane()`;
   - `Gate::new(signing, keys, fga_enabled, decider, catalog, root, store)`;
   - `IngestLane::new(store, gate, tally)` and `Arc<IngestLane>::run(&bus, &lane, stop)`;
   - `Relay::new(settings, store, gate, bus, s3, secrets, tally)`, then `relay.run()` (returns at once with no period), or
     `relay.tick()` for one tick now.
3. **The model's order is kept by construction.** The lane settles through `lakehouse_core::delivery::judged` (Deliver,
   Again, Park), judges through `intake::handle` (Judge), and records through the store's group commit (runs, edges, then
   the feed row). The tick drains through `relay::drain_one`, whose effects the `Relay` implements: record, publish again,
   drop; keep a refusal, then drop. Both pass the one `Gate`, so neither admits what the other refuses.
4. **The timer is the store's.** A window is named by the store's clock (its `Date`), never the pod's, in Python's
   float arithmetic (`a // b`, rounding to the microsecond half to even), so a Python and a Rust replica name the same
   window and either reads the other's claim. The claim lands by the store's conditional write. A read the store refuses
   as forbidden is taken as no claim, and the conditional create is the store's answer, since S3 answers a missing key so
   to an identity that may not list the bucket. RustFS 1.0.3 answers that read 404 under the relay's policy (get and put
   on its one key, no listing; measured 2026-11-17 against the check's store), so the forbidden branch is shown by the
   vectors alone.

## How parity was shown

- **Vectors through the real Python** (`scripts/_port_lanes_vectors.py` → `rust/vectors/lanes.json`, 152 cases in five
  groups; two runs write the same file): 77 environments read by the real `LineageSettings` (the chart's values, then one
  variable at a time: pydantic's lax forms of whole numbers, flags and seconds, every bound, a JSON list that is not one,
  a relay with no root, a doubled store identity; 34 refused, each by the variable it names); 25 turns of the real
  `Timer.turn` with the read and the writes answered by stand-ins (absent, held this window, the last, the next, one
  microsecond past a period ahead, in another offset, malformed, naive, denied, a read that fails; a write that lands,
  loses or fails; work that fails; held), each with its outcome, window, wait and the record it wrote; 15 ticks of the real
  `relay.tick` (an outbox of every kind of staged event, a bound on the drain, a listing that fails, a retire, a forward
  and a prune that each succeed or fail, retention on and off), each the effects in order, the counts and the report; 19
  forwards of the real `forward` over a stand-in feed and backend (a cursor behind, at, past and far past the feed's head,
  a pruned gap, a batch, refusals, a stall, a transport failure, a key read that fails, pages past 32), each the calls in
  order and the outcome; and 16 parks of the real `IngestLane.park` (ids that are no plain text, no id, data that is no
  event, an envelope already recorded, a keep that fails, a body that is no envelope), each the feed row asked about and
  the row kept. `rust/crates/lineage/tests/lanes.rs` asserts the same answers.
- **Against real collaborators** (`rust/crates/lineage/tests/lanes_live.rs`, run by `scripts/port_lanes_check.sh`): the
  pinned nats-server with the stream and consumer the chart declares, the real Python catalog on real pylance, the pinned
  RustFS with the relay's and a producer's policies provisioned by the hook's own tool, the real Python producer and lane
  (`scripts/port_lanes_fixture.py`). Nine tests:
  - the Rust lane records an event the real `publish_with_outbox` published, and its feed row, edge and run binding equal
    those the real Python lane writes for the same envelope, column for column;
  - a writer with no grant is parked under its CloudEvent id and counted, and nothing is recorded;
  - a catalog that fails the feed's write (a proxy answering 503) leaves no feed row while the earlier writes are there,
    the delivery is asked for again, and the next one lands the row once;
  - an event the real Python producer staged with the bus away is drained by the Rust tick, published again, dropped, and
    the copy that reaches the lane lands no second row;
  - two replicas' turns in one window run the work once, in two windows running, on RustFS's conditional create and
    `If-Match` replace under the relay's policy;
  - the forwarder sends every recorded event to a local backend in order, a restarted relay sends nothing its cursor
    passed, a 503 stalls the tick and the next sends from the cursor;
  - and the ways `EventPath.tla` lets an event go, each checked for NoLoss and AFeedRowIsWhole on the real tables:
    recorded by the lane (the first test), refused and parked (the second), the lane failing then the relay recording,
    the relay refusing (kept in `refusals`, dropped, OnlyAllowedEventsAreRecorded), and the lane and the relay racing (one
    feed row, nothing staged, no refusal).
- **Nine planted defects each failed a test**: a claim up to a period ahead of the store's time taken over (turn vectors);
  a denied read whose create lost taken as another's window (turn vectors); the prune moved before the forward (tick
  vectors); an unkept refusal counted as kept (tick vectors); a 401 passed as a refusal of one event (forward vectors); an
  ack wait of exactly the settle margin taken (settings vectors); a park keyed by its feed row before its CloudEvent id
  (park vectors); a lane that takes every delivery whatever its verdict (the failing-store test); and a relay that
  publishes nothing again (the bus-away test).

## What differs from the Python, on purpose

Each is outside the corpus:

- **Lineage's own settings only.** The authentication and authorization settings are `lakehouse_core::auth::settings`',
  the bus's `BusSettings`, the store identity's `StoreSettings`; each is read where it is defined. A store identity is
  required at start where the outbox or the timer root is on the store (Python fails at the first use), an outbox or root
  that is not on the store is refused (ADR 0234), and a number of seconds that is infinite is refused (Python takes
  `inf`). Pydantic's lax forms outside the corpus (`5.`, digits of other scripts) are not promised.
- **Counts are the OpenTelemetry metrics the Python records** (ADR 0238): the processed counter by door and outcome,
  the timer's turns, last claim and period, the outbox's backlog and drained, stranded and refused counts, and the
  forwarder's. The timer's `elsewhere` hook, which only forgot the backlog gauge, is not ported.
- **One listing a tick.** Python lists the outbox for its backlog and again for the events; the Rust lists once and logs
  the backlog from that listing.
- **The key id that paces reads of published keys** is read from the arrived event's facet without validating the rest of
  the claim; Python's `signature_of` answers none for a malformed claim. Only the pacing of a key id met unknown differs; a
  malformed claim is refused by the verifier either way.
- **A refusal the store's commit raises** (another subject's run) is kept with the Python store's text, since the Rust
  store answers that refusal without one.
- **The forwarder's client follows no redirect and uses no proxy**, so a backend's key reaches only the address git
  names; Python's `httpx` client takes proxies from the environment. A redirect is a stall in both.
- **A claim whose window is a JSON number** (pydantic reads one as Unix seconds) is unreadable here; every timer writes
  ISO 8601. The holder is `$HOSTNAME`, else `/etc/hostname`, where Python asks `gethostname()`.
- **An envelope holding a lone surrogate escape** cannot be read by `serde_json`, so it is taken unrecorded and its park
  keeps nothing (ADR 0224 reads envelopes so).

## The four facts

- **GitOps:** every value is the chart's (`services.yaml` renders each `LINEAGE_*`), the relay's period and root come
  from git, and the timer's claim is the only state it writes, a record the next window replaces.
- **Cloud native:** every replica runs the lane and the timer, and the bus and the store, not a leader, decide which
  replica handles a delivery and which runs a window; a replica that dies leaves nothing another must clean up.
- **Lance-namespace/table-idiomatic:** the feed, edges, runs, refusals and cursors are reached only through the catalog's
  doors (`lakehouse-tables`); the lineage crate links no Lance. A window is claimed by the store's conditional write, the
  primitive a Lance commit stands on.
- **Zero trust:** the store is reached only as the pod's own token exchanged at its STS (ADR 0234), the bus as its own
  token (ADR 0225), the backend's key read from the secret store at each tick and sent only as a sensitive header to the
  address git names, with no proxy and no redirect; a signature is verified over the bytes that arrived, never a model.
  No secret is read from the environment or written to a log.

## Not ported yet

- The wiring in the service's main, its readiness of the lane and the relay, and the admin's replay of a parked delivery
  (another feature, the doors).
- The metrics named above.
- A local (`file://`) outbox or control root.

## Consequences

- The lineage crate holds 826 counted lines of this feature (settings 180, gate 87, lane 106, forward 113, tick 340),
  and `lakehouse-core` 282 (timers 175, storage records 107).
- `lakehouse_core::storage::s3`'s request is split so a caller in `storage` can read a response whatever its status
  (`signed`), which the records' read needs for the store's clock on a 404 and a 403.
- A change to the settings, the timer's arithmetic or claim, the tick's order or report, the forwarder's cursor or the
  park's keeping changes a vector, and `make rust-check` fails until the Rust agrees; a change to the order of the
  lane's or the relay's steps changes `EventPath.tla` first.
