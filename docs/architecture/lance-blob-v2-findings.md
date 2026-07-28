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

1. Key off the row address the API already returns — `read_blobs` yields `(row_address, payload)` and
   `read_blob_ranges` yields `(request_index, row_address, payload)`; map results back by that
   identifier rather than by position.
2. Use `scanner(columns=[…], blob_handling="all_binary")` when you want row-aligned columnar results —
   it preserves cardinality correctly, nulls included.
3. If positional access is unavoidable, filter the selection to non-null rows first (`is_valid`) so the
   input and output cardinalities agree by construction.

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
