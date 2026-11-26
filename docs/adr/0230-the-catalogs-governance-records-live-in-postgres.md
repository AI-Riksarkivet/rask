# 0230. The catalog's governance records live in Postgres (2026-11-26)

**Status:** accepted as a direction; the schema, the order of the move and the questions under "Open, for the spec"
are settled by the spec that follows. Source: the 2026-11-26 review of all code and the comparison the owner asked for
(`docs/architecture/catalog-design-comparison-findings.md`). Owner, 2026-11-26, through the multi-question tool on that
comparison: "Move them into Postgres". Amends ADR 0228 (the sentence "rask's services do not open a database connection
at all") and withdraws the premise ADR 0045 and ADR 0107 state as "this estate needs no relational DB" for the
catalog's own records; what those records say about a table's commit stands.

## Context

The catalog keeps some thirty kinds of record of its own beside the tables it governs: the project and warehouse
registries, roles and principals, location and bucket claims, protection, trash, policies, contracts, labels, erasure
obligations, clone endings, idempotency keys, timer claims, announcement intents and more. Each is a JSON object under
the control root on the object store, written with a conditional put where a race was once measured
(`lakehouse.storage.conditional_records`) and with a plain put elsewhere (`lakehouse.records.record_store`).

The object store gives one atomic step: one conditional put of one object. Every change that touches two records, or a
record and something else, is therefore a sequence, and rask hand-builds what holds a sequence together: claims with
leases timed on the store's clock, fences, intents, turns, take-backs, and sweeps that finish what a crash cut.

Measured on `main` at 78b773220:

- That arbitration is about 19,200 of 92,836 production lines (20.7%).
- No request path of the catalog is one transaction.
- Of the 26 production defects the review found, 16 are in that code, and 8 of its 11 serious ones: a conditional
  write whose answer is lost read as a lost race, a take-back written without a condition, a delete with no condition
  at all (the estate demands only `If-None-Match: *` and `If-Match` on PutObject of a store, ADR 0172), a listing one
  bad record aborts.
- The store's listing is the only index: every "list all X" is a LIST of a prefix and one GET a record.

What the specification says: catalog-side state is "Business logic properties stored and managed by the namespace
implementation outside Lance context" (`lance_docs/ns_catalog/spec.yaml`). Lance defines no principal, role, grant,
trash or policy, and so no home for their records. What Lance does own, a table's versions, it commits with one atomic
write to the object store, which is why a Lance catalog needs no database for a commit; upstream removed its own
per-commit records from a Lance table because the registry "cannot sustain per-commit version-write TPS" (lance#7222).

Every catalog compared (Lakekeeper, Polaris, Gravitino, Unity Catalog) keeps its own state in one transactional store.
Lakekeeper writes a drop, its soft-delete mark and its clean-up task in one transaction and picks tasks with
`FOR UPDATE SKIP LOCKED`; its authorizer's tuples and its events stay outside that transaction, as rask's will.

## Decision

1. **The catalog's governance records are rows in a Postgres database**, and a request that changes several of them
   changes them in one transaction. The records are the catalog's own state about projects, warehouses, namespaces and
   tables: who claims what, what is protected, dropped, due, contracted, labelled, owed.
2. **A table's commit stays where Lance puts it.** The manifest's put-if-absent on the object store decides a commit;
   the catalog judges it and coordinates it as today (ADR 0157). Table data, versions, tags and branches do not move.
3. **Provenance stays as it is**: lineage's feed and edge list are Lance tables in the catalog's provenance namespace
   (ADR 0158). This record decides nothing about them.
4. **Authorization stays in OpenFGA and events on NATS JetStream.** A request still spans the database, the authorizer,
   the bus and the object store, so the order of those steps stays a designed thing: each path is ordered so a failure
   leaves a grant missing and never one too many, and the row that says a step is owed is written in the transaction
   that makes it owed.
5. **The database is the platform's** (ADR 0228 stands): the chart renders none, names it by host, and development gets
   it from the `postgres` fixture. The catalog's credential for it comes from the secret store, is scoped to the
   catalog's own database and role, and reaches the process as a file, never through the environment.
6. **Nothing is migrated.** The estate is pre-production: the records are rebuilt, and the object-store records, their
   primitives and their repairers are deleted in the change that replaces them.

## The four facts

- **GitOps.** Unchanged: the declaration in git converges the projects, warehouses and grants (ADR 0206); the schema is
  applied by the release, and no hand-run step creates or repairs a row.
- **Cloud native.** One more named platform service of a kind the estate already names for OpenFGA; no operator, no
  bundled database.
- **Lance-namespace and table idiomatic.** The spec places this state outside Lance; Lance's own commit, the only
  thing it arbitrates, is untouched.
- **Zero trust.** One more credential, scoped to one database and one role, short-lived where the platform's secret
  store issues such, and held by the catalog only. Maintenance and lineage get no database credential: they keep
  reaching these records through the catalog's doors.

## Consequences

- The conditional-record primitives, the leases on the store's clock and most sweeps lose their callers. The work is
  mostly deletion; the spec states, per record kind, what replaces it and which repairer goes.
- Listings become queries: paging by a key, filters and counts without one GET a record.
- The catalog gains a hard dependency for every request, where today a governance read needs only the object store.
  Its readiness and its behaviour during a database outage are part of the spec.
- The control-root backup shrinks to what stays on the object store; the database's backup is its owner's (ADR 0228).
- `CLAUDE.md` states the opposite premise in two places and is corrected with the first change that lands.

## Open, for the spec

1. **The records about a commit** (the commit's author, marks, pending notes and turns, history fences, file
   identities): they are catalog records, written on every commit. Whether they move, and what a commit then costs.
2. **The name registry**: namespaces and tables are rows of the directory namespace's `__manifest`, a Lance table. If
   it stays, a create and a rename span two stores; if it moves, the catalog no longer runs on Lance's own namespace
   implementation.
3. **The outboxes**: whether an event owed is a row written in the transaction of the change it announces.
4. **Maintenance's queue**: whether tasks become rows picked under a lock, or stay on JetStream.
5. **The order**: the move first, or the review's six object-store defects patched in place first.
6. **Lineage's five tables** are named here only to say they are out of scope until the owner raises them.

## Not chosen

- **Keep the records on the object store and harden them.** No new dependency, but the class of defect and a fifth of
  the code stay, and each new record kind pays for its own arbitration.
- **Move the registries into Lance tables.** Lance-shaped for creates and removes; every writer contends on one
  manifest, the table needs compaction the lakehouse does not run, and upstream withdrew the same shape for records
  written on every commit.
- **Put another catalog underneath.** Refused in ADR 0107, and nothing measured since changes that.
