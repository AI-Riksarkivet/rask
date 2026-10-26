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
| `lance/` | [`lance-format/lance`](https://github.com/lance-format/lance) `docs/src/` | `main` at `51cf89df7873d26269ba193fe971f439d3c031db` (2026-10-26) | citations across the estate |
| `ns_catalog/` | [`lance-format/lance-namespace`](https://github.com/lance-format/lance-namespace) `docs/src/` | `main` at `a13c746bdf369b62d4fc583357564f0cddfad667` (2026-10-23) | `tests/integration/test_spec_conformance.py` and criterion 2's `tests/e2e-py/test_the_catalog_is_correct.py` read `ns_catalog/spec.yaml` |

The site's Lance Reference pages (`docs/lance/file-format.md`, `guide.md`, `namespace.md`) embed every page of
`lance/format/`, `lance/guide/` and `ns_catalog/` in tree order, so a re-vendoring that adds or removes a page
regenerates those three lists.

**What is not vendored.** lance-ray's docs, LanceDB's rendered Python API reference and the blog posts on multi-base,
branching and blob v2 have no copy here. A claim once taken from them is cited to a page of these two trees, or to the
installed package (pylance's own docstrings, measured behaviour), or to lance-ray's upstream `docs/src/` by file name.

## These docs run ahead of the installed pylance

`lance/` is `main`, and the lock holds pylance 13.0.0. The docs therefore describe behaviour the installed package does
not have, and a claim taken from them is settled on pylance 13.0.0 before rask relies on it (CLAUDE.md: "or by
measuring on the installed pylance"). Measured examples of the gap:

- **The feature-flag table allocates eighteen flags** (`lance/format/table/versioning.md` § Current Feature Flags,
  `2^0` through `2^17`). pylance 13.0.0 allocates fourteen (`rust/lance-table/src/feature_flags.rs` at `v13.0.0`,
  `FLAG_UNKNOWN = 1 << 14`): `FLAG_MANAGED_BLOBS` (`2^14`, `lance/format/table/blob.md` § Capability and Compatibility),
  `FLAG_NON_REUSABLE_FIELD_IDS` (`2^15`), and the reserved `2^16` and `2^17` are newer than the installed release.
  `service_kit.lakehouse.features` names the fourteen pylance 13.0.0 allocates; a table carrying a newer one is refused
  as an unknown flag, which is the refusal the format asks of a reader that does not support it.
- **The table layout adds `_blobs/{UUID}.blob`** (`lance/format/table/layout.md` § Basic Layout), Managed blob
  payloads under `FLAG_MANAGED_BLOBS`. pylance 13.0.0 writes no such object, so the vend allow-list
  (`service_kit.lakehouse.table_layout.WRITER_PUT_GLOBS`) names none; a pylance bump that brings Managed blobs needs
  the directory judged there, and in the orphan scan, before a table may opt in.
- **The blob-v1 marker.** `lance/guide/data_types.md:43` says a 2.2 write rejects `lance-encoding:blob`, and
  `lance/guide/blob.md:65` that it is "Accepted as input; written as Blob v2"; pylance 13.0.0 does the second (measured
  2026-10-26, [[LH-246]]).
- **The cleanup default.** `lance/guide/read_and_write.md:550` says versions older than 7 days are removed by default;
  pylance 13.0.0's `LanceDataset.cleanup_old_versions` defaults `older_than` to 14 days (its source). Code that relies
  on the default cites the package.
- **`packed`.** `lance/format/file/encoding.md` § Compression Configuration says the knob takes any string; pylance
  13.0.0 packs a struct on `true`, `1`, `yes` or `on` in any case and ignores every other value, while a 2.0 file packs
  on any value (measured 2026-10-26, `catalog.core.encoding`).

### What a pylance bump re-checks

Read against these docs on 2026-10-26 (each item absent from pylance 13.0.0, measured or read off its binary):

- **Managed blobs** (`FLAG_MANAGED_BLOBS`, `_blobs/<uuid>.blob`, descriptor kinds 4 and 5): the vend allow-list has no
  `_blobs/*`; `client_fragments.verify_blob_sidecars` judges kinds 1-3 only; schema metadata `lance.blob.managed =
  "true"` turns the flag on for good, so a door either refuses the key or serves the feature; a kind-4 blob can outlive
  the data file whose stem names it, which `base_reclaim` and the orphan scan treat as ownership.
- **The inline transaction moves to manifest field 23** (`lance/format/table/transaction.md` § Inline Transaction
  Sections; field 21 becomes `transaction_section_deprecated`). `catalog.core.manifest_bytes` reads field 21 only, so a
  newer writer's staged commits would carry no transaction it can read, and every stock compaction's
  `ReserveFragments` would be refused at `version/create`.
- **`lance.row_lineage.spill`** stamps `FLAG_SPILLED_ROW_LINEAGE` (2^11) once a release spills on config; the table is
  then refused by every flag gate unless the key is refused at the doors or the feature is served.
- **Flags 2^14 to 2^17** need names in `service_kit.lakehouse.features` so a refusal names them; 2^15
  (`max_allocated_field_id`) changes how field ids are minted, which `vending.labelled_fields`,
  `encoding._encoding_by_field_id` and `client_fragments` compare by; 2^17 (object metadata) must survive a commit.
- **New encoding knobs** (`lance-encoding:alp`, `lance-encoding:axis-delta`, 2.3 writers): `catalog.core.encoding`
  refuses an unknown create property, so each is added to its knob table, with its values and the leaves it acts on,
  when the installed release honours it.
- **Address-domain BTree, Bitmap and LabelList segments** (`index_version` 1 or later store row addresses): ADR 0138's
  "compaction does not rewrite index files on a stable-row-id table" and the erasure's index surface are re-measured.
- **A BTree over `_rowid`** (field id -3): `version/create`'s `CreateIndex` judge has not met an index on a field
  outside the user schema.
- **`DataReplacement.data_change`**: a `data_change=false` repack is a compaction, which the lineage operation map
  (`DataReplacement` -> `UPDATE`) would mislabel.

## `ns_catalog/spec.yaml`

`test_every_spec_operation_is_served` checks that every operation in the vendored copy has a served catalog route.
Nothing checks the copy itself: not whether it still matches upstream, nor whether the pin above names the commit its
bytes came from. Comparing the copy with upstream is a network check, and no scheduled job runs one.

**The copy is two minors ahead of the installed SDK.** The lock holds `lance-namespace` 0.11.1, because pylance 13.0.0
declares `lance-namespace>=0.11.1,<0.12` (its PyPI `requires_dist`, read 2026-10-12). This copy carries `v0.12.0`'s
`merge_insert` `on` as a repeatable array (`eb1de88e`, `feat(spec)!: allow multiple columns for the merge insert on
key`) and `v0.13.0`'s optional `minimum_nprobes` / `maximum_nprobes` query fields. The catalog's `merge_insert` door
still takes `on` as a single key (`catalog/api/v1/endpoints/data.py`): the installed 0.11.1 model types it `str`, so a
list would fail inside `MergeInsertIntoTableRequest` rather than at the door. Both close with the SDK bump.

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

The guide says so itself (`lance/guide/blob.md:23-26`: "Append and compaction can select another exact `2.x` version
without rewriting existing files or changing that default"; `lance/guide/data_types.md:42` keeps the older "fixed once
created" line for the default). Measured on pylance 12.0.0, 2026-09-25, on local tables at 2.1 and 2.2 with stable row
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

`service_kit.lakehouse.features.describe_foreign_data_file_versions` names the files that would set it.

### The thread pools

`lance/guide/performance.md:112-114` holds for the pool named `lance-cpu`. The pool that tracks the CPU count is
`lance_background`, and neither variable sizes its base. Measured on pylance 12.0.0, 2026-09-25, counting a fresh
process's threads by name (`/proc/self/task/*/comm`) after it writes a 200,000-row local table and scans it once; each
row is the range over 5 fresh processes. `taskset -c` sets affinity, `systemd-run --user --scope -p CPUQuota=<n>%` sets a
cgroup quota, and `cpu.max` is read from inside the process:

| Visible CPUs | `cpu.max` | `lance_background` | `lance-cpu` |
| --- | --- | --- | --- |
| 4 | `max 100000` | 6-7 | 3 |
| 16 | `max 100000` | 18-19 | 3 |
| 64 | `max 100000` | 66-67 | 3 |
| 64 | `400000 100000` (4 CPUs) | 6-9 | 3 |
| 64 | `100000 100000` (1 CPU) | 3-4 | 2 |

- **`lance_background` is the available CPUs plus 2 to 5, and "available" counts the cgroup quota as well as
  affinity.** Neither `LANCE_CPU_THREADS` (1, 8, 64) nor `LANCE_IO_THREADS` (2, 64) moves that base: 65-67 after the
  write at 64 CPUs, over 3 runs of each. The pool also spawns threads on demand, and `LANCE_IO_THREADS` scales that
  growth: scanning a 3,000,000-row, 15-file local table added 10-16 threads by default, 1-3 at `LANCE_IO_THREADS=2` and
  13-37 at `=64` (3 runs each).
- **`lance-cpu` is the pool `LANCE_CPU_THREADS` governs, in both directions.** It spawns on demand, so a light workload
  shows 2-3 threads at any size and a heavy one varies widely between runs. Over 3 runs each of an IVF_PQ build over
  200,000 x 128 float32 vectors, it reached 3-27 threads at 64 CPUs unset, 2 at `LANCE_CPU_THREADS=1`, 5-9 at `=8`, and
  3-31 at `=32` on 4 CPUs: the variable, not the CPU count, sets the ceiling. Under the 1-CPU quota it stayed at 2 unset
  and reached 7-9 at `=8`. This is LD36 of `docs/audits/2026-09-25/03-lance-docs-full-audit.md` ([[LH-250]]); the thread
  counts [[LH-172]] reads as the compute pool are `lance_background`'s.
- **A cgroup quota bounds both Lance pools; the 64-wide pools in a 64-CPU, 1-CPU-quota process are not Lance's.** Under
  that quota, in 3 of 3 runs, `import lance` (which loads numpy and pyarrow) adds 64 threads before Lance spawns any, and
  numpy's OpenBLAS starts 63 of them, sized to the visible CPUs with `OMP_NUM_THREADS` unset. pyarrow's CPU pool reports
  `pa.cpu_count() == 64`, and importing pyarrow and running a threaded Parquet read added 68 threads.

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
