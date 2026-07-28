# Lance blob v2 — measured behaviour, and one landmine

> Empirical notes for the medallion bronze page-image datasets. Every claim here was produced by
> running against the **installed pylance 9.0.0**, not read from documentation. Reproduce with the
> scripts described at the bottom.

## The three placement tiers (what `packed` vs `dedicated` actually means)

`blob_field(name, inline_size_threshold=…, dedicated_size_threshold=…, pack_file_size_threshold=…)`
routes each value to one of three places, by payload size:

| tier | where the bytes live | default trigger |
|---|---|---|
| **inline** | inside the `.lance` data file, interleaved with the other columns | < 16 KiB |
| **packed** | a **shared** `.blob` sidecar — many payloads concatenated per file, rolled at `pack_file_size_threshold` | between the two |
| **dedicated** | its **own** `.blob` file, one file per value | ≥ 2 MiB |

**Evaluation order is dedicated-first**, then inline; packed is the remainder. (Counterintuitive — the
docstring states it explicitly: *"This threshold is checked before `inline_size_threshold`."*)

Measured layout, writing one value per tier with thresholds forced to 1 KiB / 64 KiB:

```
data/<fragment>/0100…blob   262,144 bytes   ← dedicated: exactly the 256 KiB payload, alone
data/<fragment>/1000…blob     8,192 bytes   ← packed sidecar (one payload here; many would share it)
data/<fragment>.lance          1,508 bytes   ← inline bytes + the external-URI *reference* + nulls
```

**Why both tiers exist.** Packed prevents small-file explosion — a million mid-sized payloads as a
million S3 objects would drown in listing cost, per-GET overhead and rate limits. Dedicated prevents
write amplification — a large payload in a shared sidecar gets shuffled by every compaction/rewrite of
that file, while its own file is *referenced* rather than re-copied, and a `BlobFile` handle can seek
inside it directly.

**External URIs are references, not copies.** A `Blob.from_uri(uri, position=…, size=…)` value stores a
pointer; the bytes stay in the foreign object and are resolved on read. Confirmed: a 20-byte slice of an
external container never appeared in the dataset's own files.

**Placement is transparent to readers.** All four shapes (inline / packed / dedicated / external URI)
round-tripped identically through `read_blobs`, `take_blobs`, `read_blob_ranges` and
`scanner(blob_handling="all_binary")`. Storage tuning is therefore a pure write-side decision — it can
be changed for new datasets without touching a single reader.

## ⚠ LANDMINE: `read_blobs` / `take_blobs` DROP null rows

The documentation states:

> *Blob selection APIs preserve logical result cardinality. `read_blobs()` and `take_blobs()` return one
> element per selected row … A null blob is returned as `None`.*

**This is not true in pylance 9.0.0.** Measured:

```
selected rows      : 3          (blobs: b"aaa", None, b"ccc")
read_blobs returned: 2          -> nulls silently dropped
take_blobs returned: 2          -> nulls silently dropped
scanner(blob_handling="all_binary") -> 3 rows, the null correctly None
```

**Why this matters here.** The bronze page-image dataset is read by row. Any code shaped like

```python
for key, (_addr, payload) in zip(page_keys, ds.read_blobs("blob", indices=idx)):   # WRONG
```

misaligns the moment one page has a null blob (a failed harvest, a skipped page) — every subsequent
payload is attributed to the **wrong page**, with no exception raised. That is a silent data-corruption
class, exactly the kind this estate's gates exist to catch.

**Safe patterns**, in order of preference:

1. Use `scanner(columns=[…], blob_handling="all_binary")` — it preserves cardinality correctly, nulls
   included, and it returns the tabular columns in the SAME scan, so alignment holds by construction with
   no mask to maintain. In this repo that is `service_kit.lakehouse.blobs.read_aligned_table`, and it is
   what the medallion cascade (`compute._carry_forward`) and the Ray media stage
   (`ray_stage_job._media_transform`) both use.
2. Key off the row address the API already returns — `read_blobs` yields `(row_address, payload)` and
   `read_blob_ranges` yields `(request_index, row_address, payload)`; map results back by that
   identifier rather than by position.
