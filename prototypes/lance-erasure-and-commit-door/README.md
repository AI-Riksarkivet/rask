# Prototype: Lance erasure and commit-door measurements (pylance 12.0.0)

Throwaway scripts that answered two design questions by measurement rather than by reading: what an
erasure must do to leave no copy of a subject (LH-178, ADR 0138), and what a catalog commit door has
to carry (LH-330). Measured 2026-10-10 on pylance 12.0.0; every table is created with
`data_storage_version="2.2"` and `enable_stable_row_ids=True`, as rask's tiers are.

Run from the repo root: `uv run python -I prototypes/lance-erasure-and-commit-door/<script>.py`.
Scratch tables go to `$LANCE_PROTO_DATA` (default `<tmp>/lance-proto`), never into the repo. Each script
prints `lance.__version__` first. `common.py` holds the helpers (cleanup runs
`cleanup_old_versions(older_than=0, delete_unverified=True, error_if_tagged_old_versions=False)`);
`nsutil.py` builds a `dir` namespace and Arrow IPC bodies; `nsprobe.py` was a namespace API probe.

| Script | Measures | Result |
| --- | --- | --- |
| `a1_child_branches.py` | deleting a branch that has a child | refused: `Branch A is referenced by [("B", 2)]`; cleaning B's history does not lift it |
| `a1b_child_branch_release.py` | releasing a branch subtree | only leaf-first works: delete B, delete A, recreate A at main's head, recreate B from A; B's own commits are lost; main's cleanup never touches `tree/<branch>/` |
| `a2_index_files.py` | erased values in BTree and FTS index files | compaction + cleanup leave both index dirs with the subject; `defer_index_remap` is refused under stable row ids; `optimize_indices()` commits nothing; rebuild (`replace=True`) or `drop_index`, then cleanup, removes them |
| `a3_txn_files.py` | the delete predicate | stored in the delete version's `.txn` AND its manifest; removed only once a later version exists and cleanup runs |
| `a4_blob_v2.py`, `a4b_blob_dedicated.py` | blob-v2 sidecars and external blobs | compaction rewrites inline, packed and dedicated sidecars, and cleanup removes the old ones; external-URI blobs are never reclaimed (inside a registered base or outside) |
| `a5_restore.py` | restoring a pre-erasure version | restore brings the row back, even after compaction, until cleanup removes the version |
| `a6_tag_on_branch.py` | a tag on a non-main branch | allowed (`{"branch":"X","version":2}`); cleanup on X raises with `error_if_tagged_old_versions=True`, keeps it with False; deleting the tag then cleanup removes the data |
| `a7_shallow_clone.py` | a shallow clone after its source cleans up | the clone becomes unreadable (`Not found` on the source's data file) |
| `b3_b5_split_merge_rowid.py` | splitting a full-sync merge_insert; row identity | 3 range-split requests with `when_not_matched_by_source_delete_filt` equal the unsplit result, `_rowid`s included; without the filter a split deletes rows outside its range; matched rows keep their `_rowid` |
| `b4_column_adds.py` | adding a column | a merge_insert carrying a column the target lacks is refused; `add_columns` (1 Merge, no data file) then a merge_insert lands it: 2 commits |
| `b6_blob_v2_doors.py` | blob-v2 rows through the doors | IPC round-trips `lance.blob.v2`; native insert and merge_insert accept all three sizes; client-written fragments pass the catalog's `commit_appended_fragments` as an Append |
| `b7_commit_door_precedent.py`, `b7b_fts_finalize_files.py` | worker writes, coordinator commits | carries Append, Overwrite, Update, Merge, Delete, UpdateConfig, CreateIndex (BTREE) and Rewrite; FTS needs `commit_existing_index_segments`, which writes index files; a full-sync Update built before a concurrent append does not delete the appended row |

Not measured here (need the deployed estate): LH-330's transaction inventory, body-cap fit and token
threading through `/produce`.
