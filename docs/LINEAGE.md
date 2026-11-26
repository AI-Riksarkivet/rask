# Lineage service: provenance as a service

The **provenance axis** from [`ARCHITECTURE.md`](ARCHITECTURE.md) §9. A separate microservice (`services/lineage/`)
that takes OpenLineage events from the catalog (every write it commits), from maintenance and from users' pipelines,
records each one in a durable commit feed and a table-level edge list, and forwards every recorded event to a graph
backend the operator chooses ([ADR 0158](adr/0158-provenance-is-a-service-the-lineage-graph-is-bring-your-own.md)). It is not part of the
catalog control plane, and it keeps no database.

```
catalog, maintenance ─lineage.events.v1─▶ lineage ──merge_insert / query──▶ catalog ──▶ lance_catalog$provenance$events
a user's pipeline ──POST /api/v1/lineage─▶   │                                         lance_catalog$provenance$edges
pipelines, notifiers ◀── GET /events ────────┤                                         lance_catalog$provenance$refusals
erasure ◀── GET /tables/{name}/derived ──────┤                                         lance_catalog$provenance$cursors
                                             └─ relay tick ─ OpenLineage HTTP ─▶ graph backend (Marquez, DataHub, …)
```

## How it complements OpenFGA and Lance

| Question | System |
|---|---|
| **who** may read/write/commit | OpenFGA (`can_*` on `table:<uuid>`, the uuid each event's `rask_lance` facet names) |
| **what / when** changed | Lance versions + tags (immutable, time-travel) |
| **how / from where / by whom** | this service: the commit feed and the edge list; the full graph in the forwarded backend |

A governed table is recorded under the uuid the catalog minted for it, with its bare catalog id (e.g. `acme$bronze$images`)
beside it as the name its event used, so the same object is governed by OpenFGA (`table:<uuid>`), versioned by Lance and
traced here, and a rename does not split its lineage (ADR 0173 §10). An external source (anything outside the catalog's governed tables, R23) is recorded
namespace-qualified, `<namespace>/<name>`, so an external input can never resolve onto a governed table's edges
(`Dataset.lineage_name` in `lineage/models.py`). The catalog names its datasets under its endpoint,
`lance-namespace://<host>/api/catalog`, with each table's storage location in the `symlinks` facet ([[LH-553]],
`docs/COMMIT-EVENTS.md` § A feed row). A dataset an outside producer names by a location a governed table holds, or lists
in its own `symlinks`, is authorized as that table, because the forwarded graph joins it there
(`lineage.api.fga_deps.resolve_aliases`), and a dataset named by such a location is recorded under the table's uuid in
the feed and the edge list too (`lineage.services.names.stamp_resolved_uuids`). Every table those aliases resolved to
is kept in the feed row's `table_uuids`, which every read of the row is decided on, with its payload or as a summary;
a staged event at `/admin/dlq`, which no door has scoped yet, has its aliases resolved as the caller when it is listed
or replayed (`DatasetFilter.scope_unrecorded`). A dataset under another catalog's
`lance-namespace://` host is another estate's table, an outside source here (`LINEAGE_CATALOG_DATASET_NAMESPACE`).

## Where the records live

Lineage holds no storage credential for its tables. They are Lance tables in the catalog's **provenance namespace**
(chart value `services.lineage.provenanceNamespace`, default `lance_catalog$provenance`, a namespace of the estate's own
warehouse, which the render and the catalog's boot both require; `LANCE_PROVENANCE_NAMESPACE` on the catalog,
`LINEAGE_PROVENANCE_NAMESPACE` on lineage, and the two must agree):

| Table | Holds |
|---|---|
| `lance_catalog$provenance$events` | the commit feed: one row per admitted event, the full event JSON in `event` |
| `lance_catalog$provenance$edges` | one row per (input, output) pair a successful run asserts, with both versions and branches |
| `lance_catalog$provenance$refusals` | outbox events refused by governance, recorded before the staged object is deleted |
| `lance_catalog$provenance$cursors` | each forwarder's position in the feed, keyed by the endpoint URL it posts to and the uuid of the feed table the position was taken on, so a feed created again is forwarded from its first row |

Lineage writes them through the catalog's `merge_insert` door and reads them through its `query` door, as its own
principal (its projected service-account token for audience `rask-catalog`, `RASK_CATALOG_IDENTITY_TOKEN_FILE`). So
every write is authorized, tier-judged and audited like any other writer's. The catalog creates the namespace at boot
(`catalog/services/provenance_namespace.py`, retried in the background until it holds), protects it against deletion,
grants each `LANCE_PROVENANCE_WRITERS` subject `writer` on it, and never announces a write under it on lineage: every
row records an announcement, so announcing that row's own commit would record it again without end.

**A row is written once.** Each table declares an unenforced primary key and is written with an insert-only merge on
it, so a redelivered event lands no second row and two racing deliveries of one event land it once. The feed's key is
one row per terminal state of a run, one per arrival of a non-terminal state, and one per static change and who vouches
for it. A static change has no run to bind to an author, and its identity is derived from fields a writer of the table
can read (`docs/COMMIT-EVENTS.md` § An event's identity), so a signed change (the catalog's, recorded off the bus or the
outbox) keeps the bare key and a change posted to the HTTP door is keyed under its verified author too: no poster can
take the row the catalog's announcement of a change needs.

**A burst is one commit per table.** Every commit is a Lance version and a fragment that each later open
and scan pays for (ADR 0174), so the events recorded while a commit is in flight land together in the next one: one
writer per process, started by a burst's first event, with no timer. The commit keeps `record`'s order for the whole
batch (the run bindings, then the edges, then the feed rows) and sends a key the batch repeats once, its first
delivery's row, so a run is bound to its first event's author. Where the doors authorize, an event of a run another
author bound first is refused with none of its rows written: the doors' rule, asked again at the commit because events
delivered together were each authorized before any of them bound its run. A batch whose rows the catalog refuses (400,
413, 422) lands in halves, so only the event it refuses fails its delivery; any other failure (a 429 at its write cap, a
5xx, no answer) fails the batch whole, and each delivery is asked for again. Lineage pulls its durable consumer
`lineage-ingest` and handles its deliveries in parallel, at most `services.lineage.deliveriesInFlight` (64)
unacknowledged at once (the consumer's `max_ack_pending`, and each replica's bound), so a backlog's events arrive
together. An event waiting to be delivered again still counts against that bound (measured on nats-server 2.15.2,
2026-11-14): one holds no other back, and as many as the bound hold back every event behind them until one is taken or
parked.

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
| `GET /events/projection` | The same feed without the per-dataset filter, cut to the fields that decide whom to notify, with the `rask_signature` lineage verified when it recorded each event, which tells the catalog's change from one a writer posted under the same identity; gated on `can_read_event_feed`, for a notifier holding `event_reader` (granted at the estate admin's tuple door) |
| `GET /tables/{name}/derived?depth=` | Every table derived from `name`, nearest first |
| `GET /admin/dlq`, `POST /admin/dlq/{run_id}/replay` | The outbox's at-risk events: list them, replay one. The list is paged: a request covers the next `limit` staged events after `after`, shows those the caller may see, and answers `next_cursor`, the opaque `after` of the next page, until nothing is staged past it. The outbox is read oldest first, events staged at one time in the order of a digest of their ids, and a page resumes strictly after the cursor's time and digest, so a caller following the cursor is shown each event it may see that stayed staged throughout the walk exactly once, whatever is delivered or staged meanwhile. The cursor holds that time and digest and no event's id |
| `GET /admin/dlq/parked`, `POST /admin/dlq/parked/{run_id}/replay` | The events parked in the refusals table, a bus delivery by the ingest lane or a staged event by the outbox drain: list them by run, re-present a run's (the estate admin's) |
| `GET /livez` | Liveness |

