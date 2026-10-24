# 0175. A data base is its tables' storage: reached under the catalog's own identity, and reclaimed by maintenance (2026-10-24)

**Status:** accepted. Source: owner, 2026-10-24, through the multi-question tool while taking LH-286 (#298), which folds
LH-423 (#448), XC-168 (#662) and the data-directory half of LH-535 (#721). Amends ADR 0170 (the catalog holds no
static key for a base either).

## Context

A table may spread its fragments across approved data bases (`catalog.multibase.dataBases`, the Uber pattern of
`lance_docs/lancemultibasebranchingblobv2.md` § 1), each table in its own directory beneath a base ([[LH-252]]). Three
gaps stood:

- Each base could carry its own credential reference, a static key pair the catalog fetched from the secret store
  (`LANCE_MULTIBASE_BASE_CREDENTIAL_REFS`). The catalog's own identity is a session its ServiceAccount token is exchanged
  for (ADR 0170), so that pair was the one static store key it still held.
- The native doors (query, count, the plans, stats, backfill) refused such a table 406, because the native reader took
  no per-base parameters, and the vend answered `server_mediated`.
- Nothing reclaimed a base. Measured on pylance 13.0.0: after an overwrite of a table whose fragments sit on a data base,
  `cleanup_old_versions(older_than=0, delete_unverified=True)` removed every superseded version and none of the base's
  files, against `lance_docs/guide.md:4349`. A deleted row's bytes stay in its base fragment behind a root
  `_deletions/` vector, and the purge of a dropped table deleted its root and left its data directory.

What the references say:

- `lance_docs/guide.md` § Per-Base Configuration: a base "may live in a different bucket, account, or storage provider";
  "every base inherits the unscoped options", and a `base_<id>.<key>` entry (or `base_store_params`) overrides them for
  one base. Measured on pylance 13.0.0: Lance honours those keys on reads and appends, and the `dir` backend's native
  query carries a connection's `storage.base_<id>.*` keys into its own open.
- Lakekeeper (read at `b9da6b5`) binds one storage profile and one credential to a warehouse
  (`crates/lakekeeper/src/service/storage/s3.rs:80-258`), prefers the server's own system identity, vends sessions
  narrowed to a table's location (`s3.rs:1055-1066`, `:1294-1360`), and refuses multi-location tables
  (`crates/lakekeeper/src/server/tables.rs:2265-2269`). Its identity is per store, never per location.

## Decision

- **A data base on the estate's store is its tables' storage, reached under the catalog's own identity**, as a table's
  root is. The per-base credential reference, its static pair, the dev fixture's scoped user and OpenBao entry, the
  vend's `server_mediated` answer for it and the native doors' 406 are gone. Every credential the catalog hands out
  stays narrowed to one table: a vend's one session grants the table's own directory in each base, read-only at every
  tier, plus delete at the table's maintain tier (`vending.VendableBases`, the base record's `data` entries).
- **A base on another store or account is out of scope** until a row adds it. That row follows Lakekeeper's unit, an
  identity per store (endpoint, STS, audience, role), delivered through Lance's per-base keys: a vend carries
  `base_<id>.*` options, and a native read runs on a connection carrying them.
- **Maintenance reclaims each table's data directories.** After `cleanup_old_versions`, the expiry pass lists each
  directory the table's base record names as a `data` entry (a plain base, never a dataset root), subtracts every file
  any retained version of main or of any branch references (a branch inherits the base under the same id and appends
  into the same directory, measured), and deletes what is left once it is older than Lance's unverified-file age, 7 days
  (`lance_docs/lance_sdk.md:931`). It signs with the maintain-tier vend; the probe that decides whether to vend asks
  for one when a stranded file is past the age. A reclaim is material work: it is audited and announced on lineage.
- **A deleted row's bytes on a base are reported, not rewritten.** The pass counts the fragments that keep them
  (`compaction.base_fragments.holding_deleted_rows`), and the compaction gate keeps refusing such a table, because
  pylance 13.0.0's `CompactionOptions` has no `target_bases` and a rewrite would move the table's rows into its root.
- **The trash purge deletes a dropped table's data directories** after its root and before it forgets its base record,
  each judged first: a directory whose claim names another table, or that a live dataset reads through, refuses the
  whole delete.

## Consequences

- The store must grant the catalog's and maintenance's identities on every data base's bucket, as on the estate's own.
  On the development store the scoped-users hook creates each bucket; a platform store's operator creates them.
- A file in a data directory younger than 7 days is kept even when no version references it, the cost Lance's own
  `data/` reclaim already pays (lance #8944): a writer staging fragments and committing more than 7 days later loses
  them.
- Erasure's residual surface `data_base:<name>` still names a deleted subject's bytes on a base until a compaction can
  rewrite them there.
