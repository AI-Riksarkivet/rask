# 0230. The catalog's governance records live in Postgres (2026-11-26)

**Status:** accepted. Source: the 2026-11-26 review of all code, the comparison the owner asked for
(`docs/architecture/catalog-design-comparison-findings.md`), and three studies made the same day: rask's own records
read kind by kind, Lakekeeper's Postgres layer read from source, and `lance_docs/` read in full with measurements on
pylance 14.0.3. Owner, 2026-11-26: "Move them into Postgres"; "the move now and not after phase 1"; the names of tables
and namespaces stay in `__manifest`; psycopg; the secret store issues the database credential. Amends ADR 0228 (the
sentence "rask's services do not open a database connection at all") and withdraws the premise ADR 0045 and ADR 0107
state as "this estate needs no relational DB" for the catalog's own records; what those records say about a table's
commit stands.

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

1. **Twenty kinds of governance record become rows in a Postgres database, and a request that changes several of them
   changes them in one transaction**: location claims, base records and data-directory claims, bucket claims, the
   project and warehouse registries, roles, principals, declared grants, protection, trash, branch trash, maintenance
   policies, cadence stamps and the due list, data contracts, labels, erasure obligations and releases, clone endings,
   idempotency keys, timer claims, and the announcement intents with both outboxes. Protection and trash are columns of
   one row a governed object, keyed by its uuid (ADR 0173); a bucket claim is a unique column of its warehouse's row.
2. **The names of tables and namespaces stay in `__manifest`**, Lance's Directory Catalog. `lance_docs/` describes two
   catalog shapes and leaves a REST server's own store open ("The server is responsible for storing and organizing
   namespace metadata", `ns_catalog/namespace/supported-catalogs/lance-rest.md`); rask keeps the first for names. The
   directory namespace of pylance 14.0.3 has no rename (measured: `NotImplementedError`), so a rename stays a
   deregister and a register, guarded by the location claim, now a row, and finished after a crash from a row that says
   it is owed. The tables are shaped so a name column could join the object's row later.
3. **A table's commit stays where Lance puts it.** The manifest's put-if-absent at its canonical path is the commit
   point (`lance_docs/lance/format/table/transaction.md`, External Manifest Store), which is what the catalog's
   finalize does today (ADR 0157). Table data, versions, tags and branches do not move.
4. **The six records about a commit stay on the object store in this move** (declared inputs, the commit's author, the
   pending notes and marks, file identities, the commit turn, the history fence) and are a second phase. Lance's own
   design for coordinating commits beside the object store is a key-value store that reserves each version with a
   put-if-not-exists; a row a table version is that store, and the second phase is judged against it after the commit
   path is measured.
5. **Provenance stays as it is**: lineage's feed and edge list are Lance tables (ADR 0158).
6. **Authorization stays in OpenFGA and events on NATS JetStream.** A request spans the database, Lance, the
   authorizer and the bus, so the order of those steps is designed, by one rule: a failure leaves a grant missing or a
   file unreferenced, never a grant too many. A tuple that gives access is written before the commit and taken back on
   failure; a tuple that removes access on a move is written before the commit; tuples of a dropped object go after
   it; bytes are deleted by a task, never in the request. A commit whose outcome is unknown is an error of its own, and
   nothing is cleaned up after one.
7. **An event owed is a row written in the transaction of the change it announces**, relayed to the bus and marked
   sent. Events are not sent best effort after the commit: provenance is built from them, maintenance is driven by
   them, and Phase 1's fourth criterion is that they are correct.
8. **The database is the platform's** (ADR 0228 stands): one cluster may serve OpenFGA's database and the catalog's,
   each with a role of its own. The chart renders none and names it by host; development gets it from the `postgres`
   fixture. Each service's credential comes from the secret store, is short-lived, and reaches the process as a file,
   never through the environment; connections are recycled within the credential's life.
9. **Maintenance holds a database role of its own**: read on the tables it reads today, write on cadence stamps and
   the due list, insert on events owed. Lineage holds none.
10. **The stack is psycopg 3, SQLModel and Alembic.** Transactions run at the default isolation level; a race is
    closed by a unique constraint, by a lock and a compare in one statement, or by an advisory lock taken before the
    check, and never by a stricter level. A transaction is never held open across a call to Lance, the object store or
    OpenFGA unless the step's order requires it, and no second connection is taken inside one.
