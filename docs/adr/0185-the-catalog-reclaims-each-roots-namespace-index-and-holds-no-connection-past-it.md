# 0185. The catalog reclaims each root's namespace index, and holds no connection past it (2026-10-30)

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
4. **A commit-path policy is not the fix.** A test arms `lance.auto_cleanup.*` on the index and shows the catalog's
   commits keep every version, so the tick stays the one thing that bounds it.

## Consequences

- A dropped namespace or table stays readable in the index's history for up to the retention plus one schedule (3,600 s
  and 600 s by default, `catalog.manifestReclaim`), and the history holds the operations of that window.
- Every catalog replica opens each root's index again every 15 minutes per plane; every replica ticks, and two
  reclaims of one root remove the same versions.
- The safety argument assumes no two clocks disagree by more than three quarters of the retention and that no
  single call on one connection runs that long; a smaller retention narrows both. A retention of 0 keeps only the
  latest version and builds a connection for every request.
- A pylance bump re-reads `DatasetConsistencyWrapper::reload`: Lance `main` already resolves the latest version
  instead while a held version carries a cleanup policy, and a reload that never probes the successor would let the
  age bound go.
