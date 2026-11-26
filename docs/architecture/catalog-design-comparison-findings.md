# Is the design right? rask beside Lakekeeper, Polaris, Gravitino, Unity Catalog and Lance itself

Read 2026-11-26, from each project's default branch: `lakekeeper/lakekeeper` `4054ae96`, `apache/polaris` `7144ebfc`,
`apache/gravitino` `0709ea68`, `unitycatalog/unitycatalog` `f59e83d3`, `lance-format/lance` `648b0f8413`,
`lance-format/lance-namespace` `a13c746b`, `lance-format/lance-namespace-impls` `f815e62b`, and rask `78b773220`. Four
researchers read primary sources only; their full findings, with file and line for every claim, are beside the review's
reports (`rask-review-2026-11-26/design/{lakekeeper,polaris-gravitino-unity,lance,rask}.md`). This page is the comparison.

## The short answer

The idea at the centre of rask is right, Lance-idiomatic, and ahead of every product compared. One structural choice
under it is not: rask is the only one of the five that keeps its governance state outside a transactional store, and
that choice was never weighed.

## What rask gets right, and nobody else does for Lance

- **The catalog arbitrates and judges commits.** Lance's own documentation says what a REST catalog is for: it "enables
  organizations to enforce governance policies such as auditing, access control, and commit validation while still
  preserving the Lance table format as the authoritative source of table state" (`lance_docs/ns_catalog/catalog/rest/index.md:11`).
  Lance specifies and ships none of those checks. Polaris, Unity, Gravitino and Lakekeeper's generic tables all answer
  `managed_versioning=false` for a Lance table and say so in their docs ("There is no commit coordination provided by
  Polaris"; Lakekeeper: "The catalog does not arbitrate writes"). For a Lance table each governs a registry entry: a
  name, a location, who may see it. rask judges every stock commit at `version/create`.
- **A credential scoped to the table.** Polaris: "Generic tables APIs do not support credential vending". Gravitino
  returns shared configured credentials. The Lance project's own adapter has three static levels.
- **Events that are not lost.** Lakekeeper publishes after the commit through an in-memory channel with no outbox;
  Polaris calls its events "not a complete audit trail ... asynchronous and best-effort"; Gravitino's "can be dropped".
  rask stages, signs and re-announces.
- **Lineage, contracts checked at commit, erasure across derived tables.** None of the others has them for any format,
  beyond stubs and an empty hook.
- **Repair that runs by itself, and tenancy declared in git.** Lakekeeper's authorization repair is a hand-run command;
  none of the four declares tenants, roles or grants.

## What every one of them does better: one transactional store

- **Lakekeeper**: one Postgres holds nearly all state, so a drop, its soft-delete flag, its clean-up task and its
  idempotency record commit together; the task queue is in the same database, claimed with `FOR UPDATE SKIP LOCKED`.
- **Polaris, Gravitino, Unity**: every piece of catalog and governance state, grants included, in one database. None
  keeps a record on object storage.
- **rask**: no request path is one transaction. Each is an ordered series of single-object writes across 31 kinds of
  JSON record, a namespace manifest, OpenFGA and the table, made safe by claims, fences, intents, timers and
  maintenance's audit.

What that costs, measured at `78b773220`:

| | |
| --- | --- |
| Hand-built mechanisms standing in for a transaction | 19,200 lines, 20.7% of production source (29% with the adjacent ones) |
| Production defects of the 2026-11-26 review that sit in them | 16 of 26 |
| Of the 11 serious ones | 8 |
| Repairers the design relies on that ship off or dry-run in `chart/values.yaml` | trash purge, drift repair, tuple rebuild, tombstone sweep, floor raise |
| States with no automatic repair | a rename cut between its two backend calls; lost parent edges; a project left `deleting_by` |

No ADR decides "no catalog database" against an alternative; it is a premise. ADR 0107 names the price: the operational
backlog Lakekeeper ships "is what rask owes in exchange for owning the catalog". Production already depends on a
Postgres, OpenFGA's, as a platform service.

The other four are not clean at the edges either: Lakekeeper writes authorization tuples outside its transaction and
compensates best effort; Polaris carries a TODO for storage effects; Gravitino says "NOT atomic ... no recovery
mechanism". A database removes the problem between records. It does not remove it between the database, the object
store and the authorizer.

## What Lance itself says about where such state belongs

- The specification puts catalog-side state outside Lance: "Business logic properties stored and managed by the
  namespace implementation outside Lance context" (`spec.yaml:3772`). It defines no principal, role, grant or policy.
- Lance tried keeping per-commit version records in a Lance table and removed it: "`__manifest` cannot sustain
  per-commit version-write TPS; it suits table create/remove, not every commit" (lance#7222). A registry in a Lance
  table is upstream's own shape for creates and removes; it is not a home for records written on every commit.
- The spec's only multi-table atomicity, the two batch operations, is implemented by no Lance backend. rask answering
  them 406 is where the ecosystem stands.

So "Lance first" settles who owns a table's versions (the store, with the catalog as judge) and is silent on where a
catalog keeps its own records. Nothing in Lance asks for them to be on the object store.

## Three ways forward for the governance records

1. **Keep them on the object store and harden.** Fix the sixteen defects, turn the repairers on, add the missing
   repairs. No new dependency; the class of defect stays, and the fifth of the code stays.
2. **Move them into Postgres**, the platform's, named as OpenFGA's is. Claims, fences, trash, protection, contracts,
   registries, intents and idempotency become rows; a multi-record change becomes one transaction; the task queue can
   join it. The table's commit stays where Lance puts it. Weeks of work, most of it deletion. Cloud native: a named
   external service, as today's. Zero trust: one more scoped credential. GitOps: unchanged.
3. **Move the registries into Lance tables, keep per-commit records as objects.** Lance-shaped for creates and removes;
   contention on one manifest, and compaction the lakehouse does not run.

## Smaller things worth taking

- Lakekeeper's idempotency record is written in the mutation's own transaction and covers every catalog mutation.
- Its "commit state unknown" is an error type of its own that suppresses clean-up. rask's conditional writes read a
  lost answer as a lost race (review finding R10).
- Lakekeeper orders a cross-namespace rename so every failure leaves the authorizer missing an edge, never holding an
  extra one, and says so beside the code.
- `CLAUDE.md` still describes a `rask-age` Postgres that ADR 0228 removed, and quotes 77,000 production lines where
  the tree holds 92,836.

## Not confirmed

Lakekeeper's listing filter end to end and its project delete path; whether Unity writes its clean-up task in the
drop's transaction; the Lance directory namespace's `manifest.rs` (its atomicity rests on the RFC author's description);
LanceDB's enterprise pages (read through a summary). Nothing here was measured on a running estate.
