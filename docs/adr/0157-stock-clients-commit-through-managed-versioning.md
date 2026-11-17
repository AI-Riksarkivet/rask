# 0157. Stock clients commit through managed versioning; pylance 13 now, lance-namespace 0.13 with pylance 14; the controlplane is deleted (2026-10-12)

**Status:** accepted. Source: owner, 2026-10-12, a grilling round over a measured upgrade study. Refines ADR 0156's C2
and C3.

## Context

Measured on pylance 12 and 13 and lancedb 0.34 and 0.40: when `DescribeTable` / `DeclareTable` answer
`managed_versioning=true`, a stock client commits every operation (create, append, delete, merge_insert, update,
add_columns, `commit(Transaction)`, restore, compaction; lancedb add/delete/restore) through
`POST /v1/table/{id}/version/create` (spec `CreateTableVersion`), with the request's `header.*` Context on every call.
A refused version/create is not bypassed: the table stays at its version and the staged manifest and data files are
left as orphans. The request carries only `{id, version, manifest_path, manifest_size, e_tag, naming_scheme}`;
transaction properties travel inside the staged manifest the client wrote into `_versions/`. Tag writes
(`_refs/tags/`) and `cleanup_old_versions` go straight to storage and never reach the namespace.

pylance 13.0.0, the newest release, declares `lance-namespace>=0.11.1,<0.12`; lance-namespace 0.13 installs beside no
released pylance (upstream widened the bound only on lance main, 14.0.0-beta). Nothing in the 0.11→0.13 spec diff
changes tags, `RestoreTable`, Context or `CreateTableVersion`.

The controlplane's only route lists a `platform.rask.io` Project CRD that rask never installs (answering 501 on the
live chart); the catalog's registry and FGA are the real project store, as in Lakekeeper.

## Decision

- **The catalog serves `CreateTableVersion` and advertises `managed_versioning=true`.** It judges each staged manifest
  (tier policy, file versions, provenance) before doing the put-if-not-exists, and the bespoke `/commit` door is
  deleted. Vended write credentials exclude `_refs/` and final `_versions/*.manifest` names, so only the catalog can
  finalize a version or move a tag: without that, the tag gate is not a gate. Precondition, measured first: the object
  store's policy can tell a staged `_versions/*.manifest-<uuid>` from a final `_versions/*.manifest`; if it cannot, the
  catalog keeps refusing managed versioning and bring-your-own writers commit through catalog doors only.
  **Measured 2026-10-12 (LH-469, #594): the store CAN tell them apart, so the catalog serves managed versioning.**
  On MinIO `RELEASE.2025-04-22T22-12-26Z` (the estate's pinned build, `.docker/minio.dockerfile`) with an STS
  session policy built by `vending.build_session_policy(..., "write")` plus the statements below, stock pylance
  13.0.0 created and appended (v1, v2) through a namespace advertising `managed_versioning=true`; the writer put only
  `data/`, `_transactions/<n>-<uuid>.txn` and `_versions/<n>.manifest-<uuid>`, and the namespace finalized. The same
  credential was refused (403) a final `_versions/<n>.manifest` in both naming schemes, a staged-to-final copy,
  `_versions/latest_version_hint.json`, `_refs/tags/published.json`, `_refs/branches/*`, any delete and a sibling
  table's prefix. Two things the write tier must carry, both measured:
  - **An explicit `Deny s3:PutObject` on `<table>/_versions/*.manifest`.** An Allow-only grant on
    `_versions/*.manifest-*` also matches `<n>.manifest-<uuid>.manifest`, and pylance reads that name as a final
    manifest: one planted there left the table unopenable ("multiple manifest naming schemes"). The Deny refuses it
    even with a valid manifest body.
  - **Put on `_transactions/*`** (the stock writer stages a transaction file before its manifest and fails without
    it) and on `_deletions/*` (delete and update stage deletion files; granted, not driven by the probe).
  The same document is expressible on AWS S3: a session policy's explicit deny overrides any allow, and `*`
  matches across `/` (IAM User Guide, "Session policies" and "Using wildcards in resource ARNs"). It is about 1.4 KB
  for long bucket and table names, under the 2,048-character inline limit; every sanctioned base adds to it. A put
  over a committed `data/`, `_transactions/`, `_deletions/` or `_indices/` file is refused by the store (ADR 0242): the
  catalog seals each file a commit adds with an object tag before its version lands, and the write tier's session
  policy, and the catalog's own canned policy, deny `s3:PutObject` over a key holding it (measured 403 on RustFS 1.0.3
  for a put, a multipart upload and a copy onto one, 2026-11-17). A store that does not evaluate
  `s3:ExistingObjectTag` on PutObject (AWS S3) lets it through, and ADR 0184's pass finds it.
- **A declaration is its declarer's** (ADR 0227 §9, owner 2026-11-17). `DeclareTable` takes the table's location claim
  fenced for the authenticated subject that sent it, so until the declarer's first `version/create` lands, or the fence
  lapses after the claim's `LEASE` (15 minutes), a catalog create of the name or a version 1 by any other principal
  answers 409 ConcurrentModification (14). The first `version/create` fences the claim under the ETag of the claim it
  reads right before the finalize (taking over its own declarer's fence) and lifts it after. A declarer asking again
  (`DeclareTable` sent again, its client's credential refresh through `DescribeTable`, or the management credential
  door at the write tier) renews its declaration's fence under the ETag it read before anything is vended, and a
  credential vended for a declaration lasts no longer than that fence, `min(LANCE_VENDING_TTL_SECONDS, LEASE)`; a
  declaration another request acted on refuses the renewal. A drop of the declared, unwritten table takes the fence
  over. The stock client's path is the spec's unchanged: no field and no call is added.
  So the open overwrite above does not reach across a declaration's lapse: measured on RustFS 1.0.3 (2026-11-17,
  `make lifecycle-rustfs-check`, a session the store's STS issued under the catalog's session policy), a declarer's
  write credential whose declaration lapsed while it stayed live put over a data file of the version another
  principal's create committed at the location (200), and that table no longer read ("Invalid range 0..403 for
  object of size 7 bytes"); with the bound, the credential expires within the second of the fence's lapse, before any
  other principal's create can fence the claim (ADR 0227 §9). A write credential on a written table is refused a put
  over every committed file of its table where the store enforces the seal (ADR 0242).
- **Provenance:** at version/create the catalog reads the staged manifest's transaction properties and refuses a
  silver/gold commit without `rask.run_id` and `rask.inputs` (and checks `can_read_data` on each input); on its own
  doors it stamps them from the Context headers. No manifest is rewritten.
- **Versions:** pylance 13 and lancedb 0.40 everywhere now; lance-namespace stays 0.11.1 and lance-ray 0.5.0 (the
  newest); lance-namespace 0.13 (array-valued `on`) moves with the first pylance release that allows it. `index_specs`
  records the requested build parameters, because Lance 13 builds fewer partitions than asked on small tables.
- **The controlplane is deleted**: projects are the catalog's, served on `/management/v1/projects`.
