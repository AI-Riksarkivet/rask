# 0197. Maintenance writes only under a vend, and no workload holds a store key (2026-11-08)

**Status:** accepted. Source: [[XC-084]] (#205), its widening on the owner's word of 2026-11-03 ("a compromised
maintenance pod must not be able to defeat an erasure or a hold"), [[LH-219]] (#121) and [[LH-238]] (#158). Extends
[ADR 0170](0170-the-catalogs-store-identity-is-its-serviceaccount.md) from the catalog to every
lakehouse workload.

## Context

At main `0ce2ce97f`: the provisioning hook made three static RustFS users, `rask-maintenance`, `rask-lineage` and
`rask-backup`, whose secrets were derived from the root secret and never expired, delivered through the dev secret store
as `*-s3` pairs. Maintenance's policy allowed get, put and delete on every bucket and key, denied only a list of control
prefixes, so it could delete or overwrite `_erasure_obligations/`, `_history_fences/`, a table's `_refs/` tags and the
`_policies/` legal-hold opt-out, delete any version of a protected or held table, and reach every bucket on the store.
The 900 s per-table maintain vend narrowed nothing while that key existed: the 2026-11-03 maintenance-plane audit found
expiry signing with its own key when a vend was absent ([[LH-219]]). pg_dump read `PGPASSWORD` from env. A process with
its own `AWS_*` environment could send a vended credential to the wrong endpoint ([[LH-238]]).

The fix's scope, as the widening states it: a cleanup credential scoped to the table it cleans, as a catalog that purges
through a per-table location credential does, and estate-wide READS for discovery and the protection pre-pass alone.

## Decision

1. **Every workload's store identity is its projected ServiceAccount token.** Catalog, lineage, maintenance and both
   backup CronJobs (each under its own ServiceAccount) exchange a token for `objectStore.identity.audience` at STS
   `AssumeRoleWithWebIdentity`; the store grants each the canned policy its `sub` names. The policies render in every
   mode in `<release>-object-store-identities`, the hook writes each inline (`--policy NAME=JSON`) and binds no user, and
   its prune removes every regular user but the root. A backup run exchanges once per run (`lakehouse.store_session.
   job_client`). pg_dump reads a PGPASSFILE built in a memory volume from the mounted secret file.
2. **Maintenance's own identity reads the estate and writes only its own records**: its due records, its cadence stamps
   and its lineage outbox on the control root. Every other write is a catalog vend's.
3. **The maintain-tier vend for main** grants the table's prefix, its history fence and the records of its committed
   files, and explicitly denies put and delete on `_refs/`: a tag exempts its version from cleanup and a branch keeps its
   files (`lance_docs/lance/guide/tags_and_branches.md`), so a credential writing a ref could pin a version an erasure
   removes or unpin one a hold keeps. A table whose resolved maintenance policy keeps its whole history is refused the
   maintain tier (409 code 19): a session policy cannot refuse a put over an existing manifest, and maintenance writes
   nothing on a held table, since commit-path cleanup fires only inside a commit and every catalog door disarms first.
4. **The trash is the catalog's to end, and maintenance deletes only a due record's bytes.** Four doors under
   `/management/v1/trash/{kind}/{uuid}`, each asking the estate root's `can_reclaim_trash`, which only maintenance's
   `reclaimer` role opens:
   - `POST .../release`: once a record is past its deadline by `trash.RELEASE_SETTLE` (15 min) and no live object holds
     it, the catalog revokes the object's grants (origin `trash_expired`), announces a `grant_revoked` it signs for each
     person who held one, and stamps the record released;
   - `POST .../credentials`: for a due table record that names a location, a session that lists and deletes that one
     record's bytes, its location in both spellings and its data directories, and nothing on the control root; a place
     that is a whole bucket is refused;
   - `DELETE ...`: once the record's place and data directories hold nothing (a record naming no location: once past
     its deadline), the catalog revokes the grants that remain, ends the object's records, releases the location's
     claim, forgets its base record and clears the trash record (`lakehouse.trash.end`);
   - `POST .../refusals`: notes a refusal maintenance met on the record.

   The catalog judges each on reads no session can write: the path's kind and uuid, the record it filed, and the
   location's claim, which must record the path's uuid, held or released. A refusal is answered 409 code 19 and noted on
   the record. Two reasons the catalog, not maintenance, ends a record: a session able to write the record the catalog
   judges its next vend by could rewrite that record's location to another table's and be vended that table's bytes
   (the review of this change found it), and `grant_revoked` is the catalog's to sign
   (`lakehouse.control_events.control_signer_role`), so a notifier that enforces signatures refused maintenance's.