**Intake.** The author is stamped from the verified bearer token, overwriting any `rask_author` facet the body carries, and
only a run's author may extend an existing run. A producer may record writing a governed output only where it holds
`can_write_data`, and reading a governed input only where it holds `can_read_data`; an external input is exempt,
since no tuple could ever exist for it. An event that names no governed dataset (a run's START with no inputs or
outputs, say) names no object a rung could be asked on, so it is recorded for any authenticated caller, bound to that
caller as its run's author, and its feed row is shown only to a holder of the estate's feed rung
(`can_read_event_feed`, at `/events/projection`): `/events` hides a row that names no table. On the bus doors an announcement the catalog signed for the caller it
authenticated is recorded on that signature instead, with neither rung re-derived: the catalog checked both before it
committed the write, so a grant revoked since takes nothing back (LH-551). The catalog's re-announcement of a commit
whose own announcement was lost is signed only for the author the catalog recorded authorizing that commit (LH-550),
so it is recorded on the signature too. A dataset's other identifiers, its `symlinks` entries and a
`lifecycleStateChange`'s `previousIdentifier`, are decided as the table each names, on the rung the dataset's own slot
needs: a governed name the caller may not see, or that no table holds any longer, is refused 403, so only the catalog's
signed announcement of a rename names the old name. One event names at most 64 inputs and 64 outputs and
weighs at most 1 MiB as JSON, else 422. Every door (HTTP, bus, outbox drain, DLQ replay) parses through the same models, so all of them refuse
alike, and one event names at most 1,000 distinct identifiers across its datasets, their other identifiers and their
`columnLineage` upstreams, what one question to the catalog's decision door holds. The caller is bound as the author
before any name is resolved, and where the catalog decides (`RASK_FGA_DECIDER_URL`), the event's names are resolved in
one decision question as the caller, so a caller who may see none of them costs the catalog that one question, never a
describe per name. This door records and triggers nothing: it publishes nothing on `lineage.events.v1`, so a write that should be
announced goes through the catalog's data-plane doors, whose emitter announces it.