3. If positional access is unavoidable, filter the selection to non-null rows first (`is_valid`) so the
   input and output cardinalities agree by construction — **and only on pylance ≥ 9** (see below).
4. Where neither is possible (a Ray actor that only receives row ids), assert
   `len(payloads) == len(row_ids)` and fail with the cause named. `ratch`'s `_BlobActor` and
   `engine._read_blobs` do this: a broadcasting UDF could otherwise return the right length from short
   input and turn a loud error into silent misattribution.

**Note the reference implementation.** `lance_ray`'s own `datasource._read_fragments` handles exactly this
case explicitly: it compares `len(blob_files)` with the number of scanned descriptors and switches between
a 1:1 zip (legacy blob columns, which return a handle per row) and a **sparse** walk that consumes a handle
only for non-null rows — with the comment *"Blob v2 currently skips null rows, returning fewer handles."*
Any code of ours that pairs `read_blobs` output positionally is doing less than the library already does.

## ⚠ The landmine is WORSE on pylance 8 — and it is not fixable there

Measured 2026-07-28 (R27 audit) against the same three-row dataset above, written by pylance 9.0.0 with one
null payload, then read at the version `.docker/ray-lance.dockerfile` used to pin:

| read path | pylance 8.0.0 | pylance 9.0.0 |
|---|---|---|
| `read_blobs` / `take_blobs` cardinality | 2 of 3 (drops nulls) | 2 of 3 (drops nulls) |
| `scanner(blob_handling="all_binary")` | **`ArrowInvalid: there were more fields in the schema than provided column indices`** — on EVERY projection shape (all columns, blob only, no projection, ±`with_row_id`) | 3 rows, null correctly `None` |
| blob descriptor `is_valid()` | **`[True, True, True]` — wrong**, so the presence-mask fallback silently mis-detects | `[True, False, True]` — correct |

So at pylance 8.0.0 there is **no** correct way to read a null-bearing blob-v2 column: the cardinality-safe
path errors out and the mask-based fallback lies. That is why the Ray image's Lance pins are now asserted
equal to the workspace's (`tests/unit/test_ray_job_images.py`) — the Ray jobs read and write the *same*
datasets the services write, so a version split there is a correctness bug, not a currency preference.

Two related version facts measured in the same pass, for anyone reading old comments in the Ray jobs:

- `lance_ray.write_lance` genuinely had **no** `enable_stable_row_ids` at lance-ray 0.4.2 (hence the
  create-with-stable-ids-then-append dance); it exists at 0.5.0. `add_columns_from`,
  `merge_columns_from`, `vector_search` and the reusable global Ray Pool are 0.5.0-only too.
- `lance_ray.create_scalar_index` fails at 0.4.2 + pylance 8.0.0 with *"BTREE distributed indexing uses
  `create_index_uncommitted(..., index_type="BTREE", fragment_ids=...)`"* — **even though 8.0.0 has both
  parameters**, contrary to what `ray_lance_job.py` recorded. The conclusion (fall back to the native
  build) held; the stated reason did not.
- `lance_ray.read_lance(uri, scanner_options={"with_row_id": True})` **does** surface `_rowid` (verified at
  both 0.4.2 and 0.5.0), contradicting `ray_stage_job.py`'s note that it does not — see the R27 record in
  `lance-ns-merge.md`.

## Sizing note for rask's page images

Riksarkivet page scans run ~5 MB (the ra-hcp profile says so itself). Against the 2 MiB default that
puts **every page in its own dedicated `.blob` file** — fine for a 10-page proof, but 100k pages means
100k objects on RustFS. Whether pages should pack instead is a deliberate tuning decision the bronze
ingest head should own (via `blob_field(..., dedicated_size_threshold=…)`), not inherit by default.

## Reproducing

Two scripts, written during the 2026-07-28 investigation:

- placement + read-API matrix (inline / packed / dedicated / external URI × `read_blobs` /
  `take_blobs` / `read_blob_ranges` / scanner), printing the on-disk file sizes per tier;
- the null-cardinality check above.

Both run under `uv run python` against the root workspace's pylance. Re-run them when pylance moves —
the cardinality bug in particular should be re-checked, and this note updated if upstream fixes it.
