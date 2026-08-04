# open_ingest — round 2

Working plan for `services/ingest`. Root `open_<topic>.md` is a WORKING plan: it is deleted and what
is still live folds into `OPEN-WORK.md` when the round lands. `docs/` is settled architecture only.

Round 1 shipped 46 commits on `ingest-plane` (not merged). Round 2 fixed the write path and the
source weld; **§ D is now RULED** (owner, 2026-08-04) — see § D2. Round 3 builds the readiness
contract that ruling defines.

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
| B1 | **One Lance fragment per image.** `units_to_table([(key, result)])` — a list of ONE. Measured: 4 fragments for 4 rows; a 10k-page volume would mean 10k fragments in one commit, 10k staging manifests, 10k FragmentMetadata blobs across a Dapr boundary. | **VERIFIED in-cluster** — 1 fragment for 4 fixtures |
| B2 | **`payload` was `pa.binary()`, not `blob_field`.** Forces every page inline; no dedicated/packed tier; no `read_blobs`/`take_blobs`/`read_blob_ranges` for readers. The code this replaced already did it right (`medallion/services/ingest.py:31`). | **VERIFIED in-cluster** |
| B3 | **A 404 was retried three times.** `except Exception -> nak()` could not tell a dead page from a network blip, against the rate-limited source the backpressure exists to protect. | **FIXED; verified by test, NOT in-cluster.** A5 proves in-cluster that a refused unit is parked once and NAMED in `errors`; that a 404 *specifically* is not retried is asserted by `test_a_404_is_NOT_retried` (respx) — the lane has no 404-serving source to point at. |
| B4 | **`units_total` was always 0.** Declared, never assigned — "4 done", never "4 of 500". No progress bar possible for exactly the long harvest where one matters. | **VERIFIED in-cluster** |
| B5 | **IIIF welded into the generic fetch path.** `fetch.py:48` routed EVERY `http(s)://` key through `storage.iiif.fetch_image`. Worse than it looked: `fetch_image`'s `client` is a REQUIRED keyword-only arg that was never passed, so the http(s) path had **never worked** — only `file://` and `s3://` were ever exercised. | **FIXED** (`5b28fbd`) |
| B6 | **`ingestIIIFVolume()` in `@rask/api`**, and a compute-zone form hardcoding `kind:'iiif', project:'default', dataset:'pages'` — a source-shaped wrapper on a source-agnostic door. | **FIXED** (`4883175`) — the registry is readable (`GET /api/ingest/sources`, each kind carrying its own option fields) and the form is built from it; adding a source touches no frontend file. |
| B7 | **Never run against a real source at scale.** Only 4 checked-in TIFFs (local-dir) and the same 4 on S3. `CHUNK_SIZE=1000`, `FETCH_BATCH=16`, `FETCH_CONCURRENCY=8` are guesses. | **OPEN** |
| B8 | **The cascade moves no data** and fires on table CREATE only — the movers' tier URIs are unset, so `handle_stage` skips its compute path (`transform.py:210`). | **UNBLOCKED by D-R3** — the trigger becomes the tag advance carrying `{from, to}`; whole-table granularity was the defect. |
| B9 | **The quality gate is not wired into this lane**, and the mover's own gate is POST-commit — bad rows are already in silver and it merely declines to trigger gold. | **UNBLOCKED by D-R1** — post-commit / PRE-PUBLISH. Not pre-commit: the version must exist for CDF to diff it. |

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

**RULED 2026-08-04 — see § D2.** The framing below is kept because it records what was wrong and
why; the answer is D-R1/D-R2/D-R3.

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

### § D2 — RULED (owner, 2026-08-04)

The three questions that needed the owner are answered. What Lance can do was never the open part —
the docs settle that: a tag creates no version, is exempt from `cleanup_old_versions`, readers pin
with `checkout_version("published")` / `DescribeTable{tag}`, and the catalog already implements all
five namespace tag operations. What needed ruling was the CONTRACT.

**D-R1 — "Ready" means THE GATE PASSED.** A committed version is not consumable. The writer commits
version N, the gate reads N, and only then does the pointer advance. A gate FAILURE leaves the
pointer at N-1, so consumers keep reading the last good version while the bad one sits above it,
committed and unreferenced. Bad rows never become consumable — which is the property the current
design cannot express at all, because a commit is instantly visible to every reader.

This also dissolves the "tension" recorded here for several rounds. A pre-commit gate cannot use the
change-data-feed because there is no version to diff — true, and irrelevant: **the gate runs AFTER
the commit**, so the version exists and CDF works normally. There was never a dilemma.

