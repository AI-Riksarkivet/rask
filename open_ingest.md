# open_ingest — round 2

Working plan for `services/ingest`. Root `open_<topic>.md` is a WORKING plan: it is deleted and what
is still live folds into `OPEN-WORK.md` when the round lands. `docs/` is settled architecture only.

Round 1 shipped 46 commits on `ingest-plane` (not merged). Round 2 exists because an owner audit
found the write path wrong on two axes and the platform welded to one source. **Nothing here is
settled — read § D first.**

---

## § A. The contract that does NOT change

The plane is **a separate, source-agnostic service**. It is not "the IIIF ingester"; it knows nothing
about IIIF, HCP, or any other source. This is invariant **I1** and it is the reason the plane exists
at all — the code it replaced had IIIF welded across twelve medallion files, which is why
`S3PrefixSource` sat written, unit-tested and unreachable for months.

**A source is a registered adapter and nothing else.** One adapter, one registry entry, one lineage
twin. Adding a source touches those three things and nothing else (gate A9).

### The SINK contract — what any writer into bronze must satisfy

Independent of who does the fetching, a write into a governed tier must:

1. **Resolve the location through the CATALOG, never compose a path.** The caller names
   `{project, dataset}`; the catalog vends the URI (I2). Two callers composing
   `{warehouse}/{project}/{dataset}.lance` from different env is how volume B overwrites volume A.
2. **Create server-side, commit client-direct.** The catalog owns `CREATE` (it centralises the
   creation-time-only flags — `enable_stable_row_ids`, `data_storage_version`) and takes fragments
   through `POST /v1/table/{id}/commit`, so no data byte transits the catalog. The namespace must
   exist first: a table lives IN a namespace and that is a catalog object with its own manifest.
3. **Commit ONCE per run** (D6). Bronze shows the prior version until the commit returns and the new
   one all at once after, so there is no observable half-ingested tier and silver can ask "did a
   publication happen?" rather than "is ingest finished?".
4. **Write fragments SIZED for Lance**, not per row. Lance's guidance is ~1M rows per fragment; the
   per-row write is the documented anti-pattern.
5. **Store payloads as a BLOB column** (`blob_field`), so Lance's three placement tiers apply —
   inline / packed sidecar / dedicated `.blob` REFERENCED rather than re-copied by compaction.
6. **Leave provenance**: a run in the graph whose INPUT is the external world (`iiif://…`,
   `s3://bucket` — never a governed tier, R23) and whose OUTPUT names the catalog table id. The
   catalog's own `create_table` / `insert` lineage is what the cascade head fires on.
7. **Report honestly**: `202` in under a second, an `Idempotency-Key` that dedupes the WORK, a
   terminal state that distinguishes COMPLETE from COMPLETE_WITH_ERRORS, and a defect flag when a
   green run has no provenance (A8).

Everything in § A is settled and round 1 satisfies it, except items 4 and 5 which round 2 fixes.

---

## § B. What round 1 got wrong

| | Defect | Status |
|---|---|---|
| B1 | **One Lance fragment per image.** `units_to_table([(key, result)])` — a list of ONE. Measured: 4 fragments for 4 rows; a 10k-page volume would mean 10k fragments in one commit, 10k staging manifests, 10k FragmentMetadata blobs across a Dapr boundary. | code fixed (3233db2), **UNVERIFIED in-cluster** |
| B2 | **`payload` was `pa.binary()`, not `blob_field`.** Forces every page inline; no dedicated/packed tier; no `read_blobs`/`take_blobs`/`read_blob_ranges` for readers. The code this replaced already did it right (`medallion/services/ingest.py:31`). | code fixed, **UNVERIFIED in-cluster** |
| B3 | **A 404 was retried three times.** `except Exception -> nak()` could not tell a dead page from a network blip, against the rate-limited source the backpressure exists to protect. | code fixed, **UNVERIFIED in-cluster** |
| B4 | **`units_total` was always 0.** Declared, never assigned — "4 done", never "4 of 500". No progress bar possible for exactly the long harvest where one matters. | code fixed, **UNVERIFIED in-cluster** |
| B5 | **IIIF welded into the generic fetch path.** `fetch.py:48` routes EVERY `http(s)://` key through `storage.iiif.fetch_image`, so any HTTP source inherits IIIF's retry policy and headers. This is I1 violated by the plane that exists to enforce I1. | **OPEN** |
| B6 | **`ingestIIIFVolume()` in `@rask/api`**, and a compute-zone form hardcoding `kind:'iiif', project:'default', dataset:'pages'` — a source-shaped wrapper on a source-agnostic door. | **OPEN** |
| B7 | **Never run against a real source at scale.** Only 4 checked-in TIFFs (local-dir) and the same 4 on S3. `CHUNK_SIZE=1000`, `FETCH_BATCH=16`, `FETCH_CONCURRENCY=8` are guesses. | **OPEN** |
| B8 | **The cascade moves no data** and fires on table CREATE only — the movers' tier URIs are unset, so `handle_stage` skips its compute path (`transform.py:210`). | **OPEN** |
| B9 | **The quality gate is not wired into this lane**, and the mover's own gate is POST-commit — bad rows are already in silver and it merely declines to trigger gold. D3 wants pre-commit. | **OPEN** |

