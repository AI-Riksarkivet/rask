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
- **Provenance:** at version/create the catalog reads the staged manifest's transaction properties and refuses a
  silver/gold commit without `rask.run_id` and `rask.inputs` (and checks `can_read_data` on each input); on its own
  doors it stamps them from the Context headers. No manifest is rewritten.
- **Versions:** pylance 13 and lancedb 0.40 everywhere now; lance-namespace stays 0.11.1 and lance-ray 0.5.0 (the
  newest); lance-namespace 0.13 (array-valued `on`) moves with the first pylance release that allows it. `index_specs`
  records the requested build parameters, because Lance 13 builds fewer partitions than asked on small tables.
- **The controlplane is deleted**: projects are the catalog's, served on `/management/v1/projects`.