**D-R2 — A TAG IS THE TRUTH; AN EVENT IS THE NOTIFICATION.** `published` on the table is the durable
answer to "what is ready?" — a consumer can ASK, at any time, including after being down for a week.
The event is only a wake-up so nobody polls. Durable state plus ephemeral notification: an event
missed while a consumer was down costs nothing, because the tag still answers. Event-only was
rejected for exactly that reason, and it is a large part of why the cascade misses a table's second
arrival today.

**D-R3 — THE SIGNAL NAMES A VERSION RANGE**, `{table, from_version, to_version}`. A consumer turns
that straight into an exact row delta — `_row_created_at_version > from AND <= to` — holding no
bookmark of its own. This is the direct fix for B8: a whole-table "something changed" cannot express
*which rows are new*, so every consumer must rescan or invent its own bookmark, and a second arrival
therefore wakes nothing useful.

**What follows without further rulings**

| | |
|---|---|
| B9 (the quality gate) | is post-commit / pre-publish. Implementation, not a decision. |
| B8 (cascade fires on CREATE only) | is the granularity defect. The trigger becomes the tag advance carrying `{from, to}`. |
| the runner-picking question | silver reads the range the signal names; it no longer needs to infer what is new. |

**Two hazards that come WITH this shape, and must be handled rather than discovered**

1. **Version N is untagged for the gate's duration**, and `cleanup_old_versions` exempts only TAGGED
   versions. A slow gate plus a short `older_than` collects the very version being gated.
2. **A tag move has no format-level atomicity.** `_refs/tags/{name}.json` is a plain JSON file and
   the format spec requires no CAS for updating it — unlike the manifest commit path. The namespace
   spec's `UpdateTableTag` *does* return `ConcurrentModification`, so the advance must go through the
   catalog rather than a direct file write.

### § D3 — Demoted: the executor

Dapr Workflow orchestrates the run — **owner-ruled 2026-08-03, not in question.** Whether its
fan-out activity drains a JetStream queue with hand-written workers or submits a Ray job is an
implementation detail behind the sink contract. Noting only that `runners/htr` already does
`ray.data.from_items([{"key": k} for k in keys])` — the same fan-out, in the estate's own idiom —
and DECISIONS #16 says the idempotent batch legs "need only NATS + Ray". Decide it after § D1.

## § E. Round-2 acceptance conditions

1. **DONE** — this document exists and is kept current.
2. **DONE** — **No source-specific code in the platform.** `fetch.py` resolves by SCHEME and imports
   no source module; `test_source_agnostic.py` proves a plain `https` fetch leaves `storage.iiif`
   absent from `sys.modules`. De-welding it surfaced that `fetch_image`'s `client` is required and
   was never passed — **the http(s) path had never worked at all**; only `file://` and `s3://` were
   ever exercised. The UI half went with it: the registry is now readable (`GET
   /api/ingest/sources`), `ingestIIIFVolume()` is gone, and the compute form is built from what the
   registry serves. (`5b28fbd`, `4883175`)
3. **DONE** — **the write path proven IN-CLUSTER.** Read back from the committed dataset on the
   digest-pinned image: `FRAGMENTS: 1` (was 4), `blob fields: ['payload']`, `read_blobs` → 4
   payloads byte-identical to the checked-in fixture, `units_total: 4`, `units_done: 4` (was 0).
4. **DONE, and it found a defect A3 cannot reach.** A5 and A3 both pass on the fixed image — the
   grace-0 kill recovers to 4 rows / 1 fragment / **0 duplicates**. But the batching change had
   broken staging's stated premise: a fragment covering N units was staged under `units[0][0]`, so a
   partially-acked batch committed its units twice (four in, six out). Witnessed deterministically,
   then fixed three ways — the manifest names every unit and is keyed on the SET, `discover_staged`
   resolves ownership rather than collecting, and a redelivered unit is never batched with a fresh
   one. (`ada4d43`; § G ends with the case it still refuses rather than guesses.)
5. **DONE** — see **§ G**.
6. **Green:** backend `1735 passed`, `uvx ruff check` clean, `svelte-check` 0 errors / 0 warnings,
   `@rask/zone-contract` 878/878, and the `tests/e2e` ingest-lane Playwright suite **7/7 in a real
   browser** against the deployed registry — including the A5 test that had only ever skipped.
   `uvx ty check` reports 101 diagnostics, **identical before and after** this round: the known
   unpinned-`ty` drift, not new.

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

---

## § G. The audit against what this replaced

Condition 5. The new plane took over the medallion's bronze head; this is what that head knew, and
whether the replacement kept it. Written because two severity-1 defects this round (`blob_field`,
and the measured `read_blobs` null-drop) were both one line away in the file being replaced — a
replacement that silently loses hard-won behaviour is a regression wearing a refactor's clothes.

Sources read: `medallion/services/ingest.py` (still present, 178 lines), `packages/storage/iiif.py`
(present), `medallion/services/iiif_produce.py` (**already deleted** — read from git history).

