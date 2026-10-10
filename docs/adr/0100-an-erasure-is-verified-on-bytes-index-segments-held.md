# 0100. An erasure is verified on bytes: index segments, held surfaces and the branch copy cap (LH-263, 2026-10-05)

`erase()` reported `complete: true` while the subject sat in files no step reached. Measured on pylance 12.0.0:
a BTREE or BITMAP segment keeps its UUID and the subject's key through the materialising rewrite (its fragment
bitmap is remapped onto the new fragment) and through `cleanup_old_versions(0)`; an external blob-v2 payload under
a registered base survives the delete, the rewrite and the cleanup byte for byte; a row put through a MemWAL shard
writer sits in `_mem_wal/<shard>/wal/` and its flushed generation while the base table counts zero rows for it.

Now every rewritten ref rebuilds each user index from its own description (`index_specs.describe_index_for_rebuild`,
built through `maintenance.rebuild_index_now` with `replace=True`); `optimize_indices(retrain=True)` is no
substitute, since it left every scalar segment's UUID unchanged. A ref whose rebuild fails is not reclaimed. The
verification proves a segment by provenance, because a clean data probe proves nothing about one: compaction
remaps a segment's fragment bitmap onto the rewritten fragment and keeps the deleted row's key, and an
`optimize_indices` merge of a BITMAP segment carries that key into the new segment (both measured). A segment is
proved when the erasure built it, or when a retained `CreateIndex` commit built it fresh (its `dataset_version`
equals the commit's read version, which a merge does not set) over exactly the fragments it covers in the version
probed, all still present, in a version whose data reads clean with deleted rows counted. A retained version
keeping any other segment with files on storage is a residual, named with what keeps it: a tag over a version
whose segment holds a key the delete door removed earlier is named in `pinned_by`, and a tag over an indexed
version the subject never touched keeps nothing. Files outside the table's own files are never touched and are
reported as `held` surfaces that keep `complete` false:
`external_base:<name>` for external payloads, `data_base:<name>` for a data file under a data base (whose rewrite
the compact gate refuses), `mem_wal` for any object under `_mem_wal/`. Pointers and data files are read from
every retained version of every ref, deleted rows included, before anything is reclaimed, because the subject is
usually deleted before it is erased (measured: a delete, or a delete and a compaction, before the erasure left the
external payload named by nothing on the head). A WAL entry is not rewritten because writer
fencing depends on its put-if-not-exists collisions. A table another dataset resolves its files through stays
refused (#114), its `compact:` and `history:` surfaces failed.

The branch copy (owner ruling, 2026-09-26): a branch's rewrite may copy the inherited fragments compaction
selects into the branch's own storage, capped at 64 MiB of source per ref per erasure and priced in each
`compact:<ref>` surface; that cap is the first pass's `COMPACTION_BOUND`. The fragments still holding the subject
are rewritten in a second pass, every other fragment excluded, with the bound raised to their size, on main and
on a branch alike: a cap below their size leaves the subject behind a deletion vector in a live file, which is
what an erasure exists to prevent. Owner ruling 2026-10-05 ("allow past the cap"): an erasure MAY rewrite past
the 64 MiB per-branch cap, which it supersedes for erasure, so on a branch that second pass copies the subject's
inherited fragments whatever their size, and the request takes as long as that rewrite does.

Not covered: a payload only an already-reclaimed version pointed at, which the table no longer records; an FTS
segment's tokens are rebuilt like any index but not observable by a byte search, so no test pins them; a segment
whose creating commit was reclaimed, or that a merge or delta built, has no provenance to read and keeps its
version residual even when it never held the subject.
