# 0221. Lineage's event path is modelled in TLA+ and checked before it is ported (2026-11-17)

**Status:** accepted; the first model of the port (ADR 0217 §5). Source: the owner, 2026-11-17: "Have you modeled
anything using the tools yet?", then "Why not start there?".

## Context

The first three features of the port (ADR 0218 to 0220) decide things from one value at one moment, and a model
checker has nothing to explore in them. Everything left in lineage is ordering: an event reaches it by the bus and
by the outbox, each step can fail, a replica can die between two writes, and a write that was given up on can still
land. The Python argues this ordering in comments. A port that reorders two steps would pass every vector.

## Decision

1. **A model is a TLA+ module under `specs/<area>/`, checked by TLC in every interleaving its config bounds**
   (`make tla-check`, `scripts/tla_check.sh`). TLC is fetched once and held to its digest
   (`scripts/toolchain.sha256`, tla2tools 1.7.4). A model is written from the code it describes, names the functions
   it models, and states what it assumes and where each assumption is held.
2. **A model is written and holds before the code it describes is ported.** The Rust is then written to the model's
   steps, and a change to the order of two effects changes the model first.
3. **`specs/lineage/EventPath.tla` is lineage's event path:** the producer's stage, publish and drop
   (`lakehouse.outbox.publish_with_outbox`); a replica's handling of a delivery with its attempts and its park
   (`lakehouse.bus_delivery.judged`, `lineage.services.consumer.handle_cloud_event`, `IngestLane.park`); the three
   writes of a record in their order (`ProvenanceStore._commit`); and the relay's drain of the outbox
   (`lineage.api.relay._drain_outbox`). Any step may fail, any process may die between steps, and a write may land
   late.
4. **What it proves, for one event of every kind with up to three failures (55,308 states) and for two interleaved
   events with one (738,589 states, the two counted as interchangeable), both in the gate, about 16 seconds together.
   Two events with two failures were checked once: 13.8 million states, four minutes, no error.**
   - *No loss:* an event its producer staged or published is always on its way, recorded, or kept where an admin
     finds it.
   - *Only allowed events are recorded*, and the relay keeps as refused only what was refused.
   - *A feed row is whole:* the run's binding and the edges are written before it.
   - *Everything comes to rest:* once failures stop, nothing is left staged, undelivered or half written.
5. **An assumption the design rests on is a config that must fail.** A config whose first line says
   `EXPECT VIOLATION <Invariant>` removes one assumption, and the gate fails unless the model breaks under it:
   - `EventPathBusLimit.cfg`: a bus that gives a delivery up after a number of attempts loses an event. The consumer
     `lineage-ingest` is declared with `max_deliver -1` (`chart/templates/_helpers.tpl`), and must stay so.
   - `EventPathBusForgets.cfg`: a bus that drops a message nobody settled loses an event. The stream drops a message
     older than `nats.streamMaxAgeSeconds` (7 days), so lineage away for longer than that loses what was published
     meanwhile and already dropped from the outbox.
6. **The model was shown to bite.** Six planted defects were each reported: parking that keeps nothing, a relay that
   drops without recording, and a lane that acks a delivery it could not record (no loss); the feed row written first
   (a feed row is whole); a refused event recorded (only allowed events); a relay that never drops what it recorded
   (everything comes to rest).

## What the model assumes

- **A1.** A write of a row is insert-if-absent and atomic, so a table is a set. Held by the catalog's primary-key
  enforcement (`catalog.services.primary_keys`), which is not modelled here.
- **A2, A4.** The two bus assumptions of point 5.
- **A3.** The bus hands a delivery to no second replica while the first still handles it: a handling is cut 10 s short
  of the ack wait (`Lane.SETTLE_SECONDS`).

## Not modelled

Who may record an event beyond "allowed or refused" (the run's author, OpenFGA), the admin's replay doors, the
forwarder and its cursor, pruning, and the catalog's side before an event is staged (whether every committed write
is announced). The table lifecycle (create, drop, undrop, destroy) is the catalog's and gets a model of its own
before the catalog is ported.

## Consequences

- The bus intake, the store's record and the relay are ported to this model's steps, and their tests replay its
  cases against the running service.
- `make tla-check` needs a Java runtime. It is not wired into CI.
- The 7-day bound of A4 is a property of the estate the model made visible, not a defect it found: it is the stream's
  declared age.