### CARRIED

| behaviour | where it lives now |
|---|---|
| `blob_field("payload")` — the four placement tiers, and `read_blobs`/`take_blobs`/`read_blob_ranges` for readers | `runtime.BRONZE_SCHEMA`. **This was the regression**; restored this round. |
| `data_storage_version="2.2"` + `enable_stable_row_ids=True` (creation-time-only) | `runtime.CREATION_FLAGS`, applied at dataset creation — which had to move BEFORE the fan-out, since workers were writing into a dataset the finalizer created afterwards. |
| ONE atomic commit per ingest | D6. `lander.commit_fragments` — one `LanceOperation.Append` for the whole run. |
| bronze OWNS its bytes (no `Blob.from_uri` external pointers) | The worker fetches and writes real bytes. The old head's reason still holds: a pointer-only bronze dangles the moment the source bucket's lifecycle moves an object. |
| retry/backoff — transport + 5xx retried with backoff, 4xx≠429 fast-failed | `fetch._fetch_http`, reimplemented generically. That policy was never IIIF-specific; it lived in `iiif.py` because that is where it was first needed. |
| **improved:** a stable `id` | The old head used a POSITIONAL id and left a note: *"If ingest ever gains true append mode, derive a stable id from source_uri."* The new plane appends, and does exactly that — `sha256(source_uri)`. The successor honoured a note the predecessor left for it. |

### DROPPED — each one a decision, stated

1. **The empty-source refusal.** The old head peeked BEFORE any write and raised on zero objects:
   *"an empty bronze is almost always a mis-set prefix, and silently 'succeeding' with zero rows
   would report a false success (and an input-less lineage edge)."* The new plane completes a
   zero-unit run as COMPLETE with 0 rows. **Should come back** — the argument is as true as ever,
   and this plane makes it worse: a mis-set prefix commits nothing, reports COMPLETE, and emits a
   WROTE edge for a table it did not write.
2. **The ingest ceilings.** `max_objects` (10k) and `max_total_bytes` (1 GiB), each refusing with a
   `ValueError` naming its env knob, mapped to 400 at the route. Deliberately *not* a memory bound —
   the old head's docstring is explicit that bounded memory was already solved and the ceiling is
   the guard against a mis-pointed prefix. The new plane has none: `s3-prefix` at a bucket root
   enumerates the whole bucket and starts fetching. **Should come back**, and it is cheaper here —
   enumeration is a discrete phase, so the count is known before a single unit is queued.
3. **The `stage` provenance stamp.** The old head wrote `stage='bronze'` at ingest, absorbing the
   retired raw→bronze mover (R23). `BRONZE_SCHEMA` has no such column. **Not fatal** — the movers'
   `_stamp_stage` appends it when absent — but a bronze table from this plane is the only governed
   dataset in the estate carrying no tier stamp. Cheap; restore it with the empty-source guard.
4. **The schema facet on the WROTE edge.** The old head returned `fields=schema.facet_fields(...)`,
   blob-aware, so the graph recorded the columns it had written. `lineage.terminal` emits outputs as
   `{namespace, name}` only. The cascade still fires (its head keys on the name pair), but
   column-level lineage cannot be derived from a run this plane recorded.
5. **`extra_columns`.** The old head accepted `{column: extractor}` for lane-specific grouping
   columns (volume/page keys); media/events lanes passed none. `BRONZE_SCHEMA` is fixed at
   `(id, source_uri, payload)`. **Correctly dropped for now**: it existed for the page lane's
   grouping, nothing consuming this plane needs it, and adding a general mechanism before something
   asks for it is how the twelve-file weld started. Revisit when a lane actually needs it.
6. **`IIIFCachedSource` — the S3 read-through cache.** Still used by `runners/htr`, not by this
   plane: the IIIF adapter builds URLs and the generic fetcher does a plain HTTP GET, so re-ingesting
   a volume re-fetches every page from the very endpoint the queue's backpressure exists to protect.
   **This one belongs to the ADAPTER, not the platform** (I1): a per-source `Fetcher` is already the
   designed seam — `fetch.py`'s docstring names cache read-through as its example — so it is adapter
   work, and putting it in the platform would re-weld what this round just separated.

### The one case the fix refuses rather than resolves

`discover_staged` resolves an overlap by dropping the fragment whose units another already covers.
That is exact while every overlap is a CONTAINMENT, which the worker guarantees by never batching a
redelivered unit with a fresh one. If that isolation ever breaks you get `F={u0..u3}` against
`H={u2,u3,u4,u5}`: neither contains the other, committing both duplicates, committing either loses.
It raises `StagingOverlapError` — loudly, with every byte still on the store and still named by its
manifest — rather than guessing. Pinned by a test, so it is a decision and not an accident.
