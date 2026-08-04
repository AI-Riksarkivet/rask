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

## § D. THE REAL QUESTION — how does a consumer know a write is ready?

**Owner ruling required. This is the design hole; everything else here is plumbing.**

### The framing that was wrong for several turns

I spent them arguing about **who executes the fetch** (hand-rolled workers vs a Ray job). The owner's
answer: *"where it executes could be python or ray or airflow, I don't care."* Correct — that is an
implementation detail behind the sink contract, and it is demoted to § D3 below.

The question that matters: **ingest is a SINK, not a special citizen.** You point it at an S3 bucket
or a Delta table and it writes into bronze. So must anything else — a Ray job, a backfill script, a
person with catalog credentials. **Nothing about the propagation of "this data is ready" may live in
the ingest service**, or every future writer has to re-implement it.

### What is actually implemented today (and why it is wrong)

    writer commits ──▶ catalog emits `insert.<table>` lineage ──▶ /bronze-arrival ──▶ medallion.bronze

Three problems:

1. **Whole-table granularity.** The event says "this table changed". A consumer has no way to know
   WHICH rows are new, so it either rescans the tier or invents its own bookmark.
2. **Ungated.** The committed version is visible to every reader the instant it lands. A quality gate
   that runs afterwards cannot un-publish it — bad rows are already readable.
3. **No pointer.** A consumer reading the table gets `latest`. There is no "the version you should
   be reading" and so no way to hold consumers at the last good version while a bad one sits above it.

Measured consequence of (1): the cascade fires on table CREATE and not on subsequent INSERTs, so a
table's second arrival wakes nothing. That is a symptom of having no publication concept, not a bug
in the trigger.

### What A18 already specified, and nobody built

> *"Gate FAIL → no tag advance, no event, FAIL lineage run, downstream provably never woken; gate
> PASS → `published` advanced, the emitted event's version equals the commit/tag-update RESPONSE
> value; a consumer resolving via the tag reads the gated version while `latest` may differ."*

The catalog already carries the whole tag surface — `tags/create`, `tags/update`, `tags/version`,
`tags/list`, `tags/delete`. **`published` appears nowhere in the estate.** The mechanism was designed
and never implemented, which is exactly why "how does that propagate" has no good answer today.

### § D1 — The proposal

**A commit is not a publication.** Two separate acts:

    1. WRITE      any writer commits fragments through the catalog     → a new VERSION exists
                  (ingest, a Ray job, a backfill, a person with creds)   readable only via `latest`

    2. PUBLISH    a gate runs on the delta; on PASS the catalog          → `published` TAG advances
                  advances the `published` tag and emits ONE event         and the event carries
                  carrying {table, from_version, to_version}                the tag-update RESPONSE

**Consumers resolve `published`, never `latest`.** The unit of "ready" is then a VERSION RANGE, and
the range is the answer to "which partition is up for grabs":

    delta = ds.to_table(with_row_id=True,
                        filter=f"_row_created_at_version > {from_version}")

That is Lance's change-data-feed, it is already proven in `runners/dummy`, and it makes the increment
explicit rather than something each consumer bookmarks for itself.

**Why this satisfies the sink requirement:** the publication contract belongs to the CATALOG, so any
writer that commits through the catalog gets it for free. The ingest service becomes one client among
several and holds no special knowledge — which is the whole point.

### § D2 — What needs deciding

| | Question |
|---|---|
| **D2a** | Is `published` the mechanism, or something else (a branch, a manifest property, a separate registry)? |
| **D2b** | Who advances the tag — the catalog itself on commit-plus-gate, or a separate publisher the writer calls? |
| **D2c** | Does the gate run pre-commit (the version never exists) or post-commit-pre-publish (the version exists but is unpublished)? D3 says a held batch must never become a version, which argues pre-commit; but a pre-commit gate cannot use Lance's CDF to see the delta, because there is no version yet to diff. **This tension is unresolved and is the crux.** |
| **D2d** | Does this apply to ALL tier transitions (bronze→silver→gold) or only the bronze entry? The estate today orchestrates ingest with Dapr Workflow but runs the movers as plain pub/sub subscriptions — an inconsistency nobody has ruled on. |
| **D2e** | Is a partition finer than a version ever needed, or is "version range" the smallest unit downstream ever wants? |

### § D3 — Demoted: the executor

Dapr Workflow orchestrates the run — **owner-ruled 2026-08-03, not in question.** Whether its
fan-out activity drains a JetStream queue with hand-written workers or submits a Ray job is an
implementation detail behind the sink contract. Noting only that `runners/htr` already does
`ray.data.from_items([{"key": k} for k in keys])` — the same fan-out, in the estate's own idiom —
and DECISIONS #16 says the idempotent batch legs "need only NATS + Ray". Decide it after § D1.

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
