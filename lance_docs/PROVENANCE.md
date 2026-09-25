# `lance_docs/` — where these files came from

This tree is VENDORED upstream documentation. It is cited as the authority for what is idiomatic to
lance-namespace, so a reader has to be able to tell which version of the contract they are reading.
That is the only reason this file exists: before it, the six documents carried no source, no commit
and no date, and a citation from them could not be checked against anything.

**Re-vendoring is not a chore, it is how a contract change reaches this repo.** Upstream ships breaking
changes under an unchanged `info.version` — see the spec entry below — so "1.0.0 both sides" is not
evidence of agreement.

## Machine-checked

| File | Source | Pinned at | Checked by |
| --- | --- | --- | --- |
| `ns_catalog/spec.yaml` | [`lance-format/lance-namespace`](https://github.com/lance-format/lance-namespace) `docs/src/spec.yaml` | commit `eb1de88e3c7e77537ec35514c0ce8b4fdace5515` (2026-09-01) | `tests/integration/test_spec_conformance.py` |

Three gates read it, and they cover different failures:

- `test_spec_has_54_operations` — the vendored copy SHRINKING.
- `test_the_vendored_spec_still_matches_UPSTREAM` — upstream ADDING or RETIRING an operation.
- `test_the_vendored_spec_matches_upstream_on_PARAMETER_SHAPES_too` — an operation that upstream
  CHANGED. The first two are blind to it, and that is not hypothetical: commit `eb1de88e`
  (`feat(spec)!: allow multiple columns for the merge insert on key`, 2026-09-01) turned
  `merge_insert`'s `on` from a `string` into a repeatable array, and the op-id check reported "zero
  difference either way" across it on 2026-09-09 while the vendored copy sat 79 lines behind carrying
  the superseded shape.

The last two are network-gated and SKIP when upstream is unreachable — drift is measured in weeks, not
in the commit in front of you, and a gate that fails on an offline laptop teaches people to ignore it.

**A known, deliberate deviation rides on this file.** The catalog's `merge_insert` door still takes
`on` as a single key (`catalog/api/v1/endpoints/data.py`): the installed `lance-namespace` 0.11.0 model
types it `str`, so accepting a list would fail inside `MergeInsertIntoTableRequest` rather than at the
door. Vendoring the current spec makes that deviation VISIBLE rather than hidden behind a stale
document — which is the point. It closes when the SDK bump lands.

## Not machine-checked

These five are tool-generated concatenations of a whole documentation tree ("Directory structure:
└── …"), not copies of a single upstream file, so no diff can verify them and none is attempted. Each
source path was confirmed to exist on 2026-09-14; none of the bundles records the commit it was
scraped at, so **a citation from these is weaker than one from `spec.yaml` and should be spot-checked
against the live docs when it is load-bearing.**

**EACH NOW NAMES A COMMIT, AND IT IS A CHECKED-AGAINST COMMIT, NOT A SCRAPED-AT ONE.** The commit a
bundle was generated at was never recorded and is not recoverable, so claiming one would be inventing
provenance. What IS knowable is the upstream commit a bundle has been read against — so that is what
the table carries, with the claim that was checked. A citation is therefore as strong as the
spot-check beside it, and no stronger.

| File | Source tree | Checked against | Claim verified |
| --- | --- | --- | --- |
| `file_format.md` | `lance-format/lance` — `docs/src/format/` | `6bd1a86a` (2026-09-18), read 2026-09-19 | three branch-reclaim claims — see below |
| `guide.md` | `lance-format/lance` — `docs/src/guide/` | `1f7b847c` (2026-09-19), read 2026-09-20 | `guide/blob.md:42` writes blob v2 with `data_storage_version="2.2"` and `blob_array` — the pair [[LH-085]] rests on |
| `namespace.md` | `lance-format/lance-namespace` — `docs/src/` | `62db3400` (2026-09-14), read 2026-09-20 | `catalog/rest/index.md:145-147` types `page_token`, `limit` and `delimiter` as Query parameters — the distinction [[LH-021]] used to separate spec vocabulary from rask dialect |
| `ray.md` | `lance-format/lance-ray` — `docs/src/` | `ae670c23` (2026-09-14), read 2026-09-20 | `write.md:31` declares `data_storage_version` on `write_lance`, which is what lets the media lane preserve blob typing |
| `lance_sdk.md` | LanceDB's rendered Python API reference | **no commit exists** — a rendered docs site, not a repo file tree | — |

**`namespace.md`'s REST-path prose is NOT the route authority — `ns_catalog/spec.yaml` is.** Its
"HTTP Request" blocks (`:7169-7260` and the rest of that section) give 16 of the 47 operations they
document at paths the OpenAPI contradicts: `POST /v1/table/{id}/index/create` where the spec says
`create_index`, `.../tag/create` where it says `tags/create`, `.../count` where it says `count_rows`,
`.../merge-insert` where it says `merge_insert`, `.../schema/metadata` where it says
`schema_metadata/update`, plus `query/analyze` and `query/explain` for `analyze_plan`/`explain_plan`.
Measured 2026-09-20 against three independent sources that all agree with each other and with the
deployed catalog: the vendored `ns_catalog/spec.yaml` (54 paths), upstream `main`'s `spec.yaml` (54
paths, fetched the same day), and the generated `lance_namespace_urllib3_client` (54 paths). So this
is a property of the upstream PROSE, not of this vendoring or of a stale pin — and it is expensive to
meet unaware: a reader citing that section concludes a conformant catalog is serving 16 operations at
the wrong path. Cite `ns_catalog/spec.yaml` for a route; `namespace.md` is for vocabulary, error codes
and semantics, which is what every other citation to it in this estate uses it for.

`lance_sdk.md` is deliberately the one row with no commit, and that is a property of the SOURCE rather
than a gap in this table: a rendered site has no revision to name. A claim from it is the weakest in
this tree and must be checked against the installed package rather than against a document.

`ns_catalog/` also holds the per-model markdown generated from the spec, plus two images. They move
with `spec.yaml` and were not re-vendored with it, so where a model page and `spec.yaml` disagree,
**`spec.yaml` is the one that was checked.**

### Spot-checks (2026-09-19)

Three claims from `file_format.md` were load-bearing for the branch-reclaim ruling, so each was read
against the live source as the paragraph above instructs. All three are unchanged upstream:

| Claim | Upstream today |
| --- | --- |
| a branch is a shallow clone of its source | `docs/src/format/table/branch_tag.md:49` |
| a file with no `base_id` resolves against the dataset root | `docs/src/format/table/layout.md:72` |
| "Source dataset remains immutable and can be garbage collected independently" | `docs/src/format/table/layout.md:156` |

**The bundle IS stale where the row said, and measurably so.** It names five feature flags;
`rust/lance-table/src/feature_flags.rs` allocates ELEVEN plus a sentinel at `1 << 11`. The six it does
not name are `disable_transaction_file`, `unstable_data_overlay_files`, `covered_index_metadata`,
`mixed_data_file_versions`, `frag_reuse_with_stable_row_ids` and `fragment_reuse_index`. That gap is
why `service_kit.lakehouse.features` reads the Rust source rather than this bundle, and is pinned by
`packages/service-kit/tests/test_a_refused_flag_is_refused_BY_NAME.py`.

**`guide.md:229` (and `:894`) holds for the manifest's version under every commit except
`write_dataset(mode="overwrite", data_storage_version=X)`; it does not bind each data file's version.**
Measured on pylance 12.0.0, 2026-09-25, on local tables at 2.1 and 2.2 with stable row ids:

- An append (`write_dataset(mode="append")` or a committed `LanceOperation.Append`), or a committed
  `LanceOperation.Overwrite`, whose files were written at ANOTHER version keeps the manifest's
  version, writes the files at the named one, and sets reader and writer flag 256
  (`mixed_data_file_versions`), which the vendored flag table (`file_format.md:5452-5474`) does not
  list. The flag is sticky: it survived deleting the foreign rows, a `write_dataset` overwrite and
  `compact_files` back to a single file version.
- `merge_insert`, `delete`, `add_columns`, `update`, `compact_files` and an overwrite that names no
  version keep the manifest's version. `write_dataset(mode="overwrite", data_storage_version=X)` moves
  it to X; it does not set flag 256, and on a table that already carries the flag it does not clear it.

`service_kit.lakehouse.features.describe_foreign_data_file_versions` names the files that would set it.

## Re-vendoring

```bash
SHA=<upstream commit>
curl -fsSL -o lance_docs/ns_catalog/spec.yaml \
  "https://raw.githubusercontent.com/lance-format/lance-namespace/${SHA}/docs/src/spec.yaml"
uv run pytest tests/integration/test_spec_conformance.py -q
```

Then update the commit in the table above — the pin and the file are one change, and a pin that names
a different commit than the bytes is worse than no pin.
