# open_table_maintenance — deletion, GC, and table maintenance

Two different subjects that keep getting confused, so they are separated here:

- **Deletion / offboarding** — removing a table, namespace, warehouse or project, and what happens
  to the tuples, bindings and bytes each one leaves behind.
- **Table maintenance** — the Lance format's own upkeep: compaction, pruning/cleanup, index updates.

## 1. Deletion — what exists today

| Object | Who | Endpoint | What it does to bytes | What it does to tuples |
| --- | --- | --- | --- | --- |
| table | `can_drop` = owner | `POST /v1/table/{id}/drop` | Lance `drop_table` removes the dataset | revoked |
| table | `can_deregister` = owner | `POST /v1/table/{id}/deregister` | **nothing** — detach only, by design | revoked |
| namespace | `can_delete` = owner | `POST /v1/namespace/{id}/drop` (`behavior=cascade`) | its tables' bytes via their drops | cascade-revoked, with a pagination ceiling so an incomplete walk cannot silently orphan grants |
| **warehouse** | — | **none** (only `deactivate` = quarantine) | — | **kept forever** |
| **project** | `can_administer` exists in the model | **none** | — | **kept forever** |

So a tenant can be frozen but never removed, and nothing reclaims a deactivated warehouse's tuples,
bindings or bucket.

## 2. Deletion — the design

**One rule: a container cannot be deleted while it still holds things.** Deletion is bottom-up, and
every level takes an explicit `cascade` to do it in one call. Refusals are 409 and **list what is
still inside** — a refusal that does not say what is blocking it just moves the search to the user.

### `DELETE /v1/warehouses/{id}` — new

- **Gate:** `can_administer` on the warehouse's **project** (owner tier). Deleting storage is a
  tenant-level act, not a namespace-level one.
- **Refuses 409** while any namespace is bound to it, naming them.
- **`?cascade=true`** drops those namespaces first (each already cascades to its tables).
- **Then:** revoke the warehouse's tuples, delete its binding records, mark the registry record
  deleted.
- **The BUCKET is not deleted.** `?purge_bucket=true` is a separate, explicit opt-in. Dropping a
  catalog entry is recoverable; deleting a customer's bucket is not, and the two must never share a
  default. A deleted-but-unpurged bucket is reported by the reconciler (below) rather than silently
  orphaned.

### `DELETE /v1/projects/{id}` — new

- **Gate:** `can_administer` on the project.
- **Refuses 409** while the project holds any warehouse, naming them. No `cascade` here: deleting a
  project must never be able to delete buckets transitively in one request.
- **Then:** revoke the project's tuples — admins, members, team edges — so no dangling subject keeps
  a grant on a tenant that no longer exists.

### Why not soft-delete everywhere

`deactivate` already covers "stop the bleeding" for a warehouse and is the right first step in
offboarding. What is missing is the second step. Adding delete does not remove `deactivate` —
quarantine → verify nothing breaks → delete is the intended sequence.

## 3. GC — the gap that has no owner

`drop` removes a dataset and `deregister` deliberately does not. Neither leaves *Lance* orphans.
What DOES leave unreferenced files:

- a partially-failed write (fragments written, commit never landed);
- a bucket whose warehouse record was deleted without `purge_bucket`;
- anything written outside the catalog into a managed bucket.

**Nothing reclaims these.** The compaction sweep runs `compact_files` +
`cleanup_old_versions` + `optimize_indices`; there is no "remove orphan files" pass, and the phrase
"remove orphans" appears nowhere in `services/compaction`. This is the honest hole behind "who
handles the deletion and recycling".

**Proposed:** a `reconcile` pass in the compaction service that lists a warehouse's bucket, subtracts
every file any live dataset version references, and reports the remainder — **report-only first**.
An orphan reclaimer that deletes on its first run, against a rule nobody has validated, is how a
maintenance job eats live data.

## 4. Table maintenance — what is actually covered

All three operations exist, in ONE pass (`compaction.services.optimize.compact_one`):

| Operation | Implementation | Tested |
| --- | --- | --- |
| **compaction** | `ds.optimize.compact_files(defer_index_remap=True)`, falling back to plain compaction when the dataset has no `row_addrs` | ✅ unit + a real-dataset sweep |
| **index updates** | `ds.optimize.optimize_indices()`, counting USER indices only (the `__lance_frag_reuse` system index would otherwise report every compacted dataset as "index maintained" forever) | ✅ incl. the defer-remap interplay and the no-index case |
| **pruning / cleanup** | `ds.cleanup_old_versions(older_than, retain_versions, error_if_tagged_old_versions=False)` — tags are EXEMPT, because the catalog creates long-lived promotion tags and the default `True` would permanently stall GC for that dataset | ✅ retention policy, tag/recent retention |

**32 unit tests green** across `test_compaction*.py` + `test_maintenance*.py`. The e2e
(`test_compaction_e2e.py`) needs a deployed stack and is skipped by default.

### What is NOT verified

- **Orphan-file reclamation** — does not exist (§3).
- **Reindex from scratch.** `optimize_indices()` folds new fragments into EXISTING indices. Nothing
  rebuilds an index whose parameters changed, and nothing reports an index that has drifted.
- **The sweep against a real S3/rustfs bucket** — the sweep tests use local dirs; only the skipped
  e2e touches object storage.
- **Multi-warehouse maintenance.** `discover_dataset_uris` walks a root; with per-warehouse buckets
  it must walk EVERY warehouse's root. Untested across more than one.

## 5. Order of work

1. `DELETE /v1/warehouses/{id}` with the empty-check, cascade and the separate bucket purge.
2. `DELETE /v1/projects/{id}` with the empty-check.
3. The reconcile/orphan REPORT pass, report-only.
4. Multi-warehouse sweep coverage (the maintenance job must see every tenant's bucket).
5. Only then consider making the reclaimer delete.

---

Delete this file when the work lands.