11. **The schema is applied by the release**: a Job runs the migrations as a hook of the upgrade, every migration in
    one transaction behind an advisory lock; a pod waits for it and refuses to start against a schema behind or ahead
    of its own; there are no down migrations.
    *Amended 2026-11-28 ([[LH-613]]):* the Job is a bootstrap Job, a plain Job of the release named by its revision and
    an Argo CD `Sync` hook, not a Helm hook: the catalog's pods wait for the schema, so a post-install/post-upgrade hook
    would deadlock `--wait --wait-for-jobs` (`docs/chart/templates/_helpers.tpl.md`, "BOOTSTRAP JOBS").
12. **With the database unavailable the catalog answers unavailable**, and its readiness says why. A client holding a
    vended credential keeps reading and writing a table's bytes until it expires.
13. **Nothing is migrated.** The estate is pre-production: the records are rebuilt (projects, warehouses and grants
    from the git declaration), and each kind's object-store code, its repairer and their tests are deleted in the
    change that replaces it. The move goes kind by kind, each change deployed and read back before the next.

## The four facts

- **GitOps.** Unchanged: the declaration in git converges the projects, warehouses and grants (ADR 0206); the schema is
  applied by the release, and no hand-run step creates or repairs a row.
- **Cloud native.** One more named platform service of a kind the estate already names for OpenFGA; no operator, no
  bundled database.
- **Lance-namespace and table idiomatic.** The spec places this state outside Lance; the names stay in Lance's
  Directory Catalog; Lance's own commit, the only thing it arbitrates, is untouched.
- **Zero trust.** A credential a service, each scoped to one role, short-lived and issued by the secret store. The
  catalog's role owns its tables; maintenance's reads what it reads today and writes its own two; the database refuses
  either anything else. Lineage holds none.

## Consequences

- The conditional-record primitives, the leases on the store's clock and most sweeps lose their callers. The work is
  mostly deletion; the spec states, per record kind, what replaces it and which repairer goes.
- Listings become queries: paging by a key, filters and counts without one GET a record.
- The catalog gains a hard dependency for every request, a commit included: a table open reads its trash state, its
  claim and its contract. Every request already needs OpenFGA, which stands on the same platform's Postgres.
- A create, a drop, an undrop and a rename still span the database and `__manifest`; each is ordered, and finished
  after a crash from a row that says a step is owed. They become one transaction only if the names follow.
- The measured gain is smaller than the hand-built arbitration's size suggests: of its 19,200 lines, 8,333 serve the
  commit path and the reconcile of OpenFGA and the buckets and do not change; an estimated 4,000 to 5,500 lines go.
  Of the review's 24 production defects, 8 are of a kind rows cannot have.
- The control-root backup shrinks to what stays on the object store, and keeps a copy of nothing the database holds;
  the database's backup is its owner's (ADR 0228). A table's directory is named by its uuid and its name stays in
  `__manifest`, so losing the database loses no table.
- `CLAUDE.md` states the opposite premise in two places and is corrected with the first change that lands.

## Open, for the spec

1. The order of the batches and the first one's content.
2. How a storage location is kept from overlapping another: a unique index refuses an equal location only; a prefix
   needs a lock and a range check, as the reference catalog does it.
3. How one replica learns that another changed a row it caches.
4. The second phase: the six records about a commit as one row a table version.
5. Whether the names follow, decided on two measurements: what a describe costs on the busiest doors at 10,000 tables,
   and how concurrent creates behave under one root.

## Not chosen


- **Keep the records on the object store and harden them.** No new dependency, but the class of defect and a fifth of
  the code stay, and each new record kind pays for its own arbitration.
- **Move the registries into Lance tables.** Lance-shaped for creates and removes; every writer contends on one
  manifest, the table needs compaction the lakehouse does not run, and upstream withdrew the same shape for records
  written on every commit.
- **Put another catalog underneath.** Refused in ADR 0107, and nothing measured since changes that.
- **Send events best effort after the commit**, as the reference catalog does. Its events are notifications; rask's
  build the provenance record and drive maintenance.
- **Catalog doors for everything maintenance reads.** A database role scoped to those tables is refused everything
  else by the database itself, and is far less to build.
- **Move the names into Postgres in this move.** Both shapes are Lance's own; the twenty kinds do not depend on it, and
  the owner keeps the Directory Catalog for names.