**Derived tables.** `GET /tables/{name}/derived` resolves `name` to the uuid of the table it names now and walks the
edge list transitively from that uuid, and from the name for an outside producer's event that carried none (a table
derived from any version of it, under any name it has had, counts), at most `depth` hops (default and cap 20). It is gated on `can_get_metadata` on the source;
a derived table the caller cannot see is dropped from the answer while the walk still passes through it, and
`complete` is `false` when the walk stopped at the depth cap with tables left to follow. This is how an erasure's
requester finds every table built from a subject's source. The catalog's erasure verification walks the same edge list
in-process with the same walk (`lakehouse.derived_tables`), to its last table, and holds the obligation open
until each derived table holds an obligation following it (ADR 0178).

**Authz.** Two layers, the same the catalog applies to `describe table`: OIDC (a valid bearer verified against Dex,
else 401) and OpenFGA (`can_get_metadata` on `table:<uuid>`, else 403; feed rows naming a dataset the caller cannot see
are dropped). Lineage reads the shared OpenFGA store and writes no tuple. Both layers are off in the code defaults and
on in the chart (`RASK_OIDC_ENABLED`, `RASK_FGA_ENABLED`). The relay tick has no route: it runs on a timer inside the
process. The bus transport has no route either: lineage pulls its consumer on its own
connection to the bus, as its own bus user.

## The relay tick

Lineage's own fixed timer runs the tick (chart `services.lineage.relay.{enabled,periodSeconds}`, timer
`lineage-relay`, every 30 s; `lineage/api/relay.py`, on `lakehouse.timers`). Every replica runs the timer's loop, and
once a window each reads the claim record `_timers/lineage-relay.json` under the control root (`LINEAGE_TIMER_ROOT`)
and tries one conditional write that claims the window; the store lets one land, and that replica runs the tick, so it
runs once a window whatever the replica count. The timer needs only the object store and the catalog, never the bus:
the relay is what records events while the bus is away. Through a read-only window the timer is held, and the first
window after it does the work. Every step is idempotent besides, so a tick that outlasts its window and overlaps the
next needs no lock:

1. **Drain the outbox.** Producers stage each event under `_lineage_outbox` before publishing it; the tick records
   each survivor, re-publishes it and deletes it, oldest first and bounded per tick. An event governance refuses is
   recorded to `lance_catalog$provenance$refusals` before its staged object is deleted. Lineage's object-store grant is this
   prefix (list, get, delete) and its timer's claim record (get, put), nothing else.
2. **Forward.** Every recorded event, as standard OpenLineage over the HTTP transport, is POSTed to
   `services.lineage.forward.url` + `endpoint` (default `api/v1/lineage`), at least once and in feed order, from a
   cursor in `lance_catalog$provenance$cursors` keyed by the endpoint URL. The cursor moves by a conditional merge
   (`source.seq > target.seq`), so a slower replica's tick never moves it back. A 400, 413 or 422 refuses that one
   event, which is counted and passed; any other non-2xx answer or a transport error stops the tick at the last event
   the backend took, and the next tick resumes there. `forward.authSecret.{name,key}` names an API key in the estate's
   secret store, which lineage reads under its own login on every tick, so a rotated key needs no restart, and which the
   store's policy grants to lineage's role alone. An empty URL forwards nothing.
