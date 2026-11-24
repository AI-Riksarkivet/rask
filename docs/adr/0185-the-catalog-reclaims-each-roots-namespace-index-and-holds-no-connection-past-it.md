# 0185. The catalog reclaims each root's namespace index, and holds no connection past it (2026-10-30)

**Reopened 2026-11-09** by [ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md): the reclaim runs in the serving process on every replica; a catalog worker role is to own it, decided after one tick is timed.

**Settled 2026-11-23** (owner, XC-204 #802: "Cut the tick's cost, no worker role"): the reconcilers stay in the catalog's
serving process and no worker role is built. Each timer's window is claimed by one replica (`lakehouse.timers`, XC-199), so a
tick runs once a window whatever the replica count, and every run is timed (`timer.work.duration`). Measured that day,
off the estate with the real catalog over a counting store: one idle commit-reconcile tick cost 22 store requests a
table (22,246 at 1,000 tables) and one read of every feed fragment per table; it now costs 16 a table and reads the
feed once a tick, and only when some table holds a version to look for. The reclaim cost 2,139 requests at 1,000 tables,
about two per retained index version. On the development estate a table costs about 0.2 s of a tick, so the commit
reconcile reaches half its 600 s period near 1,500 tables: `LakehouseTimerSlow` pages when any timer's runs average more
than half its period over an hour, and that page is when a worker role, a longer period or more CPU is decided again.

**Amended 2026-11-24** ([ADR 0177](0177-the-commit-is-the-outbox.md), XC-204): the commit reconcile no longer walks every
table each tick. It reads the tables a door filed a commit for in about the last twenty-five minutes, so its ceiling is
tables written in that time, not tables held: half its period near 1,500 such tables. The hourly walk
(`catalog-commit-walk`) reads every claimed table and reaches half its period near 9,000 tables at the same 0.2 s a
table.

**Measured 2026-11-24** on the development estate (XC-204 #802), sixteen stock writers appending through
`version/create`, each catalog pod limited to one CPU: 1.29, 2.54 and 5.20 commits a second at one, two and four catalog
replicas, the median commit 13.0, 6.3 and 2.6 s, none failed. The catalog scales by replicas, so a tick's cost is the
only reason a worker role would be built, and the amendment above removed the larger one.

**The request thread pool is 24 threads** (owner, 2026-11-24, XC-204: "24 threads, cap 16"; it was anyio's own 40, set
nowhere). A pool is safe only if its writers cannot hold it whole: an admitted bulk write holds a thread while it
commits (`catalog.maxConcurrentWrites`, 16) and a door's commit waiting for its ref's turn sleeps in another
(`commit_turns.THREAD_WAITERS`, 4, was 20), and the two together stay below the pool, which the catalog's settings and
the chart's render both refuse otherwise. Measured that day on the development estate, one CPU a pod:

| pool, write cap | 1,500 reads over 300 connections | serving loop's own answer, median | 32 writers on one table | a read of another table beside them, median and p95 |
|---|---|---|---|---|
| 40, 16 (20 waiters) | 50 to 52 s | 0.28 to 0.39 s | 3.36 writes a second | 2.1 s, 6.6 s |
| 24, 16 | 50.5 s | 0.13 s | 3.31 | 2.1 s, 7.2 s |
| 16, 8 | 50.5 s | 0.07 to 0.08 s | 2.30 | 1.7 s, 4.3 s |

**Status:** accepted. Source: LH-251 (#165): nothing reclaimed `__manifest`, every namespace operation kept a version,
and a dropped id stayed readable in its history.

## Context

A `dir` root keeps its namespaces and tables as rows of one Lance table, `<root>/__manifest`
(`lance_docs/ns_catalog/catalog/dir/index.md` § V2: Manifest). Measured on pylance 13.0.0:

- Every namespace operation rewrites the table whole as a new version, one single-fragment data file each, and keeps
  every old one: 20 creates left 21 versions, and a dropped namespace was readable at latest - 1.
- `lance.auto_cleanup.*` on the table does nothing for those commits: the backend's rewrite sets `skip_auto_cleanup`
  (lance v13.0.0 `rust/lance-namespace-impls/src/dir/manifest.rs`, `rewrite_manifest`). Armed with interval 1 and
  `older_than` 0, five creates still added five versions.
- **The backend relies on that history.** A connection holds one version of the table and reloads by probing only the
  version after it (`DatasetConsistencyWrapper::reload`, `Dataset::has_successor_version`): "this module never runs
  old-version cleanup on it". Lance `main` at `bfcc01f3` (2026-10-30) keeps the rule. After a `cleanup_old_versions` that
  removed a held connection's successor, that connection answered `InternalError` on every call: it never moved on, and
  the data file of the version it held was gone with that version. A writer on it would commit at its held version + 1
  with put-if-not-exists, a slot the cleanup had emptied.
- The spec lists three indexes on the table; the backend builds them only with `inline_optimization_enabled`, off by
  default. Counted over moto at 100 and 1,000 objects, the indexes made a create 16 store requests instead of 12 and a
  describe 6 instead of 3, with a list at 2 either way: unindexed, the table is one data file a read takes in one GET.

## Decision

1. **The catalog reclaims the history.** It is the table's only writer and already holds a write on it at every root,
   so a cron tick in the catalog (`catalog-manifest-reclaim-cron`, `catalog.api.manifest_reclaim`) runs Lance's own
   `cleanup_old_versions(older_than=manifest_retain_seconds)` on the catalog's root and every registered warehouse's,
   deactivated ones included: the index is the catalog's bookkeeping, and an offboarding's dropped ids are what it
   would keep readable. The maintenance sweep, ADR 0102's one reclaimer of governed tables, does not: the condition
   below lives in the catalog's connections, so one setting holds both halves, and maintenance holds no delete on the
   index.
2. **No connection is served past a quarter of the retention.** `NamespaceConnections` builds a connection again, at the
   index's latest version, once it is `manifest_retain_seconds / 4` old. A connection served within that age last read
   the index after its successor version was written, and the reclaim removes only versions older than the retention,
   so no connection is left below a removed version. The rest of the retention is the margin for clocks that disagree:
   the reclaim judges a version's age by the timestamp its writer stamped.
3. **The index builds no index of its own.** The catalog sets `inline_optimization_enabled=false` rather than leaving
   the backend's default, so an upstream default that changes does not change it.
4. **A commit-path policy is not the fix.** The backend's rewrite commits with `skip_auto_cleanup`, so a
   `lance.auto_cleanup.*` policy armed on the index bounds nothing (measured above), and the tick stays the one thing
   that bounds it. That is Lance's behaviour rather than rask's, so no rask test pins it: a pylance bump re-reads
   `rewrite_manifest`, as it re-reads the reload.

## Consequences

- A dropped namespace or table stays readable in the index's history for up to the retention plus one schedule (3,600 s
  and 600 s by default, `catalog.manifestReclaim`), and the history holds the operations of that window.
- One catalog replica, whichever claims the window, opens each root's index again every tick; a tick that fails is
  repaired by the next, and two reclaims of one root remove the same versions.
- A root the reclaim fails on is counted on `catalog.manifest_reclaim.failed` by root, and the same root failing on two
  ticks within half an hour pages (`CatalogManifestReclaimFailing`); one tick's failure is repaired by the next.
- The safety argument assumes no two clocks disagree by more than three quarters of the retention and that no
  single call on one connection runs that long; a smaller retention narrows both. A retention of 0 keeps only the
  latest version and builds a connection for every request.
- A pylance bump re-reads `DatasetConsistencyWrapper::reload`: Lance `main` already resolves the latest version
  instead while a held version carries a cleanup policy, and a reload that never probes the successor would let the
  age bound go.
