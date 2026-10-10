# 0018. Lance-spec landmines

**Decision.** Format-spec constraints any catalog/pipeline code must honor (each a silent footgun):

- `enable_stable_row_ids` is **create-time-only** (silently no-ops later) → verify the `FLAG_STABLE_ROW_IDS`
  bit rather than trusting the request.
- `data_storage_version` is **immutable per dataset**; 2.2 is required for blob-v2 (why blob-create stays
  server-side / centralized).
- Secondary indices reference **row address, not `_rowid`**; compaction invalidates them
  (stable-row-id-for-index is experimental).
- The conflict matrix is **per-op**: `Append`↔`Append` auto-rebases, `Overwrite`/`Restore` do not — the
  commit retry loop must classify the error, not blindly retry.
- **Ref-plane mutations (tag/branch create) emit no version** → invisible to a version-tailing outbox.
- Implement to the **model files, not the prose** (`RenameTableRequest.new_table_name` /
  `new_namespace_id`, never `new_id`).

**Rationale.** Each is a case where the wrong assumption passes tests but corrupts a property — a wrong-version
table must be recreated, an invalidated index returns wrong rows, an un-tailed ref mutation is lost lineage.
Recorded so nobody "cleans them up" back into the trap.