3. **Prune the feed** past `services.lineage.eventsRetentionDays` (default 7, by arrival time; 0 keeps everything). With a
   forward URL set, no row after the forwarder's cursor is pruned, however old: a backend that cannot take events for
   longer than the retention holds the feed until it does, and each tick it stalls counts on
   `lineage_forward_stalled_total` and logs `lineage_forward_stalled`.

The full lineage graph (upstream and downstream walks, run history, a DAG view, column lineage) is the forwarded
backend's. Development runs Marquez from `deploy/fixtures/marquez` (Service `rask-marquez:5000`), which
`deploy/values-local.yaml` points the forwarder at.

## OpenLineage facets

The catalog and maintenance build each `RunEvent` by hand and keep it spec-true through one shared helper
(`lakehouse.events.openlineage`): a UUID `runId`, the top-level `schemaURL`, and `_producer`/`_schemaURL` on every facet,
so a forwarded backend ingests them unchanged. Intake tolerates every facet, stores the whole event in the feed's
`event` column, and reads these:

| Facet | Kind | Read for |
|---|---|---|
| `rask_author` (custom), `ownership` | run / job | the feed's `author` column, stamped from the verified token at the HTTP door |
| `rask_lance` (custom) | run / dataset | the catalog operation, the run's write ref, the ref an input was read on |
| `version` | dataset | the Lance version on each side of an edge |
| `errorMessage` | run | carried in the feed for a failed run |
| `rask_signature` (custom) | run / dataset | the signature a bus or outbox event must carry (below) |

Every other facet (`schema`, `outputStatistics`, `dataQualityAssertions`, `dataSource`, `tags`, `jobType`,
`sourceCodeLocation`, `columnLineage`) rides the feed verbatim and reaches the graph backend in the forwarded event.

## Signed events (the `rask_signature` facet)

The bus's permission table says which user may publish on a topic, not who wrote a given event, so the author an event
stamps is a claim until a signature proves it. Every producer that publishes to `lineage.events.v1` or stages an event
in the outbox signs it with its own Ed25519 key. The signature rides inside the event, in the `rask_signature` facet of
`run.facets` (or `dataset.facets` for a catalog change), over the canon-1 bytes of the whole event. The facet names the
signer (`identity`), its key (`kid`) and, for a delegation, `onBehalfOf`. Lineage holds public keys only, so it can admit
or refuse an event and cannot forge one (`packages/lakehouse/src/lakehouse/events/signing.py` owns the format).

- **Who signs.** The identities the chart lists in `RASK_EVENT_SIGNERS` (`lance.signingIdentities`): the catalog and
  maintenance. A signer is its own author. The one exception is
  the catalog, the sole entry in `RASK_EVENT_DELEGATORS`, which signs for the person it authenticated.
- **Control events.** A `CatalogControlEvent` on `catalog.control.v1` is a flat envelope with no facet bag, so the same
  facet rides as its top-level `rask_signature` member, and the signed bytes are canon-1 of the whole envelope with only
  `rask_signature.signature` removed. The envelope's `actor` plays the author: a `user:` actor, the OpenFGA user of a
  caller the catalog authenticated, verifies only under a delegation (`onBehalfOf` equal to the whole actor) declared by
  a delegator, and an absent or non-`user:` actor is a service acting for itself, signed with no delegation. The
  emitting service signs at emit, before the event is staged
  (`lakehouse.events.control_emit`, its signer injected). Who signs which action is
  `lakehouse.events.control_events.control_signer_role`: the catalog signs every action it emits, for the person it
  authenticated; maintenance signs `table_purged` and `namespace_purged` as itself. No action is exempt: the lane carries only the
  lakehouse's own signed events, and a client tool publishes nothing on it (ADR 0171). A signer whose key
  is unresolved publishes and stages nothing unsigned: it withholds the event, counts it under
  `<service>_control_emit_failed_total{lance_<service>_reason="unsigned",lance_<service>_staged="false"}` and reports itself not ready, so a control
  event emitted in that window is lost, not delayed. The catalog's control relay signs nothing: it republishes a staged
  event as it was staged, in the envelope derived from it, only when it verifies as signed by the catalog's own identity against the keys that identity
  publishes, and retires anything else under the control outbox unpublished (`outbox_events_poison_dropped_total`, WARN
  `control_outbox_unverifiable`), so an object another credential stages there never becomes a catalog-signed event. An
  event the bus can never carry (past the size the publish seam admits) is retired the same way (WARN
  `control_outbox_undeliverable`), since left staged it would hold every event behind it. A staged object whose delete
  the store refuses stays staged (WARN `control_outbox_left_staged`, `outbox_drop_failed_total` for one already
  republished) and the pass goes on to the events behind it; the next tick publishes it again under the same
  `event_id`, which the stream and every subscriber deduplicate on.
