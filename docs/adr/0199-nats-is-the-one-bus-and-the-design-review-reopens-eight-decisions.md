# 0199. NATS is the one bus, the library takes subpackages, and the design review reopens eight decisions (2026-11-09)

**Status:** accepted as a record of what is decided and what is reopened; no code has changed under it. Source: the owner,
2026-11-09, during a read-only audit of `packages/lakehouse` and `services/` against the `writing-python` and `fastapi`
references and of the architecture against the four facts: "ALSO adr WE HAVE CAN BE WRONG AND BE UPDATED FOR BEING MORE
CLOUD NATIVE, GITOPs, lance idiomatic, zero trust", "Nats only now is fine in my opinion", "fix all of them then! ADRs and
backlog", and, on the flat library, "Im not really a fan of this flat structure". The audit's findings, measurements and
ordered plan are the page "Lakehouse Code Audit" (https://claude.ai/artifact/QekgLUeWuYHdPPfggWqjCs); the package ids
below (P7, P11, D8, ...) are that plan's.

## Context

Measured 2026-11-09 on the working tree at `04e1a6e5f` (291 source modules, 77,330 lines), with the chart rendered under
`deploy/values-prod.yaml`:

- **An ADR was being read as a shield.** The audit first filed eleven gaps as accepted because a record existed for
  each. The owner's rule is that a recorded decision is judged by the four facts like any other design, and reopened
  when it loses.
- **The library is flat.** 67 modules sit at the top of `lakehouse/` beside five subpackages. `governed/` holds sign-in,
  authorization and secrets together in 6,016 lines.
- **The bus is already NATS in every respect.** The chart declares JetStream streams, durables, two work queues and a
  key-value bucket; all twelve Dapr Components in the render are `pubsub.jetstream`, a cron binding or the one secret
  store; and maintenance speaks to the bus through `nats-py` (ADR 0198 decision 5), because Dapr's subscription sends no
  in-progress ack and its publish no schedule header. A broker swapped by Dapr component would break maintenance and the
  stream Job whatever the component said.
- **The reference, read from source** (Lakekeeper `08b67d7`; its chart `040875a`; its operator `27fa1e1`). One core
  crate divides into `api/` (HTTP, and the one file that installs every layer), `server/` (the catalog operations) and
  `service/` (one directory per concern: `authn`, `authz`, `catalog_store`, `secrets`, `storage`, `events`, `tasks`,
  `health`, `idempotency`, `identifier`), with process composition in `serve.rs`. Every background service is named and
  supervised, and the process exits when one returns. Health is polled in the background per dependency. The same binary
  runs headless, with no HTTP API, as a worker, and refuses to start with no work. Its chart scales workers from zero
  with a KEDA ScaledJob reading a demand endpoint of the service. It builds its own CloudEvents and publishes them
  through a backend interface with a NATS and a Kafka implementation, at most once. It speaks to NATS and Vault with its
  own clients.

## Decision

Decided now:

1. **A recorded decision is judged by the four facts.** An audit or design reports "reopen ADR N" where a record loses to
   GitOps, cloud native, Lance-idiomatic or zero trust, naming the fact, and never lists a gap as accepted because it has
   a number. Reopening stays the owner's.
2. **NATS JetStream is the lakehouse's one bus.** "Swap the broker by configuration" is not a requirement. No second
   bus adapter is built; publishing goes through one small interface so a second sink stays possible. This retires the
   premise of [ADR 0195](0195-the-three-kits-fold-into-one-lakehouse-library.md) that events are "swapped by Dapr
   component".
3. **The library stays one package and takes subpackages named for the job each does**, as the reference's `service/`
   is laid out: `errors`, `secrets`, `authn`, `authz`, `storage`, `lance`, `catalog_store`, `events`, `serve`, `api`,
   lowest first, held by an import-linter `layers` contract. Measured against that layout, 17 of 103 cross-package
   imports point up; they are cut before the move (P7). This amends ADR 0195's "organised by module"; its one package,
   no extras and no audience contracts stand.

Reopened, each decided in the package that implements it and not before:

| Record | Loses to | Direction | Decided with | Confidence |
| --- | --- | --- | --- | --- |
| [0027](0027-control-events-broadcast-ring-buffer.md), [0028](0028-control-events-per-replica-cursor-boundary.md) | cloud native: a cursor and buffer per replica; a cursor read on another replica answers an empty page (`docs/chart/templates/services.yaml.md`) | the poll reads the durable stream by sequence | P12 | high |
| [0153](0153-the-app-chart-is-the-product.md) | nothing: the record is right and the production render contradicts it (a one-replica Collector with host-path mounts) | the render is brought to the record | P14 | high |
| LH-538 (no record) | Lance-idiomatic: `lance.compaction.*` is refused where `lance_docs/lance/format/table/table_config.md:10-12` expects an implementation to honour `lance.` keys | a record of its own stating the reason, or the refusal dropped | P6 | high |
| [0188](0188-a-lost-publication-is-announced-from-its-tag-and-lifecycle-markers-stay-best-effort.md) | cloud native: a table's declare, register, rename and drop markers are at most once, so a consumer that acts on them can miss one | the markers ride the outbox the commit announcements ride | P13, after the store requests per door are counted | medium-high |
| [0185](0185-the-catalog-reclaims-each-roots-namespace-index-and-holds-no-connection-past-it.md) | cloud native: the reclaim, and four other reconcilers, run in the serving process on every replica | a headless catalog worker role, one owner per tick | P11, after one tick is timed at 1,000 tables | medium-high |
| Ruling of 2026-08-28 (`chart/values.yaml`, autoscaling on CPU, KEDA declined) | cloud native: a replica's throughput is bounded by 40 worker threads, which CPU does not show | scale on requests in flight and on queue depth | P14, after a load test at 1, 2 and 4 replicas | medium |
| [0192](0192-a-services-roles-are-granted-at-the-catalogs-doors-scoped-to-the-tenants-it-serves.md) | GitOps: a client's grants exist only in OpenFGA, so an install from git does not recreate them | grants declared in git and converged by the planned operator, the doors kept for people | the operator's own design; 0192 stands until then | medium |
| [0198](0198-maintenance-is-driven-by-commits-and-a-clone-keeps-its-source-by-a-tag.md) decision 5, [0166](0166-a-secret-read-from-the-store-is-re-read-on-a-ttl.md) | cloud native and zero trust: two bus clients and two credential paths in one product; a bus seed and a shared app token that never expire | follows the move off Dapr: one direct path, each pod trading its ServiceAccount token for short-lived credentials | D8 | follows D |

Kept after the same judgement: [ADR 0102](0102-the-sweep-is-the-one-reclaimer-lance-s-commit-path-auto.md)
and 0103 (one reclaimer, which `lance_docs/lance/guide/read_and_write.md:666-678` supports), ADR 0162 (the lakehouse
builds nothing), ADR 0157 and 0164 (stock clients commit through `version/create`, the directory catalog's own
put-if-not-exists finalize), ADR 0170 and 0197 (a workload's store identity is its ServiceAccount token), and
[ADR 0186](0186-a-commit-that-lost-its-race-takes-its-refs-turn.md): replanning in place of Lance's rebase is not
Lance-idiomatic (`lance_docs/lance/format/table/transaction.md:615-620`), and it stays until pylance's `update` and
merge take transaction properties.

## Consequences

- **Moving off Dapr is the plan's track D**, its own row after LH-494 (#638) closes, with an ADR of its own at D8. With
  NATS as the one bus, what Dapr still gives the lakehouse is credential custody: the bus seed and the store login stay
  out of the app container. Its replacement is unproven on the estate's versions (a store login from the pod's projected
  token, NATS auth callout), which is D0's work. The CloudEvent envelope is added by the sidecar today
  (`lakehouse.control_events`), with no `type` or `subject` of the lakehouse's own, so the lakehouse builds its envelope
  first (D1), whether or not the rest follows.
- **The reference's process shape is adopted as packages of the plan, not as records**: a background loop that ends
  ends the process, health polled in the background per dependency, a body cap with a request timeout and a panic
  catcher installed by the app factory (P2), and a worker role of the same image (P11).
- **A row that implements a reopened record amends that record in its own commit**, with the measurement that settled
  it. Until then the record describes the code as it runs, and its header says it is reopened here.
- **Two things wait on the owner and are in no package**: a public, per-project CloudEvents stream for clients (today
  `docs/COMMIT-EVENTS.md` calls the bus topics internal and a client polls the feed), and whether projects, warehouses
  and grants become resources declared in git.
