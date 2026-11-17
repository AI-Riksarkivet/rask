# `lance_docs/` — where these files came from

This tree is VENDORED upstream documentation. It is cited as the authority for what is idiomatic to Lance and
lance-namespace, so a reader has to be able to tell which version of the contract they are reading.

**Re-vendoring is how a contract change reaches this repo.** Upstream ships breaking changes under an unchanged
`info.version` (the `spec.yaml` entry below), so "1.0.0 both sides" is not evidence of agreement.

## Two upstream trees, verbatim

Each is the upstream `docs/src/` directory as it stands at the commit named, every file kept (images included), nothing
edited; the repo's pre-commit hooks trim trailing whitespace and add final newlines.

| Directory | Source | Pinned at | Read by |
| --- | --- | --- | --- |
| `lance/` | [`lance-format/lance`](https://github.com/lance-format/lance) `docs/src/` | `main` at `a254af62a491335861b77c589d55e6966c6b8511` (2026-11-17) | citations across the estate |
| `ns_catalog/` | [`lance-format/lance-namespace`](https://github.com/lance-format/lance-namespace) `docs/src/` | `main` at `a13c746bdf369b62d4fc583357564f0cddfad667` (2026-10-23) | `tests/integration/test_spec_conformance.py` and criterion 2's `tests/e2e-py/test_the_catalog_is_correct.py` read `ns_catalog/spec.yaml` |

The site's Lance Reference pages (`docs/lance/file-format.md`, `guide.md`, `namespace.md`) embed every page of
`lance/format/`, `lance/guide/` and `ns_catalog/` in tree order, so a re-vendoring that adds or removes a page
regenerates those three lists.

**What is not vendored.** lance-ray's docs, LanceDB's rendered Python API reference and the blog posts on multi-base,
branching and blob v2 have no copy here. A claim once taken from them is cited to a page of these two trees, or to the
installed package (pylance's own docstrings, measured behaviour), or to lance-ray's upstream `docs/src/` by file name.

## These docs run ahead of the installed pylance

`lance/` is `main`, and the lock holds pylance 14.0.3. The docs therefore describe behaviour the installed package does
not have, and a claim taken from them is settled on pylance 14.0.3 before rask relies on it (CLAUDE.md: "or by
measuring on the installed pylance"). Measured examples of the gap:

- **The feature-flag table allocates twenty flags** (`lance/format/table/versioning.md` § Current Feature Flags,
  `2^0` through `2^19`). pylance 14.0.3 allocates eighteen (its release notes: flags `2^14` to `2^17` allocated,
  `FLAG_UNKNOWN = 1 << 18`), so `FLAG_ROW_ADDR_INDICES` (`2^18`) and `FLAG_INLINE_DELETIONS` (`2^19`) are newer than the
  installed release. `lakehouse.features` names all twenty and supports none above `2^13`; a table carrying one is
  refused by name, which is the refusal the format asks of a reader that does not support it.
- **The table layout adds `_blobs/{UUID}.blob`** (`lance/format/table/layout.md` § Basic Layout), Managed blob
  payloads under `FLAG_MANAGED_BLOBS`. pylance 14.0.3 writes them: a table created with schema metadata
  `lance.blob.managed = "true"` carries flags `(16386, 16386)` and a `_blobs/` directory (measured 2026-11-17). The
  catalog serves none: every door refuses the key (`catalog.core.formats.FLAG_LOCKING_KEYS`), every flag gate refuses
  `2^14`, and the vend allow-list (`lakehouse.table_layout.WRITER_PUT_GLOBS`) names no `_blobs/*`. Serving them needs
  the directory judged there, and in the orphan scan, before a table may opt in.
- **Repacking a fragment's columns** (`lance/guide/read_and_write.md` § Repack columns: `max_data_files_per_fragment`,
  `scope`, `dataset.stats.column_layout_stats()`, and a compaction that commits two versions). pylance 14.0.3's
  `compact_files` takes neither argument and its stats object has no such call (measured 2026-11-17).
- **`packed`.** `lance/format/file/encoding.md` § Compression Configuration says the knob takes any string; pylance
  13.0.0 packs a struct on `true`, `1`, `yes` or `on` in any case and ignores every other value, while a 2.0 file packs
  on any value (measured 2026-10-26, `catalog.core.encoding`).

### Pages that the installed pylance contradicts

Measured 2026-11-12 on pylance 13.0.0 and lance-namespace 0.11.1, on local datasets. Each is a page to read with its
correction, and the correction is what rask relies on. Re-measured on pylance 14.0.3 and lance-namespace 0.13.0,
2026-11-17: the shallow clone's base paths, a branch's own data files, the error codes, the missing `.proto`, the
branch ref file's `version_mapping`, cleanup's refusal of a tagged version, the `compaction_mode` spellings,
`LANCE_FTS_TARGET_SIZE` and the percent-encoded ref file name hold as written. `lance.compaction.index_remap_mode`,
the copied dataset and the absent fragment tree were not re-measured.

- **A shallow clone's base paths** (`lance/format/table/layout.md` § Shallow Clone gives the clone root at id 0 and the
  source at id 1, named after its tag, with new files at `base_id: 0`). `shallow_clone` writes one base path, id 0, with
  no name, naming the source's root even when cloned from a tag; inherited files carry `base_id` 0 and the clone's own
  files carry none and resolve under the clone's root.
- **A branch's own data files** (`lance/format/table/branch_tag.md` § Branch layout lists `_versions`, `_transactions`,
  `_deletions` and `_indices`). A written branch holds `tree/<branch>/data/*.lance` too, and its base path 0 is the
  parent's root as an absolute path, through which its inherited files resolve.
- **A copied dataset** (`lance/format/table/layout.md`: a copied dataset "will function immediately"). Holds for a
  dataset with no base paths: after copying a root and removing the original, main reads, and a branch (or a shallow
  clone) fails `Not found` on its inherited files, which resolve through the original root's absolute path.
- **The error codes** (`ns_catalog/namespace/operations/errors.md` lists 0 to 21). `spec.yaml` and lance-namespace
  0.13.0 define 22 (`TableBranchNotFound`) and 23 (`TableBranchAlreadyExists`); cite `spec.yaml`.
- **Unrendered message tables.** `lance/` holds 71 placeholders (69 `%%% proto.message.* %%%`, 2
  `%%% mem_wal.message.* %%%`), and pylance 14.0.3 ships no `.proto`, so a message's field numbers are measured or read
  off the binary, never off these pages (`lakehouse.manifest_wire`).
- **A branch's ref file** (`lance/format/table/branch_tag.md` § Branch File Format). `_refs/branches/<branch>.json` also
  carries `identifier.version_mapping`, a list of `[version, uuid]` pairs, which the page's field table omits.
- **`lance.auto_cleanup.delete_concurrency`** (`lance/guide/read_and_write.md`). pylance 14.0.3's
  `cleanup_old_versions` takes `delete_concurrency`, as the page says; whether the table config key is parsed was
  measured on 13.0.0 (never) and not since.
- **Tagged versions and cleanup** (`lance/guide/read_and_write.md`: tagged versions are exempt). `cleanup_old_versions`
  refuses the whole call when an old version is tagged unless `error_if_tagged_old_versions=False`; with it, it cleans
  the untagged versions and keeps the tagged one. Every age-bounded reclaim rask runs passes it (maintenance's expiry,
  `maintenance/run`, the erasure's, the namespace index's); `version/delete` names its versions and passes `True`, so a
  tagged one refuses the range whole.
- **`lance.compaction.index_remap_mode`** (`lance/format/table/table_config.md`: `direct` or `merged`) accepts `direct`
  and `compact`, and **`lance.compaction.compaction_mode`** takes `reencode`, `try_binary_copy` and `force_binary_copy`
  where the page spells them with hyphens. A wrong value is refused when a compaction runs, not when it is stored; every
  door refuses a `lance.compaction.*` key ([[LH-538]]).
- **The `fmindex` scalar index** (`lance/format/index/scalar/fmindex.md`): pylance 14.0.3's `create_scalar_index` names
  no such type, so its layout does not appear under `_indices/`. `MINHASHLSH` builds on 14.0.3.
- **The fragment tree** (`_bt/`, flag 4096, listed in `lance/format/table/layout.md` § Basic Layout): pylance 13.0.0
  wrote none, and no write measured on 14.0.3 set the flag, as its own page says of released Lance, so the vend allow-list and the orphan scan name no `_bt/`.
- **`LANCE_FTS_TARGET_SIZE`** (`lance/format/index/scalar/fts.md` § Configuration) is not read; the other documented
  `LANCE_FTS_*` settings are.
- **Branch and tag names** (`lance/format/table/branch_tag.md`). A branch's ref file percent-encodes every character a
  file name cannot hold as it stands (`ä` is `%C3%A4.json`), not only `/`; Unicode letters are accepted in branch and
  tag names; a branch name may start or end with `.`. The catalog's create door holds a branch to its own rule
  (`lakehouse.branch_layout`, [[LH-203]]).

### Measured on pylance 14.0.3 and lance-namespace 0.13.0

For LH-595 (#831), on local datasets, 2026-11-15 and 2026-11-17. What changed from 13.0.0, and what rask does about it:

- **`read_transaction` raises `TypeError` on a `Rewrite` that rewrote an index** (lance-format/lance#11117, the fix
  proposed in #11118). pylance builds `LanceOperation.RewrittenIndex` from two of its five fields
  (`python/src/transaction.rs`). Every reader of a transaction goes through `lakehouse.transactions.read_transaction`,
  which reads the three as absent; `packages/lakehouse/tests/test_a_rewrite_that_rewrote_an_index_reads_back.py` goes
  red the day a release reads it back unaided.
- **A compaction of a stable-row-id table rewrites a BTREE index** under a new uuid (BITMAP, LABEL_LIST and INVERTED
  are not rewritten). A materialising compaction leaves a deleted row's key out of the rewritten BTREE; a BITMAP
  segment keeps it after cleanup. `optimize_indices()` after a delete commits nothing. ADR 0138's rule, drop or
  rebuild every index, stands.
- **A compaction on a branch that inherited a BTREE index commits a version no reader can open**: the rewritten
  index's files are written under `tree/<branch>/_indices/<uuid>/` and its entry still names base 0, the parent's
  root, so a query the index serves fails `Not found`. Not filed upstream. `version/create` refuses the commit
  (`managed_versions._refuse_an_index_read_elsewhere`), pinned by
  `test_a_compaction_that_would_leave_a_branchs_inherited_index_unreadable_is_refused`, and an erasure job drops the
  index before it compacts.
- **The `dir` backend's `insert_into_table` with `branch=work` appends to main** and answers main's version; on main it
  answers the row count and the version. Not filed upstream. The catalog's insert door commits through a handle opened
  on the ref.
- **`DirectoryNamespace.namespace_exists` on a table's id raises `NamespaceNotFoundError`**, where 13.0.0 answered
  present.
- **The bundled REST client sends POST** for `count_rows`, `tags/list` and index stats, as `spec.yaml` says, so the
  catalog serves each spec operation at the spec's method only. The client implements 51 of the 54 operations; the
  three it leaves to the base class are `update_field_metadata`, `batch_create_table_versions` and `batch_commit_tables`.
- **pylance writes managed blobs**, which the catalog refuses (above): schema metadata `lance.blob.managed = "true"` at create stamps `(16386, 16386)` and
  writes `_blobs/`. No other create, append, delete, update, merge, schema change, index build, compaction, tag, branch
  or config write sets a flag above `2^13`, and `lance.row_lineage.spill` with an append stamps none (`(2, 10)`).
- **The inline transaction is written at manifest field 23 only**: the field holds 0 on an Append's manifest and 151
  on a CreateIndex's, field 21 is absent from both, and `catalog.core.manifest_bytes` reads the transaction from each
  (it reads field 23 first and field 21 after it).
- **A write that names neither** writes file version 2.2 with no stable row ids (`write_dataset(table, uri)`: flags
  `(0, 0)`). Every create the catalog makes names both (`lakehouse.features.CREATE_FORMAT`).
- `cleanup_old_versions` takes `delete_concurrency`; `compact_files(defer_index_remap=True)` is refused on a
  stable-row-id table; `MINHASHLSH` builds.
- lance-namespace 0.13's `MergeInsertIntoTableRequest.on` and the urllib3 client's `merge_insert_into_table` take the
  key as a list; a string is refused by the model.

Held as before, measured again on 14.0.3: an append at another file version stamps flag 256 (`(258, 258)`), and
lancedb 0.41.0 opens such a table; `update`, `delete`, `add_columns`, `alter_columns`, `drop_columns`, `drop_index`,
`compact_files` and `optimize_indices` take no transaction properties; `LanceDataset.commit` keeps a caller's
transaction uuid; `cleanup_old_versions` deletes `_transactions/` files and refuses a tagged old version unless
`error_if_tagged_old_versions=False`; a same-type `alter_columns` re-mints the field id and drops its metadata; Lance
accepts dropping the declared primary key; `register_table` at an absent location is accepted and keeps no properties;
`tags.get_version` answers the version alone; `LanceFileReader` cannot project a file holding a blob-v2 column;
`add_bases` writes `is_dataset_root` as given; a branch's ref file is percent-encoded and carries
`identifier.version_mapping`; the cleanup floor keeps `_transactions/` and `_deletions/` files above it; a read of an
overlapping index's statistics still migrates the table unless `LANCE_AUTO_MIGRATION` is `false`.

Held by tests that drive pylance 14.0.3 itself and pass on it: managed versioning's detached copy, the finalize's
put-if-not-exists and a lost commit's outcome
(`services/catalog/tests/test_a_stock_client_commits_every_operation_through_version_create.py`,
`test_a_lost_commit_race_is_retryable_not_a_server_fault.py`), flag 16 on a shallow clone
(`tests/unit/test_maintenance_features.py::test_a_shallow_clone_sets_the_base_paths_flag`), `tracked_files` over old
versions (`tests/unit/test_orphan_files.py::test_files_only_an_OLD_version_references_are_not_orphans`) and the
maintenance index readers (`tests/unit/test_index_health.py`). `explain_cleanup_old_versions` counts the versions a
cleanup at the same age would remove and leaves a tagged one out (measured: 4 of 6 with one tagged). Flag 64 (data
overlay files) is set by no release build, so nothing writes it to measure.

Not possible through pylance 14.0.3, so not judged: a BTree over `_rowid` (`KeyError`), and
`DataReplacement.data_change` (the Python operation carries `replacements` only).

**Not re-measured on 14.0.3.** These are still as measured on 12.0.0 or 13.0.0, and each inline comment names its
release: the timings and thread-pool figures (the retry budgets against a store that drops or never answers, the
124.9 s and 182.6 s bounds, the memory peaks, § The thread pools below); the races (a tag created during a cleanup,
concurrent renames, row-id order under racing appends, the merge that drops its key check, § Transaction properties);
what needs a counting or faulting store (the request counts of a pass and of an open, the store-fault message texts,
endpoint scheme handling); the crash measurements (a pyarrow offset past its buffer, 150,000 OR terms); the spelling
hazards `lakehouse.base_refs` refuses; and every `rust/...` source line cited at `v12.0.0` or `v13.0.0`.

## `ns_catalog/spec.yaml`

`test_every_spec_operation_is_served` checks that every operation in the vendored copy has a served catalog route.
Nothing checks the copy itself: not whether it still matches upstream, nor whether the pin above names the commit its
bytes came from. Comparing the copy with upstream is a network check, and no scheduled job runs one.

**The copy is at the installed SDK's minor.** The lock holds `lance-namespace` 0.13.0, because pylance 14.0.3 declares
`lance-namespace>=0.13.0,<0.14` (its PyPI `requires_dist`, read 2026-11-15). This copy carries `v0.12.0`'s
`merge_insert` `on` as a repeatable array (`eb1de88e`, `feat(spec)!: allow multiple columns for the merge insert on
key`) and `v0.13.0`'s optional `minimum_nprobes` / `maximum_nprobes` query fields, and the installed model carries
both: the catalog's `merge_insert` door reads `on` as that array into the request (LH-537,
`catalog/api/v1/endpoints/data.py`).

**The namespace prose is not the route authority; `spec.yaml` is.** `ns_catalog/namespace/supported-catalogs/lance-rest.md`
gives operations at paths the OpenAPI contradicts (read 2026-10-26 on this copy, lines 707-933); measured 2026-09-20,
16 of the 47 operations the prose documented were at such paths (`.../index/create` for `create_index`, `.../tag/create` for `tags/create`, `.../count` for `count_rows`,
`.../merge-insert` for `merge_insert`, `.../schema/metadata` for `schema_metadata/update`, `query/analyze` and
`query/explain` for `analyze_plan` / `explain_plan`), while `spec.yaml`, the generated
`lance_namespace_urllib3_client` and the deployed catalog agreed on 54 paths. Cite `spec.yaml` for a route; the prose
is for vocabulary, error codes and semantics.

The per-model pages under `ns_catalog/namespace/operations/models/` are generated from the spec upstream; where one and
`spec.yaml` disagree, `spec.yaml` is the one the conformance test reads.

## Measured against the installed release

### A dataset's default file format version is fixed at create; each data file's is not

The guide says so itself (`lance/guide/blob.md:24-27`: "Append and compaction can select another exact `2.x` version
without rewriting existing files or changing that default"; `lance/guide/data_types.md:51` calls it the dataset's default). Measured on pylance 12.0.0, 2026-09-25, on local tables at 2.1 and 2.2 with stable row
ids:

- An append (`write_dataset(mode="append")` or a committed `LanceOperation.Append`), or a committed
  `LanceOperation.Overwrite`, whose files were written at ANOTHER version keeps the manifest's version, writes the files
  at the named one, and sets reader and writer flag 256 (`FLAG_MIXED_DATA_FILE_VERSIONS`,
  `lance/format/table/versioning.md` § Current Feature Flags). The flag is sticky: it survived deleting the foreign
  rows, a `write_dataset` overwrite and `compact_files` back to a single file version.
- `merge_insert`, `delete`, `add_columns`, `update`, `compact_files` and an overwrite that names no version keep the
  manifest's version. `write_dataset(mode="overwrite", data_storage_version=X)` moves it to X; it does not set flag 256,
  and on a table that already carries the flag it does not clear it.
- From pylance 13.0.0, `update()`, `merge_insert().data_storage_version()` and `compact_files()` also take a
  `data_storage_version`, which can stamp the flag: measured 2026-10-12 (only `update` was run),
  `update(data_storage_version="2.1")` on a 2.2 table leaves flags `(258, 258)`. No rask code passes it, and a stock
  client's `update` is judged at `version/create`, which refuses the files that would set it.

`lakehouse.features.describe_foreign_data_file_versions` names the files that would set it.

### The thread pools

`lance/guide/performance.md:118-135` names two pools, and both hold as written. The IO pool's width is the object store's
(8 on a local store, 64 on a cloud one, `LANCE_IO_THREADS` to override), and the compute pool is the tokio runtime
`lance-cpu`, which `LANCE_CPU_THREADS` sizes. Every pool Lance sizes by CPU count counts the cgroup quota, so the
lakehouse sets no Lance thread variable ([[LH-250]]). The pools in a lakehouse process that size themselves to the
visible CPUs instead are numpy's OpenBLAS and pyarrow's CPU pool, which the image runs one thread wide
(`OMP_NUM_THREADS=1` in `.docker/rest-catalog.dockerfile`, measured at build by `.docker/pool-gate.py`). The CPU-sized
pools, from the pylance 13.0.0 source (tag `v13.0.0`):

- **`lance-cpu`** (`rust/lance-core/src/utils/tokio.rs:78-87`): one worker and up to N blocking threads, kept for the
  life of the process. N is `LANCE_CPU_THREADS`, otherwise `num_cpus::get()` less `LANCE_IO_CORE_RESERVATION` (2), and 1
  on two CPUs or fewer (`:19-43`); `num_cpus::get()` counts the cgroup quota.
- **`lance_background_thread`**, pylance's executor (`python/src/executor.rs:44-48`; `lance_backgroun` in
  `/proc/*/task/*/comm`, which keeps 15 characters): a default tokio runtime, one worker per available CPU, plus blocking
  threads started on demand that end after 10 s idle. Under load its count follows the work's concurrency, not the CPUs.
- **rayon's global pool**, on unnamed threads, which carry the process name: an index build's compute runs there, not on
  `lance-cpu`. With `RAYON_NUM_THREADS=1`, an IVF_PQ build that took 6 s ran on one thread for 48 s.

Measured on pylance 13.0.0, numpy 2.4.6 and pyarrow 25.0.0, 2026-10-31, on a 20-CPU aarch64 host. Each row is one fresh
process per run that writes a 200,000 x 128 float32 local table, scans it, builds an IVF_PQ index (16 partitions) and idles
12 s, its threads counted by name (`/proc/self/task/*/comm`) every 20 ms; each cell is the range over 3 runs. The quota is
`systemd-run --user --scope -p CPUQuota=<n>%`:

| Quota | Variable set | `lance-cpu` peak | `lance_background` after the scan / during the build / idle | OpenBLAS workers started by `import numpy` | `pa.cpu_count()` | Build CPU/wall |
| --- | --- | --- | --- | --- | --- | --- |
| none (20 CPUs) | none | 12-14 | 21 / 29-31 / 20 | 19 | 20 | 12.7-12.8 |
| 1 CPU | none | 2 | 2 / 11-16 / 1 | 19 | 20 | 1.0 |
| 1 CPU | `OMP_NUM_THREADS=1` | 2 | 2 / 10-13 / 1 | 0 | 1 | 1.0 |
| 4 CPUs | none | 3 | 5 / 15-19 / 4 | 19 | 20 | 3.6 |
| none | `LANCE_CPU_THREADS=1` | 2 | 21 / 30 / 20 | 19 | 20 | 10.8-11.0 |
| 1 CPU | `LANCE_CPU_THREADS=8` | 9 | 2 / 10-12 / 1 | 19 | 20 | 1.0 |

- **`LANCE_CPU_THREADS` moves `lance-cpu` in both directions, and the work that runs there with it.** Compacting 64
  fragments of 4,000 rows into one took 0.2-0.3 s at 2.0-2.1 CPU/wall unset and 1.5 at `LANCE_CPU_THREADS=1`, its CPU
  split between `lance-cpu` and `lance_background` (no quota, 2 runs each). It bounds no other pool: at 1 the IVF_PQ
  build still ran at 10.8-11.0 CPU/wall, on rayon's threads. A quota bounds all three Lance pools: rayon's ran the build
  on 20, 1 and 4 threads with no quota, a 1-CPU and a 4-CPU quota (1 run each). The lakehouse builds no index and
  compacts no table (ADR 0162); a bring-your-own job's process runs those pools.
- **An idle `lance_background` is its workers, one per CPU of the quota.** The 10-19 during the build are blocking
  threads, so a count of a process under load includes them, whatever its quota.
- **OpenBLAS and pyarrow ignore the quota**: 19 workers and a 20-wide pool under a 1-CPU quota, as with none. Both read
  `OMP_NUM_THREADS`.

Measured on pylance 12.0.0, 2026-09-25, on a 64-CPU host, by thread name after a fresh process writes a 200,000-row
local table and scans it once (5 runs each): with no quota, `lance_background` held 6-7, 18-19 and 66-67 threads at 4,
16 and 64 visible CPUs (`taskset -c`), and neither `LANCE_CPU_THREADS` (1, 8, 64) nor `LANCE_IO_THREADS` (2, 64) moved
that base. Its threads started on demand follow the IO pool: scanning a 3,000,000-row, 15-file local table added 10-16
of them by default, 1-3 at `LANCE_IO_THREADS=2` and 13-37 at `=64` (3 runs each). On the estate's S3 store the IO pool
is 64 wide by default; how that shows in `lance_background` there is not measured.

### Transaction properties (pylance 13.0.0, 2026-10-14)

What [[LH-225]] relies on was measured:

- **They persist with the commit.** `write_dataset(..., transaction_properties=...)`, `LanceDataset.insert(...,
  transaction_properties=...)` (passed through to `write_dataset`) and `LanceDataset.commit(Transaction(...,
  transaction_properties=...))` each write them into the version's transaction, and
  `read_transaction(v).transaction_properties` returns them. A stock client committing through the namespace under
  `managed_versioning` carries them in the transaction its staged manifest names.
- **`update`, `delete` and `add_columns` take none**, and neither does `MergeInsertBuilder.execute`; a merge can carry
  them only through `execute_uncommitted` and `LanceDataset.commit`.
- **That path drops the merge's key check under a race.** The Python `LanceOperation.Update` has no inserted-rows
  filter, so `LanceDataset.commit` rebases an `execute_uncommitted` merge over a writer that landed first without
  re-checking keys: two racing `when_matched_fail().when_not_matched_insert_all()` inserts of one key both committed,
  with `max_retries=0` as well, where `execute` refuses the second. A detached commit finalized at exactly the next
  version through the `dir` backend's `create_table_version` loses the race instead (`ConcurrentModificationError`,
  "Version 2 already exists ... with different content"), and re-planning sees the winner's rows.
- **`cleanup_old_versions` deletes `_transactions/`**, so a reader of the stamp must run inside version retention.

## Re-vendoring

```bash
D=$(mktemp -d)
git -C "$D" clone -q --depth 1 --filter=blob:none --sparse https://github.com/lance-format/lance.git lance
git -C "$D/lance" sparse-checkout set docs/src
git -C "$D" clone -q --depth 1 --filter=blob:none --sparse https://github.com/lance-format/lance-namespace.git ns
git -C "$D/ns" sparse-checkout set docs/src
rm -rf lance_docs/lance lance_docs/ns_catalog && mkdir -p lance_docs/lance lance_docs/ns_catalog
cp -a "$D/lance/docs/src/." lance_docs/lance/ && cp -a "$D/ns/docs/src/." lance_docs/ns_catalog/
git -C "$D/lance" log -1 --format='%H %cs'; git -C "$D/ns" log -1 --format='%H %cs'
uv run pytest tests/integration/test_spec_conformance.py -q
```

Then, in the same commit: update the two pins above (a pin that names a different commit than the bytes is worse than
no pin), regenerate the page lists of `docs/lance/{file-format,guide,namespace}.md`, re-point every citation whose page
or line moved (a line citation names the same text in the new copy), and re-read the "ahead of the installed pylance"
list against the lock.