---

## § C. Round-1 root cause

The plane replaced the medallion's bronze head **without reading what that head already knew**.
`blob_field` was one line away in the file being replaced; `service_kit/lancekit/blobs.py` is a shared
helper that went unused; `docs/architecture/lance-blob-v2-findings.md` contains measured behaviour —
including that `read_blobs`/`take_blobs` silently DROP null rows in pylance 9.0.0, contradicting their
own documentation — and was never opened.

**A replacement that silently loses hard-won behaviour is a regression wearing a refactor's clothes.**
§ E item 5 is the systematic version of this check.

---

## § D. THE OPEN DECISION — who executes the fetch?

**Owner ruling required. Do not implement either side until it is made.**

Round 1 built a bespoke executor: Dapr Workflow fans out child workflows, each publishes units to a
JetStream WORK_QUEUE, and hand-written Python workers drain it. **The estate already has a pattern
for "fan out over N keys, do work, write Lance", and this duplicates it.**

`runners/htr/src/runner/pipeline.py:63`:

```python
ds = ray.data.from_items([{"key": k} for k in keys], override_num_blocks=...)
```

That is the same fan-out, in the estate's own idiom. The medallion movers submit Ray jobs through
`ray_kit` in response to a Dapr trigger (submit-and-ack, no polling). And Lance's own distributed-write
guide describes exactly this: workers call `write_fragments`, one worker collects the metadata and
commits once.

### Option 1 — Ray Data, as a sealed runner

`runners/ingest`, baked into the ray image, submitted by the ingest service on accept.
`from_items(keys)` → `map_batches(fetch + validate)` → `write_fragments` per block → one commit
through the catalog.

* **For:** the estate's existing pattern; Ray owns fan-out, backpressure, retries and actor pools;
  block size IS the fragment size, so B1 cannot recur by construction; deletes ~1,500 lines of
  queue/worker/staging; matches Lance's documented distributed write.
* **Against:** needs the Ray cluster up; per-unit durable redelivery becomes Ray's task retry rather
  than a broker's; politeness against a rate-limited HTTP source has to be expressed as actor-pool
  concurrency instead of `max_ack_pending`.

### Option 2 — keep the queue + workers

* **For:** durable per-unit redelivery with a DLQ for poison units; `max_ack_pending` is real
  backpressure against a rate-limited external API; runs with no Ray cluster.
* **Against:** re-implements task distribution the estate already has; every property above had to be
  hand-built and two of them (B1, B3) were built wrong.

### Option 3 — per source kind

Bulk/local sources (an S3 prefix) go through Ray Data — Lance auto-parallelises a
`pyarrow.dataset` and estimates partitioning from data size. Rate-limited external APIs keep the
queue for politeness and per-unit redelivery.

* **Against:** two executors to maintain and reason about.

**Recommendation: Option 1**, unless per-unit durable redelivery against a rate-limited API is a
requirement you want to keep. The sink contract in § A is identical under all three — only the
executor changes — so this decision does not invalidate § A.

---

## § E. Round-2 acceptance conditions

1. This document exists and is kept current.
2. **No source-specific code in the platform.** Fix B5 and B6. A test proves a non-IIIF `https`
   source never touches `storage.iiif`.
3. **The write path proven IN-CLUSTER**: print fragment count (1 for a 4-fixture run, was 4),
   `blob_field_names(schema) == ['payload']`, `read_blobs` bytes byte-identical to a fixture,
   `units_total == 4`, `units_done == 4`.
4. **Recovery re-earned.** The ack moved from per-unit to per-batch, so a killed pod now drops a
   whole held batch's acks. Re-run A5 (corrupt) and A3 (kill). If A3 no longer lands every row, fix
   it — stage before ack, or bound the batch under `ack_wait` — never weaken the assertion.
5. **Audit against what this replaced** (§ C): every behaviour of `medallion/services/ingest.py`,
   `iiif_produce.py` and `packages/storage/iiif.py` listed as CARRIED or DROPPED-because-X. At
   minimum: blob tiering, cache read-through, retry/backoff, stage stamping, page ordering.
6. **Green:** `uv run pytest -m "not slow"`, `uvx ruff check`, `bunx turbo --cwd=frontend run check
   lint fmt:check` for any zone touched, and the `tests/e2e` ingest-lane Playwright suite against the
   deployed lane. Every new behaviour gets a NAMED test.

**Deferred to round 3:** B7 (real source at scale — do it after § D is ruled, or it measures an
executor that is about to be replaced), B8, B9.

---

## § F. Where things are

| | |
|---|---|
| branch | `ingest-plane`, pushed to origin, **not merged** |
| worktree | `/home/blackwell/Desktop/rask-ingest` |
| the other checkout | `/home/blackwell/Desktop/rask` on `open-ingest-etl` — has no `services/ingest`, and still shows the round-1 `open_ingest*.md` because this branch is unmerged |
| re-runnable lane | `scripts/ingest-lane.sh {deploy\|image\|fixtures\|run\|corrupt\|kill}` |

Merge state: `main` is 42 commits ahead (incl. the `media → explorer` rename #85). A dry-run merge
conflicts on `services/gateway/__init__.py` (main edited the same route table), `frontend/bun.lock`,
and `open_ingest_etl.md`.
