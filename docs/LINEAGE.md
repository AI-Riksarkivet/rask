# Lineage service: provenance as a service

The **provenance axis** from [`ARCHITECTURE.md`](ARCHITECTURE.md) §9. A separate microservice (`services/lineage/`)
that takes OpenLineage events from the catalog (every write it commits), from maintenance and from users' pipelines,
records each one in a durable commit feed and a table-level edge list, and forwards every recorded event to a graph
backend the operator chooses ([ADR 0158](adr/0158-provenance-is-a-service-the-lineage-graph-is-bring-your-own.md)). It is not part of the
catalog control plane, and it keeps no database.

```
catalog, maintenance ─lineage.events.v1─▶ lineage ──merge_insert / query──▶ catalog ──▶ provenance$events
a user's pipeline ──POST /api/v1/lineage─▶   │                                         provenance$edges
pipelines, notifications ◀── GET /events ────┤                                         provenance$refusals
erasure ◀── GET /tables/{name}/derived ──────┤                                         provenance$cursors
                                             └─ relay tick ─ OpenLineage HTTP ─▶ graph backend (Marquez, DataHub, …)
```

## How it complements OpenFGA and Lance

| Question | System |
|---|---|
| **who** may read/write/commit | OpenFGA (`can_*` on `table:<uuid>`, the uuid each event's `lance` facet names) |
| **what / when** changed | Lance versions + tags (immutable, time-travel) |
| **how / from where / by whom** | this service: the commit feed and the edge list; the full graph in the forwarded backend |

A governed table is recorded under the uuid the catalog minted for it, with its bare catalog id (e.g. `bronze$images`)
beside it as the name its event used, so the same object is governed by OpenFGA (`table:<uuid>`), versioned by Lance and
traced here, and a rename does not split its lineage (ADR 0173 §10). An external source (anything outside the governed tiers, R23) is recorded
namespace-qualified, `<namespace>/<name>`, so an external input can never resolve onto a governed table's edges
(`Dataset.lineage_name` in `lineage/models.py`).

## Where the records live

Lineage holds no storage credential for its tables. They are Lance tables in the catalog's **provenance namespace**
(chart value `services.lineage.provenanceNamespace`, default `provenance`; `LANCE_PROVENANCE_NAMESPACE` on the catalog,
`LINEAGE_PROVENANCE_NAMESPACE` on lineage, and the two must agree):

| Table | Holds |
|---|---|
| `provenance$events` | the commit feed: one row per admitted event, the full event JSON in `event` |
| `provenance$edges` | one row per (input, output) pair a successful run asserts, with both versions and branches |
| `provenance$refusals` | outbox events refused by governance, recorded before the staged object is deleted |
| `provenance$cursors` | each forwarder's position in the feed, keyed by the endpoint URL it posts to |

Lineage writes them through the catalog's `merge_insert` door and reads them through its `query` door, as its own
principal (its projected service-account token for audience `rask-catalog`, `RASK_CATALOG_IDENTITY_TOKEN_FILE`). So
every write is authorized, tier-judged and audited like any other writer's. The catalog creates the namespace at boot
(`catalog/services/provenance_namespace.py`, retried in the background until it holds), protects it against deletion,
grants each `LANCE_PROVENANCE_WRITERS` subject `writer` on it, and never announces a write under it on lineage: every
row records an announcement, so announcing that row's own commit would record it again without end.

**A row is written once.** Each table declares an unenforced primary key and is written with an insert-only merge on
it, so a redelivered event lands no second row and two racing deliveries of one event land it once. The feed's key is
one row per terminal state of a run, one per arrival of a non-terminal state, and one per static change.

**A burst is one commit per table.** Every commit is a Lance version and a fragment that each later open
and scan pays for (ADR 0174), so the events recorded while a commit is in flight land together in the next one: one
writer per process, started by a burst's first event, with no timer. The commit keeps `record`'s order for the whole
batch (the run bindings, then the edges, then the feed rows) and sends a key the batch repeats once, its first
delivery's row, so a run is bound to its first event's author. Where the doors authorize, an event of a run another
author bound first is refused with none of its rows written: the doors' rule, asked again at the commit because events
delivered together were each authorized before any of them bound its run. A batch whose rows the catalog refuses (400,
413, 422) lands in halves, so only the event it refuses fails its delivery; any other failure (a 429 at its write cap, a
5xx, no answer) fails the batch whole, and each delivery retries. Lineage's bus subscription delivers in parallel
(`concurrencyMode: parallel`; the JetStream component's default is one at a time), at most
`services.lineage.deliveriesInFlight` unacknowledged at once (its durable's `maxAckPending`), so a backlog's events arrive
together and an event the sidecar is retrying holds no other back.

**The feed's sequence is Lance's own.** The tables use stable row ids, which Lance assigns in commit order
(`lance_docs/lance/format/table/row_id_lineage.md`, "Row ID Assignment"), and `seq` is `_rowid + 1`. A follower reading `seq > since`
therefore never misses a row that commits after it read. The contract is [`COMMIT-EVENTS.md`](COMMIT-EVENTS.md).

**What an edge asserts.** Only a successful run (`COMPLETE`) asserts edges: every input to every output it wrote, at
the versions the `version` dataset facet names. A failed run (`FAIL`/`ABORT`) produced no data and asserts none, though
its events stay in the feed with their `errorMessage`. A static change (`DatasetEvent`) reads nothing and asserts none.
An in-place refinement (a run that reads and writes one table) bumps that table's version and is no edge
(`edges_of` in `lineage/services/provenance.py`).

## The doors

| Method / path | Purpose |
|---|---|
| `POST /api/v1/lineage` | Authenticated OpenLineage intake (the HTTP transport's default path): a `RunEvent`, or a `DatasetEvent` for a change no job performed |
| `GET /events` | The durable commit feed, governed per dataset: newest first by `after`, or forward from `since` with `next_cursor`; filterable by `table` or `namespace`; reports `oldest_seq` |
| `GET /events/projection` | The same feed without the per-dataset filter, cut to the fields that decide whom to notify; gated on `can_read_event_feed`, for the notifications reconciler |
| `GET /tables/{name}/derived?depth=` | Every table derived from `name`, nearest first |
| `GET /admin/dlq`, `POST /admin/dlq/{run_id}/replay` | The dead-letter queue: list parked deliveries, replay one |
| `POST /lineage-events` | Dapr subscription on `lineage.events.v1` (sidecar only) |
| `POST /lineage-relay-cron` | Dapr cron binding that drives the relay tick (sidecar only) |
| `GET /livez` | Liveness |

**Intake.** The author is stamped from the verified bearer token, overwriting any `author` facet the body carries, and
only a run's author may extend an existing run. A producer may record writing a governed output only where it holds
`can_write_data`, and reading a governed input only where it holds `can_get_metadata`; an external input is exempt,
since no tuple could ever exist for it. One event names at most 64 inputs and 64 outputs and weighs at most 1 MiB as
JSON, else 422. Every door (HTTP, bus, outbox drain, DLQ replay) parses through the same models, so all of them refuse
alike. This door records and triggers nothing: it publishes nothing on `lineage.events.v1`, so a write that should be
announced goes through the catalog's data-plane doors, whose emitter announces it.

**Derived tables.** `GET /tables/{name}/derived` resolves `name` to the uuid of the table it names now and walks the
edge list transitively from that uuid, and from the name for an outside producer's event that carried none (a table
derived from any version of it, under any name it has had, counts), at most `depth` hops (default and cap 20). It is gated on `can_get_metadata` on the source;
a derived table the caller cannot see is dropped from the answer while the walk still passes through it, and
`complete` is `false` when the walk stopped at the depth cap with tables left to follow. This is how an erasure's
requester finds every table built from a subject's source. The catalog's erasure verification walks the same edge list
in-process with the same walk (`service_kit.lakehouse.derived_tables`), to its last table, and holds the obligation open
until each derived table holds an obligation following it (ADR 0178).

**Authz.** Two layers, the same the catalog applies to `describe table`: OIDC (a valid bearer verified against Dex,
else 401) and OpenFGA (`can_get_metadata` on `table:<uuid>`, else 403; feed rows naming a dataset the caller cannot see
are dropped). Lineage reads the shared OpenFGA store and writes no tuple. Both layers are off in the code defaults and
on in the chart (`RASK_OIDC_ENABLED`, `RASK_FGA_ENABLED`). The two Dapr-delivered routes verify the Dapr app token,
which only the sidecar stamps: the cluster route publishes `/api/lineage` onto lineage's root, but a request through
the Gateway reaches the app port directly and never carries that token, so the routes refuse it. The service refuses
to boot if either would mount without the token.

## The relay tick

A Dapr `bindings.cron` component (chart `services.lineage.relay.{enabled,bindingName,schedule}`, binding
`lineage-relay-cron`, `@every 30s`) POSTs to the relay route on every replica's sidecar. Every step is idempotent, so
overlapping ticks need no lock (`lineage/api/relay_cron.py`):

1. **Drain the outbox.** Producers stage each event under `_lineage_outbox` before publishing it; the tick records
   each survivor, re-publishes it and deletes it, oldest first and bounded per tick. An event governance refuses is
   recorded to `provenance$refusals` before its staged object is deleted. Lineage's object-store grant is only this
   prefix (list, get, delete).
2. **Prune the feed** past `services.lineage.eventsRetentionDays` (default 7, by arrival time; 0 keeps everything).
3. **Forward.** Every recorded event, as standard OpenLineage over the HTTP transport, is POSTed to
   `services.lineage.forward.url` + `endpoint` (default `api/v1/lineage`), at least once and in feed order, from a
   cursor in `provenance$cursors` keyed by the endpoint URL. The cursor moves by a conditional merge
   (`source.seq > target.seq`), so a slower replica's tick never moves it back. A 400, 413 or 422 refuses that one
   event, which is counted and passed; any other non-2xx answer or a transport error stops the tick at the last event
   the backend took, and the next tick resumes there. `forward.authSecret.{name,key}` names an API key in the Dapr
   secret store, read on every tick so a rotated key needs no restart, and denied to every other app's sidecar. An
   empty URL forwards nothing.

The full lineage graph (upstream and downstream walks, run history, a DAG view, column lineage) is the forwarded
backend's. Development runs Marquez from `deploy/fixtures/marquez` (Service `rask-marquez:5000`), which
`deploy/values-local.yaml` points the forwarder at.

## OpenLineage facets

The catalog and maintenance build each `RunEvent` by hand and keep it spec-true through one shared helper
(`service_kit.openlineage`): a UUID `runId`, the top-level `schemaURL`, and `_producer`/`_schemaURL` on every facet,
so a forwarded backend ingests them unchanged. Intake tolerates every facet, stores the whole event in the feed's
`event` column, and reads these:

| Facet | Kind | Read for |
|---|---|---|
| `author` (custom), `ownership` | run / job | the feed's `author` column, stamped from the verified token at the HTTP door |
| `lance` (custom) | run / dataset | the catalog operation, the run's write ref, the ref an input was read on |
| `version` | dataset | the Lance version on each side of an edge |
| `errorMessage` | run | carried in the feed for a failed run |
| `rask_signature` (custom) | run / dataset | the signature a bus or outbox event must carry (below) |

Every other facet (`schema`, `outputStatistics`, `dataQualityAssertions`, `dataSource`, `tags`, `jobType`,
`sourceCodeLocation`, `columnLineage`) rides the feed verbatim and reaches the graph backend in the forwarded event.

## Signed events (the `rask_signature` facet)

A bus door authenticates the sidecar that delivered an event, not the producer that wrote it, so the author an event
stamps is a claim until a signature proves it. Every producer that publishes to `lineage.events.v1` or stages an event
in the outbox signs it with its own Ed25519 key. The signature rides inside the event, in the `rask_signature` facet of
`run.facets` (or `dataset.facets` for a catalog change), over the canon-1 bytes of the whole event. The facet names the
signer (`identity`), its key (`kid`) and, for a delegation, `onBehalfOf`. Lineage holds public keys only, so it can admit
or refuse an event and cannot forge one (`packages/lineage-kit/src/lineage_kit/signing.py` owns the format).

- **Who signs.** The identities the chart lists in `LINEAGE_SIGNERS` (`lance.signingIdentities`): the catalog and
  maintenance. A signer is its own author. The one exception is
  the catalog, the sole entry in `LINEAGE_DELEGATORS`, which signs for the person it authenticated.
- **Control events.** A `CatalogControlEvent` on `catalog.control.v1` is a flat envelope with no facet bag, so the same
  facet rides as its top-level `rask_signature` member, and the signed bytes are canon-1 of the whole envelope with only
  `rask_signature.signature` removed. The envelope's `actor` plays the author: a `user:<sub>` actor verifies only under a
  delegation (`onBehalfOf` equal to the whole actor) declared by a delegator, and an absent or non-`user:` actor is a
  service acting for itself, signed with no delegation. The emitting service signs at emit, before the event is staged
  (`service_kit.control_emit`, its signer injected). Who signs which action is
  `service_kit.control_events.control_signer_role`: the catalog signs every action it emits, for the person it
  authenticated; maintenance signs `table_purged` and `namespace_purged` as itself. No action is exempt: the lane carries only the
  lakehouse's own signed events, and a client tool publishes nothing on it (ADR 0171). A signer whose key
  is unresolved publishes and stages nothing unsigned: it withholds the event, counts it under
  `<service>_control_emit_failed_total{lance_<service>_reason="unsigned"}` and reports itself not ready, so a control
  event emitted in that window is lost, not delayed. The catalog's control relay signs nothing: it republishes a staged
  event verbatim only when it verifies as signed by the catalog's own identity against the keys that identity
  publishes, and retires anything else under the control outbox unpublished (`outbox_events_poison_dropped_total`, WARN
  `control_outbox_unverifiable`), so an object another credential stages there never becomes a catalog-signed event.
- **What lineage accepts, and from where.** With `signing.enforce` on (the default, rendered only where auth and the
  secret store are on) lineage records an event only when a listed signer's published key verifies it. All four doors
  that record a bus or staged event apply the rule: the `/lineage-events` subscription, the outbox drain, the operator's
  DLQ replay and the parking route's re-ingest. The authenticated HTTP door (`POST /api/v1/lineage`: a user's pipeline, the web
  zones, any OpenLineage client) takes no signature; it authenticates the caller's bearer token and strips any
  `rask_signature` the body carries. With `signing.enforce` off lineage verifies nothing and ignores a present facet.
- **What is refused.** An event with no facet (`unsigned`), a malformed facet or an unsupported `alg` or `canon`, an
  identity outside `LINEAGE_SIGNERS` (`signer`), a signer that is not the stamped author and declares no delegation
  (`author`), a delegation from a non-delegator (`delegation`) or for another subject (`author`), a `kid` its signer does
  not publish (`kid`), and bytes that do not verify (`signature`, `uncanonical`, `encoding`). A refusal is final, so
  nothing retries or parks it: the subscription and the parking route acknowledge the delivery, the drain records the
  verdict with the event and retires the staged object, and the operator's replay is refused. The only trace is
  `lineage_signature_refused_total{lance_lineage_reason}` and a `lineage_signature_refused` WARN line naming the run, the
  claimed identity and the reason. A key list that cannot be read is an outage, not a verdict, and the delivery is
  retried. What each reason means and what to do:
  [RUNBOOK-oncall.md](runbooks/RUNBOOK-oncall.md#lineage-refusing-a-signers-events).
- **The doors that act on the bus.** Notifications (`/lineage-events`, `/control-events`), the catalog's own
  `/control-events` (which feeds `GET /management/v1/events` and the warehouse-binding cache) and maintenance's
  `/maintenance-arrival` verify the same signatures before they act, in the mode `RASK_SIGNATURE_DOORS` names (chart
  `signing.doors`: off, observe or enforce). A lineage event verifies against `RASK_EVENT_SIGNERS` and
  `RASK_EVENT_DELEGATORS`, lineage's own sets. A control
  event verifies against the identities `RASK_CONTROL_SIGNER_ROLES` lists for the role its action names. Only an event
  the door would act on is verified. Enforcing, a refusal is acknowledged and drives nothing, counted in
  `<service>_signature_refused_total` by door and reason; every one of those series exists at 0 from the door's
  registration, so a flat 0 is the healthy reading and a single refusal pages
  ([RUNBOOK-oncall.md](runbooks/RUNBOOK-oncall.md#a-bus-door-refusing-events)). A key list that cannot be read is
  retried. Observing, the door acts as before and counts what enforcing would refuse in
  `<service>_signature_would_refuse_total`.
- **The previous-key window.** Each identity publishes `signing-public-<identity>` with its current key first and at
  most one previous key. An event signed just before a rotation verifies while the previous key is listed, and lineage
  reads a list through its own sidecar and caches a successful read for 60 s. Provisioning, rotation and loss of the
  keys: [OPERATORS.md](OPERATORS.md) § 6.

## Code shape

- `lineage/models.py`: Pydantic `RunEvent` / `DatasetEvent` (the OpenLineage wire subset), the per-event bounds and
  dataset identity.
- `lineage/services/provenance.py`: `ProvenanceStore`, the feed, edge list, refusals and cursors over the catalog's doors.
- `lineage/services/forward.py`: the forwarder. `lineage/services/{consumer,staged,signature}.py`: shared intake.
- `lineage/api/v1/endpoints/{ingest,events,tables,dlq}.py`: the HTTP doors; `lineage/api/dapr.py` and
  `lineage/api/relay_cron.py`: the sidecar-only routes; `lineage/api/{security,fga_deps}.py`: authn and authz.