5. **Maintenance holds no OpenFGA credential.** OpenFGA has no per-tuple access control: a client allowed to write one
   tuple may write any, and one able to grant itself an owner or estate rung could lift a hold through the catalog's own
   doors. Maintenance's drift reconciler reads the grant census and asks for its two repairs at `/management/v1/grants`
   (`GET /census`, `POST /ghosts/revoke`, `POST /tenancy/rebuild`), each asking the estate root's
   `can_reconcile_grants`, which only the `reconciler` role opens. The census leaves out the objects the deployment keeps
   for itself, and the catalog judges every candidate again on its own reads: a ghost no project or warehouse record, held
   location claim or trash record names, and a tenancy edge the warehouse's own record names. The chart's
   `lance.fgaWriters` is the catalog alone, and maintenance mounts no OpenFGA token.
6. **Records are written and deleted by key.** pyarrow's `create_dir` puts every parent of a key as a marker, and its
   deletes put the parent back (measured on pyarrow 25.0.0), writes outside what a session scoped to the keys may make,
   so `record_store.put_record`, `stored_objects` and the purge's byte delete reach S3 through the object client.
7. **Maintenance reads the catalog's records where the catalog keeps them.** The chart moves no control root
   (`LANCE_REST_ROOT` is `objectStore.bucket`), so maintenance's `controlRoot` and `policyRoot` values are gone: they
   could only point its reads, and the writes its policy grants on the control bucket, away from the catalog's records.

## Consequences

- A compromised maintenance pod reads the estate and, beyond its own records, writes only what a vend the catalog judged
  reaches: the version history of a table that is not held, and the bytes of a trash record that is due. It writes no
  ref, policy, obligation, grant or record the catalog judges by. What a cleanup credential can still do is delete the
  versions of the table it was vended for, which is what cleanup is.
- Each reclaimed record and each maintained pass costs an STS session; a record the catalog refuses mints none.
- The policy sizes stay under STS's 2,048 characters: the maintain tier fits a 316-character table prefix at a
  63-character bucket (the write tier's 89 remains the binding one), and a reclaim session measured 410 to 793 characters
  for ordinary records and 1,765 at the largest plausible (63-character buckets, non-ASCII names, two data directories),
  measured on the policy builder. A record whose place and four data directories sit in four 63-character buckets
  exceeds it: the vend is refused 500 code 18 (`SessionPolicyTooLong`), noted on the record, and the record is kept.
- Proven on a real RustFS in the `rustfs-provisioning` lane: a reclaim session deletes its record's bytes, and its put on
  the record, its delete of a sibling's bytes and of `_table_inputs/` are refused
  (`tests/e2e-py/test_a_reclaim_session_deletes_one_tables_bytes_and_nothing_else.py`).
- Residuals, stated:
  - `_maintenance_due/state/clones.json` is still written under maintenance's own identity, and the catalog's commit
    judge reads it, so a compromised pod could empty it and let a stock compaction break a clone. The owner chose, on
    2026-11-08, to protect a registered clone with a Lance tag on its source version instead, which removes the list;
    that is its own ADR and rows.
  - The grant census reads at most 20,000 tuples (200 pages of 100). Past that the drift report is incomplete, and the
    repair does not run on an incomplete report.
  - A namespace renamed after a crashed undrop is not caught by the end door's name check; the purge's own manifest
    pre-check refuses it.