- **What lineage accepts, and from where.** Wherever auth is on (the chart then renders the signer and
  delegator sets, and lineage refuses to start with a secret store and no signer) lineage records an event only when a
  listed signer's published key verifies it. All four doors
  that record a bus or staged event apply the rule: the ingest lane (`lineage.api.ingest_lane`), the outbox drain, the
  operator's replay of an outbox event and the estate admin's replay of a parked delivery. The authenticated HTTP door (`POST /api/v1/lineage`: a user's pipeline, any
  OpenLineage client) takes no signature; it authenticates the caller's bearer token and strips any
  `rask_signature` the body carries. A lineage with authentication off, or with no secret store to read
  published keys from (the offline and Dagger lanes), verifies nothing and admits nothing on a present facet.
- **What is refused.** An event with no facet (`unsigned`), a malformed facet or an unsupported `alg` or `canon`, an
  identity outside `RASK_EVENT_SIGNERS` (`signer`), a signer that is not the stamped author and declares no delegation
  (`author`), a delegation from a non-delegator (`delegation`) or for another subject (`author`), a `kid` its signer does
  not publish (`kid`), and bytes that do not verify (`signature`, `uncanonical`, `encoding`). A refusal is final, so
  nothing retries or parks it: the ingest lane acknowledges the delivery, the drain records the
  verdict with the event and retires the staged object, and the operator's replay is refused. The only trace is
  `lineage_signature_refused_total{lance_lineage_reason}` and a `lineage_signature_refused` WARN line naming the run, the
  claimed identity and the reason. A key list that cannot be read is an outage, not a verdict, and the delivery is
  retried. What each reason means and what to do:
  [RUNBOOK-oncall.md](runbooks/RUNBOOK-oncall.md#lineage-refusing-a-signers-events).
- **The lanes that act on the bus.** The catalog's control lane (which feeds `GET /management/v1/events`) and the maintenance worker's write-event lane verify the same signatures before they act, in
  the mode `RASK_SIGNATURE_DOORS` names (chart `signing.doors`: off, observe or enforce), and a notifier's doors verify
  them with the same settings (`lakehouse.events.signature_doors`). A lineage event verifies against `RASK_EVENT_SIGNERS` and
  `RASK_EVENT_DELEGATORS`, the sets lineage reads under the same names. A control
  event verifies against the identities `RASK_CONTROL_SIGNER_ROLES` lists for the role its action names. Only an event
  the door would act on is verified. Enforcing, a refusal is acknowledged and drives nothing, counted in
  `<service>_signature_refused_total` by door and reason; every one of those series exists at 0 from the door's
  registration, so a flat 0 is the healthy reading and a single refusal pages
  ([RUNBOOK-oncall.md](runbooks/RUNBOOK-oncall.md#a-bus-door-refusing-events)). A key list that cannot be read is
  retried. Observing, the door acts as before and counts what enforcing would refuse in
  `<service>_signature_would_refuse_total`.
- **The previous-key window.** Each identity publishes `signing-public-<identity>` with its current key first and at
  most one previous key. An event signed just before a rotation verifies while the previous key is listed, and lineage
  reads a list from the secret store under its own login and caches a successful read for 60 s. Provisioning, rotation and loss of the
  keys: [OPERATORS.md](OPERATORS.md) § 6.

## Code shape

- `lineage/models.py`: Pydantic `RunEvent` / `DatasetEvent` (the OpenLineage wire subset), the per-event bounds and
  dataset identity.
- `lineage/services/provenance.py`: `ProvenanceStore`, the feed, edge list, refusals and cursors over the catalog's doors.
- `lineage/services/forward.py`: the forwarder. `lineage/services/{consumer,staged,signature}.py`: shared intake.
- `lineage/api/v1/endpoints/{ingest,events,tables,dlq}.py`: the HTTP doors; `lineage/api/ingest_lane.py`: the bus
  transport's handling and park; `lineage/api/relay.py`: the relay tick and its timer;
  `lineage/api/{security,fga_deps}.py`: authn and authz.
