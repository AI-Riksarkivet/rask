# open_backlog_left — everything that is LEFT, in delivery order

This file replaces `open_lakehouse_diff_left.md`, `open_goal.md`, `OPEN-WORK.md`, `open_gateway.md`,
`open_controller.md`, `open_projects.md` and `open_ray_handover.md`. Those are deleted; git history
holds them, and a test docstring citing `open_lakehouse_diff_left.md § X` resolves there.

**Only open work is here.** Everything already done was dropped on the owner's instruction
(2026-09-10): *"I dont care about keep tracking what have been done. i just want to wrap things up."*
Do not add "what we fixed" sections to this file — that is what commit messages are for.

<!-- FOCUS:START -->
## FOCUS NOW

**FINISH THE LAKEHOUSE. It is the priority and nothing else competes with it.**
The lakehouse is four services: **catalog, lineage, medallion, maintenance.**

It is done when all five hold (owner, 2026-09-10):

1. **Provenance/lineage is correct** — a write's provenance survives it.
2. **The catalog is correct for lance-ns**, and correct for **auth / authz / governance**.
3. **It is NOT coupled to a workflow engine or to Ray.** Dapr Workflow and Ray are things the
   lakehouse can be driven BY, never things it depends ON.
4. **Events are correct.**
5. **It is resilient.**

**THEN phase 2 — COMPUTE:** compute, ingest, ray-kit, and maintenance's Ray half. This is where BYO
lives: bring your own workflow engine (here Dapr Workflow) and your own distributed engine (here Ray).

**THEN phase 3 — CONTROLPLANE:** controlplane, gateway, notifications.

**LOW PRIORITY — do not work these:** flows, search, viewer, annotator.
**FRONTEND:** fix opportunistically, in the same change as the service it belongs to. Never a
frontend-only campaign.

### Standing constraints

**Secrets reach a workload by exactly three paths and no others** (owner, verbatim): *"Never secret
through envs. Either from ESO, secret store dapr and STS for zero trust."* — Dapr secret store
(OpenBao) for a pod with a sidecar; ESO for a pod without one (Ray lane, web zones, runners); STS for
STORAGE (`vending.build_session_policy`, bucket+prefix, 900 s). **Never through env** — not process
env, not a k8s Secret via `envFrom`, not a chart value, never a fallback chain. A scoped static key is
not a fix. Read the running pod, and never one spelling of a mount.

**Never Docker — Dagger builds every image. Never mypy, never `# type: ignore` — narrow or cast.
Idiomatic to lance-ns, never Iceberg; read `lance_docs/` and cite it. No backward compat. Read skill
REFERENCES, not the index. Verify external claims against the source. Comments carry rationale and
provenance, never history.**

**A VERDICT IS NOT EVIDENCE IT IS STILL TRUE — re-measure before working a row.** Of 17 rows settled
on 2026-09-09, 8 were already fixed, 2 asked for less than they said, 1 described the wrong thing.

### Verification, per commit

`uvx ty check`, `uv run ruff check`, the TESTPATHS the change touches — **and always the invariant +
integration layers**, where a one-service change breaks another. Full suite ~8m30s, backgrounded, once
per batch. Anything deployable is **BUILT with Dagger, DEPLOYED to k3s and OBSERVED working** — never
claim it works first. **Push every commit.**
<!-- FOCUS:END -->

---

## What is left, counted

**272 open items**, deduped from 325 raw rows mined out of the seven files above.

| Phase | Items | High |
| --- | --- | --- |
| **1 · Lakehouse** (catalog, lineage, medallion, maintenance) | 125 | 21 |
| **1 · Cross-cutting** (service-kit, storage, chart, build, tests) | 51 | 9 |
| **2 · Compute** (compute, ingest, ray-kit) | 31 | 6 |
| **3 · Controlplane** (controlplane, gateway, notifications) | 24 | 5 |
| **Frontend** (opportunistic) | 13 | 1 |
| **Low priority** (flows, search, viewer, annotator) | 28 | 1 |

Counts are re-derived by `tests/unit/test_the_backlog_counts_itself.py`, so the table cannot drift from
the rows below. Items are numbered continuously; the ids in brackets are the source rows they came
from, kept so an old citation still resolves.


---

## PHASE 1 · LAKEHOUSE — the priority

**catalog · lineage · medallion · maintenance.** Grouped by the five conditions above, in that order.

### Provenance & lineage is correct

_Every governance promise the lakehouse makes rests on the run record being emitted, stored and reproducible; where it is wrong the estate cannot say which run wrote which bytes._

**LH-122 · ~~A namespace listing filtered to empty is indistinguishable from a namespace with no tables~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* `authorization_truncated` is NOT a "this listing was filtered" flag — it is set only when OpenFGA's ListObjects hits its 1000-object server cap (`truncated = len(objects) >= LIST_OBJECTS_SERVER_CAP`), so the flag the row observed beside `tables: []` for an estate admin means the admin's OWN grants were silently dropped by the cap, not that a no-grant reader was filtered to empty; the withheld-count gap the row asks for is real but its diagnosis names a mechanism the code does not have.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/namespaces.py:735-793 (`GET /v1/namespace/{id}/table/list`: `listing = await fga.list_objects(...)`; `authorization_truncated = listing.truncated`; `names = [name for name in names if f"table:{...}" in allowed]`; `if authorization_truncated: response.context = {**(response.context or {}), "authorization_truncated": "true"}`); packages/service-kit/src/service_kit/governed/fga.py:630-636 (`truncated = len(objects) >= LIST_OBJECTS_SERVER_CAP` + `openfga_list_objects_possibly_truncated` warning); services/catalog/src/catalog/api/v1/endpoints/tables.py:212-225 (same construction on `GET /v1/table`). Nowhere is a count of FGA-filtered-out entries computed or returned — `names` is rebound by the filter and the pre-filter length is discarded.
- *What would reopen it:* Find a code path where `authorization_truncated` (or any other response field) is set as a consequence of the `can_read_data` filter removing entries, independent of `listing.truncated`; or show `LIST_OBJECTS_SERVER_CAP` is not OpenFGA's ListObjects ceiling. Either would restore the row's stated mechanism.

**LH-127 · `lance-ray-durable` has been dead for 26 days and is accumulating the whole stream**
`lineage, compute, chart` · med

- *Why open:* Measured 2026-09-10: on the LINEAGE stream, consumer `lance-ray-durable` reports 2,445 unprocessed messages and a last delivery of 26 days ago — it is bound, durable, and nothing is draining it. Every event the estate emits accrues to it forever. A durable consumer nobody reads is not free: JetStream cannot age messages out of a stream while a consumer still needs them, so this one pins the entire retention window and the 5.8 MiB grows without bound. Nothing reports it — the depth is visible only by asking NATS directly.
**NOT the same shape as lineage's own consumer, which was filed beside this and STRUCK.** Lineage's is ephemeral BY DESIGN — the chart states it ("a durable cursor would defeat its replay-rebuilds-the-graph recovery story") and `_is_replay` accepts what the replay re-presents, logging at INFO. This one is the opposite: a DURABLE consumer with a queue group that nothing is attached to, so nothing accepts and nothing acks.
- *Closes when:* Establish whether anything is meant to consume `lance-ray-durable` (the name suggests the Ray lane). If yes, fix the subscriber and drain it; if no, delete the consumer so retention can do its job. Then add the depth of every consumer on the estate's streams to whatever the maintenance sweep already reports, so a dead subscriber is visible without a NATS client.

**LH-002 · The reconcile sweep warns every tick on 32 `storage_loss` + 2 `unreadable` datasets that are all test residue**
`lineage, maintenance` · **HIGH**

  **RE-MEASURED 2026-09-11 — THE SYMPTOM IS REAL, THE DIAGNOSIS IS WRONG, AND THE REMEDY CLOSES
  NOTHING.** Both WARN lines do fire on every 300 s tick at exactly `storage_loss=32, unreadable=2`.
  But the 32 are NOT dead test residue whose storage is gone: **29 of the 32** — every one alice's
  bearer can see — are LIVE, catalog-registered, readable tables. The catalog describes them 200 with
  a location, `count_rows` on `tracka$brdel_a9bf2fbf` answers 3, and `/history` shows main at v1. So
  the sweep is reporting live data as storage loss, which is the opposite of the row's story and a
  worse defect than the noise it complains about. The remedy fails too: on 2026-09-30 `prune_runs`
  deletes the 08-31 runs, but `prune_orphan_datasets` leaves 31 of the 34 nodes in place because they
  carry a CREATED edge (`cypher.py:276-284` requires `nc = 0`), so the next sweep warns identically.
  Waiting for retention cannot close this row.
- *Why open:* Retention (30d) and `prune_orphan_datasets` landed and the graph is converging (79→2 unreadable, 37→32 storage_loss), but both warnings still fire on every 5-minute tick over 1,163 Dataset nodes, so a real storage loss would arrive indistinguishable from the noise. The 19 genuinely-dead nodes have runs dated 2026-08-31..09-07 and the one-off purge was refused as a destructive graph write.
- *Closes when:* Let 30-day retention reach the 2026-08-31 runs (2026-09-30), then re-measure the reconcile warnings and confirm `datasets=[...]` names only live datasets.

**LH-003 · The `parent`, `processingEngine` and engine-version run facets are neither emitted nor stored, and the graph has no version/branch/tag/clone nodes**
`lineage, catalog` · low · **THE CONDITION-1 HALF IS CLOSED 2026-09-11; the rest is struck as not blocking**

- *CLOSED AND OBSERVED — the branch-ref drop, which this row did not name and which was the only part
  blocking condition 1.* `d5f1f19c`. The catalog has LIVE branch writes and `emit_measured_write`
  already read the version back off the right ref (its docstring explains why) and then dropped the ref,
  so the WROTE edge recorded a number that names two snapshots. Reproduced on the installed pylance:
  main v2, a branch write gives branch v3, a later main write gives main v3, different contents.
  Observed on the deployed services: a branch write's `lance` facet carries `ref: feat`, a main write
  carries none, `SET_WROTE_REF` is present, and both `LATEST_WRITE_VERSION` and `SCHEMA_LATEST` filter
  `w.ref IS NULL` so a branch version can no longer be reported as main's to `core/reconcile.py`.
- *THE REST OF THE ROW IS MEASURED STILL-TRUE AND STRUCK ANYWAY, because it blocks nothing live:*
  * `parent` — the emitter CAN stamp it (`lineage_kit/schemas.py` carries `ParentRunFacet`) and no
    producer does, but the CONSUMER side was deleted by owner ruling and is pinned deleted
    (`tests/unit/test_lineage.py::test_the_run_hierarchy_consumer_side_stays_deleted`). Emitting into a
    consumer that is required to ignore it is work with no reader. `cascade_id` already answers "which
    runs belong to this batch" and is threaded end to end.
  * `processingEngine` — absent estate-wide; the events are spec-valid without it.
  * Version nodes — contradicts a recorded decision (`endpoints/versions.py`): version history comes
    from the FORMAT, who-did-it from the lineage store, joined on the number; "a third that merged them
    would just be a copy of one of them, free to drift".
  * Clone edges — no rask door creates a clone; `shallow_clone` appears only as a shape the sweep must
    DETECT. There is nothing to record.
- *One residual worth a separate row if anyone wants it:* a rename strands history — `Dataset` MERGEs on
  `name` and no re-link statement exists.

**LH-004 · Four OpenLineage emit kernels and three `RunEvent` builders, all swallowing transport failures, with no outbox**
`lineage, catalog, maintenance, medallion, service-kit` · **HIGH** · **blocked:** owner acknowledgement of R10

- *Why open:* Measured at HEAD 2026-09-09: builders are `lineage_kit/runs.py`, `medallion/schemas/events.py`, `lineage/seed.py`; kernels are `lineage_kit/{emitter,runs}.py`, `service_kit/lancekit/lineage_emit.py`, `catalog/core/lineage_emit.py`, `maintenance/core/lineage_emit.py`. Only the producer-URI defect was fixed. The bronze-write emit is the cascade head, so a swallowed emit means the whole bronze→silver→gold run never happens and nothing reports it.
  **WHAT SWALLOWING COSTS, measured 2026-09-10 rather than argued:** ingest is the estate's only HTTP lineage producer, and `POST /api/v1/lineage` had served TWO requests in lineage's retained log and refused both — a 100% failure rate — while 806 events reached the graph over the Dapr topic from producers that never take that path. Every ingest run landed its rows with no provenance and reported COMPLETE. Two distinct causes, both now fixed (`d5cd2af1`, `06302b7f`): the emitter could present only the shared bearer at a door that refuses it from a privileged name, and the run's external INPUT was authorized as a governed table. Neither was visible from the suite; both came from driving the lane.
  **RE-MEASURED 2026-09-11 — THE DATA-LOSS HALF IS ALREADY CLOSED, and this row asks for a
  27-importer refactor to fix it.** The claim was that `lineage_kit/emitter.py` swallows a transport
  failure and stages nothing, "so every HTTP producer that is not ingest loses the event outright".
  That set is EMPTY. Measured at HEAD: the only SERVICE importing `lineage_kit.emitter` is ingest —
  the other five importers are modules inside lineage-kit itself — and ingest carries the backstop.
  Every other producer publishes through `service_kit.lakehouse.outbox::publish_lineage_with_outbox`,
  whose `_publish_staged` writes the event to the outbox BEFORE the publish and lets a publish failure
  propagate, leaving the staged copy for the relay. catalog, maintenance, medallion and lineage all
  route through it.
  **WHAT IS GENUINELY LEFT is two things, neither of them data loss on transport:** (1) the
  CONSOLIDATION — four kernels and three builders, which is duplication rather than a defect, and
  (2) a narrower residual the catalog's own header already names: it has no TRANSACTIONAL outbox, so a
  crash between the Lance write and the publish still loses the event. That one is not fixable by
  consolidation — it needs a durable producer, and the architecture has no DB to give it.
  **THE CP-007 BLOCKER IS CLEARED (2026-09-11).** This said staging was worth nothing until ingest could write the outbox at all; it can now — the credential is vended through the catalog's outbox door and the whole path was observed end to end (`lineage_outbox_drained drained=1 stranded=0`). Re-measured the same day, what remains is the CONSOLIDATION, which is what the R10 block is actually about: `lineage_kit/emitter.py` swallows a transport failure with `log.warning("lineage_emit_failed")` and stages nothing, so every HTTP producer that is not ingest loses the event outright. Ingest survives only because it carries its OWN `_stage_undelivered` backstop — one copy, in one service, which is precisely the duplication R10 exists to end.
- *Closes when:* Delete `service_kit.lancekit.openlineage`/`lineage_emit` and the per-service `lineage_emit.py` copies, route every producer through `packages/lineage-kit`'s emitter and one `RunEvent` builder, and stage each event in an outbox before transport so a failed emit is retried rather than dropped.

**LH-006 · `UPSTREAM`/`DOWNSTREAM`/column-lineage Cypher is unbounded `*1..`, and Dataset nodes carry no `latest_version`**
`lineage` · med

- *Why open:* The `/producers` and retention/index clauses closed; traversal depth did not. Verified at HEAD: `services/lineage/src/lineage/services/cypher.py:329,334,445,448` are all `*1..`, and `age.py:44` names the unbounded path over a grown graph as why a pooled connection cannot be pinned. `with_depth` (cypher.py:326) exists but no door applies a ceiling.
- *Closes when:* Apply `cypher.with_depth` (or a validated integer literal) with a `Query(ge=1, le=N)` bound to the `UPSTREAM`, `DOWNSTREAM`, `COLUMN_UPSTREAM` and `COLUMN_DOWNSTREAM` statements, and add a `latest_version` property to the Dataset node maintained on write.

**LH-007 · ~~`ray_stage_job.py` re-creates its target with `mode="overwrite"` every run, re-minting `_rowid` for the whole tier~~ — CLOSED 2026-09-11**
`medallion` · was HIGH

- *Closed by:* `2afdda03`. The distributed output lands in a staging dataset and ONE `merge_insert`
  converges it (`_land_staged`), so a re-derivation keeps `_rowid` for every row that survives it.
- *OBSERVED ON THE LIVE ESTATE 2026-09-11*, two full distributed re-derivations of the same tier
  through the deployed job:

      run 1 -> version 1   rowids {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6, 7:7}
      run 2 -> version 2   rowids {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6, 7:7}

  The version advanced, so a real write happened; identity survived it. Before the change the second
  run emptied the tier and re-appended, shifting every id — which is how silver came to hold 8 of 8
  `source_rowid` values naming bronze rows that no longer existed. The staging set was cleaned up
  (`_staging` absent from the destination afterwards).
- *Why staging and not something cheaper:* `lance_ray` offers only create/append/overwrite — no
  distributed merge — and `enable_stable_row_ids` is create-time-only. An append CANNOT preserve
  identity: `lance_docs/file_format.md:3998` mints new ids "sequentially starting from `next_row_id`",
  while only an update remaps one (`:4025`). Measured over three append-then-retract runs, `id=2` moved
  `_rowid` 1 -> 3 -> 6; through `merge_insert` it held at 1. A driver-side streaming merge was
  disqualified separately: driver RSS scales with the SOURCE (442 / 973 / 2161 MB for 104 / 416 /
  1040 MB inputs), which defeats the reason the distributed lane exists.
- *The two refusals are the load-bearing part.* `when_not_matched_by_source_delete` keeps the full-sync
  semantics the stage always had, and against a ZERO-ROW source it matches every row in the
  destination and empties the tier. So an ABSENT staged output (the write returned without committing)
  and an EMPTY one (the transform produced nothing) are refused separately — they mean different
  things to whoever is debugging a lost stage. The estate already refuses this one lane over (the media
  retraction runs only `if written and run`); the sibling merge in `compute.py` carries no such guard,
  which is exactly how copying that call site would have imported the hazard.
- *`_staging` is a maintenance control prefix.* A staging set is a real Lance dataset while it exists.
  Once the destination exists the walk cannot reach it (it descends a dataset root's children only into
  `tree/`), but on the run that CREATES the destination the parent is a plain directory and a crash in
  that window leaves the set discoverable, compactable and counted as governed — proven by a RED test
  that reported it as governed before the fix.

**LH-008 · ~~Publication deltas are insert-only~~ — BOTH HALVES CLOSED 2026-09-11**
`medallion, catalog` · was med · the cascade's own handling of a DELETE is now LH-132

- *THE DELETION HALF IS CLOSED AND OBSERVED.* `6f58b07c` adds a `deleted` kind to the change feed,
  served on demand from `POST /v1/table/{id}/changes`. Driven on live storage 2026-09-11: three rows
  `{a:0, b:1, c:2}`, delete `b`, feed reports `[1]` and nothing else. A retracted row is followable.
- *SERVED ON DEMAND, not stamped into the publish event* (decided 2026-09-11). A deleted-row set is
  unbounded, so stamping it turns a large delete into a large message on the bus; publishing a version
  range and letting the consumer pull is what every change-data system of this shape does.
- *`deleted` is NOT a scan predicate, and the code refuses to pretend otherwise.* The version columns
  describe rows the table STILL HAS, so a deleted row is absent from every scan; `change_filter`
  refuses the kind with that reason and `dataplane.read_deleted_row_ids` answers from the TRANSACTION
  range instead. A feed that answered the wrong rows with a 200 is the failure `changes.py`'s own
  header exists to prevent.
- *Two facts that came from driving the real API rather than reading it:* `delta()` REFUSES an open
  window (`end_version=None` raises "Must specify both with_begin_version and with_end_version"), so
  the door closes an omitted end at the version of the dataset it opened — exact, because it is the
  same handle the delta is read from. And `get_deleted_row_ids()` returns ONLY `_rowid`, which is
  Lance's answer rather than a projection choice: the rows are gone.
- *THE UPDATE HALF IS CLOSED.* `ca5d6141` moves `scripts/ray_stage_job._delta_filter` to
  `_row_last_updated_at_version > N`. ONE column answers both kinds, which is measured rather than
  assumed: a never-updated row carries that column equal to its CREATION version (three rows at v1, an
  append at v2, an update at v3 read `[1, 1, 2, 3]`), so `> N` selects inserted and updated together.
  The catalog's feed still keeps the kinds apart — a consumer applying both streams double-counts an
  insert reported as both — while this lane hands whatever it selects to one `merge_insert` on `id`.
- *A SWEEP DOES NOT TURN THE DELTA INTO A FULL RESCAN,* which was the reason to check before shipping
  it: measured 2026-09-11, `compact_files` (4 fragments → 1) and `cleanup_old_versions` leave both
  version columns byte-identical, so a maintenance pass re-derives nothing.
- *Pinned as the whole boundary rather than the case that fired* (`tests/unit/test_ray_stage_job.py`):
  changed-by-update, changed-by-insert, and untouched — the last asserting the delta carries exactly
  the changed row, so a future widening that quietly restores a full rescan fails here.

**LH-009 · ~~branch-blind reconcile~~ — THE CORRUPTING HALF CLOSED 2026-09-11; a coverage gap remains**
`lineage` · was med

- *Closed by:* `d5f1f19c` + the pin in `services/lineage/tests/test_the_reconcile_legs_agree_on_one_ref.py`.
  Reconciliation compares three things and acts DESTRUCTIVELY on the difference — it MERGEs a synthetic
  run and a versioned WROTE edge. The comparison is only meaningful if all three legs name one ref, and
  they did not: `LATEST_WRITE_VERSION` returned whichever write was most recent by `event_time`
  INCLUDING one that landed on a branch, and that was compared against main's on-disk version. A branch
  write therefore read as main drift and triggered a back-fill, on a 300 s tick.
- *All three legs now mean MAIN, and that is pinned as a SET rather than one assertion each* — the
  failure mode is one leg becoming branch-aware while the others do not:

      graph    LATEST_WRITE_VERSION   filters `w.ref IS NULL`
      storage  read_storage_version   opens `lance.dataset(uri)`, which is main only
      repair   backfill_write         stamps no ref, so it records the main write it read

- *WHAT REMAINS IS A COVERAGE GAP, not corruption:* a branch is never reconciled at all, because the
  storage leg only ever opens main. A branch that drifts is invisible rather than mis-reported. That is
  the safe direction and arguably correct — the reconciler exists to answer "does the graph agree with
  the table" — but it should be a deliberate scope statement rather than an accident, and reporting
  branch coverage as EXCLUDED (the way the sweep reports its other exclusions) would make it visible.

**LH-135 · ~~the running FGA model is missing a relation the repo defines~~ — NOT A MODEL DEFECT; CLOSED 2026-09-11**
`catalog` · was med

- *WHAT IT ACTUALLY WAS: a stale IMAGE, not a broken write path.* The catalog IS the FGA provisioner
  (`main.py:140`, `provision=True`) and rewrites the authorization model at boot from its own bundled
  `model.json`. The estate was running `lance-rest-catalog:main-8c229296`, built before
  `warehouse#event_stager` existed — measured, that image's bundled model contains ZERO occurrences of
  it — so every boot re-published a model without the relation and `bootstrap-admin` failed on the
  tuple for `user:service-ingest`, on every upgrade, for as long as the estate stayed behind main.
- *Closed by deploying current main.* Verified on the live estate: the deployed pod's `load_model()`
  carries the relation, `check can_stage_events` for `user:service-ingest` on
  `warehouse:lance_catalog` answers **True**, and the exact tuple write that had been failing now
  succeeds.
- *THE ROW'S OWN EVIDENCE WAS WRONG and that is worth keeping.* It recorded "`fga.provision()`
  returned a NEW model id whose contents, read back by id, contain zero occurrences" — the read-back
  queried the wrong model, and the conclusion drawn from it (that the write path publishes a model
  missing a relation its source defines) described a defect that did not exist. The lesson is the
  estate's own: a verdict is not evidence it is still true, and a read-back has to be shown to be
  reading the thing it claims to.
- *What remains is a REAL gap this exposed, filed on its own terms:* nothing detects that the deployed
  catalog's model is older than the chart's grants. The failure was visible only as a job that failed
  on every upgrade and was never chased.

**LH-134 · Credential vending accumulates one STS identity record per vend, and at ~100k the store cannot restart**
`catalog, chart` · **HIGH** · filed 2026-09-11 · found by an outage, not by a review

- *WHAT HAPPENED.* The object store was restarted during the LH-133 cutover and never came back. It
  answered `/health` 200 and `/health/ready` 503 forever, with its own log saying why: repeated
  `walk_dir` timeouts (`timeout_ms: 5000`) over `.rustfs.sys/config/iam/`, then
  `IAM failed to load initial data after 3 attempts`, then **`IAM bootstrap retry failed; service
  remains degraded`**. It does not retry past that, so the store is permanently unavailable.
- *THE COUNT IS THE CAUSE:* **107,485 entries** under `.rustfs.sys/config/iam/sts/` — one
  `<access-key>/identity.json` per `AssumeRole`. `vending.mode: sts` has been the default since
  2026-09-03 and every vend mints one; nothing prunes them, and the IAM load walks all of them inside
  a 5-second disk timeout.
- *IT IS NOT A PROPERTY OF THAT STORE.* Measured on the NEW store roughly twenty minutes after
  cutover: **867** entries already under `.minio.sys/config/iam/sts/`. MinIO documents a purge of
  EXPIRED STS credentials, which RustFS appears not to have — but "documents" is not "observed", and
  the failure mode is a store that will not start, so it has to be driven rather than assumed.
- *WHAT MAKES IT A CONDITION-5 ROW rather than housekeeping:* nothing measured it, nothing alerted on
  it, and the symptom is indistinguishable from a hung store. The estate ran for days one restart away
  from an object store that could not come back, and the only reason it surfaced is that a migration
  restarted the pod on purpose.
- **RATE AND SOURCE MEASURED 2026-09-11, and the rate makes this urgent rather than tidy.** Sampled
  over 60 s on the live store: **280 new entries per minute — 16,800/hour**. The previous store became
  unrestartable at 107,485, which at this rate is reached in **about six hours**. The earlier "867
  after twenty minutes" reading understated it.
- *THE SOURCE IS THE SWEEP, and the waste is structural rather than a busy estate:* the audit trail
  names `service-maintenance` vending `tier=write` per TABLE, and `sweep._maintain_one`'s dispatch
  calls `credentials.write_options_for` for every PLANNED dataset — before anything has decided the
  dataset needs a rewrite. The estate's own history is that almost nothing is ever rewritten
  (`fragments_removed_total=0` across 785 ticks), so nearly every one of those 900-second credentials
  is minted for a unit that writes nothing.
- *`credentials.py` argues "NO CACHE, deliberately — maintenance vends once per WORK ITEM", and the
  argument is sound while the premise holds.* It does not: the vend is per PLANNED item, not per item
  that writes. The fix that keeps the design is to vend LAZILY — decide with the read credential
  whether this unit will write, and only then ask for a write credential — which leaves the
  no-cache reasoning intact and removes the ~130 wasted vends per tick.
- **CLOSED AND OBSERVED 2026-09-11.** The sweep now asks, with a READ and before any credential, whether
  the unit can write anything at all; a dataset that cannot is maintained under the ambient read
  credential and never vends. Measured on the live estate across the deploy:

      before   3,913 records   growing 280/min (16,800/hour)
      after    2,232 records   growing     0/min

  The count FELL, which answers the open question about MinIO's purge: it does run, and it was simply
  being outpaced roughly fifty to one. The sweep is unaffected — policies loaded, 95 registry buckets
  discovered, datasets walked, no errors.
- *The probe is conservative and says so in one direction only:* more than one fragment, any superseded
  version with cleanup on, any real index with optimize on, and every unreadable or unanswerable case
  all count as "may write", because skipping a dataset that WOULD have been maintained is the failure
  this service exists to prevent while a spare credential is only a cost.
  A vend TTL of 900 s means every record older than that is garbage by construction, so the check is
  cheap: count the prefix, alert on growth that does not fall. Do not close it on documentation.

**LH-133 · ~~Swap the object store from RustFS to MinIO~~ — DONE AND OBSERVED 2026-09-11**
`chart, storage, catalog` · was HIGH · unblocks LH-051, which is now the open half

- *OBSERVED ON THE LIVE ESTATE, not inferred from a successful rollout:* the store serves **109
  buckets / 24,147 objects / 7.2 GiB**, `lance-catalog` matches the source object-for-object (2,180),
  the fleet is 48/48 healthy with nothing unready, maintenance sweeps datasets against it, the
  lineage plane writes without a denial, and ZERO rustfs objects remain. The four old PVCs are KEPT
  as the rollback and deliberately not reclaimed.
- *The StatefulSet adopted the pre-seeded volumes by name*, so the data was in place before the store
  first started — no second copy and no empty-store window, as the corrected cutover plan below says.
- **FOUR CHART DEFECTS THIS FOUND, each of which failed a deploy rather than a test**, and all now
  fixed: a hand-edited `Chart.lock` whose digest no longer described `Chart.yaml`; a `pre-upgrade`
  hook consuming an ESO-delivered key the same upgrade introduces (a deadlock by construction —
  seeding OpenBao does not break it and a hand-patched Secret is reverted by ESO within seconds);
  dropping that phase producing the OPPOSITE deadlock, since the fleet cannot become ready without
  its scoped users and post-upgrade hooks only run after readiness; and ESO publishing only the
  SECRET half of the root pair because the access key was added to the ESO-OFF render alone.
- *Two failures that were already there and are not this row's:* the deployed catalog image predated
  the `warehouse#event_stager` relation the chart grants, so `bootstrap-admin` failed on every
  upgrade until the estate was moved onto current main; and the old store could not restart at all
  (LH-134).

- *WHY, in one measurement:* RustFS gates `AssumeRole` to root and the right cannot be granted — a
  policy-attached scoped user gets HTTP 403 while root gets a session token, and `mc admin policy
  create` refuses an `sts:AssumeRole` statement outright (*"invalid resource, type: 'unknown'"*). The
  identical probe on MinIO accepts the scoped user AND enforces the narrowing (cross-tenant GET and
  read-tier PUT both `AccessDenied`). Full evidence on LH-051. This is NOT the ARN-roles feature on
  RustFS's roadmap — letting a non-root user assume at all is a different capability, and it is the
  one the estate needs.
- *THE DATA PLANE IS ALREADY PORTABLE, which is what makes this a chart migration rather than a
  rewrite.* Measured: 40 Python files mention `rustfs` and essentially all of it is PROSE — comments,
  docstrings and one operator-facing error message — plus a single config DEFAULT
  (`media/config.py::s3_secret_field = "rustfs-secret-key"`). `storage/client.py` is generic by
  construction and says so in its first line. The endpoint is already `RASK_S3_ENDPOINT_URL` with
  alias fallbacks, so no service needs a code change to point elsewhere.
- *THE SURFACE IS THE CHART:* 25 templates name RustFS and `values.yaml` carries 78 such lines. The
  load-bearing ones are the operator + `rustfs-tenant.yaml` (a Tenant CR with a `volumeClaimTemplate`
  whose keep-PVC durability posture the P4 ruling pins — the MinIO equivalent must preserve it),
  `rustfs-scoped-users.yaml` (which ALREADY drives `minio/mc` and whose `mc admin policy create` /
  `user add` / `policy attach` calls port unchanged), the bucket bootstrap job, `external-secrets.yaml`
  and `_helpers.tpl`.
- *DO NOT FOLD LH-051 INTO THIS ROW.* The swap is the prerequisite; giving the catalog a scoped user
  and a warehouse-covering policy is the fix, and keeping them separate is what lets the migration be
  verified on its own (every existing suite still green against the new store) before the credential
  changes underneath it.
- *Two things to settle while doing it, neither a blocker:* MinIO's community edition is AGPL-3.0 and
  its features have been moving to the commercial AIStor — it runs as a separate server so it does not
  reach rask's own Apache-2.0 licensing, but the direction is worth knowing. And Ceph RGW is the other
  STS-complete option the vending module already names, at considerably more operational weight.
- *THE CUTOVER MIRRORS FROM A LIVE SOURCE, and that is what makes it safe.* Measured on the live
  estate 2026-09-11: the store holds **109 buckets / 24,132 objects / 7.1 GiB**, and the RustFS
  StatefulSet is OWNED by the Tenant CR (`controller: true`), so the upgrade garbage-collects the
  server while its four PVCs survive. The obvious order — upgrade first, then resurrect a reader over
  the orphaned volumes — makes the source a thing that has to be rebuilt before it can be read, and
  the recorded plan said exactly that. It is the weaker order. Instead:
    1. pre-create the four PVCs the chart's StatefulSet will claim by name
       (`data-{0..3}-rask-minio-0`) and run a temporary MinIO on them with the root credential the
       chart renders;
    2. `mc mirror` bucket by bucket from the RUNNING RustFS into it — source healthy throughout,
       nothing to reconstruct if it goes wrong, and the old PVCs untouched as the rollback;
    3. delete the temporary pod and upgrade. A StatefulSet claims PVCs BY NAME, so the chart's store
       adopts the seeded volumes and comes up holding the data — no second copy of 7.1 GiB and no
       window where the estate has an empty store.
  The lakehouse is unavailable only for the upgrade itself.
- *TWO BEHAVIOURS THE HOOKS DEPEND ON WERE MEASURED AGAINST RUSTFS AND ARE NOT YET RE-DRIVEN:* that
  `mc admin policy create` OVERWRITES (the `post-upgrade` pass is only sound because it does), and
  that a `StringNotLike s3:prefix` condition on a Deny is ENFORCED rather than dropped. Both are
  called out in `minio-scoped-users.yaml` with their dates and the store they were taken on. A
  condition a store silently drops turns a Deny into a hole rather than a hard failure, so these are
  re-drives, not formalities.
  **RE-MEASURED 2026-09-11 — ALREADY FIXED, the same day, by `2afdda03`.** The distributed tabular
  branch no longer overwrites: it stages under `<dst>/_staging/<idempotency-key>`
  (`ray_stage_job.py:832-848`), `_land_staged` runs ONE
  `merge_insert("id").when_matched_update_all().when_not_matched_insert_all().when_not_matched_by_source_delete()`
  (:710-712) and `_drop_staged` cleans up (:852-854). Measured through `_run_stage`'s REAL distributed
  branch: identity survives a re-derivation. The row's framing ("scratch+merge confirmed, needs three
  guards") is the `1feb3a00` state that `2afdda03`/`f791af95`/`4546b0f2` superseded hours later.
- *Closes when:* the chart deploys MinIO in place of RustFS, every bucket and scoped user is
  provisioned by the ported hooks, the credential-isolation e2e passes against it, and the estate is
  observed serving the lakehouse from it end to end.

**LH-132 · The cascade's delta lane and its full lane DISAGREE about a deleted row**
`medallion` · med · filed 2026-09-11

- *MEASURED, not inferred.* Bronze `{0,1,2}` derived to silver; delete bronze `id=1`; a DELTA run
  (`base_version` set) leaves silver holding `[0, 1, 2]` and logs `RAY-STAGE OK … lane=delta rows=0
  delta_empty=1` — it reports "nothing changed" about a retraction. A FULL run over the same upstream
  answers `[0, 2]`. So whether a delete propagates depends on which lane the scheduler happened to
  pick, and the lane that skips it is the one an operator reads as the cheap, correct path.
- *This is LH-008's title's second half, one layer down.* The catalog door serves deletions on demand
  (`6f58b07c`) and that is closed; the estate's OWN consumer — the cascade — still does not apply them.
- *THE JOIN IS THE ROOT KEY, and root provenance is exactly what makes it reach every hop.*
  `DatasetDelta.get_deleted_row_ids()` yields the upstream `_rowid`s that vanished, which joins only at
  the head (silver's `source_rowid` IS bronze's `_rowid`). What reaches deeper is the column both sides
  already share: `carry_source_rowid` KEEPS root provenance rather than re-minting per hop, so measured
  2026-09-11 over a real three-tier chain, `gold.source_rowid == silver.source_rowid == bronze._rowid`.
  The upstream side of the join is therefore `source_rowid` where it exists and `_rowid` at the head —
  the same head-detection the stamp itself uses — and the downstream side is always `source_rowid`.
- *So the mechanism is a set difference over one int64 column per side*, deleting the small dead set
  rather than filtering on an unbounded `NOT IN`. It is exact for `1:1` at every hop and for `1:N` at
  the head. Its ONE imprecision is `1:N` deeper in: siblings share a root key, so deleting one of
  several children upstream leaves the root present and the tier below keeps its rows. That is an
  under-deletion — the safe direction, and it must be stated rather than discovered.
- *Two guards this cannot ship without:* the retraction has to run BEFORE the lane's `delta_empty=1`
  early return, because a deletion-only change IS an empty delta (that is precisely what the
  measurement above shows); and an upstream whose key set reads back empty must REFUSE rather than
  retract, or one unreadable scan deletes a governed tier — the same guard shape as
  `StagedOutputEmptyError`.

**LH-010 · HTR-lane cascade residuals: the P7b re-cut, the bronze→silver geometry stage runners, and populating the in-dataset `lineage` column**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* Two of the three bundled residuals are real, but the THIRD is done: the in-dataset `lineage` column is populated end-to-end today — the work order carries `lineage_document`, the Ray job reads it from `RASK_LINEAGE_DOCUMENT`, and `stamp_stage` writes it as a `pa.json_()` column. The smaller true fix is the P7b runner re-cut plus the bronze→silver geometry stage runners; drop the lineage-column clause.
  **Evidence:** DONE: services/medallion/src/medallion/services/transform.py:742 (`lineage_document=lineage_doc.to_json()`) → packages/service-kit/src/service_kit/lakehouse/work_order.py:71-73,153 (`lineage_document: str = ""`; `("RASK_LINEAGE_DOCUMENT", self.stamp.lineage_document)`) → scripts/ray_stage_job.py:444 (`lineage = os.environ.get("RASK_LINEAGE_DOCUMENT", "")`) → packages/service-kit/src/service_kit/lakehouse/stage_stamp.py:138-142 (`out = _set_or_append(out, pa.field(LINEAGE_COLUMN, pa.json_()), document.cast(pa.json_()))`); also services/medallion/src/medallion/services/ray_submit.py:197. STILL OPEN: the htr runner has no Lance read/write at all — grep for `lance` across runners/htr/src returns only two unrelated comment hits (runners/htr/src/runner/main.py:126, runners/htr/src/runner/transcribe_service.py:254), and its entrypoint still drives `build_source`/`build_sink` over S3/FS (runners/htr/src/runner/main.py:22, :42-60) into ALTO writers (runners/htr/src/runner/pipeline.py:9-16); no geometry stage runner exists in chart/values.yaml:1442-1450 (`bronze-to-silver`, `silver-to-gold`, `media-to-silver`) and none in services/medallion/src/medallion/services/.
  **Reopen if:** Show `stamp_stage`'s lineage column is not actually reaching a governed tier at runtime (e.g. `lineage_document` is empty on every real cascade path), which would put the third clause back in scope. Conversely, a `lance` import + bronze-read in runners/htr/src/runner would close clause one.
`medallion, catalog` · med

- *Why open:* #88 closed witnessed end-to-end 2026-08-05 but its residuals were folded here at `open_htr_governance.md`'s retirement and none have landed: the owner-directed P7b re-cut, the geometry stage runners, and the in-dataset `lineage` column that rides when the stage runner supplies the LineageDoc.
- *Closes when:* Re-cut the runner's stage job to read bronze Lance and emit gold rows directly (reusing the lane's parser/register/facet seams), add the bronze→silver geometry stage runners, and populate the in-dataset `lineage` column from the stage runner's LineageDoc.

**LH-011 · ~~three lineage knobs ship off in the prod render~~ — TWO WERE ALREADY RIGHT; the third needs a NUMBER**
`lineage` · was med · **blocked:** owner — what freshness budget?

- *RE-MEASURED 2026-09-11, confirming the 09-10 reading:* `runRetentionDays: 30` (values.yaml:497) and
  `compaction.lineageEmit: true` (:1660) are correct in the values the prod render inherits, and
  `values-prod.yaml` overrides neither. Two thirds of this row describe a defect that is not there.
- *WHAT IS ACTUALLY OPEN is one value, and it is a POLICY choice rather than a fix:* `freshnessBudgetHours`
  is 0 (off). Turning it on makes the reconcile sweep and the per-dataset GET flag `stale: true` for any
  dataset whose newest commit is older than the budget, and WARN `lineage_reconcile_stale` every tick
  for each one. So the number IS the contract — too low and every tick warns about data that is fine,
  too high and the clause asserts nothing. There is no defensible default to infer from the code, which
  is why this is marked blocked rather than picked.

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* Two of the three named knobs are already correct in the values the prod render inherits — `runRetentionDays: 30` and `compaction.lineageEmit: true` ship in `chart/values.yaml` and `values-prod.yaml` overrides neither — so only `freshnessBudgetHours: 0` is genuinely unset; the smaller true fix is one line plus the live confirmation that the pruner deletes.
  **Evidence:** chart/values.yaml:497 (`runRetentionDays: 30`, with the 2026-09-08 owner ruling recorded at :488-496 — "30 BY OWNER RULING 2026-09-08, and 0 (keep everything) is what it replaced"); chart/values.yaml:1655 (`lineageEmit: true`, "ON since 2026-08-16"); chart/values.yaml:434 (`freshnessBudgetHours: 0` — the one still off). chart/values-prod.yaml sets none of the three (grep across the file returns zero hits), and its `services.lineage` block at :37-45 overrides only `replicas`, `demoData`, `outbox.enabled`, `reconcile.enabled`, so Helm's map merge leaves 30/true/0 standing.
  **Reopen if:** A prod overlay, `values-live-pins.yaml` entry or `--set` in the deploy path that re-zeroes `runRetentionDays` or flips `lineageEmit` off; or a rendered prod manifest showing `RASK_RUN_RETENTION_DAYS=0`. The 'confirm the pruner actually deletes Run nodes' half remains unverified — I did not query the live graph.
`lineage, maintenance, chart` · low · **blocked:** owner decision on the retention window (14d flagged as short for a compliance trail)

- *Why open:* Recorded as a NICE gap and never flipped: prod has the reconcile pruner deployed with the retention knob off, so Run nodes grow forever, and the compaction FAILURE lineage surface stays dark.
- *Closes when:* Set `runRetentionDays`, `compaction.lineageEmit: true` and a real `freshnessBudgetHours` in `chart/values-prod.yaml`, then confirm the reconcile pruner actually deletes Run nodes past the window.

**LH-012 · ~~`write_gold` silently falls back to a `pa.string()` lineage column~~ — CLOSED 2026-09-11**
`medallion` · was low

- *The fallback is deleted, so a `pa.json_()` failure raises.* It never degraded the column: a string
  column looks identical in a schema print and is silently unqueryable as provenance — every JSON
  function raises a coercion error on it and the JSON scalar index refuses it outright — so what it
  produced was a column that cannot answer the question the column exists for, reported as success.
- *Pinned by the PREMISE rather than the absence* (`tests/unit/test_the_gold_lineage_column_is_really_json.py`):
  deleting a fallback is only safe while `pa.json_()` actually works in the pinned pyarrow, so the
  test asserts that, and that a string column is not an interchangeable substitute. A test asserting
  "the except clause is gone" would pin the edit; this pins the reason.

- *Why open:* On a plain `pa.string()` column every JSON function and the JSON scalar index fail (`json_get_string` coercion error; 'A JSON index can only be created on a Binary or LargeBinary field') — a silently unqueryable provenance column. pyarrow is pinned to 24.0.0 where `pa.json_()` works, so the fallback is dead code today, which is exactly why nobody will notice when it stops being dead.
- *Closes when:* Delete the `except (AttributeError, ArrowNotImplementedError, TypeError)` fallback in `scripts/medallion_demo.py::write_gold` so a `pa.json_()` failure raises loudly.

**LH-136 · ~~The reconcile sweep compares two MAXIMA, so a lost lineage event below the tip is never found and the dataset reports `in_sync`~~ — FIXED 2026-09-11**
`lineage` · was **HIGH** · owner condition 1 (a write's provenance survives it)

- *FOUND BY DRIVING THE LIVE ESTATE 2026-09-11, not by reading a row.* `reconcile` compared
  `latest_write_version` (the graph's newest `WROTE` edge) against the dataset's current on-disk
  version. Two maxima can agree while an intermediate version has no edge at all, so a write whose
  lineage event was lost and which a later write then superseded was invisible: the sweep classified
  the dataset `in_sync`, and `BACKFILLABLE_STATES` — the only path to recovery — was never entered.
  The provenance of that version was gone permanently, and nothing else in the estate looked for it.
- *MEASURED, on the deployed catalog and lineage services:*
  - `bronze$events` answered `{"in_sync": true, "graph_version": 87, "storage_version": 87}` while its
    retained versions **76 (`Overwrite`), 80, 82 and 83 (`Update`)** carried no lineage event.
  - `transcripts_v2$annotations` answered `{"in_sync": true, "graph_version": 7, "storage_version": 7}`
    with versions **1, 2 and 4** un-provenanced.
  - Estate-wide, **8 of 29 retained data-operation versions held no provenance**, under a reconciler
    reporting perfect health on every one of them. Lance's own transaction log is what separates the
    two classes: `Append`/`Overwrite`/`Update` changed what the table says, while `Rewrite`,
    `BaseOperation` and `CreateIndex` are maintenance and are not expected to carry provenance.
- *THE HOLE NEEDS NO CATALOG DOOR, which is why it is a provenance property rather than an endpoint
  bug.* A write-tier vended STS credential grants `PutObject` on `<table-prefix>/*`, and
  `core/vending.py:148-155` says in those words that this "also covers `_versions/`" — so the holder
  can commit a whole Lance version client-side, manifest included. Driven end to end: a vended write
  credential appended version 2 to a governed table; `/history` reported `{"version": 2, "operation":
  "Append"}` and the graph held one event, for version 1. **The estate's production direct-writers are
  all legitimate users of that right** — `scripts/ray_stage_job.py` commits silver and gold with
  `write_dataset`/`merge_insert`/`delete` against the URI, and maintenance's in-pod compaction rewrites
  and reclaims versions — so denying `_versions/` in the session policy would break the cascade. The
  writer emits its provenance voluntarily; this sweep is the floor under that, and the floor had a hole.
- *FIXED.* `core/reconcile.py` gains `read_storage_versions` (one manifest-directory listing, the same
  call the freshness axis already pays) and `_recover_holes`; the repository gains `write_versions`
  (`cypher.WRITE_VERSIONS`, `w.ref IS NULL` so a branch's own version sequence cannot answer for
  main's); `ReconcileStatus.versions_without_lineage` and `SweepReport.provenance_holes` report it, and
  the cron back-fills each hole with the same minimal edge the tip back-fill already writes.
- *THREE PROPERTIES THE FIX IS PINNED ON*
  (`tests/unit/test_the_reconcile_sweep_sees_provenance_holes_below_the_tip.py`): the comparison is
  ONE-DIRECTIONAL (`cleanup_old_versions` reclaims manifests, so a graph version storage no longer has
  is not a finding — reading that direction would make every maintained dataset permanently red, which
  is how an axis gets switched off); the axis is OFF without an injected reader; and an UNREADABLE
  dataset is never probed, because that absence is already the version check's finding.
- *WHAT THE AXIS THEN SHOWED ABOUT THE ESTATE, which is a bigger finding than the sweep bug.* Splitting
  `bronze$events`'s 87 versions by the AUTHOR on their `WROTE` edge: **61 carry a real producer** (full
  provenance), **9 carry both a producer and `reconcile`** (the sweep raced the emit — redundant, not
  lost), **13 carry ONLY `reconcile`** (the actor and the derivation are gone; the bare fact survives)
  and **4 carried nothing at all** (76, 80, 82, 83 — what this fix finds). All 23 retained versions of
  that table are DATA operations, so none of the 17 is a compaction being mis-read. **~20% of the
  busiest governed table's writes lost their producer's provenance**, in a contiguous band (65-67,
  71-84) that names a sustained Ray-lane emit failure rather than a one-off. The sweep is the floor;
  the producer is the defect, and it belongs to [[LH-004]]'s emit-kernel work, not here.
- *THE CLASSIFIER IS WHY THE FINDING IS WORTH READING.* First deployed without one, the axis reported
  `transcripts_v2$annotations` holes `[1, 2, 3, 4, 6]` — but 3 is a `CreateIndex` and 6 a maintenance
  version, so **2 of the 10 holes estate-wide were false positives**. `MAINTENANCE_OPERATIONS` is a
  DENYLIST of the three pylance documents as content-preserving (`Rewrite`, `CreateIndex`,
  `UpdateConfig`); everything else — including `BaseOperation`, which is what `type(op).__name__` yields
  for an op pylance has no subclass for, and an unreadable transaction's `None` — is unknown and is
  REPORTED. An allowlist would fail silent, which for a control whose only job is finding missing
  provenance is the one direction that cannot be tolerated. Paid one transaction read per HOLE, so a
  healthy dataset pays none.
- *WHAT IT DOES NOT RECOVER, stated so the axis is not mistaken for more than it is.* The back-filled
  edge carries `author='reconcile'` and no inputs — storage can supply THAT a version was written and
  its schema, never who wrote it or what it derived from. A hole recurring on the same dataset names a
  producer that is not emitting, which is a defect upstream; the recovery is a floor, not a substitute.
  Recovery is capped at `MAX_HOLES_BACKFILLED_PER_TICK` (25) per dataset per tick and the remainder is
  logged — an UNTRACKED dataset has every retained version as a hole, and an uncapped tick would make
  its cost a function of the largest history in the estate. The REPORT is never truncated.

**LH-137 · Two live faults block real cascade work and appear in no backlog row: `unconfined_uri` refuses every `bind86` silver→gold hop, and `/compaction_plan` answers 404 for medallion tier ids**
`medallion, catalog` · **HIGH** · found by the 2026-09-11 audit, observed but NOT root-caused

- *Why open:* Both were seen on the live estate and neither is tracked. (a) Every one of the 8
  silver→gold triggers for project `bind86` on 2026-09-10 was DROPped with
  `medallion_stage_from_uri_refused` and parked on `dlq.silver-to-gold` within a second of publish —
  `medallion_dlq_parked_total` steps in lockstep with `medallion_stage_refused_total{reason=unconfined_uri}`.
  So a whole project's cascade is stopped by a confinement check nobody has diagnosed. (b)
  `/compaction_plan` answers 404 for medallion tier ids, so the distributed compaction door cannot be
  driven for exactly the datasets the cascade writes (0 distributed commits against 24 in-pod fallbacks
  in 24 h).
- *NOT REPRODUCED 2026-09-11:* the stage-runner pods were recreated at 11:17 and the text log format
  renders no `extra`, so the specific `_preflight`/`_confine_from_uri` branch could not be named;
  GreptimeDB's `opentelemetry_logs` was not queried, and no new silver→gold traffic has run since.
  The refusal count in the current pod's window is 0 — absence of traffic, not evidence of a fix.
- *NARROWED BY READING 2026-09-11, and the narrowing is why a live drive is still required.* The
  refusal is `uri_within(read_root, trigger.from_uri)` at `transform.py:682`. BOTH sides are supposed
  to be catalog-sourced: `publication_trigger.py:147` carries `extra["location"]` ("the catalog's
  VENDED location … instead of composing a path of its own (I2)"), and `_resolve_roots`
  (`transform.py:614-628`) asks `describe_table_location(from_dataset)` LAST so it overrides every
  composed answer. Two catalog answers for one table should be equal, so the refusal means one of
  exactly three things: the publication's `location` extra is absent or stale, `from_dataset` resolves
  to a different id than the publication's object, or the tenant path composes a root the vend does
  not share. bind86's tables are in a per-project warehouse (`s3://bind86-wh/<hash>_bind86-<tier>$<t>`,
  read off the graph), which is the shape most likely to expose the third. The log line DOES carry
  `supplied` and `root` in `extra` — the deployed text formatter drops `extra`, which is why the
  branch cannot be named from the logs and a drive is needed.
- *Closes when:* Drive one `bind86` silver→gold hop (owner-authorised 2026-09-11), print `supplied` vs
  `read_root` at the refusal, and fix whichever of the three the values name; reproduce (b) by calling
  `/compaction_plan` with a medallion tier id and fixing whichever of the id resolution or the route is
  wrong.

**LH-138 · The reconcile TIP axis still stamps a `reconcile` edge on a maintenance version**
`lineage` · low · residue of LH-136's fix

- *Why open:* `_recover_holes` classifies holes with `MAINTENANCE_OPERATIONS` (a compaction/index/config
  version is not a provenance hole), but the TIP comparison in `reconcile()` does not: when the newest
  on-disk version is a `Rewrite`, storage is AHEAD of the graph, the dataset is classified
  `storage_ahead`, and the back-fill writes a `WROTE` edge saying a run wrote it. Nothing was lost and
  nothing is written twice — `backfill_write` MERGEs on a deterministic run id — so this overstates
  rather than loses, which is why it is low.
- *Why it is NOT simply "apply the classifier at the tip":* the tip back-fill exists to realign the two
  maxima so the drift classification converges. Withhold it and a compacted dataset stays
  `storage_ahead` on every tick forever — the permanently-red failure the one-directional rule in
  `test_the_reconcile_sweep_sees_provenance_holes_below_the_tip.py` is written against. The coherent
  fix is to compare the graph's tip against the highest DATA version on disk, which costs one
  transaction read on the drift path only.
- *Closes when:* `reconcile()` resolves the storage tip to the newest non-maintenance version (reusing
  `read_version_operations`), so a compaction at the tip is not drift at all; pin that a `Rewrite` tip
  leaves the dataset `in_sync` with no back-fill, and that a data tip still classifies `storage_ahead`.

**LH-014 · The DIY provenance recipe (`stamp_stage`, `source_rowid`, the tier contract) is written down nowhere**
`medallion, lineage` · low

- *Why open:* Marked Doc in §O1 — anyone writing a new lane has to reconstruct the contract from the code.
- *Closes when:* Write the recipe (`stamp_stage`, `source_rowid`, the `{id, payload, stage, lineage, source_rowid}` tier contract) into `docs/architecture/` or the `rask-lance-catalog` skill.

### Catalog is correct for lance-ns

_The catalog is the estate's only door to Lance, so a spec deviation, an unregistered table or a silently-dropped parameter is a lie told to every client that trusts the spec._

**LH-016 · `bronze-media` and `silver-media` still hijack medallion namespaces inside `lakehouse-wh`, and unbind is refused because they hold real tables**
`catalog` · **HIGH** · **blocked:** owner decision (destructive on real tables) plus a human bearer — no service identity holds `can_administer` on `project:lakehouse`

- *Why open:* The unbind door landed and deployed (`DELETE /v1/warehouses/{id}/namespaces/{ns}`, 0280adfb) and `gold` unbound 200, but `bronze-media` answered 409 NamespaceNotEmptyError ('still holds 1 table(s): objects') and `silver-media` holds `features` at `s3://lakehouse-wh/a76d1ca5_silver-media$features`. The plan claimed both prefixes were empty (a pyarrow FileSelector returning 0 entries); the catalog disagreed.
- *RE-MEASURED 2026-09-11 — IT IS ONE NAMESPACE NOW, NOT TWO.* Listed `lakehouse-wh` on the live store
  (post-migration): `a76d1ca5_silver-media$features/` is still there, and so is a second spelling,
  `fa8bff0d_lakehouse$silver-media$features/`. **`bronze-media$objects` is not in that bucket at all**,
  so the half of this row that needed a drop-or-relocate decision for bronze no longer has an object to
  decide about. The decision that remains is silver's alone.
- *Still genuinely BLOCKED, and correctly so:* the remaining action is destructive on a real table, and
  no service identity holds `can_administer` on `project:lakehouse` — by design, since a service that
  could unbind a tenant's namespace is a service that could unbind any of them.
- *Closes when:* Owner decides drop-or-relocate for `silver-media$features` (both spellings), then
  calls the unbind door for that namespace. with a human bearer holding `project:lakehouse#can_administer`.

**LH-018 · The governed commit door is the non-spec `/commit`; `CreateTableVersion`/`BatchCommitTables` carry no lineage, gate, protection or replay marker**
`catalog` · **HIGH** · **blocked:** owner acknowledgement of R1

  **RE-MEASURED 2026-09-11 — EVERY CLAUSE IS TRUE AND THE ASK IS STILL WRONG.** Measured by hand
  against the deployed catalog as well as by audit: of the three ops the title names, **two answer 406
  `UnsupportedOperationError` on the dir backend** — `batch_commit_tables` and
  `batch_create_table_versions` — so they carry no lineage because they do nothing, and attaching
  governance to them is decoration. Only `create_table_version` is live, and it is FGA-gated at
  `can_write_data`, **the identical rung the governed `/commit` door uses** (bob, a non-owner, cleared
  both and was refused 403 at `protection` and `deregister`). "Protection" applies to NEITHER: it is a
  DELETION control (`require_not_protected` is called only from drop/deregister/rename/warehouse/project
  delete). So the real gap is ONE op missing a lineage emit and a replay marker, not a governance
  chain bypassed. **The `managed_versioning=true` clause is the dangerous one:** the spec defines it as
  the caller using namespace version ops "instead of relying on Lance's native version management" —
  advertising it invites clients onto a catalog-mediated commit pointer, which is the Iceberg shape
  CLAUDE.md's permanent LANCE-ONLY ruling exists to avoid.
- *Why open:* Version routes at `endpoints/versions.py` are mounted and FGA-gated (`_BATCH_PATHS`, `_action_relation` → `can_write_data`) but nothing else runs on them, and `managed_versioning` is never advertised in `DescribeTable`, so a stock Lance client's commit bypasses the whole governance chain. `batch_commit_tables` is `UnsupportedOperationError` on the dir backend and always will be.
- *Closes when:* Owner acknowledges R1 (the governed commit path IS the spec's managed-versioning path); then attach lineage emit, the quality gate, the replay marker and protection to `CreateTableVersion` in `endpoints/versions.py`, advertise `managed_versioning=true` in `DescribeTable`, alias then remove `/commit` (data.py:326-364, dataplane.py:556-637), and back `batch_commit_tables` with rask's own staged-manifest KV.

**LH-019 · rask-only governance side effects still run inside spec handlers (warehouse-scoped namespace refusal, trash soft-delete, protection 409, lineage keys in schema metadata, implicit BTREE, insert pre-coercion, maintenance 503 on POST reads)**
`catalog` · **HIGH** · **blocked:** the management-API carve, plus an owner ruling on the protection error code

  **RE-MEASURED 2026-09-11 — SIX of the seven side effects confirmed, and the remedy is aimed past
  its doors.** Confirmed executing inside spec handlers: the warehouse-scoped namespace refusal
  (`fga_deps.py:877` via `namespaces.py:132`), trash soft-delete (`tables.py:508-516`,
  `namespaces.py:481`), protection refusals inside drop/deregister/rename (`tables.py:476,597,899`),
  and the rest. The "Closes when" is partly aimed at doors that already answer and partly at a
  mechanism that does not do what it claims; its first clause is LH-021/R2 — a whole-surface carve
  needing owner acknowledgement — not work this row can do. The protection-code question (3 vs 19)
  remains an undecided owner tie.
- *Why open:* Only the `branch`-honouring clause closed; eight ops still refuse `branch` and the side effects change what a spec client observes — 7 of the 8 conformance blockers. Four refusals were measured as typed spec errors, but the protection refusal mints `NamespaceNotEmptyError` code 3 for a protected TABLE, so a generated client empties-and-retries forever; the recorded reason for not using code 19 was measured false and the 19-vs-3 split is an undecided tie.
- *Closes when:* Move the rask-only side effects behind the management API, re-express each remaining refusal with the spec's own code, decide the protection code (3 vs `InvalidTableStateError` 19) for all four protected object kinds, and honour `branch` on the eight refusing ops via the plumbing at `dataplane.py:1085`.

**LH-020 · Two of the three stock Lance clients still do not drive the deployed catalog: lancedb cannot address a nested namespace, lance-ray is untested**
`catalog` · **HIGH** · **blocked:** lancedb upstream fix; the lance-ray leg waits for the compute pass

  **RE-MEASURED 2026-09-11 — THE REMEDY'S PREMISE IS MEASURED FALSE.** Clause (a) holds: the
  conformance suite imports only `lance_namespace`, drives 10 read ops plus one write round trip, and
  nothing in `tests/e2e-py` imports lancedb or lance_ray. But step 1 of "Closes when" — *file the
  lancedb upstream issue (no way to express a multi-segment identifier via `open_table`)* — **would
  close nothing, because the premise is false in the locked version**: `namespace.py:573-577` and
  `lance_sdk.md:286` express it, and it was driven LIVE against the deployed catalog, reading 36 rows
  through lancedb. Filing that issue would spend an upstream ask on a bug that does not exist. What is
  genuinely open is the lance-ray leg and extending the suite across all three clients.
- *Why open:* Spec-verbatim REST is proven only for pylance's `RestNamespace` (10 read ops plus a full create/insert/tag/untag/drop round trip). lancedb 0.34.0 connects and lists the root but `open_table("acme-bronze$agnostic")` is refused 400 because it passes a single unsplit string in both path and body; relaxing `reconcile_body_id` is refused as an outer-layer workaround, so the fix is upstream and unfiled.
- *Closes when:* File the lancedb upstream issue (no way to express a multi-segment identifier via `open_table`); drive `read_lance(table_id=[...], namespace_impl="rest")` against the catalog with vended creds and `ray.init(address="local", _temp_dir=...)`; extend `tests/e2e-py/test_the_stock_lance_client_drives_the_catalog.py` / `make e2e-spec-conformance` across all three clients.

**LH-021 · 25 rask-only route groups still sit on the spec prefixes instead of a versioned management API**
`catalog` · med · **blocked:** owner acknowledgement of R2

- *Why open:* `/commit`, `/credentials`, `/publish`, `/protection`, `/undrop`, `/maintenance/*`, `/policy/*`, `/access/*`, `/history`, `/blobs`, `/v1/warehouses`, `/v1/projects`, `/v1/model`, `/v1/access`, `/v1/events`, `/v1/me`, `/v1/user-state`, `/v1/stores` plus non-spec query params, headers (`X-Lance-Run-Facets`, `x-lance-originator`) and envelope dialects all sit on `/v1/{namespace,table,materialized_view,transaction}`, which is what makes the side effects observable to a spec client.
- *Closes when:* Owner acknowledges R2; then move those 25 route groups (full list in `docs/audits/lakehouse-2026-09/lance-conformance-and-build-rules.md` §4) onto a separate versioned management prefix, Lakekeeper's `/management` vs `/catalog` split.

**LH-022 · The joint `lance-namespace` 0.12.0 + pylance bump is unmade, so `merge_insert`'s `on` stays a single string and a composite merge key is inexpressible**
`catalog` · med · **blocked:** upstream pylance/lance-namespace `on` type agreement

- *Why open:* The 0.12.0 bump was made and reverted 2026-09-07: `MergeInsertIntoTableRequest.on` is `List[str]` in 0.12.0 while pylance's native `merge_insert_into_table` takes `str`, so merge through the native path breaks for any value of `on`. The 2026-09-09 pylance 10→11 bump did not lift the ceiling (11.0.0 declares the same `lance-namespace<0.9` cap). The `on: list[str]` door was likewise implemented and reverted because only the BRANCH path would have worked.
- *Closes when:* Establish how the pylance 11 Rust binding types `merge_insert_into_table(on=...)`, lift the `<0.12` ceiling on the nine pins with whatever pylance version accepts a list, then re-apply the `on: list[str]` door in `data.py`'s merge handler plus the per-column index coverage and re-run the catalog + integration suites.

**LH-023 · A client-supplied `delimiter` is refused 400 rather than honoured on all 153 catalog ops**
`catalog` · med

- *Why open:* Only the silent half closed: a router-level guard now refuses an unsupported delimiter with code 13 instead of reporting a real table as 404. Honouring it means threading the delimiter through both `parse_identifier` AND `fga.canonical_object_id`, and authorizing against a differently-spelled object was judged worse than the bug being fixed — so the design was deferred, not done.
- *Closes when:* Add a request-scoped delimiter dependency feeding `core/identifiers.py::parse_identifier` (lines 59-63) and `fga.canonical_object_id` so identifier splitting and FGA canonicalisation agree, declare `delimiter` on the served ops, and replace the refusal guard.

**LH-024 · ~~Every Q13 `LANCE CLAIM` verdict was measured on pylance 10.0.0 and is unverified against the locked 11.0.0~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* There are no Q13 `LANCE CLAIM` verdicts at HEAD to re-measure: the register that held them was deleted on 2026-09-10, and the only occurrence of either phrase in the tree is this backlog row itself — the surviving truth is narrower, that pylance-9/10-measured prose still sits in service-kit and the skill under a lock pinned to 11.0.0.
- *Evidence:* `grep -rln 'LANCE CLAIM\|Q13' . --include=*.md` returns exactly one file: open_backlog_left.md (the row at line 217). The 13 `LANCE CLAIM` rows lived in open_lakehouse_diff_left.md, deleted by commit 8e91896e ("docs(backlog): one register of what is LEFT", 2026-09-10) — `git show 8e91896e^:open_lakehouse_diff_left.md | grep -c 'LANCE CLAIM'` = 13, and several were already re-measured on pylance 11 before deletion (Q13-2 and Q13-3 carry "STRUCK 2026-09-09 — RE-MEASURED ON pylance 11"). Q13-5 survives as its own row (LH-009). The lock is pylance 11.0.0 (uv.lock; pyproject.toml:72). The residual: claims measured on older pylance still stand in packages/service-kit/src/service_kit/lakehouse/features.py:109,228,245,400,500,508, objectfs.py:215, blobs.py:63,197, work_items.py:129, lance_session.py:6, lance_metrics.py:8 and .claude/skills/rask-lance-catalog/SKILL.md:287,297,317,474,491,518.
- *What would reopen it:* Finding a Q13 section with `LANCE CLAIM` rows in any tracked file at HEAD would restore the row as written.

**LH-025 · ~~`create_table`'s properties never reach the dataset~~ — CLOSED 2026-09-11**
`catalog` · was med

- *Closed by `9dc53371`.* `_write_blob_into` now merges the caller's properties into the dataset's
  schema metadata — the only place holding both the freshly-written dataset and the properties.
  MERGE, never `replace=True`: a replace drops the internal `lineage.*` coordinates. Nothing has
  written them at create time, but the rule belongs to the seam rather than the call site.
- *Pinned through the REAL write* (`dir` backend + a real `lance.write_dataset`), read back through
  `read_schema_metadata` — the door a client uses. A test asserting on the response body would have
  passed the whole time the defect existed, because the echo was never the broken part; the eviction
  case is asserted through the RAW dataset, since `read_schema_metadata` filters `lineage.*` out by
  design and could never have seen them disappear.

- *Why open:* Verified still true: `catalog/services/dataplane.py:324` passes `properties` into `ns.declare_table(...)` and lines 305/308/369 echo them back, but nothing writes them onto the dataset — while `update_schema_metadata` (dataplane.py:1423) proves the write path exists. So §7.1's 'stamped at create' is not readable off the table.
- *Closes when:* In `catalog.services.dataplane.create_table`, merge `parsed_properties` into the dataset's schema metadata through the same seam `update_schema_metadata` uses (never `replace=True`, so internal `lineage.*` keys survive), pinned by a test that reads the properties back OFF the Lance dataset.

**LH-026 · ~~Schema and data evolution is neither exposed nor governed — no add/alter/drop column door, no compatibility check, no owner gate, no schema history~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* The add/alter/drop-column doors DO exist, are FGA-gated, emit lineage and answer typed spec codes, and per-version schema history is served by lineage — what is genuinely missing is narrower: a compatibility check and an owner rung for a breaking change.
- *Evidence:* Doors: services/catalog/src/catalog/api/v1/endpoints/columns.py:54 (`/{id}/add_columns`), :85 (`/{id}/alter_columns`), :116 (`/{id}/drop_columns`), :150 (`/{id}/backfill_column`), plus `update_field_metadata` and `schema_metadata/update`; each awaits `lineage_deps.emit_measured_write` with ADD_COLUMNS/ALTER_COLUMNS/DROP_COLUMNS (columns.py:69-79, 100-110), and `branch` is honoured (columns.py:10-12). Authz is router-wide (api/v1/router.py:47 → fga_deps.authorize). Errors are typed, not raw pylance: dataplane.py:1300-1364 wraps every op in `_column_op`, which maps a missing column to `TableColumnNotFoundError` and a lost race to `ConcurrentModificationError` (dataplane.py:1075-1095), and refuses unsupported spec shapes with `UnsupportedOperationError` 501 (dataplane.py:1307-1325). Schema history: lineage's services/lineage/src/lineage/api/v1/endpoints/datasets.py:87-95 serves the per-version schema off the WROTE edge, and the catalog's own commit log is versions.py:57 `GET /{id}/history`. WHAT IS TRUE: no compatibility check anywhere in columns.py/dataplane.py, and `_action_relation` (fga_deps.py:292) leaves these suffixes at the writer rung — no owner gate for a breaking drop/retype.
- *What would reopen it:* Showing that `POST /v1/table/{id}/drop_columns` 404s on the deployed catalog, or that a caller receives an unmapped pylance exception from a column op, would restore the row as written.

**LH-027 · ~~`register_table` enforces no location containment~~ — THE ROW IS THE CONFLATION IT ASKS FOR**
`catalog` · was med · closed 2026-09-11 by measurement, not by code

- *THE ROW ASKED FOR A GUARD THAT CANNOT FIRE AND SHOULD NOT EXIST, and I shipped it before measuring
  — then reverted it the same day.* It reasoned by analogy from `warehouses.py`, which refuses a
  WAREHOUSE over a reserved bucket. The analogy does not hold, and `rask-lance-catalog` names the error
  outright: the reserved bucket blocks the WAREHOUSE route — a tenant claiming platform storage, where
  `provision_bucket` is idempotent so the claim silently succeeds, the project becomes the bucket's
  owner, and a later project-policy set governs every tenant's data in it — while leaving the
  REGISTRATION route open, which names one dataset and makes nobody an owner of anything.
  *"Conflating them is how you conclude the cascade can never be governed"* — and the cascade head
  registers its bronze seed into precisely that bucket.
- *IT WAS ALSO INERT.* `register_table` addresses a location inside the root it is connected to and
  nowhere else (`catalog_register.relative_location`'s own docstring); every caller sends a RELATIVE
  path — undrop sends the final path segment alone — and the backend refuses an absolute URI outright.
  So the guard read a field that never arrives: a control that cannot fire, whose only effect would
  have arrived the day absolute URIs became valid, by closing a route the design deliberately opens.
- *ROOT CONTAINMENT IS ALREADY ENFORCED, one layer up.* `relative_location` refuses a dataset URI
  outside the catalog's connection root and names both in the error. That is the containment Lakekeeper
  describes, applied at the producer seam rather than re-derived at the door from a bucket string the
  door does not receive.
- *What remains is a test rather than a guard* (`tests/integration/test_register_refuses_reserved_platform_storage.py`,
  rewritten in place): the platform's own bucket stays registrable, and locations are root-relative by
  construction.

**LH-028 · Three container-tier deletion paths were never driven live: warehouse delete, project delete, cascade DETACH + plural undrop, and bucket-purge sole-ownership**
`catalog` · med

- *Why open:* Table-level drop/protect/force/undrop are proven; the CONTAINER tier is not, and that is exactly where `force` and cascade interact.
- *Closes when:* Drive warehouse delete, project delete, cascade DETACH + the plural undrop and `projects_claiming_bucket` bucket-purge against the deployed release (`scripts/e2e_live.sh`) and pin each.

**LH-029 · ~~`batch_commit_tables` cannot converge: a retry re-runs the atomic native commit, hits `TableAlreadyExists`, and never reaches the ownership seeds~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* The described divergence cannot occur on the estate as configured: `impl=dir` does not implement `batch_commit_tables` at all, so the door answers UnsupportedOperation (→406) before any table is created and the seed loop is never reached — no table lands, nothing is stranded, and a retry hits 406, not `TableAlreadyExists`.
- *Evidence:* Measured on the locked SDK: `lance_namespace/__init__.py:931` raises `UnsupportedOperationError("Not supported: batch_commit_tables")`, inherited unimplemented by `lance.namespace.DirectoryNamespace` (driven directly against a real `connect("dir", …)`: two consecutive calls both raise it, never TableAlreadyExists). The estate runs that impl: services/catalog/src/catalog/core/config.py:42 `impl: str = Field(default="dir", alias="LANCE_REST_IMPL")` and chart/templates/services.yaml:70 `- { name: LANCE_REST_IMPL, value: "dir" }`. The route raises at services/catalog/src/catalog/api/v1/endpoints/versions.py:152 (`native.call`), so the non-convergent seed loop at :176-190 and its `ServiceUnavailableError` at :191-196 are dead code on this deployment. The reason nothing noticed: tests/unit/test_batch_commit_seeding.py:43 monkeypatches `ver.native.call` to a no-op, so the only test of this route never touches a backend.
- *What would reopen it:* Show `LANCE_REST_IMPL` set to a backend that implements `batch_commit_tables` (e.g. `rest`) in a deployed values file, or a `dir`-backend call to `batch_commit_tables` that returns a response instead of UnsupportedOperationError.

**LH-030 · ~~A partially-failed warehouse delete reports nothing~~ — CLOSED 2026-09-11**
`catalog` · was med

- *Closed by `80587a3a`.* The endpoint already LOGGED what landed and then re-raised, so the caller
  received a problem body that said nothing: a delete that did nothing and one that destroyed three
  namespaces before failing were identical on the wire, and they need different next actions.
- *Carried as RFC 9457 extension members* (§3.2) rather than in `detail`, which is deliberately
  redacted on a 5xx because it can carry paths, DSNs and driver text. `problem_detail` refuses to let
  an extension redefine a reserved field, so it can add to the body and never rewrite `status` or
  un-redact `detail`.
- *`PartiallyApplied` is a DECLARED carrier,* not an attribute stapled onto a base error — the first
  attempt set `problem_extra` on a bare `ServiceUnavailableError` and `ty` refused it, where the
  tempting repair is a suppression. It subclasses ServiceUnavailable because every step is idempotent
  and the recovery is to re-issue the same call, which is what a 503 asks for.

- *Why open:* The delete response is not honest about partial failure, so the caller cannot tell what was destroyed from what survived.
- *Closes when:* Return a per-object outcome in the warehouse delete response, with a test that forces a mid-delete failure.

**LH-031 · Root `ListNamespaces` asks only the default namespace, so a spec client discovers no warehouse-bound data from the root**
`catalog` · med

- *Why open:* Upheld by construction: the root id has no top segment to route by, so `dependencies.get_namespace` returns the DEFAULT namespace and `_drain_namespaces` asks that one backend — a root listing sees only the shared default root's `__manifest`. The estate proves the consequence: `maintenance/reconcile._top_level_namespaces_across` exists because each tenant's namespaces live in that tenant's own bucket.
- *Closes when:* Federate the root listing — enumerate the warehouse registry, drain each root and merge — relying on the per-item `can_get_metadata` filter this route already applies; that requires `get_namespace` to hand back a handle per root.

**LH-032 · `handle_validation_error` hardcodes 422 while emitting `ErrorCode.INVALID_INPUT`, which maps to 400 — the vendored spec contains zero 422s**
`catalog, service-kit` · med

- *Why open:* Measured 2026-09-09 and recorded so the next reader does not start the cheap fix, which does not exist: `install_problem_handlers` is installed on every app that can import `lance_namespace` (app.py:157-165) and 153 suite assertions expect 422; narrowing by path fails because `router.py:55-75` mounts spec and rask-only routes equally under `/v1`.
- *Closes when:* Derive a per-ROUTE 'this operation is in the spec' marker FROM the vendored spec rather than a hand-maintained tag set, have `handle_validation_error` answer 400 on spec routes only, and update the assertions that reach it.

**LH-033 · ~~Nine catalog operations answer 501: branch-scoped `query`/`explain_plan`/`analyze_plan`, `create_index`/`create_scalar_index`, `stats`, `index/list`, `index/{n}/stats`~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* No catalog operation answers 501 for this: `ErrorCode.UNSUPPORTED` maps to **406** by explicit owner decision (Q3, 2026-09-02), and the ops are SERVED on main — only the branch-scoped form is refused, at ten doors now, not nine.
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/ns_errors.py:39 `ErrorCode.UNSUPPORTED: 406` (with the reasoning at :30-38), pinned by tests/unit/test_ns_errors_contract.py:49-50 (`status == 406`). The refusal is branch-only: services/catalog/src/catalog/services/dataplane.py:1264-1270 `if branch is not None: raise UnsupportedOperationError(…)` — every door calls it AFTER reconciling the id and BEFORE delegating main's request to the backend. Ten call sites, not nine: data.py:212 `plan_table_compaction`, :245 `commit_table_compaction`, :601 `query_table`, :684 `explain_table_query_plan`, :702 `analyze_table_query_plan`; indices.py:84 `create_table_index`, :117 `create_table_scalar_index`, :159 `list_table_indices`, :176 `describe_table_index_stats`; tables.py:1077 `get_table_stats`. Each main-branch path is real work, e.g. tables.py:1078 `return native.call(ns, "get_table_stats", req)` and indices.py:86-97 (queue-or-build plus a measured lineage emit).
- *What would reopen it:* A response from any of those doors carrying HTTP 501, or a main-branch call to `stats`/`index/list`/`create_index`/`query` that is refused rather than served.

**LH-034 · Compression is never configured anywhere and there is no decision record**
`catalog, medallion` · med

- *Why open:* Listed Medium in §O1 with no note and no work. The setting is schema-resident, so retrofitting it later costs a rewrite and gets dearer with corpus size.
- *Closes when:* Choose a compression configuration on the create path and record it in `docs/DECISIONS.md`.

**LH-035 · The query door serves only the blob DESCRIPTOR shape with no `all_binary` opt-in, and `read_blob_ranges` is undocumented as the batched byte-fetch path**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* Only the opt-in half is genuinely open — the query door truly has no inline-bytes flag, but `read_blob_ranges` IS already documented as the batched byte-fetch path (just not in the door's own docstring), so the smaller true fix is: add a `blob_handling`/`all_binary` field to the query request model and cross-reference the existing doc from the `/blobs` docstring.
  **Evidence:** OPEN half: `QueryTableRequest.model_fields` (lance_namespace 0.11.1) = identity, context, id, branch, bypass_vector_index, columns, distance_type, ef, fast_search, filter, full_text_query, k, lower_bound, nprobes, offset, prefilter, refine_factor, upper_bound, vector, vector_column, version, with_row_id — no `blob_handling`/`all_binary`; services/catalog/src/catalog/api/v1/endpoints/data.py:597-610 delegates the whole body to `native.call(ns, "query_table", body)` and adds no such field. A grep for `all_binary` across services/ + packages/ hits only read paths outside the catalog (packages/service-kit/src/service_kit/lakehouse/blobs.py:189, viewer/pages.py:20, medallion/compute.py:477). ALREADY DONE half: docs/audits/lakehouse-2026-09/lance-conformance-and-build-rules.md:424 and :426 describe `read_blob_ranges(column, requests=[(row, offset, length)], selector=…)` verbatim as "the batched, planned alternative for a 'many rows, one range each' client" and name it beside rask's `/v1/table/{id}/blobs` door; docs/architecture/lance-blob-v2-findings.md:46, 98, 154 document its result shape. What is missing is only the cross-reference in the door's own docstring (services/catalog/src/catalog/api/v1/endpoints/data.py:522-538, which covers Range/Content-Range/ETag/If-Range and never names the batched API).
  **Reopen if:** Either a `blob_handling` field appearing on the query door's request model, or evidence that `read_blob_ranges` is documented nowhere in docs/ (contradicting lance-conformance-and-build-rules.md:424-426).
`catalog` · med · **blocked:** owner acknowledgement of R8

- *Why open:* Descriptor-first reads are already the default (`POST /v1/table/{id}/query` returns `struct<kind, position, size, blob_id, blob_uri>` with `lance-encoding:blob` metadata), so the main clause is refuted — but a caller that wants bytes inline has no opt-in, and the `/blobs` door streams `take_blobs` with Range/ETag one object at a time while pylance's batched `read_blob_ranges` is documented nowhere. It is the contract a Spark connector or BYO engine expects.
- *Closes when:* Add an `all_binary`/`blob_handling` opt-in flag to the query door's request model so a caller can ask for inline bytes, and document `read_blob_ranges` beside the `/blobs` Range/ETag door.

**LH-036 · ~~Body-id reconciliation (A1) is missing on four catalog routes~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* There are no unreconciled routes left: an AST walk of every `{id}` route in the catalog finds that EVERY handler whose body model carries an `id` field calls `reconcile_body_id`, and every remaining body-bearing `{id}` route uses a rask-local schema with no `id` field at all.
- *Evidence:* services/catalog/src/catalog/core/identifiers.py:94-101 is the single reconciler (400 on a mismatch), with 52 references across the catalog source and 44 call sites in the endpoint modules. AST scan of services/catalog/src/catalog/api/v1/endpoints/*.py: 40 `{id}` routes take a `lance_namespace` `*Request` body with an `id` field and all 40 reconcile — branches.py:55,62; columns.py:68,99,130,158,177,225; data.py:431,460,600,664,679,701; indices.py:83,116,153,172; namespaces.py:148,273,457,730; tables.py:260,355,440,594,658,891,1038,1073; tags.py:51,60,67,74; transactions.py:24,31; versions.py:222,241,249; views.py:47,70. The 29 `{id}` routes that do NOT call it all take rask-local schemas whose `model_fields` contain no `id` (verified by import: AccessCheckRequest, AccessGrantRequest, ManagedAccessRequest, CommitFragmentsRequest, CompactionPlanRequest, CompactionCommitRequest, TableChangesRequest, GateSpecRequest, GcRequest, CompactRequest, SetProtectionRequest, PolicyRequest, PublishRequest, TransformSpecRequest, TransformNameRequest) or an Arrow-IPC `bytes` body. The row's dependent clause is satisfied too: the stats/index doors now read `branch` from a declared body (indices.py:153-159, :172-176; tables.py:1073-1076), and tests/unit/test_siblings_agree.py:159-195 gates hand-parsed `dict` bodies that reconcile `id` but drop `branch`.
- *What would reopen it:* Name a catalog route with `{id}` in its path whose request body model has an `id` field and whose handler body contains no `reconcile_body_id` call.

**LH-037 · `POST /v1/namespace/{id}/create` and `POST /v1/table/{id}/register` accept `mode` and answer 409 whatever it says**
`catalog` · med

- *Why open:* Classed silently-weaker in the dropped-parameter sweep: a caller asking for an idempotent or overwrite mode gets the same 409 as a caller asking for strict create.
- *Closes when:* Implement the `mode` values on both doors (or refuse an unsupported one 400 rather than 409), with tests per mode.

**LH-038 · `POST /v1/table/{id}/version/list` accepts `page_token` and ignores it**
`catalog` · med

- *Why open:* Classed read-from-wrong-target in the sweep; a paging caller silently re-reads the first page forever.
- *Closes when:* Honour `page_token` in `version/list` (or refuse it 400), with a test that pages twice and gets different rows.

**LH-039 · ~~`POST /produce` accepts a governed-tier claim in `settings` and disregards it~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* `POST /produce` takes no request body at all — there is no `settings` object on that door and no governed-tier claim to disregard; its entire request surface is one required header and two query params, and the tier it seeds is fixed (`bronze$events`).
- *Evidence:* Rendered from the live app factory (`medallion.producer.app.openapi()`): `/produce POST → params: [('project','query'), ('rows','query'), ('Idempotency-Key','header'), ('dapr-api-token','header'), ('authorization','header'), ('dapr-caller-app-id','header')], requestBody: None` (same for `/ingest-media`). The handler signature confirms it: services/medallion/src/medallion/api/produce.py:43-60 — `dapr: DaprClientDep, settings: SettingsDep, originator: Depends(authorize_produce), idempotency_key: Header(alias="Idempotency-Key"), project: ProjectParam = None, rows: Query(ge=1, le=1_000_000) = None`; `settings` there is the injected `MedallionSettings`, not a wire field, and it is forwarded as such at :93 `run_produce(dapr, settings, token=idempotency_key, project=…, originator=…, rows=rows)`. The seeded tier is not caller-supplied: services/medallion/src/medallion/services/produce.py:53-59 registers and seeds `bronze$events` unconditionally. A grep for a wire-level `"settings"` key across services/ and packages/ finds no request model with such a field, and no `tier` parameter on any medallion door.
- *What would reopen it:* An OpenAPI render of `/produce` showing a requestBody, or any medallion request model with a `settings` field carrying a tier name.

**LH-040 · Put-if-not-exists is verified only on RustFS, so Lance's CAS commit model is untested on any other store**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* No CAS probe exists anywhere in the registration or validation path — but a warehouse never names a store, so the true fix is one conditional-put step added to the existing /validate probe, not a per-store refusal at registration.
  **Evidence:** services/catalog/src/catalog/api/v1/endpoints/warehouses.py:938-978 (POST /{warehouse_id}/validate: the whole probe is scope-only); warehouses.py:1035-1039 the probe's four IO steps are write_inside / read_inside / SCOPE_CHECK / cleanup — no conditional put; services/catalog/src/catalog/services/vend_probe.py:30 SCOPE_CHECK = 'write_outside_refused' is the only security claim the report makes; warehouses.py:122-200 create_warehouse has no CAS check at all. WHY THE 'PER-STORE' FRAMING OVERSHOOTS: create_warehouse provisions a bucket on the catalog's OWN configured endpoint (warehouses.py:181-183 `root_uri = f"s3://{bucket}"` + `provision_bucket(bucket, settings.storage_options())`) — a warehouse names a bucket, never a store; services/catalog/src/catalog/core/config.py:74-76 states the operator invariant that every multibase data base MUST share the catalog's S3 endpoint + creds; services/catalog/src/catalog/api/v1/endpoints/stores.py:130-133,152 force every ATTACHED store read_only, so no second writable store can be registered. THE PROBE ALREADY EXISTS, unwired: scripts/verify_lance_storage.py:255-286 check_conditional_put does exactly this against the configured endpoint, as a manual script. The only contended proof is opt-in and RustFS-only: tests/e2e-py/test_object_store_cas_e2e.py:38-46,74-77 (skips unless LANCE_E2E_S3_ENDPOINT is set).
  **Reopen if:** A conditional-put ProbeCheck appearing in warehouses.py::_run_scope_probe / vend_probe.py, OR a warehouse record that carries its own endpoint distinct from settings.storage_options() (which would make 'per-store' the right shape after all).
`catalog, storage` · med

- *Why open:* The Lance commit model assumes conditional put; on any store rask might run on other than RustFS (COS/GooseFS need commit locks per the guide) that assumption is untested and nothing refuses such a store at registration.
- *Closes when:* Add a per-store CAS probe to the warehouse validation endpoint so an unsupported store is refused at registration.

**LH-041 · Branch/tag writes are unconditional at every layer including pylance's `Tags::update` — a lost update in waiting**
`catalog` · med

- *Why open:* `_set_tag` is unconditional all the way down and nothing has verified whether RustFS honours `If-Match` on the tag object.
- *Closes when:* Use an object-store conditional put on `_refs/tags/<name>.json` and verify RustFS honours `If-Match` there.

**LH-042 · ~~Blob v2 default thresholds disagree three ways (64 KB/4 MB vs 16 KiB/2 MiB vs rask's measured 64 KiB/4 MiB)~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* The thresholds are pinned at the measured values and a test re-measures them against Lance itself on every run, so a pylance retune reds the suite rather than silently moving payloads — one residual: the medallion's blob columns pin nothing.
- *Evidence:* services/ingest/src/ingest/runtime.py:29-36 pins BLOB_INLINE_SIZE_THRESHOLD = 64*1024 and BLOB_DEDICATED_SIZE_THRESHOLD = 4*1024*1024 with the rationale that the guide stores them in the dataset SCHEMA and rejects a differing append; runtime.py:170-174 applies them to the bronze payload's blob_field. The re-measurement is automated and written against the FORMAT, not the constant: services/ingest/tests/test_blob_placement_thresholds.py:16-18 ('deliberately written against LANCE, not against our constant'), :38-46 _placement writes one row per band and reads back the descriptor kind, :50-60 parametrized across all four boundaries. The three disagreeing sources are confirmed present but resolved in rask's favour: lance_docs/guide.md:574 says 16 KiB / 2 MiB, lancemultibasebranchingblobv2.md:669-671 says 64 KB / 4 MB (which AGREES with the measurement), runtime.py:63-67 records the measurement that settles it. RESIDUAL, stated rather than cut: services/medallion/src/medallion/services/ingest.py:57 and compute.py:523,586 call blob_field(name) with no thresholds, so the medallion plane inherits whatever pylance defaults to.
- *What would reopen it:* A pylance bump landing with test_blob_placement_thresholds.py still green while the real placement boundaries have moved (i.e. the test measures the constant rather than the format) — or evidence that the medallion's unpinned blob_field writes threshold metadata that can conflict with ingest's pinned values.

**LH-043 · Unknown whether MemWAL server-id sharding fits append-only bronze landing (coordinator-free ingest)**
`ingest, medallion` · med · **blocked:** §K

- *Why open:* Blob v2 columns read `None` through the MemWAL scanner today, so the shape cannot be evaluated without a prototype.
- *Closes when:* Prototype MemWAL server-id sharding against bronze landing after §K.

**LH-044 · `tags/create` drops `branch`, `branches/create` drops `from_branch`+`from_version`, `branches/delete` drops `name`**
`catalog` · low

- *Why open:* Classed cosmetic in the sweep and untouched — but a `branches/create` that ignores `from_branch`/`from_version` silently branches from the wrong point.
- *Closes when:* Honour `branch` on `tags/create`, `from_branch`/`from_version` on `branches/create` and `name` on `branches/delete`, each with a test that a non-default value changes the result.

**LH-045 · ~~rename / backfill / alter_transaction / MV create+refresh / batch-create + batch-commit versions return 501 from the native backend~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* rename is NOT one of them — it has been served in-process since #5b — so the set is 6 ops, not 7, and the coverage figure is 48/54, not the 47/54 the row cites.
- *Evidence:* RENAME IS BACKED: services/catalog/src/catalog/api/v1/endpoints/tables.py:853-919 routes to `dataplane.rename_table` (line 919, `partial(dataplane.rename_table, ns, so, segments, ...)`) and never reaches native.call; the docstring at :870-875 says exactly why ('the dir backend's rename_table is a hard 501 ... It is served here as what the spec describes'); the implementation is services/catalog/src/catalog/services/dataplane.py:387. docs/COVERAGE.md:22 states the tally as 48/54 backed with 6 spec-correct refusals, and :17-20 carries the 2026-08-05 correction that rename was 'still listed as unsupported long after #5b backed it'. THE OTHER SIX ARE GENUINELY 501, all via native.call → services/catalog/src/catalog/services/native.py:43-46: versions.py:123 batch_create_table_versions, versions.py:152 batch_commit_tables, transactions.py:32 alter_transaction, columns.py:159 alter_table_backfill_columns, views.py:48 create_materialized_view, views.py:71 refresh_materialized_view. docs/COVERAGE.md:47-61 already records each with its rationale; what is missing against the Closes-when is only the word 'permanent' — it currently reads 'need upstream work', not a final refusal with the format guard's finality.
- *What would reopen it:* A live probe showing POST /v1/table/{id}/rename answering 501, or a seventh op reaching native.call that COVERAGE.md's list of six omits.

**LH-046 · A malformed BRANCH name on the S3-backed catalog answers `Internal 18` instead of `InvalidInput 13`**
`catalog` · low · **blocked:** upstream Lance fix

- *Why open:* Upstream-blocked, not a mapping gap: branch creation on S3 reaches Lance's clone path BEFORE its ref-name validator and dies `OSError('... Clone operation should not enter build_manifest.')` — the same text a collision produces. Measured both ways 2026-09-07: the `dir` backend answers `Ref is invalid` (mapped to 13), the deployed S3 estate panics. Duplicating Lance's validator locally is refused deliberately.
- *Closes when:* File the upstream Lance issue that an invalid branch name panics in the clone path before validation, then map the typed error in `_classify_ref_error` once Lance raises `Ref is invalid` on S3 too.

**LH-047 · `lance_docs/` is 79 lines behind upstream and carries no manifest, pinned commit or provenance line**
`catalog` · low

- *Why open:* The vendored spec is 79 lines short of upstream main (101 changed) — the error contract is byte-identical, which is what made §A5's work safe, but all six files were hand-vendored with no manifest and no automation, so a reader citing them cannot tell which spec version they describe. Q3's 406 decision already cites a `spec.yaml` version this repo does not hold.
- *Closes when:* Re-vendor the six `lance_docs/` files from a named upstream commit and land a provenance line/manifest recording the source version and commit beside them.

**LH-048 · Two upstream defects are unfiled: pylance's GET routes (A3) and the 0.12.0 `header.` vs `headers.` prefix in the bundled client**
`catalog` · low · **blocked:** upstream

- *Why open:* Both are upstream bugs rask works around; neither issue has been filed, so no fix version can be tracked.
- *Closes when:* File the two upstream issues and track the fix version.

**LH-049 · Whether descriptions bind to the catalog object is undecided**
`catalog` · low · **blocked:** owner IA ruling

- *Why open:* Narrowed by verification — the properties write endpoint exists and deregister does NOT lose the description, so only the binding question itself is left. It needs a ruling, not code.
- *Closes when:* An owner ruling on whether descriptions bind to the catalog object; if yes, wire the binding through the properties endpoint.

**LH-050 · TRIPWIRE: no query store for listings, deliberately, until interactive-frequency listing load appears**
`catalog` · low · **blocked:** the tripwire itself — interactive-frequency listing load

- *Why open:* Recorded as a deliberate no-op rather than an oversight: nothing happens here until listings are measured at interactive frequency.
- *Closes when:* Measured evidence that listings are being hit at interactive frequency, then a query-store design round.

### Governance: auth, authz, tenancy

_Multi-tenancy is the product claim; every item here is a place where one tenant's data, credentials or grants are protected by convention rather than by an enforced check._

**LH-051 · ~~the catalog presents the store's ROOT account~~ — CLOSED AND OBSERVED 2026-09-11**
`catalog, storage, chart` · was HIGH

- *OBSERVED on the live estate from inside the running pod, using the credential the way the app gets
  it* (the Dapr secret store, never env): reads the governed bucket, writes and deletes under the
  control root, **still vends** (`AssumeRole` → a 498-char session token), and is DENIED on the
  observability store. The vend is the row's whole point: on the previous store a policy-attached
  scoped caller got HTTP 403 and the right could not be granted at all, which is why the root key was
  a REQUIREMENT of `vending.mode: sts` rather than an oversight.
- *AND IT GENUINELY LOST ADMIN, which is the blast-radius change rather than the cosmetic one:* driven
  with the same credential — create-user DENIED, write-policy DENIED, list-policies DENIED, data
  unaffected. A compromised catalog can no longer mint an identity, widen its own policy, or read the
  policies governing every other plane.
- *The data grant stays WIDE and that is deliberate:* warehouse buckets are minted at runtime by
  `POST /v1/warehouses`, so a render-time bucket list is stale by construction — the same reason the
  ray and lineage policies already record. `CreateBucket`/`DeleteBucket` are held because provisioning
  and purge are this service's own doors.
- *Residual:* the identity is a static scoped key rather than a per-request one. That is the correct
  rung for a service that must vend for buckets which do not exist yet, and the narrowing the caller
  actually receives is the per-vend session policy on top of it.

- *Why open:* Re-measured on the running pods 2026-09-10: maintenance, medallion, lineage, viewer and
  ingest each hold their own scoped identity, and only `LANCE_S3_ACCESS_KEY_ID=rustfsadmin` remains
  (`chart/templates/services.yaml:92-94`). It cannot be fixed by copying the other five: an STS session
  policy can only RESTRICT the role it is cut from, while the catalog vends for warehouses minted at
  RUNTIME — a role narrowed to today's buckets cannot vend tomorrow's, and a role covering every future
  bucket is root wearing another name.
- **MEASURED 2026-09-11 — "ASSUME A ROLE PER VEND" IS NOT AVAILABLE ON THIS BACKEND, so one of the
  three candidate answers is struck.** Probed against the live RustFS STS from inside the cluster:

      AssumeRole from a SCOPED caller (rask-ray-compute)  -> REFUSED
      AssumeRole from the ROOT caller (rustfsadmin)       -> ACCEPTED

  RustFS delegates from the CALLER and does not resolve permissions for the `RoleArn`, so a catalog
  holding a narrow identity cannot assume a wider role to vend with. `vending.StsVendor`'s docstring
  records AssumeRole as "MEASURED" working from the Ray head on 2026-08-30 — true, but that head held
  the ROOT key at the time (`cc75585d` corrected it), so what the measurement proved is narrower than
  it reads: that root can assume, not that the flow is caller-agnostic.
- **VENDOR-CONFIRMED 2026-09-11** (`github.com/orgs/rustfs/discussions/1125`): *"temporary accounts
  currently inherit user permissions and cannot yet specify policies using ARN; this will be implemented
  soon."* That is exactly what the probe above measured, so the limit is RustFS's roadmap rather than a
  quirk of this build — and it means the answer must not depend on ARN policy resolution.
- **MEASURED 2026-09-11 — OPTION 1 IS NOT REACHABLE ON RUSTFS AT ALL, and this row previously
  concluded the opposite.** The hope was that a catalog USER whose policy the warehouse registry
  maintains would be inherited by every vend. It cannot be, because the scoped user cannot make the
  call. Same request shape, same pod, same endpoint, one run:

      RustFS   scoped user (policy attached)  -> HTTP 403
      RustFS   root (rustfsadmin)             -> ACCEPTED (487-char session token)

  And the right cannot be granted: `mc admin policy create` REFUSES a statement carrying
  `sts:AssumeRole` outright — *"invalid resource, type: 'unknown', pattern: '*'"* — so RustFS's policy
  engine has no vocabulary for the STS action. **AssumeRole is root-only on this build and is not
  policy-grantable.** That is why `rask-catalog` holds `rustfsadmin`: under `vending.mode: sts` it is a
  REQUIREMENT of the mode, not an oversight, and no amount of policy work removes it.
- **THE SAME PROBE ON MINIO PASSES, including the part that matters.** A real MinIO stood up in the
  cluster, the identical scoped user and policy, the identical call:

      MinIO    scoped user (policy attached)  -> ACCEPTED (484-char session token)
      MinIO    root (minioadmin)              -> ACCEPTED

  and the narrowing is ENFORCED rather than merely issued — vending as the scoped user with a session
  policy bound to one prefix: in-scope GET allowed, CROSS-TENANT GET `AccessDenied`, read-tier PUT
  `AccessDenied`. MinIO's own documentation states the rule this rests on — *"AssumeRole requires
  authorization credentials for an existing user"* and *"the permissions of the returned credentials
  are inherited from the policies attached to the built-in user"*.
- *So the backend choice, not the policy design, is what gates this row.* The capability LH-051 needs
  exists on MinIO today and does not exist on RustFS today. Waiting for RustFS's ARN work is not the
  only blocker either — ARN-bound roles are a DIFFERENT feature from letting a non-root user assume at
  all, and it is the latter this row needs.
- *One shape avoids the whole question and is worth weighing against a migration:* `web_identity`
  (`AssumeRoleWithWebIdentity`) needs NO SigV4 credential at the catalog, so the catalog would hold no
  storage identity whatsoever — strictly better than a scoped one. `WebIdentityVendor` is already
  implemented and RustFS supports the flow, but `rustfs.oidc.enabled` is `false` (confirmed: the
  running pod carries no OIDC env), and the mode cannot serve the CASCADE, whose stage runners
  authenticate with `dapr-api-token` + `x-lance-service-identity` and hold no bearer.
  (The estate is deliberately storage-agnostic — endpoint-swappable, never a code change — so MinIO or
  AWS would additionally offer per-ROLE policies; that would be a nicer implementation of the same
  design, not a different decision.)
- *So the decision is between TWO options, not three:*
  1. **A USER policy the warehouse registry MAINTAINS** — the mint path updates the vending identity's
     policy as warehouses are created, and every vended session inherits it. Keeps the identity
     genuinely bounded and removes root. Costs a policy write to the storage backend on every warehouse
     mint plus a reconciler for when that write is lost, which is a new failure mode on the create path.
     Reachable on RustFS today via the `mc admin policy` pair the chart already uses.
  2. **Accept the widest role, bounded by network + audit + rotation** — what runs today, made
     deliberate instead of accidental, with the compensating controls named and tested.
  3. **PER-WAREHOUSE CREDENTIAL ON THE WAREHOUSE RECORD — the prior art, and probably the right answer.**
     Lakekeeper (`docs.lakekeeper.io/docs/latest/storage/`) solves this exact problem — a catalog vending
     for warehouses created at runtime — by putting the credential in the warehouse's STORAGE PROFILE
     rather than on the catalog: an `assume-role-arn` "assumed for every IO Operation", a SEPARATE
     `sts-role-arn` that mints the downscoped vended token, and an external ID so a system identity
     cannot be tricked across accounts. The catalog then needs no wide identity at all; it uses THAT
     warehouse's credential to vend for THAT warehouse, downscoped to the table's location.
     **The ROLE form of this needs a backend that resolves a RoleArn, which RustFS does not (above), but
     the CREDENTIAL form works here today**: the warehouse mint already creates the bucket, and the chart
     already creates scoped users with `mc admin policy create` + `attach --user`, so a per-warehouse
     scoped credential stored on the warehouse record is reachable with the pieces in hand. It also
     removes ROOT specifically, which matters beyond blast radius: root can create users, rewrite
     policies and delete buckets — a per-warehouse data credential can do none of those.
     Note [[LH-067]] already carries "endpoint/credential field on the warehouse record" as its residual,
     so this is one change serving two rows.
- *Closes when:* the owner picks one; then provision `rask-catalog` the way
  `rustfs.medallionAccessKey`/`maintenanceAccessKey` are (chart provisioning hook, key defaulted to the
  provisioned user) and extend `tests/unit/test_a_provisioned_identity_is_one_the_service_uses.py`.

**LH-128 · ~~A cascade identity is still `owner` of every TABLE it registers, because create-on-parent seeds self-ownership~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* A service-issued token no longer receives `owner` on what it registers — the seed writes only the parent edge, and the project admin owns the table transitively.
- *Evidence:* /home/blackwell/Desktop/rask/services/catalog/src/catalog/api/fga_deps.py:1034 (`grant_owner=token.iss != SERVICE_DOOR_ISSUER`); /home/blackwell/Desktop/rask/services/catalog/src/catalog/api/security.py:47,163 (the service door mints `iss="rask://service-door"`); /home/blackwell/Desktop/rask/packages/service-kit/src/service_kit/governed/fga.py:1320-1333 (owner tuple omitted, hierarchy edge still written in the same batch); /home/blackwell/Desktop/rask/packages/service-kit/src/service_kit/governed/auth/model.fga.yaml:1273-1305 (case 'a table a MACHINE registered is owned by the project, never by the machine' — service-silver-to-gold asserts can_drop/can_deregister/can_restore/manage_grants all false, alice true); /home/blackwell/Desktop/rask/tests/unit/test_a_machine_created_table_is_owned_by_its_project.py:1-60
- *What would reopen it:* A stage-runner-issued create that still writes a `user:<service> owner table:<id>` tuple — i.e. a service principal whose token carries an `iss` other than SERVICE_DOOR_ISSUER, or a create door that bypasses `seed_ownership` and calls `grant_on_create` with the default `grant_owner=True`.

**LH-053 · Bucket claims are keyed by warehouse ID, so two warehouse IDs can both claim the SAME bucket — and the four control-root JSON stores it must live in are not collapsed**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The bucket claim is real but narrow — a scan-based guard already refuses a sequential double-claim; what is missing is only the ATOMIC bucket-keyed claim, and the store-collapse the row makes it wait on has already landed.
  **Evidence:** /home/blackwell/Desktop/rask/services/catalog/src/catalog/services/warehouses.py:233-236 (`projects_claiming_bucket`) called at /home/blackwell/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/warehouses.py:180-183 — so a double-claimed bucket IS detected, contra the row's 'No code path detects'; the defect is TOCTOU: the listing is read at warehouses.py:152 before authz, the reserved guard, and the network `provision_bucket` at warehouses.py:186, and the record write is keyed by warehouse id (/home/blackwell/Desktop/rask/services/catalog/src/catalog/services/warehouses.py:186-193 `create_warehouse_record` → `_warehouse_key(record["id"])`), so two different ids racing on one bucket both win. NO bucket-keyed claim exists (grep `bucket-claims` across the tree hits only open_backlog_left.md:399). THE BLOCKER IS DEAD: the conditional-create primitive exists and is already in use — /home/blackwell/Desktop/rask/packages/service-kit/src/service_kit/lakehouse/records.py:89 `create_json` (`IfNoneMatch: *` on s3, `open(...,"xb")` locally), used for the warehouse mint (warehouses.py:193) and the write-once namespace binding (warehouses.py:253); and the four hand-rolled control-root stores WERE collapsed — /home/blackwell/Desktop/rask/packages/service-kit/src/service_kit/lakehouse/record_store.py:1-27 ('the shape four registries hand-rolled: protection, maintenance_policies, trash and warehouse_records'), imported by protection.py:31, maintenance_policies.py:43, trash.py:35, warehouse_records.py:19.
  **Reopen if:** A `_warehouses/bucket-claims/<bucket>.json` (or equivalent) written through `records.create_json` on the warehouse-create path, or a reconcile category that reports two warehouse records naming one bucket — neither exists (reconcile.CATEGORIES at services/maintenance/src/maintenance/services/reconcile.py:86-95 has no such row). The smaller true fix: write the bucket-keyed claim via the already-shared `create_json` in `create_warehouse`; no store collapse and no #85 record primitive is needed first.
`catalog` · **HIGH** · **blocked:** owner ruling: pull the bucket claim forward as its own store, or confirm it stays behind the #85 record primitive

- *Why open:* Deferred by diff2's F1 landing note rather than by omission: the fix belongs with #85's collapse of the four control-root JSON stores, not as a fifth ad-hoc store. No code path detects a double-claimed bucket and recovery is manual — Mallory ends up holding `owner` on a warehouse whose `root_uri` is another tenant's bucket, and `set_project_policy` resolves through the same registry so her maintenance policy can destroy their version history.
- *Closes when:* Collapse the four control-root JSON stores into a single conditional-create record primitive, then express the warehouse-id mint and a bucket-KEYED claim (`_warehouses/bucket-claims/<bucket>.json`, written with the same `IfNoneMatch: *` primitive) on top of it.

**LH-054 · The credential-isolation e2e SKIPS against the shipped stack, so cross-tenant credential refusal is proven only by rask's own offline policy evaluator**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The legs no longer skip against the deployed estate — the live runner provisions tenant B and the chart vends `sts` (→ mode `direct`) — so what is actually left is the CI kind stack, and the row's `web_identity` requirement is the wrong lever.
  **Evidence:** /home/blackwell/Desktop/rask/scripts/e2e_live.sh:234-271 provisions a fixed second tenant and exports `LANCE_E2E_PROJECT_B` + `LANCE_E2E_TENANT_B_TOKEN` ('the credential-isolation legs will RUN'), and e2e_live.sh:319 runs `pytest tests/e2e-py -m e2e`, which includes the suite. /home/blackwell/Desktop/rask/chart/values.yaml:955 `mode: sts` — and /home/blackwell/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/credentials.py:125 sets `mode = "server_mediated" if creds is None else "direct"`, so the suite's own gate (/home/blackwell/Desktop/rask/tests/e2e-py/test_credential_isolation_e2e.py:110, skip unless `direct`) passes under `sts`. `web_identity` would make it WORSE, not better: /home/blackwell/Desktop/rask/services/catalog/src/catalog/core/vending.py:354 returns None without a caller OIDC token. The test file carries live measurements from the unskipped drives (test_credential_isolation_e2e.py:139, :243-249). WHAT IS STILL OPEN: the CI harness /home/blackwell/Desktop/rask/scripts/e2e_stack.sh:277-284,305-306 lists the suites it runs and this file is not among them, and it exports no tenant-B vars; /home/blackwell/Desktop/rask/Makefile:882-889 still says 'NOT in e2e-ci'. The widened-policy SABOTAGE lever does not exist anywhere (no such fixture or chart value).
  **Reopen if:** A `LANCE_E2E_TENANT_B_TOKEN` export or `test_credential_isolation_e2e.py` line inside `scripts/e2e_stack.sh` would close the remaining half; conversely, if `chart/values.yaml` vending.mode were `mode_b` the `direct` gate would skip again. Live pass/fail against the deployed estate is UNVERIFIED from here — I read the harness and the chart, not a run.
`catalog, storage, chart` · **HIGH**

- *Why open:* The code landed (3cacdd91) but every test skips on the shipped stack and a skip reads identically to a pass — RustFS never evaluates the session policy. `scripts/e2e_stack.sh` provisions neither web-identity vending nor a second tenant admin, and the sabotage half needs a deliberately-widened-policy lever that must not be reachable in production.
- *Closes when:* Provision `vending.mode=web_identity` (requires `rustfs.oidc.enabled` + `auth.enabled`) plus a SECOND tenant with its own admin subject in `scripts/e2e_stack.sh`, add the widened-policy sabotage lever to the harness chart values, then run the credential attack e2e unskipped in CI.

**LH-055 · The FGA model has no `branch`/`column`/`base`/`estate` type, `can_set_protection` collapses onto `can_drop`, and `project` has no security_admin/data_admin/role_creator split or machine identity**

- *RE-MEASURED 2026-09-11 — THE TYPE GAPS ARE REAL; THE PROTECTION CLAUSE IS A DOCUMENTED DECISION.*
  Read off `model.json`: no `branch`, `column`, `base` or `estate` type, no `can_set_protection`
  relation anywhere, and `project` carries only `admin` / `member` / `team` / the `can_*` derived from
  them. Every type gap the row names is present, and this is the first row today that measured exactly
  as written.
- *BUT `can_set_protection` "collapsing onto `can_drop`" is a decision with a recorded reason,* not an
  oversight: `fga_deps.py:209-213` — "arming/disarming the safety on an object is a statement about its
  DESTRUCTION, so it clears the same owner bar as the drop it guards — a writer must not be able to
  disarm protection they could never act on." Splitting it would need to say what the ARM bar is
  separately from the DISARM bar, because only the second is dangerous. Treat that clause as a design
  question, not a defect to fix.
- *WHAT THE REST NEEDS IS OWNER DESIGN INPUT, which is why it is not being worked:* a `branch` type
  needs a rule for what a branch-scoped grant means when the branch is a whole parallel dataset; a
  `column` type needs the classification vocabulary LH-058 is about; `security_admin` / `data_admin` /
  `role_creator` are a policy split about who may hand out what. Adding the types without answering
  those ships a model that grants nothing and an enforcement surface that checks nothing.
`catalog, service-kit, openfga` · **HIGH** · **blocked:** owner decision on the model shape (and on introducing `estate` vs documenting the warehouse-as-root convention) — coordinate with the `role`→`project` edge so `model.fga` changes once

- *Why open:* Re-measured 2026-09-09: all five Lakekeeper per-action rungs are zero. `_OWNER_SUFFIX_RELATION` maps `protection` to `can_drop`, so the person protection is meant to stop holds the rung that disarms it — four-eyes is unexpressible. `project` carries only admin+member, so one principal holds both data power and granting power. And root-ness is a convention: `model.fga` declares no `estate`, so `can_observe_events`/`can_browse_storage` exist on EVERY warehouse and resolve to that warehouse's owner — only the app checking them against `settings.fga_root_object` keeps them estate-scoped, and a future check on a non-root warehouse would silently grant estate-wide privilege and still pass `fga model test`.
- *Closes when:* Add a `can_set_protection` rung remapped out of `_OWNER_SUFFIX_RELATION`; add the project role split plus a machine/operator identity; add the `branch` type, a column-policy relation and an `estate` root with `can_create_project`, moving `can_observe_events`/`can_browse_storage` onto it and repointing `fga_root_object` (`catalog/core/config.py`) with its one seeded tuple; `.fga.yaml` cases and `_CHILD_EDGE_PARENT_TYPES` for each.

**LH-056 · Branch-scoped governance is missing: no FGA `branch` type, vending/protection/trash are branch-blind, and branch/tag creation emits no control event**
`catalog, lineage, notifications` · **HIGH** · **blocked:** owner acknowledgement of R5, plus an owner decision on who is TARGETED by a tag/branch control event (rask-notifications: an event naming nobody is undeliverable); the stats/index body clause waits on A1

  **RE-MEASURED 2026-09-11 — NARROWER ON THE ASK, WIDER ON THE DEFECT; both blockers are stale.**
  Confirmed: `model.fga:41-508` declares ten types and no `type branch`; the only branch rung is
  `define can_create_branch: owner` (:412 — the row's `:349,357` are stale refs), and `model.fga.yaml`
  asserts that rung alone. **But the ask aims at the wrong seam:** `canonical_object_id` is a string
  join over PATH segments, and the object is chosen in `authorize` (`fga_deps.py:709-710`), which
  never reads `branch` from the request — so making that function branch-aware changes nothing. A
  separately-verified consequence belongs here: **`branches/delete` resolves to the WRITER rung while
  `branches/create` is owner-tier**, so the door that destroys a branch clears a lower bar than the one
  that creates it.
- *Why open:* The nine data doors were fixed and pinned; the governance half is untouched. `model.fga:349,357` has only `can_create_branch: owner`, so branch writes fall through to the table's `can_write_data`, and `canonical_object_id` joins the table's path segments only — the FGA object is `table:<ns>$<table>` whatever branch a request names, so a `can_write_data` holder writes ANY branch and no grant can cover main alone. Vending is not scoped to `tree/<b>/`, protection and trash have no per-branch records, `parent_branch`/`parent_version` facets are absent, and driven against the deployed catalog `tags/create` and `branches/create` both answered 200 with zero control events because `ControlAction` is a 38-value `Literal` with no tag or branch action. The branch refusal on `stats`, `index/list` and `index/{n}/stats` was added as a QUERY parameter while those routes declare no body, so the spec's `{"branch": …}` body is still dropped.
- *Closes when:* Add `type branch { parent:[table]; reader/writer; can_write_data }` to `model.fga` with `.fga.yaml` cases; make `canonical_object_id` branch-aware; scope vended STS prefixes to `tree/<b>/` via `vending.build_session_policy`; add per-branch protection and trash records; emit `parent_branch`/`parent_version` facets; add tag/branch values to `ControlAction` in `control_events.py` and regenerate `docs/catalog-openapi.json` + the TS client; land A1 so stats/index read `branch` from a declared body.

**LH-057 · Per-base credential vending is unimplemented — the vendor refuses any table whose fragments carry a `base_id` instead of vending a union of bases**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The union vend LANDED — the manifest's base_paths are read and granted READ in the session policy — leaving only the `has_external_bases` short-circuit and the per-base write/deny split the code declined on the record.
  **Evidence:** IMPLEMENTED: /home/blackwell/Desktop/rask/services/catalog/src/catalog/core/vending.py:129,180-201 — `build_session_policy(bucket, prefix, tier, bases)` emits a `ListBase<n>` + `BaseObjects<n>` pair per base (own bucket ARN each), with `_reject_iam_metacharacters("base path", base)` applied to the manifest-sourced path at :180. The manifest read is reused, not doubled: /home/blackwell/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/credentials.py:138-163 `_dataset_facts` returns `(version, base_paths)` off one `lance.dataset` open (it replaced `_current_version`, which no longer exists), and :113 passes `bases=declared_bases` into `vendor.vend`; every vendor signature now takes `bases` (vending.py:82,208,279,353). The §H12 maintainer-probe cost is addressed there. STILL REAL, and narrower than the row: /home/blackwell/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/credentials.py:104-106 still returns `mode="server_mediated"` when `settings.multibase_data_base_list` is set and `has_external_bases` (/home/blackwell/Desktop/rask/services/catalog/src/catalog/core/vending.py:424-448) is true — that is the only remaining refusal, and it is latent behind an empty deployed list. DECLINED ON THE RECORD: vending.py:143-146 states every base gets READ ONLY and that write-on-target_bases / deny-on-reference-only 'need evidence a manifest read does not yet distinguish'; vending.py:148-155 states one grant shape covers both layouts deliberately so the policy need NOT consult `BasePath.is_dataset_root`.
  **Reopen if:** If `credentials.py:104-106` were removed (or narrowed to the bases the policy cannot cover) the row would close in its useful sense. The row's own cited lines are stale — `core/vending.py:213` is inside `_expiry_millis`/`StsVendor.__init__` and `:278` is the `StsVendor.vend` signature that now forwards `bases`. Smaller true fix: drop the `has_external_bases` fallback now that the policy covers the bases; the `is_dataset_root` clause should be struck, not built.
`catalog, maintenance, storage` · **HIGH** · **blocked:** owner acknowledgement of R4; the bases-as-storage-profile framing it rides on

- *Why open:* Two of three clauses closed (the `session_token` seam and `expires_at_millis` on both vend paths) and the falsy-zero guard is fixed, but `endpoints/credentials.py:76-130` and `core/vending.py:213,278` still refuse rather than vend. §H12 is the measured cost: 69 datasets a tick refused compaction because the vended session policy cannot reach a base the manifest declares. Latent behind `settings.multibase_data_base_list` (deployed empty), so it fails on the first estate that enables the feature.
- *Closes when:* In `core/vending.py`, vend the union of the manifest's `base_paths` with per-base rights — read on inherited bases, write on `target_bases`, never on reference-only bases — resolving each path by `BasePath.is_dataset_root`, reusing the manifest `credentials.py::_current_version` already reads, and applying `build_session_policy`'s `*`/`?` metacharacter refusal to manifest-sourced paths.

**LH-058 · No column-level classification or policy exists: `columns.py` has no FGA check and `pii` survives only as a key in seed data**
`catalog, lineage, openfga` · **HIGH** · **blocked:** the FGA model-shape decision (the `column` relation is part of it)

  **RE-MEASURED 2026-09-11 — THE CORE CLAUSE IS EXACTLY TRUE AND THE REMEDY WOULD NOT CLOSE IT.**
  Confirmed in code and against the LIVE store (`01KYPGG8F8MAZTJANME4K077DE`, model
  `01M288WT5QQRB5TC6S9VBZGFS0`): ten types, zero relations naming column/classification/mask, and no
  `classification`/`sensitivity` field anywhere. But "apply masking on `query`" is a control aimed at
  one of at least five read doors, and **the traffic that matters does not reach it**: `credentials`
  is a data-read action (`fga_deps.py:86`) that vends a whole-prefix READ session, so a caller holding
  the reader rung reads the raw columns from object storage without passing any door that could mask
  them. Column masking cannot be enforced at a query door while credential vending hands out the
  bytes.
- *Why open:* Section I names it 'the lever the estate cannot express' — governed tables carry no per-column sensitivity, so a GDPR/secrecy classification cannot be recorded, checked or enforced, masking/deny per column is unexpressible, and only column LINEAGE exists.
- *Closes when:* Put a classification field on the dataset/column metadata and the dataset node, add a `column` relation to `model.fga`, apply masking on `query` and on descriptor-first reads in `columns.py`, and add a door to set and read the classification.

**LH-059 · ~~The bronze write doors never self-check `can_create_table` on `namespace:bronze` — the FGA model describes a rung nothing enforces~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* `can_create_table` on the bronze namespace IS enforced before the Lance write — by the catalog's create-on-parent guard against the medallion's own service identity — so the row's grep tests the wrong file.
- *Evidence:* Both bronze doors now register BEFORE writing bytes. /home/blackwell/Desktop/rask/services/medallion/src/medallion/services/produce.py:122-126 ('ask first, write second, so no window exists in which bronze rows sit on disk unregistered'), :153-167 calls `catalog_register.register_written_dataset` → `POST /v1/table/{id}/register` (/home/blackwell/Desktop/rask/services/medallion/src/medallion/services/catalog_register.py:464), and only then :193 `seed_bronze`. Media: /home/blackwell/Desktop/rask/services/medallion/src/medallion/services/media_produce.py:146,177 calls `ensure_stage_output` → `POST /v1/table/{id}/create` (catalog_register.py:331) before :201 `_seed_and_ingest`. At the catalog, `register`, `create` and `declare` are all create-on-parent (/home/blackwell/Desktop/rask/services/catalog/src/catalog/api/fga_deps.py:132-136), which resolves to `can_create_table` on `namespace:<parent>` (fga_deps.py:325-343 `_create_parent_check`) and is enforced by the router-wide `Depends(authorize)` (/home/blackwell/Desktop/rask/services/catalog/src/catalog/api/v1/router.py:47, fga_deps.py:692-695). The subject checked is the medallion's own service principal: /home/blackwell/Desktop/rask/services/catalog/src/catalog/api/security.py:160-168 mints an IDToken whose `sub` is the presented `x-lance-service-identity`. `bronze_dataset` defaults to `bronze$events` (/home/blackwell/Desktop/rask/services/medallion/src/medallion/core/config.py:519), so the parent is exactly `namespace:<project>-bronze` — the object the row asks for.
- *What would reopen it:* The row's grep is literally still true (`can_create_table` in services/medallion/src hits only core/config.py:259, transform.py:346 and train.py:229) but proves nothing: the medallion presents its identity to the door that checks. The residual, narrower gap: the register/create call is skipped when `settings.catalog_url` is unset or `compute_enabled` is false (produce.py:152, media_produce.py:170) — the documented ungoverned dev shape. It would be falsified by a deployed medallion with `MEDALLION_CATALOG_URL` unset.

**LH-060 · Nothing detects a table stranded between the Lance write and the ownership grant — a catalog object with ZERO FGA tuples is invisible to the drift report**
`catalog, maintenance` · med · **blocked:** owner decision on the detector's gate (A/B/C/D) and on the read pattern (per-object FGA cross-check vs one bulk `read_tuples`)

- *Why open:* `table_create.py` writes the dataset then grants, so a crash in the window leaves a table with real bytes, present in the namespace object index, carrying zero tuples — recoverable only by a hand-written tuple. None of `reconcile.CATEGORIES`' eight starts from a table. The door the detector was to be built on is ruled out, driven live: `GET /v1/table` filters on `can_read_data`, and filtering ON tuples cannot reveal their ABSENCE — maintenance's own `service_headers` answered `tables: 0` on an estate holding 1,134 owner tuples while a Dex-minted bearer for alice answered 246. Both halves were built green and reverted deliberately.
- *Closes when:* Owner picks the gate — (A) the `LANCE_PRIVILEGED_SUBJECTS` service principal the credentials door already uses, (B) a modelled estate rung, (C) per-project so each tenant admin sees their own, or (D) skip the detector and reorder declare→grant→write — then build it inside the catalog (the only holder of both the table index and the FGA client), lifting `tables.py::list_all_tables`' walk into a shared service, with a decided read pattern and a migration-window exemption rule.

**LH-061 · There is no write-capable reconcile: stranded objects cannot be listed, repaired or dropped, and FGA tuples cannot be rebuilt from the catalog**
`catalog, maintenance` · med · **blocked:** owner: the 2026-08-15 'No — not yet' ruling must be overturned

- *Why open:* Owner-deferred 2026-08-15 ('No — not yet'). It is the ONLY repair for the batch-commit case, the two deliberate `undo=None` doors and anything created before the compensation pass; separately there is no additive tuple rebuild driven from the catalog registries, so after a loss or migration the tuple estate cannot be reconstructed (the only `reconcile.py` in the tree is lineage storage-drift, a different thing).
- *Closes when:* Once the deferral is overturned: a write-capable reconcile pass that lists stranded objects and repairs or drops them at every tier, driven additively from the catalog registries, with opt-in drift deletion behind a dry-run mode.

**LH-062 · `type role` in `model.fga` has no `project` edge, so a global role name can hold a rung on any tenant's namespaces**
`catalog` · med · **blocked:** owner decision, held alongside the FGA model-shape item — two uncoordinated changes to `model.fga` are worse than one

- *Why open:* Re-measured: `team` IS scoped (`project.team: [team]`) but `role` carries only `assignee` and is reachable from nothing. Live blast radius is two tuples (`role:validators#assignee → validator → namespace:acme_gold` and `namespace:acme-gold`) with nothing tying `validators` to `acme`. The model edge alone would be an unused relation; the enforcing half is the real work, because a namespace's tenant resolves only through the binding registry — a registry read on a security-critical grant path.
- *Closes when:* Add `define project: [project]` to `type role` in `model.fga` (with `model.fga.yaml` + `model.json` together — `make fga-test` diffs all three), then validate in `access.py::_grant_or_revoke`, before `fga.write_tuples`, that a `role:` grantee's project matches the object's tenant resolved namespace→binding→warehouse→project.

**LH-063 · Every FGA grant is keyed on a Dex-CONNECTOR-scoped protobuf subject, with no subject-identity migration story**
`catalog` · med · **blocked:** owner decision — latent and IdP-conditional today, since `sub` is stable per Dex/Keycloak realm

- *Why open:* Live tuples read `user:CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs`, which base64-decodes to the user id AND the connector name — so changing the Dex connector alone re-keys every grant, never mind changing IdP. A configurable `subject_claim` at `governed/deps.py:181,208` would be the illusion of portability (existing tuples still name the old value), and the planned Keycloak sync writes `team#member`/`role#assignee` only, not the per-user grants tenants accumulate.
- *Closes when:* Design and land a stable internal principal id that FGA keys on, with the IdP subject as a mapped attribute, plus the migration that re-keys existing tuples; the configurable claim is one part of that and must not ship alone.

**LH-064 · Lineage-bus producers are unauthenticated: no Dapr `accessControl` on `dapr-caller-app-id` and no producer signature over the CloudEvent**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The two producer-authentication clauses are genuinely open (no Dapr accessControl anywhere in the tree; no CloudEvent signature verified at the bus door), but the row's third Closes-when clause — stamping the subject through enforce_output_authz — landed on 2026-09-09, so the remaining true fix is only the app-id policy plus the signature.
  **Evidence:** services/lineage/src/lineage/api/dapr.py:32 — on_lineage_event is guarded ONLY by Depends(require_dapr_token), a shared app token. services/lineage/src/lineage/api/fga_deps.py:243-273 — enforce_bus_authz reads author_sub_from_payload, refuses an unauthored event, and delegates to enforce_output_authz with _StampedAuthor(subject) at :273 (landed in 35fcabb4 'feat(lineage): the bus door authorizes what it records (E2)', 2026-09-09), so clause 3 is DONE. fga_deps.py:226-235 — _StampedAuthor's own docstring: 'nothing proves the stamp', which is the signature gap. `grep -rn accessControl` over chart/, services/, packages/ and the whole tree returns zero hits; the only Dapr Configuration CRD is chart/templates/observability.yaml:72-99 (lance-tracing), carrying `features:` and no accessControl. No signature/HMAC/verify code exists in services/lineage/ or packages/lineage-kit/ (grep for signature|hmac|signed returns only prose). chart/templates/dapr-component.yaml:19-23 scopes the PUBLISH component to catalog (+ maintenance when lineageEmit) — Dapr component scoping, which is not the accessControl policy the row names.
  **Reopen if:** An `accessControl` block on a Dapr Configuration referenced by the lineage sidecar, or signature-verification code inside on_lineage_event / handle_cloud_event, would close the two remaining clauses. Conversely, showing that _StampedAuthor never reaches enforce_output_authz (fga_deps.py:273) would restore the third clause and make this plain still_real.
`lineage` · med

- *Why open:* The owner delegated the decision 2026-09-02 and the row explicitly stays in the backlog — the bus door is the integrity of the lakehouse's write record. The decided shape (mTLS SPIFFE app-id policy while Dapr is the transport, a transport-independent producer signature that survives a Dapr retreat, `enforce_output_authz` stamping the subject either way) is a design, not landed code.
- *Closes when:* Add the Dapr `accessControl` policy naming the permitted producer app-ids on the lineage subscription, verify a producer signature over the CloudEvent in the bus door, and stamp the subject through `enforce_output_authz`.

**LH-065 · ~~Prod vending is `mode_b` everywhere, so the tenant-isolation machinery is dormant on every shipped estate (and no `/refresh-credentials` door exists for the alternative)~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* The shipped chart default is `vending.mode: sts`, not `mode_b` — the tenant-isolation machinery is the DEFAULT posture on every rendered estate, and the refresh mechanism the row wants already exists client-side as a re-vend before expiry.
- *Evidence:* chart/values.yaml:955 `mode: sts # mode_b | web_identity | sts | static`, with the rationale at chart/values.yaml:942-954 stating why it moved off mode_b ('It defaulted to mode_b for a reason that turned out to be a DEFECT rather than a choice') and recording 'PROVEN on RustFS 2026-09-03 ... vended for one table read that table (4 rows) and was refused on another with 403 AccessDenied'. chart/templates/services.yaml:115 renders it as LANCE_VENDING_MODE, so the chart value reaches the catalog. The enabling fixes are in the tree: 01222590 (2026-09-03, 'STS vending could never sign its own AssumeRole') and 3cb13c88 (2026-09-03, 'a vended credential was correct and unusable'), landing services/catalog/src/catalog/core/vending.py:280-312. The code default at services/catalog/src/catalog/core/config.py:266 is still mode_b, but the chart is the single deploy artifact and overrides it; chart/values-prod.yaml sets no vending key at all. On the refresh half: no /refresh-credentials route exists (grep across the tree returns only docs/SYSTEM-SKETCH.md:172), but packages/service-kit/src/service_kit/lakehouse/vended_credentials.py:73-113 VendedCredentialCache re-vends through the SAME door before expires_at_millis minus a refresh margin, and services/ingest/src/ingest/runtime.py:854-862 wires it — a separate endpoint is not what makes the alternative work.
- *What would reopen it:* A values file or live release setting `vending.mode: mode_b` (nothing in chart/values-prod.yaml, values-local.yaml or values-live-pins.yaml touches vending), or evidence the STS path fails against the deployed RustFS. NOTE: this verdict is measured from the CHART, not from a running cluster — the live estate's actual LANCE_VENDING_MODE is UNVERIFIED here.

**LH-066 · The maintenance identity is one key across every warehouse rather than a per-warehouse scoped credential**
`maintenance, chart` · med · **blocked:** the per-base `managed`/`reference-only` warehouse-record policy this depends on

- *Why open:* The row's first clause landed 2026-09-07 (`delete_location` refuses a location holding files but no `_versions/` marker, pinned by `test_purge_refuses_a_location_that_is_not_a_dataset.py`). The second is untouched: the service that rewrites and deletes in every bucket in the estate does so under one long-lived identity.
- *Closes when:* Scope the maintenance identity per warehouse in `chart/templates/maintenance.yaml:123` and `chart/values.yaml:1517` instead of one estate-wide key.

**LH-067 · Warehouse storage cannot be expressed as bases: one endpoint and one key for the whole estate, and a warehouse-rooted connection swaps only `root`**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The single-endpoint/single-key connection shape is real and the warehouse record still carries no endpoint or credentials — but two of the four Closes-when clauses are already satisfied or moot, and I ran the probe the row asks for: the `base_<id>.<key>` keyed form IS supported on the installed pylance while `aws_provider_scheme` is still absent.
  **Evidence:** CONFIRMED DEFECT: services/catalog/src/catalog/core/config.py:389-399 namespace_properties() emits exactly one storage.endpoint / access_key_id / secret_access_key / region; services/catalog/src/catalog/core/namespace.py:27-35 build_namespace_for_root swaps ONLY `root` ('Same impl + object-store credentials/endpoint as the default connection'); services/catalog/src/catalog/services/warehouses.py:114 `_CALLER_OWNED = frozenset({"id","bucket","root_uri","project"})` — no endpoint/credential field on the record; services/catalog/src/catalog/services/dataplane.py:216-221 hands every base the SAME `so` dict with the stated invariant 'every allowlisted data base MUST share the catalog's endpoint/creds'. ALREADY DONE: dataplane.py:236 already passes `target_bases` on the catalog write door (composed at :208-215). MOOT: `grep -rn storage_profile` over the tree matches only docs/audits/lakehouse-2026-09/lakehouse-analysis.md:140 — there is no storage-profile-per-warehouse code path to delete. PROBE RESULT (run today against the installed pylance 11.0.0, note the audit doc at docs/audits/lakehouse-2026-09/lance-conformance-and-build-rules.md:416 recorded 10.0.0): lance.write_dataset's docstring for `base_store_params` reads 'These take precedence over ``base_<id>.<key>`` entries in ``storage_options``' — the keyed form exists; `lance.dataset()` now also accepts `base_store_params`, so per-base creds on the READ path are expressible in pylance (dataplane.py:218-220's 'the read path passes only the top-level storage_options' is a property of rask's code, not of the library); `aws_provider_scheme` appears nowhere in the installed package, so that sub-clause remains upstream-blocked.
  **Reopen if:** A warehouse record carrying an endpoint or credential field (warehouses.py:114), or a `base_<id>.` key composed anywhere in the catalog, would close the remaining half. If a future pylance drops `base_store_params` from lance.dataset(), the read-path finding reverts.
`catalog, storage` · med · **blocked:** owner acknowledgement of R3; the `aws_provider_scheme` clause additionally waits on an upstream pylance release

- *Why open:* `config.py:60-62,101-102` hardcodes the single-endpoint/single-key connection shape, so a warehouse record cannot declare per-base endpoints or credentials, a write door cannot say which base it targets, and a warehouse in another bucket/account/region (or on its own RustFS instance) is inexpressible — per-tenant or per-region storage cannot be modelled and failover cannot be an edit of `base_paths`. `base_store_params` exists but the keyed `base_<id>.<key>` form and `aws_provider_scheme` are unverified on the installed pylance, so code depending on them would be guessing. Per-base vending rides on this.
- *Closes when:* Probe the installed pylance for the `base_<id>.<key>` keyed form and `aws_provider_scheme` and record the result; then add `initial_bases` and `base_<id>.<key>` option fields plus per-warehouse endpoint/credentials to the warehouse record, add `target_bases` to the catalog write doors, and delete the storage-profile-per-warehouse code path.

**LH-068 · ~~Legacy data that predates warehouses has no migration path~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* Data in the default root that predates warehouses IS reached by the sweep, by policy and by vending, and a sanctioned bytes-first migration door with tests already binds such a namespace to a warehouse — the row's stated consequence does not hold.
- *Evidence:* SWEEP: services/maintenance/src/maintenance/services/purge.py:244-261 maintained_roots = settings.sweep_buckets UNION the warehouse registry's buckets, and services/maintenance/src/maintenance/core/config.py:233-237 puts the primary `s3_bucket` (the legacy root) into sweep_buckets unconditionally — no warehouse record required. VENDING: services/catalog/src/catalog/api/v1/endpoints/credentials.py:97-113 resolves the target from describe_table's `location` and never touches the warehouse registry (grep for 'warehouse' in that file returns nothing). POLICY: packages/service-kit/src/service_kit/lakehouse/maintenance_policies.py:131-160 resolve_policy matches table and namespace records by bucket-qualified PATH; only the `project` kind resolves via warehouse buckets, so a legacy dataset is policy-reachable. MIGRATION EXISTS: services/catalog/src/catalog/schemas.py:700-707 `adopt_existing` ('the sanctioned bytes-first migration'), implemented at services/catalog/src/catalog/api/v1/endpoints/warehouses.py:540-594 with both hazard guards preserved, and covered by tests/unit/test_warehouse_adopt_existing.py:17-40. SUPPORTED STATE: services/catalog/src/catalog/core/config.py:85-89 declares 'an unbound namespace always routes to the default root' as additive and backward-compatible.
- *What would reopen it:* A sweep, vend or policy path shown to return nothing for a table in the default root, or evidence that adopt_existing cannot bind an already-existing top-level namespace. What is genuinely missing is a bulk BYTE-COPY tool for the #54 flow (the operator half of 'copy the datasets into the bucket, THEN bind') — a far smaller item than 'no migration path'.

**LH-069 · Pre-registry 'ghost' project ids were never migrated, which is why the consume-side id rule stays looser than the mint rule**
`catalog, medallion` · med · **blocked:** owner decision: adopt the ghost ids into the registry, or revoke them

- *Why open:* The ghost ids never passed the mint rule, so tightening the consume rule would refuse them and the adopt-vs-revoke call has to come first. No live defect from the asymmetry itself — `is_safe_project` uses `fullmatch` and the Pydantic models anchor — but tightening is wire-visible on medallion's generated clients.
- *Closes when:* Decide adopt-vs-revoke for pre-registry project ids and run the migration over the live control root; then tighten the consume-side project-id rule to match the mint rule and regenerate medallion's clients.

**LH-070 · No versioned authz-model migration (`ACTIVE_MODEL_VERSION` + an idempotent `migrate()`) — the 3-axis model shipped without it**
`service-kit, catalog` · med

- *Why open:* The study ruled it mandatory before the 3-axis model and the model shipped anyway; there is no recorded active model version and no idempotent migration, so a model change cannot be rolled out safely.
- *Closes when:* Add an `ACTIVE_MODEL_VERSION` constant plus an idempotent `migrate()` that writes the authz model and pins the active version, called at service start.

**LH-071 · ~~Tuple helpers were never split into `tuples.py` and there are no golden tuple tests~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* Both of the row's stated reasons are false at HEAD: grant_on_create is NOT one inline grant (tuple construction is already factored into a shared seam with an invariant gate on its callers), and golden tuple tests asserting exact (user, relation, object) triples already exist in two suites.
- *Evidence:* THE SEAM: packages/service-kit/src/service_kit/governed/fga.py:125-153 `hierarchy_edge_tuples(child_object, parent_object, parent_relation)` returns both directions of the link; its caller set is closed and pinned by tests/unit/test_invariants.py:935-946 test_only_the_sanctioned_writers_seed_a_hierarchy_edge. fga.py:1312-1341 grant_on_create composes owner + hierarchy tuples through it and delegates to write_tuples — it constructs no edge itself. GOLDEN TESTS: packages/service-kit/tests/test_fga_edges.py:76-215 asserts the exact tuple keys for grant_on_create (parent edge + inverse in one batch :76; no edges for a root object :98; inverse skipped where the model declares no child :105; non-default parent relation :186; built on the shared pairing :194) and for revoke_object_tuples (:134, :151); tests/unit/test_a_machine_created_table_is_owned_by_its_project.py:52-95 captures the tuples one table create ACTUALLY writes, at write_tuples rather than at the call site, precisely so a threaded-then-ignored flag cannot pass.
- *What would reopen it:* Showing grant_on_create builds its edge tuples inline rather than via hierarchy_edge_tuples, or that test_fga_edges.py asserts call shape instead of tuple content. WHAT IS ACTUALLY LEFT is smaller and different: no module literally named tuples.py exists, and the raw grant/revoke door still composes a ClientTuple inline at services/catalog/src/catalog/api/v1/endpoints/access.py:350 and :521 with no golden test of its own — that narrower item is worth keeping, the row as written is not.

**LH-072 · The vended response still mixes credentials and config in one dict**
`catalog, storage` · med

- *Why open:* Half of the study's #2 landed per-vendor (`expires_at_millis`); the split never shipped, so a client cannot tell which fields are secret and which are configuration.
- *Closes when:* Split the vended response into `credentials` and `config` objects across the vendors, updating the generated clients that consume it.

**LH-073 · Right to erasure is a Lance row delete only — it reaches no blob sidecar, clone/branch or version-pinning tag**
`catalog, maintenance, notifications` · med

- *Why open:* A governance feature a lakehouse buyer expects, and none of the propagation exists.
- *Closes when:* Propagate a row delete to blob sidecars (reachability GC), to clones/branches via the referrer registry and to tags pinning old versions, plus a delete-subject door in notifications (G6).

**LH-074 · No quotas or storage accounting per project/warehouse**
`catalog, maintenance` · med

- *Why open:* Listed as 'none' today; branch-by-root gives per-directory cost for free, so the mechanism exists and the accounting does not.
- *Closes when:* Add per-project/warehouse storage accounting using branch-by-root's per-directory cost, and quota enforcement at the create/write doors.

**LH-075 · The read audit log has no retention policy and no index on dataset in GreptimeDB**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* Retention already exists — a database-level 14d TTL applied to GreptimeDB's `public` database by a Helm hook, which opentelemetry_logs (where read_data lands) inherits — so the trail does NOT grow unbounded and only the index half of the row is real.
  **Evidence:** RETENTION IS PRESENT: chart/values.yaml:2806-2820 documents `observability.retention: "14d"` as 'Applied as a database-level TTL on the GreptimeDB `public` database via a post-install/post-upgrade hook Job (templates/greptimedb-ttl-job.yaml), so every table (opentelemetry_logs, opentelemetry_traces, the metrics physical table) inherits it'; chart/templates/greptimedb-ttl-job.yaml:1-68 is that hook, issuing `ALTER DATABASE $DB SET 'ttl'='$TTL'` at :65; chart/templates/otel-collector.yaml:239 records the value observed on the store ('`ttl = '14days'`, verified on both the logical and the physical table'). The audit stream reaching that table: packages/service-kit/src/service_kit/governed/audit.py:1-11, 23, 46, 49-80 — the dedicated `lance.audit` logger, READ_ACTION = 'read_data', exported over OTLP to GreptimeDB. INDEX IS ABSENT: `grep -rniE "create index|skipping_index|inverted_index|fulltext|ALTER TABLE" chart/` matches only chart/templates/age-postgres.yaml:41, an AGE Postgres column index — nothing touches GreptimeDB's schema. Smaller true fix: add index DDL on the audit stream's dataset/resource column, in the same hook shape as greptimedb-ttl-job.yaml. Separately worth its own row (NOT what this one says): 14d is short for a compliance trail and is estate-wide, so 'the audit stream needs its own, longer retention' is a real but different item.
  **Reopen if:** Showing read_data rows land in a table that does not inherit the `public` database TTL, or that `ALTER DATABASE ... SET 'ttl'` does not propagate to opentelemetry_logs. Measured from the CHART; the live estate's applied TTL is UNVERIFIED here beyond the in-repo observation note at otel-collector.yaml:239.
`catalog, chart` · med

- *Why open:* The doors are done and observed end to end 2026-09-08 (`read_data` rows queryable by SQL); the Collector's half is missing, so the trail grows unbounded and filters scan.
- *Closes when:* Set retention on the audit table and add an index on the dataset column in the GreptimeDB/OTel Collector configuration in `chart/`.

**LH-076 · `can_observe_events` is the estate-admin bar under a name that says 'read the feed', and its comment names one of its four consumers**
`catalog, service-kit` · med · **blocked:** the comment half is unblocked; the rename half needs an owner ruling on repointing live checks + reseeding

- *Why open:* Verified still open at `service_kit/governed/auth/model.fga:197-200`: the comment documents only the `/v1/events` feed, while tenant minting (`endpoints/projects.py`), every raw tuple route (`access_admin.py:199`, router-wide `require_relation(..., "can_observe_events", settings.fga_root_object)`) and store registration all gate on it. No `can_administer_estate` exists.
- *Closes when:* Rewrite the comment above `define can_observe_events: owner` to enumerate all four gated operations with their call sites; then, separately, decide whether to add a distinctly-named `can_administer_estate` that `projects.py`, `access_admin.py` and `POST /v1/stores` alias to.

**LH-077 · `alter_transaction` gates a whole `AlterTransactionRequest` at one committer-tier check while the model claims a per-action distinction**
`catalog, service-kit` · med · **blocked:** owner ruling on per-action vs one-check authorization (the delete path also needs the `fga` CLI, which 403s through the sandbox proxy)

- *Why open:* `model.fga:443,445` still define `can_set_property: editor` and `can_cancel: committer` as removal candidates awaiting this decision, and `fga_deps._authorize_transaction` (`api/fga_deps.py:381`) picks between exactly `can_describe` and `can_set_status` — so anyone who can commit can also edit properties, and the model claims a distinction nothing enforces.
- *Closes when:* Either extend `endpoints/transactions.py`'s `alter` route to authorize per state-action (making `can_set_property`/`can_cancel` real doors), or delete both lines from `model.fga` with `fga model test` green — noting that deleting the `editor` rung is a second decision, since `viewer` inherits from it and it carries its own direct-grant slot.

**LH-078 · 8 credential vends per tick still 403, so 8 rewrites sign with the ambient root key, and no counter or alert surfaces it**
`maintenance, catalog` · low

- *Why open:* The headline is fixed (207 AMBIENT → 8, 277 SCOPED, via `_ALTERNATIVE_RUNGS` giving the `credentials` action a second rung), but the remaining 8 are a different population: the 92 per-warehouse `maintainer` tuples cover every warehouse in the registry, so these are tables the registry does not account for. The ambient fallback is loud in the pod log and reaches no report field, counter or alert.
- *Closes when:* Identify the 8 tables whose `POST /v1/table/{id}/credentials?tier=write` still 403s despite `can_maintain` and either register them or make the vend failure refuse the rewrite instead of falling back to the ambient key; and expose the AMBIENT-vs-SCOPED split as a counter/alert rather than only a log line.

**LH-079 · Two standing answers on the `x-api-key` principal contradict each other, and its key store and rotation model are undesigned**
`catalog, gateway` · low · **blocked:** owner decision reconciling Q7 with A6

- *Why open:* Q7 was decided 2026-09-02 (support both spec identity headers; keys minted, scoped and revoked by the management API as FGA principals with an expiry) — but A6 later measured that the spec's `security` block is a DISJUNCTION, that bearer alone is conformant (155 of 160 ops declare it; 401 with no credential and 401 with `x-api-key` alone), and struck the work as a second credential plane against the secret-store-only rule. Meanwhile no key store or rotation model exists.
- *Closes when:* Owner rules whether Q7's api-key principal is withdrawn in favour of A6's bearer-only position or the management API mints scoped, expiring keys after all; edit the losing row out rather than leaving both, and if it survives, write the key-store and rotation design into the management API RFC.

**LH-080 · ~~`can_promote` buys nothing on `table` because `validator ⊇ owner`~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* `can_promote` DOES discriminate on `table` — a writer is refused it — and `validator ⊇ owner` never implied otherwise; the case where the second door was free (publish requiring `owner`) was closed by the new `publisher` rung.
- *Evidence:* packages/service-kit/src/service_kit/governed/auth/model.fga:377 `define validator: [user, role#assignee, user with non_expired_grant, role#assignee with non_expired_grant] or owner or validator from parent` — direct grants and a parent cascade, so validator is not merely owner. :405 `define can_promote: validator`. Proven against the compiled model in CI: packages/service-kit/src/service_kit/governed/auth/model.fga.yaml:366-367 asserts dave `can_write_data: true, can_promote: false` on `table:acme_gold_catalog`, :373-376 asserts carol (validator via role) `can_promote: true` on the same table (.github/workflows/ci.yml:208 runs `fga model test`). Two live table-scoped call sites: services/catalog/src/catalog/api/v1/endpoints/publication.py:262-268 (the `accept_assertions` override) and endpoints/models.py:142 → api/fga_deps.py:1181-1182. publication.py:252-261 records that an ordinary publish now needs `publisher`, not `owner`, "which is what makes this second door cost something".
- *What would reopen it:* An assertion in model.fga.yaml showing a plain `writer` on a table passing `can_promote`, or `table.validator` reduced to `owner` alone.

**LH-081 · ~~Nothing proves `_authorize_transaction`'s two branches gate equivalent privilege~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* The two-branch equivalence IS proven against the compiled model — same three subjects, both branches, run by `fga model test` in CI — just not in the file the row names.
- *Evidence:* packages/service-kit/src/service_kit/governed/auth/model.fga.yaml:617-630 (object-scoped branch on `transaction:acme_silver$commit1`: dave can_describe/can_set_status true; carol can_describe true, can_set_status false; eve both false) vs :639-649 (parent-scoped branch on `namespace:acme_silver`: dave can_get_metadata/can_update_properties true; carol can_get_metadata true, can_update_properties false; eve both false) — the exact pairs services/catalog/src/catalog/api/fga_deps.py:413-419 sends. The yaml reads the real model (model.fga.yaml:12 `model_file: ./model.fga`, guarded by tests/unit/test_fga_model_contract.py:322-325) and is evaluated by the real engine (.github/workflows/ci.yml:208 `fga model test --tests $AUTH/model.fga.yaml`), so it is not a recording fake. The `parent` tuple that makes the object branch resolve is model.fga.yaml:52.
- *What would reopen it:* Removing model.fga.yaml:639-649 (or :617-630) without a replacement, or CI ceasing to run `fga model test` — either would leave the claim resting on api/fga_deps.py:386-405's docstring again.

**LH-082 · The gateway proxies the catalog's full all-method write surface to the public ingress and nothing says whether that is intended**
`gateway, catalog` · low · **blocked:** owner ruling on whether all-method public exposure of `/api/catalog/*` is intended

- *Why open:* Verified still present: `services/gateway/src/gateway/__init__.py:225` is `Route("/api/catalog", "", *catalog)` — every method, straight through from the ingress `- path: /api` rule. The home BFF is GET-only on purpose and writes go through SvelteKit remote functions, but this row bypasses that shape entirely; the catalog's safety then rests wholly on its own OIDC + FGA, and no ruling for it exists.
- *Closes when:* Record the ruling — either a rationale comment on the `Route("/api/catalog", …)` row plus a line in `docs/DECISIONS.md` stating the full write surface is deliberately internet-facing behind catalog-side OIDC+FGA, or narrow the row's method set.

### Not coupled to a workflow engine or Ray

_The platform claims to run any workload on any engine, and today the deployed stage lane, the catalog's published API and the media write path all name Ray._

**LH-083 · `engine_registry.executor_for` has ZERO callers — the deployed stage lane still calls `ray_submit` directly at `workflow.py:495`**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The bypass is real and unchanged at workflow.py:495, but the row's second closing action is already done — `ray-kit` is no longer a medallion dependency.
  **Evidence:** BYPASS STANDS: services/medallion/src/medallion/workflow.py:486 imports `submit_stage_job` and :495 calls it directly inside the `submit_stage` activity; `executor_for`'s only callers are tests (services/medallion/tests/test_the_chosen_engine_is_the_engine_that_runs.py:67,69,77,80 — no production caller in `grep -rn executor_for services/ packages/`), and services/medallion/src/medallion/services/transform.py:779 still constructs `InProcessExecutor(settings.storage_options)` by hand while `RayJobExecutor` is constructed only at engine_registry.py:70. ALREADY DONE: services/medallion/pyproject.toml:7-36 lists no `ray-kit` (`grep -n ray` returns nothing), and uv.lock's `[[package]] name = "medallion"` dependency block has no ray-kit entry — the orphaned comment at pyproject.toml:11-13 is what remains of it. Smaller true fix: route workflow.py:495 through `engine_registry.executor_for(...)`; the pyproject edit is a no-op.
  **Reopen if:** A `ray-kit` line reappearing in services/medallion/pyproject.toml, or a production call to `executor_for` in workflow.py.
`medallion, ray-kit` · **HIGH** · **blocked:** Q17-2 (the Ray adapter's fate)

- *Why open:* Corrected with `ast` rather than grep: exactly one in-scope bypass remains, `workflow.py:495` (`submit_stage_job`, the deployed stage lane) — `train.py:280` is the TRAIN lane and `ray_submit.py:318,425` are the adapter's own calls. The load-bearing half is the opposite error: nothing resolves an engine through the port, `transform.py:778` constructs `InProcessExecutor` by hand and `RayJobExecutor` is constructed by nothing. Migrating `transform.py` would be cosmetic; `workflow.py:495` is the site that CHOOSES an engine.
- *Closes when:* Route `workflow.py:495`'s stage submission through `engine_registry.executor_for(...)` once the Ray adapter's fate is decided, then drop `ray-kit` from `services/medallion/pyproject.toml`.

**LH-084 · `BAKED_JOBS_DIR`/`BAKED_CLUSTER_JOBS` live in the shared library and the catalog enforces them, so a non-Ray lane cannot be declared and the word 'Ray' reaches every API client via the published OpenAPI**
`catalog, service-kit, medallion` · was HIGH · **closed by measurement 2026-09-11**

- **RE-MEASURED 2026-09-11 — BOTH HALVES ARE ANSWERED, and one of them was never ours.**
- *The enforcement half is GONE.* `BAKED_JOBS_DIR` and `BAKED_CLUSTER_JOBS` appear nowhere in
  `packages/service-kit/src` or `services/catalog/src` — measured, zero occurrences — so the catalog no
  longer enforces a baked Ray job list and a non-Ray lane is declarable.
- *The OpenAPI half is THE SPEC'S OWN WORDING, not our coupling.* The word `Ray` survives in
  `docs/catalog-openapi.json` exactly three times, all on `MaterializedViewUdtfEntry`
  (`memory` / `num_cpus` / `num_gpus`, described as "Ray actor … request") — and those three
  descriptions are carried verbatim by the vendored `lance_docs/ns_catalog/spec.yaml:5845,5850,…`. The
  FIELD NAMES are already engine-neutral; only the spec's prose names an engine. Two further matches
  are our own docstrings naming `lance-ray` as one of the CLIENT libraries a vended credential serves,
  alongside pylance and the LanceDB SDK — a list of clients, not a dependency.
- *So the closure asked for would mean DIVERGING FROM THE SPEC we implement,* which the estate's own
  rule forbids ("idiomatic to lance-ns"). Changing those strings would make our published schema differ
  from the spec's for cosmetic reasons, and a client generated from either would disagree about a field
  it must send.
- *What would be worth doing, if anything:* raise the wording upstream. It is the spec that names one
  engine in a field that does not need it.

**LH-085 · The multimodal write lane is single-driver even though `lance_ray` 0.5.0 does not strip blob typing**
`medallion` · med

- *Why open:* NOT refuted — the measurement was reproduced first-hand on the exact library set the cascade image pins (`.docker/ray-lance.dockerfile:67`: lance-ray==0.5.0, pylance==10.0.0, pyarrow==25.0.0, held equal to the venv by `tests/unit/test_ray_job_images.py:105`; ray 2.58.0). The row's verdict column is truncated in the register at the script path, so §Q13's header rule applies: re-derive before working it.
- *Closes when:* Re-derive the blob-typing measurement against lance-ray 0.5.0 inside the ray-lance image, then remove the single-driver constraint from the media write path if blob typing survives a distributed `lance_ray.write_lance`.

### Events are correct

_The cascade, the inbox and every downstream consumer are driven by events, so a trigger that does not fire or a payload that names the wrong thing fails silently and is reported by nothing._

**LH-088 · ~~Every lineage restart replays the retained stream and logs ~125 `dapr_dead_letter_parked` ERRORs claiming provenance was lost~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* Option (b) landed 2026-09-09: a byte-identical replay the durable feed already holds is no longer re-authorized, so it never becomes a DROP and never reaches the dead-letter ERROR.
- *Evidence:* services/lineage/src/lineage/api/fga_deps.py:270-276 — `except PermissionDeniedError: if not await _is_replay(event, payload, request): raise` then `log.info("lineage_replay_not_reauthorized", …)`. `_is_replay` (:280-303) compares the stored payload byte-for-byte: `stored = await repository.recorded_event(event.run.run_id, event.event_type); return stored is not None and stored == payload`, and its docstring records the measurement ("a full 2 161-message replay left the durable feed flat at 3 248 rows"). The exemption covers the row's `ingest_run_mutation_denied` burst too, because that path raises the same `PermissionDeniedError` (fga_deps.py:354-355) inside the same try. Without the raise, services/lineage/src/lineage/services/consumer.py:71-75 never returns `_DROP`, so api/dapr.py:59-79's `dapr_dead_letter_parked` ERROR is not reached. Landed in a007b224 (2026-09-09) and pinned by services/lineage/tests/test_the_bus_door_authorizes_what_it_records.py:265,291 (a replay differing in any field is still refused; an unauthored replay is exempt, an unauthored new event is not).
- *What would reopen it:* Removing the `_is_replay` exemption, or a restart log still showing a burst of `lineage_event_unauthorized` + `dapr_dead_letter_parked` for events already in the feed — the latter is a DEPLOYED-estate observation I did not make.

**LH-089 · ~~Neither `medallion.bronze` trigger head can be retired: no remaining writer can publish, so disabling either strands a lane~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* Every premise is false at HEAD: `/produce` and `/ingest-media` ARE catalog-mediated, stage runners DO register and publish, no stage-runner token dedupe exists, and retiring a head was raised and ruled AGAINST.
- *Evidence:* `/produce` registers BEFORE the write: services/medallion/src/medallion/services/produce.py:152-161 calls `catalog_register.register_written_dataset`, with :123-127 stating "GOVERNANCE PRECEDES THE FIRST ROW … ask first, write second" and :168-180 failing the request on a refusal. `/ingest-media` asks the catalog: services/medallion/src/medallion/services/media_produce.py:174-179 `catalog_register.ensure_stage_output`. Stage runners register and publish: services/medallion/src/medallion/services/transform.py:992 `ensure_stage_output`, :1275 and :1363 `publish_stage_output`; services/medallion/src/medallion/workflow.py:1369-1371. NO TOKEN DEDUPE: services/medallion/src/medallion/api/bronze_arrival.py:78-82 — "there is no token de-duplication in the stage runners either — a comment here claimed one until 2026-08-08; `transform.py` only reads the token into logs and lineage run-ids". RETIREMENT RULED AGAINST: bronze_arrival.py:68-71 ("IT DOES NOT REPLACE /bronze-arrival, AND RETIRING EITHER HEAD IS NOT THE FIX … that was ruled against") citing docs/architecture/medallion-cascade.md:24-44 ("DECIDED — the two cascade heads are distinct events, and both must fire"). Both routes still exist (bronze_arrival.py:41 and :99).
- *What would reopen it:* A `/produce` path that writes bronze without touching the catalog, or a reversal of docs/architecture/medallion-cascade.md §10 making single-head the target shape.

**LH-090 · ~~`POST /v1/table/{id}/publish` emits the `table_published` control event carrying the WRONG `to_version`~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* `table_published` carries the version the `published` tag was actually moved to — there is no mechanism in the publish path that could produce a different `to_version`.
- *Evidence:* services/catalog/src/catalog/services/publication.py:454-459 — `_set_tag(ns, storage_options, table_id, tag, version)` is immediately followed by `PublicationResult(..., from_version=previous, to_version=version, ...)`, i.e. the tag and the reported `to_version` are the same literal `version`; `from_version=previous` is the tag's prior value read at publication.py:404. services/catalog/src/catalog/api/v1/endpoints/publication.py:330-344 — the emit passes `from_version=result.from_version, to_version=result.to_version` straight into `publication_extra`, which puts them on the event verbatim (publication.py:148). services/medallion/src/medallion/services/publication_trigger.py:143-144 — the consumer reads `extra.get("to_version")` through unchanged. tests/unit/test_publication.py:310-317 drives the REAL `publish_table` endpoint (not a copy) and asserts `(announced[0]["from_version"], announced[0]["to_version"]) == (None, good)` where `good` is the version written. The dropped-parameter defect that DOES exist in this file is a different one and is already fixed: services/catalog/src/catalog/services/publication.py:427-434 records `assert_quality` having been called with only `candidate.uri`, so it re-opened bare and scanned `latest`; `version=version` is now passed. Nothing named `to_version` was ever read from `dataset.version` — `git show d46eb0ad` (the commit that introduced the emit, 2026-08-04) already used the requested version.
- *What would reopen it:* A commit path where `_set_tag` is called with a version different from the one stamped into `PublicationResult.to_version`, or an emit site for `table_published` other than publication.py:330 (grep for `action="table_published"` across services/ returns exactly one non-test producer).

**LH-091 · The change feed has no control-lane EVENT, so a BYO consumer must poll `POST /v1/table/{id}/changes`**
`catalog, lineage, notifications` · med

- *Why open:* The door landed and was driven live 2026-09-09 (three defects found and fixed), leaving exactly one residue: a consumer can only poll, never be told.
- *Closes when:* Emit a control-lane event on `catalog.control.v1` when a governed table's version advances, so a change-feed consumer is notified rather than polling.

**LH-092 · The ingest-lane slice proves the TRIGGER chain but not the DATA chain — `MEDALLION_FROM_URI`/`TO_URI` are unset there**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The tier URIs the row asks to configure are already rendered for the lane's deploy path — the only true residue is that the lane never asserts a committed silver/gold version with row counts.
  **Evidence:** scripts/ingest-lane.sh:79-97 (`lane_values`) sets `medallion: {enabled: true}` and does NOT override `medallion.compute`; chart/values.yaml:1187 has `compute: true` as the shipped default; chart/templates/medallion.yaml:505-512 renders `MEDALLION_COMPUTE_ENABLED`, `MEDALLION_FROM_URI` and `MEDALLION_TO_URI` under `{{- if $root.Values.medallion.compute }}` for every stage runner. So the guard at services/medallion/src/medallion/services/transform.py:971 (`if settings.compute_enabled and from_uri and to_uri:`) is satisfied in the lane slice, not bypassed. The row's cited locator (medallion.yaml:502-503) has drifted to 511-512. The values.yaml:1176-1181 caveat 'REQUIRES OpenBao off' is itself stale — the medallion now fetches its S3 secret through Dapr (services/medallion/src/medallion/core/config.py:447-449 + packages/service-kit/src/service_kit/governed/secrets.py:147-193), and chart/templates/medallion.yaml:522 withholds the plaintext key only when `secretsViaDapr` is on. What IS absent: scripts/ingest-lane.sh asserts only bronze — `committed_version` (line 506-508) and `units_done` (line 512-515), repeated at 617-619 and 717-721 — and the word silver/gold appears in the script only inside the comment at line 89. Nothing reads a silver or gold version or row count.
  **Reopen if:** A `MEDALLION_FROM_URI` that renders empty in the lane's `helm template` output (run `scripts/ingest-lane.sh` render and grep), or an assertion in ingest-lane.sh that opens the silver/gold dataset and checks its version or row count.
`medallion` · med

- *Why open:* `medallion/services/transform.py:970` guards the whole compute path on `settings.compute_enabled and from_uri and to_uri`; in the lane slice those URIs are unset and no project routing is configured, so the stage runner wakes, emits and writes nothing. Partly overtaken — `chart/templates/medallion.yaml:502-503` now renders both for the chart deploy path — so what remains is the lane proving bronze→silver→gold moves BYTES.
- *Closes when:* Configure the tier URIs (or the per-project warehouse registry) in `scripts/ingest-lane.sh` and assert a committed silver and gold version with row counts, not just a `POST /medallion-event 200`.

**LH-093 · ~~The event actor is one `author.sub` string where it should be a closed union~~ — THE LIVE DEFECT CLOSED 2026-09-11; the union is struck**
`medallion, catalog, service-kit` · was low

- *Closed by:* `a47baa01`. The row's stated CONSEQUENCE — "a producer stamping a role literal targets
  nobody and the event is still acked SUCCESS" — had two parts, and re-measuring separated them.
  * The TARGETING half was already solved and the row did not say so: `NotificationReason.ORIGINATOR`
    exists precisely for it, wired end to end (producer stamps, `api/fanout.py` appends to the audience
    and delivers under its own reason). Its docstring states the design: the role literal is "a truthful
    statement about who ran the stage", so `originator` is a SEPARATE field rather than an overwrite —
    "overwriting attribution to fix targeting would trade one wrong answer for another".
  * The LIVE defect was that only ONE producer applied the rule. The catalog guarded with
    `is_person_subject`; `medallion/schemas/events.py` wrote `if originator:` and kept every value — in
    the service whose authors ARE the role literals. It could not have been shared where it lived: the
    rule was inside the catalog and the medallion cannot depend on the catalog.
  * Now `service_kit.lakehouse.subjects`, imported by both. OBSERVED on the deployed medallion:
    `data_eng`/`analyst`/`ray`/`reconcile`/`*`/`team:eng#member`/`user:alice` all dropped, a dex subject
    stamped. Before, all eight were kept.
- *THE CLOSED-UNION ASK IS STRUCK.* Making a role literal unrepresentable would forbid a value the
  estate deliberately keeps as truthful attribution, and contradicts the recorded reasoning above. The
  defect was never that the literal exists; it was that one producer treated it as an address.
- *Two things the RED test caught that reading would not have:* the catalog's own denylist was missing
  `reconcile`, and a first draft added `htr` — a WORKLOAD name, which the platform is forbidden to know,
  so a denylist entry for a modality would have been the defect rather than the fix.
- *Residual, recorded honestly:* enumerating the chart's literals in a shared seam is itself a mild
  coupling. The frozenset is documented as the FLOOR, not the design; the durable answer is for a
  producer to mark its own non-person authors rather than for the platform to list them.

**LH-094 · The reconcile scan reports 65 incomplete units and reclaims nothing, but its recorded numbers and stated cause are both STALE — re-measure before working it**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* Both halves the row prescribes have landed — `incomplete`/`excluded` are defined and the depth-limit inflation is fixed, and flag 16 no longer blanket-refuses because the gate now parses `BasePath.is_dataset_root`; what is left is only the narrower `is_protected` containment question.
  **Evidence:** The definitions the row says to establish FIRST are stated in code: services/maintenance/src/maintenance/services/reconcile.py:236-244 — `excluded_datasets` is 'datasets the unreferenced-file method does not APPLY to … Deliberately not in `incomplete`, which gates the purge', and `incomplete` is fed from reconcile.py:546/569/582/642/834/841/848/859 (the `storage:lance-catalog` entries the row asks to trace come from `_read_registry` at 559-569 and the bucket walk at 841-848). The `incomplete=65` cause is closed by LH-100's fix, present at HEAD: services/maintenance/src/maintenance/services/optimize.py:143-170 `_may_hide_a_dataset` plus its call at optimize.py:236 — a truncation is now recorded only where a subdirectory actually exists below the bound. The flag-16 half of the 'original plan' has landed too: services/maintenance/src/maintenance/services/optimize.py:602-620 records that refusing on flag 16 alone was over-broad and that the gate now 'asks about the BASES, not about the FLAG', wired at optimize.py:626-640 via `gather_compaction_bases(ds, dataset_root_probe(uri, storage_options))`, which reads `BasePath.is_dataset_root` (packages/service-kit/src/service_kit/lakehouse/features.py:384,404,508). The residue: NO per-base `managed`/`reference-only` field exists — packages/service-kit/src/service_kit/lakehouse/base_refs.py:84-89 `BaseRefs` carries only `protected: set[str]` and `unreadable`, and the refusal at optimize.py:651-653 is driven by `is_protected`'s two-way containment rule (base_refs.py:96-110), which refuses a branch at `<dataset>/tree/<name>` because it lies UNDER a protected root. The smaller true fix is: decide whether `is_protected` should exempt an `is_dataset_root=False` (reference-only) base, and record that distinction on the base — not a new warehouse-record schema plus scoped cleanup credentials.
  **Reopen if:** A live reconcile tick whose `incomplete` is non-zero for a reason other than a genuinely unreadable prefix, or a `maintenance_refused_protected_base` log line naming a dataset whose only referrer resolves through an `is_dataset_root=False` external blob base — that would prove the reference-only field is still load-bearing. (The row's live numbers — 13/65/442 — are estate claims I could not verify from the tree.)
`maintenance, catalog, ingest` · **HIGH**

- *Why open:* **RE-MEASURED 2026-09-10 on the live estate and the headline is falsified.** Two consecutive reconcile ticks five minutes apart both report `total=13 counts={'ghost_projects':1,'unbound_namespaces':4,'orphan_buckets':12,'orphaned_annotation_tasks':3,'orphan_files':0} incomplete=65 excluded=442`. Against the numbers this row carried (`total 611, incomplete 490, orphan_files 598, orphan_buckets 12`): only `orphan_buckets` still matches. `orphan_files` is **0**, not 598 — nothing is being named as an orphan at all.
  The stated CAUSE does not match either. `maintenance_refused_protected_base` is still firing hard (286 in one sweep window, against the 220-per-six-hours this row recorded), but the refusals name a DATASET and not a bucket, and the reason is the shallow-clone rule working as designed: `uri='s3://tracka-wh/4d70964a_tracka$brupd_06b2abb0/tree/dev' reason='another dataset resolves its files through tracka-wh/4d70964a_tracka$brupd_06b2abb0 (shallow clone / multi-base)'`. Those are BRANCH paths whose files resolve through the parent, which is exactly what `base_refs.protected_roots` exists to protect. So "each refusal protects an entire bucket because a base reference names a bucket rather than a base" is not what the estate does today.
  **`incomplete` IS FULLY EXPLAINED and belongs to [[LH-100]], not here.** Read in-pod from the reconcile route's own response (the log line carries sources, not reasons): 65 units were 64 x `depth limit reached` plus ONE unreadable record — `lance-catalog/_projects/_probe.json`, a 1-byte object containing `x` that no source in this repo writes, i.e. estate residue that had made the projects-registry scan permanently incomplete. Removed 2026-09-10 after inspecting it; the count went 65 -> 64 and the remainder is now a single homogeneous cause, which LH-100 owns. Nothing here is a flag-16 refusal, and `excluded=442` is the deliberate exclusion counter, reported so coverage stays visible rather than a failure.
- *Closes when:* FIRST establish what `incomplete=65` and `excluded=442` actually are — read `reconcile.py`'s definition of each and follow the ~53 `storage:lance-catalog` entries to the code that emits them — because the per-base policy this row used to prescribe was designed against numbers that no longer reproduce. If a flag-16 refusal still blocks a real reclaim after that, the original plan stands: a RED test pinning what pylance does to an external blob under `initial_bases` before changing any GC behaviour, parse `BasePath.is_dataset_root`, add a per-base `managed`/`reference-only` field to the warehouse record read by `service_kit/lakehouse/base_refs.py::protected_roots`, and issue cleanup credentials WITHOUT delete rights on reference-only bases.

**LH-095 · ~~A repeatedly-refused trash record is indistinguishable from a transient one — `attempts`/`last_refusal` are never persisted~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* `attempts` and `last_refusal` ARE persisted on the trash record and consumed by the purge — the row's 'grepped at HEAD and found nothing' is falsified.
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/trash.py:105-155 — `note_refusal(...)` builds `{**record, "attempts": (attempts if isinstance(attempts,int) else 0)+1, "last_refusal": {"at": at, "reason": reason}}` (lines 138-146) and writes it through `mutate_json` (line 149), which is exactly the conditional read-modify-write primitive the row's Closes-when asks for, for exactly the reason it gives (docstring lines 122-128: `expires_at` must never move). It returns `None` on `RecordMissingError`/`RecordChangedError` (lines 150-152) so bookkeeping never outranks the refusal. services/maintenance/src/maintenance/services/purge.py:466-491 — `_refuse` is the ONE helper every refusal arm routes through; it calls `trash.note_refusal` (line 488) and puts the count on the report (`RefusedRecord(..., attempts=attempts)`, line 491). services/maintenance/src/maintenance/services/purge.py:118-134 — `RefusedRecord.attempts: int | None`. Landed in commit 72f07827 'fix(maintenance): a permanently-refused trash record now reads as permanent' (2026-09-10), confirmed an ancestor of HEAD via `git merge-base --is-ancestor`. The lease clause the row already struck is likewise enforced, not assumed: services/maintenance/src/maintenance/api/routes.py:50-55 records the invariant test that fails the moment `replicas` stops being a literal 1.
- *What would reopen it:* A refusal arm in purge.py that appends to `out.refused` without going through `_refuse`, or a trash record read back after two refusal ticks whose `attempts` is still absent or 1.

**LH-096 · Every Lance-serving path opens `lance.dataset()` per request, no process shares a `lance.Session`, and nothing bounds Lance's 1 GiB metadata + 6 GiB index + 2 GiB io-buffer defaults against the 512 Mi pod tier**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The bare per-request opens are real and unchanged outside maintenance, but the per-pod sizing half of the coupled Closes-when is already solved generically, so the remaining work is threading one existing seam — not a handle cache with a freshness contract plus per-service cgroup arithmetic.
  **Evidence:** Still real: 66 non-test `lance.dataset(` call sites across services/ + packages/, of which exactly 7 pass a session — all in maintenance (reconcile.py:328, optimize.py:574, purge.py:280, purge.py:710, orphans.py:173, sweep.py:270) plus base_refs.py:187 which mints its own default. Per service: medallion 15, catalog 12, maintenance 9, ingest 8, service-kit 8, viewer 6, lineage 6. Named sites open bare at HEAD: services/lineage/src/lineage/core/reconcile.py:61,79,96,111 and services/medallion/src/medallion/services/compute.py:135,145,177,178,237,273; the catalog's request path is services/catalog/src/catalog/core/namespace.py:92,114 plus services/catalog/src/catalog/services/dataplane.py:307,613,648,822,882 (the row's locator `catalog/core/namespace.py:48` has drifted — line 48 is a branch-failure classifier, the opens are at 92/114). The residue the row bundles is also real: `LANCE_CPU_THREADS`/`LANCE_IO_THREADS`/`LANCE_LOG` appear NOWHERE in the tree (grep over .py/.yaml/.sh returns nothing), and `instrument_lance_if_available` is called only by catalog/main.py:39, lineage/main.py:29, medallion/stage_runner.py:38, medallion/producer.py:47, maintenance/service.py:44 — not ingest, viewer or search. What is NOT still needed: the sizing clause. packages/service-kit/src/service_kit/lakehouse/lance_session.py:81-123 `affordable_cache_bytes` reads the container's own cgroup and scales the requested caps proportionally (default 0.4 of the limit), and services/maintenance/src/maintenance/core/config.py:392-410 shows the whole call shape — `affordable_cache_bytes(md << 20, idx << 20)` then `lance_session(...)`. A converting service states a ratio; no per-pod arithmetic is required of it, so 'sized to each pod's cgroup limit' is satisfied by using the seam.
  **Reopen if:** A converted service that still OOMs after passing `lance_session(affordable_cache_bytes(...))`, which would show the clamp is insufficient and the coupled redesign is warranted; or a `lance.dataset(..., session=...)` call outside services/maintenance and base_refs.py.
`viewer, medallion, lineage, catalog, ingest, maintenance, service-kit, chart` · **HIGH**

**RE-MEASURED 2026-09-10, AND THE COUPLING THE ROW ASSERTS DOES NOT APPLY TO THE HALF THAT MATTERS.** The row treats "stop opening per request" and "size the caches" as one change. They are two, and only the first is dangerous: a cached HANDLE pins a version, which is why the viewer's registry is a read-only trade. A bounded `lance.Session` is NOT a handle cache — its keys are `(uri, version, etag)`, so a compaction writes NEW keys and there is no freshness contract to design and no stale-read window; `lance_session.py` records that, and that it is thread-safe under 8x50 concurrent opens. **Maintenance has already shipped exactly this and it is the proof:** `shared_lance_session()` caps it at 128 MB metadata + 256 MB index and threads it through reconcile, purge and the orphan scan. **Measured today:** every lakehouse pod runs a 512 Mi limit (128 Mi request), and ONLY maintenance passes a session — catalog opens 12 bare datasets, medallion 15, lineage 6, with 6 more in shared service-kit code. Each of those mints Lance's 1 GiB metadata + 6 GiB index ceilings and discards them WITH the handle, so the cache never engages at all: ten version-opens against a shared session grow `size_bytes` 168 -> ~75k, the same opens without one leave it flat. So the safe, proven half is a bounded session per service — soft LRU bounds, no version pinning, the pattern already running in maintenance — and it needs ONE seam plus 39 call sites converged onto it, not a redesign. - *Why the rest of the row is open:* A shared handle also pins a version, so the fix needs `checkout_latest` (or a session-scoped open) plus an explicit freshness contract per service. Measured: 53 `lance.dataset()` call sites and 5 pass a session; the catalog opens ~24 bare datasets per request path. The same row carries the rest of the runtime hygiene: no `LANCE_CPU_THREADS`/`LANCE_IO_THREADS`/`LANCE_LOG` in the Ray `runtime_env`, `instrument_lance_metrics` never called by ingest/viewer/search/annotator, no branch/tag name validation at the door, blob thresholds unpinned on some create paths, `allow_http` not derived from the endpoint scheme, missing HTTPX timeouts.
- *A LIVE OOM, 2026-09-10, on the one service that ALREADY has the bounded session.* `rask-maintenance` was OOMKilled (exit 137, `reason: OOMKilled`) against its 512Mi limit and restarted; steady-state is 153Mi. It happened while three manual `/maintenance-reconcile-cron` invocations overlapped the 5-minute scheduled ticks — self-inflicted, and the pod recovered unaided — so this is a data point about HEADROOM, not a standing outage.
  Two things it settles and one it does not. It settles that the 512Mi tier has little margin on the scan path, and that **a bounded `lance.Session` is not by itself sufficient** — maintenance ships `shared_lance_session()` at 128MB metadata + 256MB index and still died. It does NOT establish that Lance caches are the allocator: the reconcile scan's own structures over 93 buckets and ~1,163 Dataset nodes are an equally plausible source, and nothing here separates them. Attribute before sizing — `index_cache_size_bytes` tuned against the wrong consumer buys nothing.
  **CHASED, AND IT IS NOT CONCURRENCY — IT IS ARITHMETIC.** `_reconcile_lock` is correct (`if locked(): return skipped`, `routes.py:149`) and the manual calls were sequential, so nothing overlapped. The caps are the problem: `shared_lance_session()` is built from `lance_metadata_cache_mb=128` + `lance_index_cache_mb=256` (`maintenance/core/config.py:38-39`) = **384 MB**, against a **512Mi** pod limit with a measured process baseline of **153Mi**. 384 + 153 = 537 MB. **A fully-warmed session OOMs this pod by construction**, and warming is exactly what a scan that opens every dataset in 93 buckets does — which is why the kill came after repeated reconcile passes rather than during one.
  The session's own docstring gives the right reason for capping — Lance's 1 GiB + 6 GiB defaults "dwarf the pod's own 512Mi limit" — and then picks caps that exceed what is left after the process itself. So the estate's ONE example of the fix this row prescribes is also an example of getting it wrong: **the caps must be sized against (limit - baseline), not against the limit**. Sizing every other service to this pattern would propagate the error to catalog, medallion, lineage and ingest.
- *MEASURED 2026-09-10 — copying the one worked example would have OOMed the whole lakehouse.* Live per-container memory against the uniform 512Mi limit: maintenance 269Mi, catalog 214Mi, lineage 200Mi, medallion-producer 189Mi, ingest 165Mi, viewer 157Mi. Add maintenance's configured 384 MB of session cache to ANY of them — 653, 598, 584, 573, 549, 541 — and every one exceeds 512Mi. This row's Closes-when says to size each service the same way, and maintenance is the only existing implementation, so the pattern the other five would be copied from is the one that OOMKilled its own pod.
  **OBSERVED ENGAGING ON THE LIVE ESTATE 2026-09-10 17:48:42**, not merely deployed: `lance_cache_clamped_to_container requested_bytes=402653184 granted_bytes=214748364 container_budget_bytes=214748364 fraction=0.4` — 384 MB asked for, 205 MB granted, read from the pod's own cgroup. The fresh pod's baseline is 139Mi (down from the 269Mi that included a partly-warm session under the old caps), so the steady state is ~344/512 against the 537 that killed it.
  **Fixed generically instead of per service** (`1ee04aa0`, `6ca5bec9`): `service_kit.lakehouse.lance_session.affordable_cache_bytes` reads the container's own cgroup and reduces the requested caps PROPORTIONALLY (preserving the caller's ratio) when their sum exceeds an affordable share, default 0.4 of the limit. So the converting services need no per-pod arithmetic — they state a ratio and call it. Verified on the real host: `/sys/fs/cgroup/memory.max` = 536870912 in `rask-maintenance`, v1 path absent. At 0.4 the budget is ~205MB, which every service above clears; maintenance is the tightest at 474/512 and its 269Mi includes a partly-warm session under the OLD caps, so the post-deploy baseline should fall.
- *Closes when:* One coupled change per Lance-serving service: a shared session/dataset handle with an explicit refresh policy at `viewer/api/v1/endpoints/pages.py:90`, `medallion/services/compute.py`, `lineage/core/reconcile.py` and `catalog/core/namespace.py:48`, AND in the same change `index_cache_size_bytes` (+ `io_buffer_size` if `LANCE_IO_THREADS` is raised) sized to each pod's cgroup limit; then the three `LANCE_*` vars in the Ray `runtime_env`, one `instrument_lance_metrics` call per process, door-side branch/tag validation, pinned blob thresholds, `allow_http` derived from the endpoint scheme and HTTPX timeouts.

**LH-097 · Tiers re-materialise managed blob bytes per tier instead of silver being a shallow clone of bronze@N plus `add_columns`**
`medallion, maintenance, catalog` · med · **blocked:** owner acknowledgement of R9 plus the storage-vs-coupling trade, and the recorded clone→source edge

- *Why open:* `compute.py` copies managed blob bytes into every tier — measured ~100% per tier materialised against 0.16% (bronze) / 0.19% (silver) carried as forwarded external descriptors — on the ground that 'the bytes exist nowhere else', which a shallow clone refutes. The prerequisite guard is now measured live (43,604 base-ref observations, 220 refusals in six hours, both on-demand doors passing `protected`), so what is left is the lifecycle trade — a referencing silver means bronze can never be reclaimed independently — and the measurement itself, which has never been taken.
- *Closes when:* Land the clone→source pins; add a `scripts/` measurement of both shapes (bytes and latency) against the medallion's blob path on one corpus; then make silver a shallow clone of bronze at a pinned version plus `add_columns` in `medallion/services/compute.py`.

**LH-098 · Maintenance's reclamation trail carries no attempt count, no duration and no per-object fact a TENANT can query**
`maintenance, lineage` · med

- *Why open:* The first half landed and is deployed — `audit_material_work` records a rewrite on the `lance.audit` stream keyed on `table:<id>`, wired into `_record_dataset_outcome` and gated on `_did_material_work` — but it is operator-global telemetry on a 14-day trace TTL, so 'which compaction rewrote my table last quarter, and did it fail first?' has no tenant answer. It is also not yet OBSERVED (2,200 dataset outcomes with `fragments_removed` and `old_versions_removed` both summing to 0, because the estate is converged), and `maintenance/core/lineage_emit.py` gives FAIL a deterministic run id per dataset so attempts merge onto one node and are structurally uncountable.
- *Closes when:* Add attempt number and duration to the audit record and make it FGA-gated per object so a tenant can query its own table's rewrites; fix the deterministic FAIL run id in `maintenance/core/lineage_emit.py`; then observe it on a dataset that actually has fragments to reclaim, measured through the running app or a door.

**LH-099 · A sweep that deleted a terabyte and one that deleted nothing produce the same-shaped report — no bytes-reclaimed anywhere, and no control event for what was rewritten**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* `summarize` already carries bytes-reclaimed; the true residue is only the missing metric export for the SWEEP and the owner-blocked control-event decision.
  **Evidence:** Falsified: 'Nothing in `summarize` carries reclaimed bytes'. services/maintenance/src/maintenance/services/sweep.py:975-979 — `"bytes_removed": sum(r.bytes_removed for r in results)`, with the comment recording it was write-only until 2026-08-15; sourced from services/maintenance/src/maintenance/services/optimize.py:92 (`bytes_removed: int = 0`) and 511 (assigned from the Lance cleanup stats), and also carried per-dataset at sweep.py:603 and into the audit record at sweep.py:834. Still real: the METRIC. services/maintenance/src/maintenance/core/metrics.py:142-150 `record_reclaimed(*, fragments_removed, versions_removed, indices_optimized)` takes no bytes; the only bytes instrument is `maintenance.trash.bytes_reclaimed` (metrics.py:90-91), fed solely by the trash purge (purge.py:688), never by the sweep. Still real: the event. `table_maintained` exists nowhere in the tree (grep over .py/.ts/.json returns nothing), and the `ControlAction` literal at packages/service-kit/src/service_kit/control_events.py:35-140 now has 41 members (the row says 38) with no maintenance verb; services/maintenance's only `emit_control` call is the rare purge at services/maintenance/src/maintenance/services/purge.py:575 (`table_purged`/`namespace_purged`), and the hourly sweep publishes no control event at all.
  **Reopen if:** A `bytes_removed` argument added to `record_reclaimed` in maintenance/core/metrics.py, or a `table_maintained` member in the ControlAction literal.
`maintenance, service-kit, notifications` · med · **blocked:** owner decision, shared with the branch/tag control-event question — whether anyone should be TOLD a table was compacted, or whether it is an audit record only

- *Why open:* Nothing in `summarize` carries reclaimed bytes, there is no metric, and no control event is published per reclaiming sweep. The audit-log half is done (`maintenance_dataset_outcome`, one structured line per dataset per tick), but the rare destructive purge emits `table_purged`/`namespace_purged` while the hourly sweep over every dataset emits none — and there is no `table_maintained` action in `ControlAction`'s 38 values to emit even if it wanted to.
- *Closes when:* Add bytes-reclaimed to `summarize` and export it as a metric; decide with the branch/tag control-event question whether a compaction is an audit record or a notification, and if an event, add `table_maintained` across the three-file ControlAction contract (`service_kit/control_events.py:94` plus emitter/consumer, pinned by `tests/unit/test_control_action_three_file_contract.py`) and emit it from `maintenance/services/sweep.py:486-524` and `catalog/api/v1/endpoints/maintenance.py:39-56`.

**LH-100 · ~~49 `models/<run>/<id>/` prefixes still file as IncompleteScan coverage gaps~~ — CLOSED 2026-09-10**
`maintenance` · med

- *Closed by:* `_may_hide_a_dataset` in `maintenance/services/optimize.py`. The walk now records a
  truncation only where a subdirectory actually exists below the depth bound — a directory whose
  children are all FILES cannot hide a Lance dataset, because a dataset IS a directory with a
  `_versions/` child (`objectfs.is_lance_dataset_root`, the estate's one definition). An unreadable or
  vanished prefix errs toward REPORTING: trading a false gap for a false CLEAN is strictly worse, since
  the first blocks a purge and the second lets one run over ground nobody scanned.
- *What the measurement settled:* 64 of 64 `incomplete` units were `depth limit reached`, the majority
  `s3://lance-catalog/models/<model>/<hash>` — model artefacts, which are files. Depth 6 finds no extra
  datasets, so raising `discoveryMaxDepth` was the wrong lever: it costs a wider scan across 93 buckets
  and leaves every gap in place. The lever was already deployed at its default (`=3`); that half of the
  original row was falsified.
- *The owner decision:* fix the reporting, purge stays report-only. So `report_is_clean` may now
  certify an estate it was previously refusing, which is the intended outcome (LH-094 records that
  nothing is being reclaimed) — but the destruction posture itself is unchanged and
  `MAINTENANCE_TRASH_PURGE_ENABLED` remains report-only by default.
- *Pinned by:* `maintenance/tests/test_the_bucket_walk_does_not_invent_coverage_gaps.py` — a directory
  of files at the bound is not a coverage gap; a directory of directories still is.

**LH-130 · ~~The catalog's distributed compaction doors rewrite fragments with none of the gates its own compact button applies~~ — CLOSED 2026-09-11**
`catalog` · conditions 2 + 5

- *Closed by:* `850b1274`. `dataplane.commit_compaction` now calls `require_compactable` — the
  feature-flag evidence gate plus the #114 shallow-clone base-refs guard, the same pair
  `maintenance.services.optimize` asks before its own `compact_files` and the same pair
  `maintenance.compact_now` already applied behind `POST /v1/table/{id}/maintenance/compact`.
- *What was wrong:* the estate published TWO writer-tier routes onto one operation, both exposed at
  the ingress under `/api/catalog`, and gated one. `compaction_plan` / `compaction_commit` reached
  `lance_optimize` directly. So which answer a caller got depended on which door they happened to
  call — worse than an ungated door alone, because the estate looked gated.
- *Why the gate is on COMMIT:* planning is a manifest read that moves no byte and mints no version;
  the commit publishes the new files and drops the old fragments. A plan nobody commits costs nothing,
  and gating the read half would refuse callers who are only asking a question.
- *Pinned by:* `tests/unit/test_maintenance_runs_on_workers.py` — one test that a shallow clone is
  refused, and a second that the two doors give the IDENTICAL reason rather than merely both refusing.
  An operator told "no" at one door and something else at the other cannot tell whether they met one
  policy or two.
- *Found by the backlog re-measure while checking a different row.* It was in no backlog row at all.

**LH-131 · The INGEST stream leaks one durable consumer per run — 3,090 bound, ~100/day, and the health surface documents the opposite**
`ingest, service-kit, chart` · **HIGH** · phase 2 (ingest), but the resource it exhausts is the lakehouse's own NATS

- *RE-MEASURED 2026-09-11 — THE LEAK IS REAL AND IS NOT CURRENTLY GROWING.* Read off the live
  JetStream monitoring endpoint: `INGEST` holds **3,090 consumers for 32 messages**, against 5 / 8 / 2
  / 5 on MEDALLION, DLQ, TRAINING and LINEAGE. So the shape is confirmed and it is this stream alone.
  The count is EXACTLY the 3,090 the row was filed with, which refines its "~100/day": the leak accrues
  per RUN, and ingest has not run since. It is a defect that resumes the moment the lane does, not an
  active fire — unlike LH-134, which was measured still climbing at 280/min.

- *MEASURED on the live estate 2026-09-11* via `nats consumer ls INGEST`:
  **3,090 consumers**, named `ingest-<run_id>` (`services/ingest/src/ingest/queue.py:404`). The stream
  is `Retention: WorkQueue, Maximum Age: unlimited, Maximum Consumers: unlimited`, first sequence
  2026-08-11 — so ~100 new consumers a day for a month, and nothing removes them. MESSAGES are fine (32
  live; WorkQueue deletes on ack). It is the CONSUMER state that grows without bound, replicated across
  3 NATS replicas.
- *THE HEALTH SURFACE ASSERTS THE OPPOSITE, which is why nobody has seen it.*
  `services/ingest/src/ingest/queue_health.py:55` documents the `consumers` field as: "Zero is the
  normal IDLE state, not a fault: the drain creates one durable per run (`ingest-<run_id>`) and it goes
  away with the run, so between runs there is nothing bound." Measured, 3,090 are bound between runs.
  An operator reading that number against that sentence concludes 3,090 runs are in flight.
- *Blast radius is NOT confined to ingest.* This is the same NATS the lineage feed, the control-plane
  broadcast and the medallion cascade ride on. Unbounded consumer state on a shared JetStream is a
  condition-5 (resilient) problem for the lakehouse whoever owns the fix.
- *Closes when:* the per-run durable is deleted when its run ends (or the drain uses an EPHEMERAL
  consumer, which is what a per-run subscription actually wants — it has no cross-restart state to
  keep), the 3,090 existing ones are reaped, and `queue_health`'s docstring is rewritten to say what
  the field means. A bound on `Maximum Consumers` would turn the silent leak into a loud refusal and is
  worth considering alongside.
- *Found while re-measuring LH-127*, which is about a different consumer on a different stream.

**LH-129 · The Ray job reads `S3_KEY`/`S3_SECRET` from process env while the work order's `RASK_CREDENTIAL_REF` seam is consumed by nobody**
`medallion, ray-kit, chart, service-kit` · **HIGH** · phase 2 (compute), but it is the standing SECRETS rule

- *Measured 2026-09-11 on the running estate.* `scripts/ray_stage_job.py:86-88` reads
  `os.environ["S3_KEY"]` / `os.environ["S3_SECRET"]`, and the Ray head takes them via `secretKeyRef`
  env — a k8s Secret arriving as process env, the delivery the owner's rule forbids outright ("not
  process env, not a k8s Secret via `envFrom`"). Five more arrive the same way
  (`LINEAGE_SERVICE_TOKEN` + four `RASK_LINEAGE_TOKEN_SERVICE_*`).
- *The STS seam already exists and is dead.* `work_order.to_env` emits `RASK_CREDENTIAL_REF`
  (`packages/service-kit/src/service_kit/lakehouse/work_order.py:136`) precisely so no credential
  VALUE rides the submission — and **no consumer reads it**. Measured across `scripts/`, `services/`,
  `packages/`, `runners/`: `RASK_CREDENTIAL_REF` 0 consumers.
- *And it is a class, not one field.* Six work-order env vars have zero consumers —
  `RASK_TASK`, `RASK_MERGE_KEY`, `RASK_WRITE_MODE`, `RASK_IDEMPOTENCY_KEY`, `RASK_CODE_VERSION`,
  `RASK_CREDENTIAL_REF`. `RASK_WRITE_MODE` is the sharpest: every live submission ships
  `RASK_WRITE_MODE=merge_insert` (read off the Ray job list 2026-09-10) and the job never consults
  it, so the submitter's declared write semantics have no effect on what the job does. The chart has
  `test_no_dead_chart_env_vars` for exactly this failure; the work order has no equivalent.
  **RE-MEASURED 2026-09-11 — THE CORE DEFECT STANDS AND THIS IS THE ONE ROW WHOSE REMEDY WORKS.**
  Confirmed: `ray_stage_job.py:86-88`, `ray_train_job.py:64-65` and `ray_lance_job.py:45-46` all read
  `S3_KEY`/`S3_SECRET` from process env — the delivery the owner's secrets rule forbids outright — and
  six work-order env vars still have zero consumers. The STS half is feasible with what exists: the
  catalog runs `LANCE_VENDING_MODE=sts` and a read-tier vend returns a 900 s prefix-scoped credential.
  One live blocker first: the head is a HAND-APPLIED Deployment from `deploy/ray-lance-demo.yaml`
  (`kubectl get rayservice,raycluster` is empty and the chart renders 0 RayServices), so a chart-only
  fix cannot reach it. **Phase 2 — do not work ahead of the lakehouse.**
- *Closes when:* the Ray job vends its storage credential (the catalog's STS door, the same one
  `POST /v1/outbox/credentials` was added to) keyed on `RASK_CREDENTIAL_REF`, and `S3_KEY`/`S3_SECRET`
  leave the pod env; plus a gate over `work_order.to_env` asserting every emitted name has a consumer,
  so a dead field fails a test rather than shipping a contract nobody honours.
- *Not closed by* `cc75585d`, which fixed a DIFFERENT half — the published credential was the RustFS
  ROOT secret; it is now scoped and proven bounded. That made the env-borne credential correct in
  SCOPE while leaving it wrong in DELIVERY.

**LH-101 · The sweep has no per-tick budget and no rotated bucket order, so at estate scale the tail is maintained only if the tick has time left**
`maintenance` · med

- *Why open:* Nothing records which buckets a tick actually reached, so silent starvation of the last buckets is undetectable.
- *Closes when:* Give the sweep an explicit per-tick time budget and a rotated bucket order, and report which buckets a tick covered.

**LH-102 · Storage reclamation has never been run live — trash purge must go first, and it is gated on a clean drift report**
`maintenance` · med · **blocked:** a clean, complete drift report (the zero-tuple detector plus the `incomplete` rows)

- *Why open:* The order is load-bearing: a bounded delete of a RECORDED path (trash purge) beats prefix subtraction, and the reclaiming sweep may not run until the drift report is clean — which the missing zero-tuple detector and the 61 `incomplete` rows prevent.
- *Closes when:* Drive `purge_expired_trash` then the reclaiming sweep against a live estate once the drift report is clean, and record the bytes actually reclaimed.

**LH-103 · ~~The maintenance sweep has never been run against a real S3 — its e2e is env-gated and skips~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* The maintenance sweep + orphan scan IS driven against a real S3 in the harness, and a skip against a live stack is already a hard failure.
- *Evidence:* scripts/e2e_stack.sh:253 exports LANCE_E2E_S3_ENDPOINT=http://localhost:9900 (the live RustFS); scripts/e2e_stack.sh:284 puts tests/e2e-py/test_maintenance_s3_e2e.py in the pytest invocation of step 8/8 against that stack; scripts/e2e_stack.sh:294-299 turns ANY non-zero skip count in that run into `exit 1` ("FAIL: e2e suites SKIPPED against a LIVE stack"). Makefile:876-877 `e2e-ci` shells to that script, and .github/workflows/ci.yml:447+/:479 runs `make e2e-ci` as the `e2e-stack` job. The suite itself creates four uuid-suffixed buckets and sweeps only those (tests/e2e-py/test_maintenance_s3_e2e.py:34-40,145-165), so it is safe against the live store. Commit 52697717 landed it as "run LIVE (#80)".
- *What would reopen it:* If test_maintenance_s3_e2e.py were absent from the scripts/e2e_stack.sh:279-286 pytest argument list, or if the skip-gate at :294 ran before that invocation (or excluded that suite), the row would still stand.

**LH-104 · ~~No index is ever built on a governed table — search tunes `nprobes` for one that is not there and `maintenance.indexAckWait` is a 3600s placeholder~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* Indexes ARE built on governed tables — by the medallion cascade, by ingest, and by the catalog's own doors under live e2e; what is genuinely unexercised is only the QUEUED lane, which ships disabled.
- *Evidence:* services/medallion/src/medallion/services/compute.py:265-278 rebuilds the JSON scalar index over `lineage->run_id` after every distributed stage write; services/ingest/src/ingest/lander.py:244-250 creates BITMAP `partition_key` + BTREE `id` on every committed bronze dataset; services/catalog/src/catalog/api/v1/endpoints/indices.py:68-129 builds in-process (the `_queue_build` branch is skipped when no topic is set) and tests/e2e-py/test_track_a_acceptance.py:743-771 drives BOTH `create_scalar_index` and `create_index` against a governed table on the deployed catalog (measured live 2026-08-31). The queued J7 lane is OFF by default: chart/values.yaml:1585 `indexTopic: ""` and services/catalog/src/catalog/core/config.py:348 `maintenance_index_topic` default "", set in no values file including chart/values-prod.yaml. chart/values.yaml:1593 `indexAckWait: 3600s` is still a guess (its own comment at :1588-1592 reasons about it, cites no measurement). services/search/src/search/services/constants.py:13-25 still tunes VECTOR_NPROBES=20 / MAX=0.
- *What would reopen it:* If `_index_lineage` at medallion/compute.py:265 were unreachable (no caller), and lander.py:244 were dead, and the track_a e2e index legs were skipped on every run, the headline claim would hold. The narrower true residual: nothing has ever driven `maintenance_index_topic` end to end in-cluster, so indexAckWait remains unmeasured — that smaller row is real.

**LH-105 · There is no reindex-from-scratch operation in `services/maintenance`**
`maintenance` · med

- *Why open:* Carried as a one-phrase row, so a corrupt or mis-parameterised index can only be repaired by hand.
- *Closes when:* Add a drop-and-rebuild-index operation to `services/maintenance` with a door, a task record and a test.

**LH-106 · Resilience rows exist only inline in `RESILIENCE.md`: the chaos harness was never automated, DLQ sidecar parking was never driven live, and lineage scale-0 restart-replay was never re-verified**
`lineage, chart` · med

- *Why open:* The pull-a-service chaos rows were driven by hand and never encoded (deliberately out of default `make e2e` — they scale shared infra). Gap #2's poison-inject → Dapr `deadLetterTopic` parking has only unit tests (the #83 DLQ drive exercised the OUTBOX surface, not sidecar parking) and the runbook §6.5 it pointed at no longer exists. Honesty-note row 1 (lineage scale-0 → restart-replay under the per-app queue-group components) still awaits its one-shot re-verify on a fresh deploy.
- *Closes when:* Encode the chaos rows as an automated mutating harness kept out of default `make e2e`; drive a poison message live to observe Dapr `deadLetterTopic` parking; re-verify lineage scale-0 → restart-replay on a fresh deploy; and rewrite the dangling §6.5 runbook pointer.

**LH-107 · The catalog is correct only at `replicas=1` because `controlEmit`'s ring buffer and cursor are per-replica, and every stage runner calls it**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The per-replica buffer and cursor are exactly as described, but the only thing that degrades above replicas=1 is the /v1/events poll cursor for the admin console — nothing a stage runner calls; the honest minimum fix is session affinity, not a shared buffer.
  **Evidence:** services/catalog/src/catalog/core/control_buffer.py:20-44 is a per-process `deque` with a per-process monotonic `_cursor`; its own module docstring (:1-11) states it is per-replica. chart/values.yaml:1025-1029 records the boundary verbatim and adds the severity the row omits: a load-balanced poll "degrades to noisy `reset`s (safe, since events are hints, but wasteful)". `since()` (control_buffer.py:46-63) answers `reset=True` on an out-of-window cursor and the client re-reads authoritative state, so the failure mode is wasteful, not incorrect. No Service in chart/templates/ carries `sessionAffinity` (the only hits are vendored CNPG CRDs). The only consumers of `/v1/events` are the console and its gate (services/catalog/src/catalog/api/v1/endpoints/events.py:38, me.py:17) — no stage runner polls it; stage runners hit describe/create/vend, which hold no per-replica state. The blocker still holds: chart/values.yaml:1174 and chart/values-prod.yaml:28 both pin `stageRunnerReplicas: 1`.
  **Reopen if:** If a stage runner or the cascade read `/v1/events` (rather than the broadcast Dapr subscription in services/catalog/src/catalog/api/control_relay.py), or if `since()` could return wrong events rather than `reset=True`, the row's full 'shared buffer' ask would be warranted. The smaller true fix: add `sessionAffinity: ClientIP` to the catalog Service before raising `services.catalog.replicas`.
`catalog, medallion, chart` · med · **blocked:** the decision to raise `medallion.stageRunnerReplicas` above 1

- *Why open:* `values.yaml:941` records that both the ring buffer and its cursor are per-replica, so scaling the catalog past one needs session affinity or a shared buffer — while the workflow hosts DO scale (placement spreads instances, `queueGroupName` makes replicas competing consumers). The one component every stage calls for describe/create/vend is the one that cannot scale out. Stage runners are pinned at 1 replica by owner ruling, making it a recorded ceiling whose symptom would be catalog latency naming nothing.
- *Closes when:* Before `medallion.stageRunnerReplicas` is raised above 1: give `controlEmit` a shared buffer and cursor (or put session affinity on the catalog Service) so `services.catalog.replicas` can exceed 1.

**LH-108 · The lineage + OpenFGA store is still the hand-rolled `rask-age` StatefulSet; the CNPG cutover is built but off**
`lineage, chart` · med · **blocked:** owner decision (StatefulSet vs CNPG ImageVolume extension) + a K8s 1.33 / CNPG 1.27 cluster

- *Why open:* Listed as open decision #1 and unresolved in the tree: `age.cnpgCluster.enabled` defaults false, the CNPG operator runs with nothing to reconcile, and the cutover needs the built extension image (`.docker/cnpg-age-ext.dockerfile`), K8s 1.33+ and CNPG >= 1.27. The two paths are mutually exclusive and `age-cluster.yaml` fails the render if both are on, so this is a one-way decision nobody has taken.
- *Closes when:* Decide StatefulSet-vs-CNPG for the lineage/OpenFGA Postgres; if CNPG, build and publish `.docker/cnpg-age-ext.dockerfile`, flip `age.cnpgCluster.enabled` in `chart/values-prod.yaml`, and migrate the `lineage` + `openfga` databases.

**LH-109 · Eleven stable live-suite failures are still unclassified after the `/runs?limit=1000` drift repair**
`medallion, maintenance, catalog, lineage` · med

- *Why open:* Driven twice against the deployed estate 2026-09-10: 13 failed / 117 passed / 3 skipped / 1 xfailed. One class was repaired (five call sites still sent `limit=1000` after the board was capped at 200; 422 count 6 → 0). The stable eleven — `governed_union` x4, `maintenance_e2e`, `maintenance_s3` x2, `medallion_e2e`, `media_e2e`, `outbox_e2e` — are all in scope and none carries a verdict; two further legs flip between consecutive runs.
- *Closes when:* Give each of the eleven a verdict — SUITE-DRIFT / ESTATE-DEFECT / CONTAMINATION / ALREADY-FIXED — starting with the two whose assertions carry the storage-residue findings (`gold$catalog`, `uiproof-gold$catalog` on buckets that do not exist), and run the two flaky legs repeatedly rather than once.

**LH-110 · No documented, exercised restore of the control root (projects, warehouses, bindings, trash)**
`catalog, chart` · med

- *Why open:* There is a chart snapshot for RustFS and Postgres and nothing that has ever been restored from it, so the control root's recoverability is untested.
- *Closes when:* Write and actually exercise a restore of projects, warehouses, bindings and trash from the snapshot, and document it.

**LH-111 · ~~No in-flight blob-byte admission budget — the catalog counts requests, not bytes~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* The blob door already streams in bounded 8 MiB windows and never buffers a payload, so 'a large blob request is admitted on the same terms as a small one' does not describe the memory cost — the exposure is a concurrency COUNT, not bytes.
- *Evidence:* services/catalog/src/catalog/services/blob_serving.py:38-41 sets `_BLOB_CHUNK_BYTES = 8 * 1024 * 1024`; :77-87 `chunks()` yields the window one bounded `read_range` at a time, and the module docstring (:15) states "a multi-GB blob is served through bounded `read_range` windows". The door itself (services/catalog/src/catalog/api/v1/endpoints/data.py:509-571) returns a StreamingResponse over those chunks — data.py:527-531 says the read-side mirrors the write-side OOM guard. So a 4 GB blob and a 4 KB blob cost the same resident bytes. What the row's fix targets does not exist: no byte budget anywhere (grep byte_budget/max_inflight_bytes/blob_budget across services/, packages/, chart/ returns nothing), and services/catalog/src/catalog/api/load_shed.py:33-51 gates only bulk Arrow-IPC POST writes on a request COUNT, not blob GETs.
- *What would reopen it:* If `chunks()` read the whole window in one call, or if `read_blob` materialised the payload before returning, the row's mechanism would be real. The narrower true residual: blob GETs are not counted by load_shed at all, so N concurrent readers cost N x 8 MiB — a concurrency cap on the blob door (mirroring load_shed's 429 + Retry-After), not a byte budget answering 503.

**LH-112 · Unknown whether Lance honours a tag that pins a BRANCH version during main cleanup**
`maintenance, catalog` · med

- *Why open:* Unmeasured, and it becomes load-bearing as soon as the sweep maintains branches.
- *Closes when:* A test on `tree/<branch>/` with a root tag, run through main cleanup.

**LH-113 · One 340-line catalog `Settings` class carries every domain's configuration**
`catalog` · med

- *Why open:* Listed OPEN in the Q3 carry-over table and neither re-measured nor struck.
- *Closes when:* Split the catalog `Settings` into per-domain settings blocks, the shape the eight services already share via `GovernedAuthSettings`.

**LH-114 · ~~Multi-base (`base_paths`) is implemented but never exercised, and no test proves cleanup on a shared non-root base spares its sibling~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* All three asks are met: a multi-base fixture is provisioned and driven live, the orphan scan is tested over a real shallow clone, and a REAL sweep tick is asserted to spare the sibling.
- *Evidence:* Multi-base fixture, live: scripts/e2e_stack.sh:27-28 defines `BASE_A=s3://mb-a` / `BASE_B=s3://mb-b`, :109 installs the chart with `catalog.multibase.dataBases=[mb-a, mb-b]`, :189 provisions both buckets, :266 exports them, :281 runs tests/e2e-py/test_multibase_e2e.py — under the no-silent-skips gate at :294-299. Orphan scan over a multi-base dataset: tests/unit/test_orphan_files.py:336-352 `test_a_shallow_clone_is_refused_because_its_data_lives_elsewhere` builds it with the real `ds.shallow_clone(...)` API and asserts `checked is False`, `structural is True`, reason names `base_paths`. Shared-base cleanup sparing the sibling: tests/unit/test_base_refs_guard.py:205-224 `test_a_REAL_SWEEP_TICK_refuses_the_source_of_a_live_clone` drives the real `run_sweep` (via _sweep_results at :177-202, stubbing only the S3 fs and discovery) and asserts the SOURCE comes back refused with "resolves its files through"; :137-176 proves in a COLD subprocess that without the guard the clone breaks; :227-244 pins that an ordinary dataset is still swept, so the guard is not a blanket refusal.
- *What would reopen it:* If test_base_refs_guard.py's sweep test stubbed `protected_roots` itself (it does not — that is the stated point of the file, :208-212), or if the multibase e2e were absent from the e2e_stack pytest list, the row would still stand.

**LH-115 · Manifest flags 16/64 are refused only by the orphan pass, not by the rest of the maintenance surface**
`maintenance` · low

- *Why open:* The refusal knowledge lives in `maintenance/services/orphans.py` and `test_orphan_files.py`, and no other operation consults it.
- *Closes when:* Apply the 16/64 flag refusal to the other maintenance operations (compaction, reclamation, the sweep), sharing one predicate with `orphans.py`.

**LH-116 · ~~Fragment sizing is left at Lance defaults with no per-table lever~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* Fragment sizing is not at Lance's default and has a lever at three scopes — a per-tier default with the sizing rationale written where it is chosen, a per-table/namespace/project policy record that overrides it, and a per-request override on the compaction door — and the sweep and the off-pod rewrite both carry it.
- *Evidence:* PER-TIER DEFAULTS + THE RATIONALE, where the default is chosen: /home/blackwell/Desktop/rask/services/maintenance/src/maintenance/services/tiers.py:1-37 (the module docstring's two-forces argument and the bronze/silver/gold rows-x-row-size-x-fragment table), :47-58 `BRONZE_TARGET_ROWS=512` / `SILVER_TARGET_ROWS=262_144` / `GOLD_TARGET_ROWS=524_288`, :171-181 `target_rows_for()` returning None when the tier cannot be read. PER-TABLE LEVER: /home/blackwell/Desktop/rask/services/catalog/src/catalog/schemas.py:455-461 `MaintenancePolicy.target_rows_per_fragment` (with its sizing rationale in the comment above it), stored/resolved by /home/blackwell/Desktop/rask/packages/service-kit/src/service_kit/lakehouse/maintenance_policies.py:133-196 where `kind == "table"` is an exact match that wins over namespace and project records. APPLIED BY THE SWEEP: /home/blackwell/Desktop/rask/services/maintenance/src/maintenance/services/sweep.py:396 (tier default into the plan), :420-421 (policy override), :554 (into `compact_one`), and :496-497 for the off-pod distributed rewrite. PER-REQUEST OVERRIDE ON THE DOOR: /home/blackwell/Desktop/rask/services/catalog/src/catalog/schemas.py:403-406 `CompactRequest.target_rows_per_fragment`, wired at /home/blackwell/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/maintenance.py:133 and :157, and honoured at /home/blackwell/Desktop/rask/services/catalog/src/catalog/services/maintenance.py:311-322. CONSUMED: /home/blackwell/Desktop/rask/services/maintenance/src/maintenance/services/optimize.py:251, :271-272, :525, :670. Driven by tests at /home/blackwell/Desktop/rask/services/catalog/tests/test_the_compact_button_enqueues.py:110-123 and /home/blackwell/Desktop/rask/services/maintenance/tests/test_compaction_runs_off_the_pod.py:96,146.
- *What would reopen it:* Show that a table cannot get its own `target_rows_per_fragment` — e.g. that `resolve_policy` never reaches the `kind == "table"` branch for a real dataset URI, or that `compact_one` drops the value before `compact_files`. Or show that the sizing rationale is absent from every site where a default is chosen (it is at tiers.py:1-37 and schemas.py:455-461).

**LH-117 · ~~The orphan scan has no chart toggle — `MAINTENANCE_ORPHAN_SCAN_ENABLED` is env-only~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* `MAINTENANCE_ORPHAN_SCAN_ENABLED` has been a chart value since 2026-08-15 — `maintenance.orphanScan`, defaulted true in values.yaml and rendered into both the maintenance deployment and the maintenance worker.
- *Evidence:* CHART VALUE: /home/blackwell/Desktop/rask/chart/values.yaml:1693 `orphanScan: true`, with the 25-line rationale block at :1668-1692 recording exactly the gap this row names ('the chart rendered no env var for it, so the scan could not be turned on in a deployed estate by any means'). WIRED THROUGH BOTH TEMPLATES: /home/blackwell/Desktop/rask/chart/templates/maintenance.yaml:208 `- { name: MAINTENANCE_ORPHAN_SCAN_ENABLED, value: {{ hasKey .Values.maintenance "orphanScan" | ternary .Values.maintenance.orphanScan false | quote }} }` and /home/blackwell/Desktop/rask/chart/templates/maintenance-worker.yaml:166 (identical). CODE DEFAULT UNCHANGED (still off absent the chart): /home/blackwell/Desktop/rask/services/maintenance/src/maintenance/core/config.py:299 `orphan_scan_enabled: bool = Field(default=False, alias="MAINTENANCE_ORPHAN_SCAN_ENABLED")`. Landed 2026-08-15, commit 81af086f 'fix(maintenance): #128a + #128d + #114 — the reclaimer could certify an estate it never scanned'.
- *What would reopen it:* `helm template` the chart with `maintenance.orphanScan` set and find the env var absent from the rendered maintenance pod spec, or find a third maintenance workload that runs the reconciler and gets no `MAINTENANCE_ORPHAN_SCAN_ENABLED` row.

**LH-118 · An unparseable `lance-catalog/_projects/_probe.json` that nothing writes keeps `registry:projects` in the reconcile report's `incomplete: 61`**
`maintenance` · low · **blocked:** owner authorisation to delete an object from the live control root

- *Why open:* Measured live on pylance 11: char-0 JSONDecodeError, and nothing in the tree writes the file, so it is estate residue. `_list_json_records` reporting what it cannot parse is correct — the file is the defect. `incomplete` blocks `report_is_clean` and therefore the purge, but is not the sole blocker (13 real drift findings remain). Not acted on because deleting an object from the owner's live control root is not a change to make unasked.
- *Closes when:* Owner authorises deleting `s3://lance-catalog/_projects/_probe.json` from the live control root; the other 60 `incomplete` rows are `depth limit reached` on the discovery walk, already recorded at `optimize.py:115`.

**LH-119 · The dropped-parameter sweep's coverage is unassessed — 16 verify calls and the completeness critic failed on the weekly subagent limit**
`catalog, medallion, ingest` · low

- *Why open:* 22 candidates produced 53 verdicts (40 real) but the critic never ran, so the remaining rows are candidates, not a finished list. The stated reset (2026-09-04 06:00) has passed.
- *Closes when:* Re-run the sweep's completeness critic and the 16 failed verify calls, then re-state the remaining rows as measured rather than candidate.

**LH-120 · Both medallion entrypoints read settings at import time — `producer.py:170`, `producer.py:202` and `stage_runner.py:46`**
`medallion` · low

- *Why open:* Re-measured at HEAD 2026-09-09: the LOGGING half is false (neither entrypoint configures logging at module level), but the SETTINGS half stands at three sites, which is the half that matters — a config error fails at import rather than in the lifespan.
- *Closes when:* Move the `get_settings()` calls at `producer.py:170`/`:202` and the `_settings = get_settings()` bind at `stage_runner.py:46` into the lifespan/factory.


---

## PHASE 1 · CROSS-CUTTING — service-kit, storage, chart, build, tests

Shared machinery. The first group is what blocks the lakehouse and should be read as part of phase 1.

### Serves the lakehouse (phase 1)

_These cross-cutting rows sit directly under the catalog, lineage and the medallion cascade — secrets that never reach the pods that hold them, plaintext store hops, the authz model and the audit trail every governed commit depends on; left open, phase-1 work runs on an estate whose credentials silently go stale and whose audit record can be deleted unauthenticated._

**XC-001 · No `checksum/secret` pod-template annotation anywhere, so a rotated Secret is never re-read — six of seven zones polled lineage with a dead token and 46% of its traffic was 401**
`chart, lineage, frontend-zones` · **HIGH**

- *Why open:* Measured live: `rask-web-lakehouse` polled `GET /events` with a `LINEAGE_SERVICE_TOKEN` (hash `1b55ba766c3e962c`) matching no key in `rask-infra-credentials`, and 2,627 requests — 46% of all lineage traffic — were refused 401 while the zone rendered the 401 as an empty feed. A restart fixed that instance; nothing prevents the next rotation, because the Deployment reference and the render were both correct and only the running pod was wrong.
- *Closes when:* Add a `checksum/secret` pod-template annotation (sha256sum of the rendered Secret) beside every `secretKeyRef` env in `chart/templates/`, and pin it with a rendered-manifest test that fails when a template adds a `secretKeyRef` without the annotation.

**XC-002 · The chart fix moving the Ray head's S3 credential onto the sanctioned ESO path is committed and never deployed**
`chart, medallion, storage` · **HIGH** · **blocked:** owner go-ahead for a live credential rotation

- *Why open:* Live, `rask-ray-compute-s3` is a hand-applied Secret on none of the three sanctioned paths, and `rask-infra-credentials`' `ray-compute-access-key`/`ray-compute-secret-key` decode to `rustfsadmin` because `values.yaml` ships `rayComputeAccessKey: ""` and `infra-credentials.yaml:36` falls back to `rustfs.accessKey`. Both halves are in the chart and render green (369 chart/secret invariants), but rolling them out rotates a live credential and either half alone is `SignatureDoesNotMatch` on every cascade call.
- *Closes when:* Run `helm upgrade` first (rotates the RustFS user and updates the ESO target), then re-apply `deploy/ray-lance-demo.yaml` preserving the head's current image tag — re-applying that manifest has reverted the head to `ray-lance:dev` and broken the cascade before — and verify a cascade tick.

**XC-003 · `lance.audit` shares `opentelemetry_logs` with all telemetry: 14-day TTL, and an unauthenticated in-cluster `DELETE` on the audit stream is accepted**
`catalog, lineage, medallion` · **HIGH**

- *Why open:* Confirmed against the live store 2026-09-07: audit lands in GreptimeDB `opentelemetry_logs` (477,096 rows) beside every other signal, that table declares `ttl = '14days'`, a `DELETE` on the audit stream is accepted, and both queries reached `:4000/v1/sql` with no credentials from inside the cluster. Sharing one table means the catalog's authn/authz/credential-issuance records can carry neither their own retention nor their own access policy. (The correlation half of this row was refuted — `CorrelationFilter` stamps request_id/trace_id on the root handler.)
- *Closes when:* Split `lance.audit` out of the shared `opentelemetry_logs` pipeline in `chart/templates/otel-collector.yaml` into its own append-only sink with non-14-day retention, and put authentication in front of GreptimeDB's `:4000/v1/sql` so a DELETE is not accepted from any in-cluster pod.

**XC-004 · 43 secret refs still arrive through env (APP_API_TOKEN ×10, the zones' OIDC/session/lineage tokens, `ray-lance-head`) while ESO is built, provisioned and switched off**
`chart, viewer, lineage, catalog` · **HIGH** · **blocked:** owner decision — a `helm upgrade` release with `externalSecrets.enabled=true`; the estate carries seven hand-deployed images a values-mismatched upgrade would revert to chart defaults

- *Why open:* Re-measured 2026-09-08: the 43 refs sort into ~34 ESO / ~5 STS / ~4 Dapr-store, the ESO auth half (bao kubernetes backend + bound `lance-infra` role) landed and the operator runs with 3 pods — but `externalSecrets.enabled` is still false and zero ExternalSecret/SecretStore/ClusterSecretStore objects exist, so nothing is migrated. Separately `MEDIA_S3_ACCESS_KEY_ID` on `rask-viewer` is the RustFS ROOT pair (`rask-app`/`AWS_ACCESS_KEY_ID`) wearing a scoped name, findable only by following the secretKeyRef.
- *Closes when:* Set `externalSecrets.enabled=true` in `chart/values.yaml`, add ExternalSecret entries in `chart/templates/external-secrets.yaml` for the ten `APP_API_TOKEN` refs plus the seven zones' `OIDC_CLIENT_SECRET`/`SESSION_SECRET`/`LINEAGE_SERVICE_TOKEN` and `ray-lance-head`, and deploy via `make k3s-up` rather than `kubectl set image`; separately replace `rask-viewer`'s `MEDIA_S3_ACCESS_KEY_ID` with a catalog-vended STS credential (`catalog.core.vending.build_session_policy`). Check `apply_dapr_secrets` does not already overwrite a ref at boot before migrating it.

**XC-005 · The OpenBao seed Job and the three ExternalSecrets carry no `helm.sh/hook`, so adding one property to an ExternalSecret destroys the whole Secret for ~12 minutes**
`chart` · **HIGH** · **blocked:** owner decision: turn today's silent 12-minute Secret outage into a loudly aborted release

- *Why open:* `chart/templates/openbao.yaml`'s seed Job and `chart/templates/external-secrets.yaml` apply in arbitrary order in one `helm upgrade`, and ESO syncs a Secret whole-or-not-at-all under `creationPolicy: Owner`. Measured on revision 116: ExternalSecret created 06:51:38Z, Secret reappeared 07:03:14Z — 696 s with five web zones in CreateContainerConfigError. It bites hardest exactly where the zero-trust work is heading, since each scoped identity changes an ExternalSecret's data list.
- *Closes when:* Extract the seed into a named template and add a `helm.sh/hook: pre-upgrade` copy (not pre-install — on a first install OpenBao does not exist and the wait would hang) with a bounded `activeDeadlineSeconds`, leaving install-time behaviour unchanged.

**XC-006 · No auto-unseal for OpenBao — every pod restart or node drain leaves it sealed and the whole fleet hangs at 'waiting for application startup'**
`chart, catalog, lineage, medallion, notifications` · **HIGH** · **blocked:** owner decision between ESO, bank-vaults and vault-operator

- *Why open:* `chart/values.yaml:2705` states that devMode=false requires an operator to `bao operator init` and unseal by hand, and neither values file carries auto-unseal wiring (`grep -i unseal chart/values-prod.yaml` finds only prose). Apps consume secrets fail-closed through Dapr at startup, so a routine node drain or OOM deadlocks catalog, lineage and medallion together.
- *Closes when:* Adopt the recorded destination (external-secrets / bank-vaults / vault-operator auto-unseal) in `chart/templates/openbao.yaml` + `chart/values-prod.yaml`, and add a seal-status alert rule to `chart/alerting/rules.yml`.

**XC-007 · Every store the fleet dials is plaintext — 171 `http://` store URLs on the live Deployments, RustFS S3 + STS, OpenFGA, the AGE DSN, OpenBao, the NATS monitor and OTLP**
`catalog, lineage, maintenance, medallion, chart` · **HIGH** · **blocked:** owner decision on introducing a certificate source; the AGE half comes free with the CNPG cutover (next row)

- *Why open:* Counted off the live Deployments 2026-09-07 and re-confirmed by reading the schemes the running pods dial: 171 `http://` store URLs, 9 `https://` (all external), one `postgresql://` with no `sslmode`; `LANCE_`/`LINEAGE_`/`MAINTENANCE_`/`MEDALLION_`/`MEDIA_S3_ENDPOINT` and the STS endpoint plaintext, `RASK_FGA_API_URL` plaintext with no credentials at all (`fga.py:295-350` builds ClientConfiguration with none), OpenBao `http://` + `skipVerify: true`, Dex `http://`, Postgres `sslmode=disable`. Dapr Sentry mTLS covers only sidecar-to-sidecar hops, every store sits outside it, and no rask test covers TLS on store hops (re-measured true 2026-09-10).
- *Closes when:* Introduce a certificate source (this estate has neither cert-manager nor a chart-generated certificate), then flip each rendered scheme: `chart/templates/_helpers.tpl:654` (RustFS https, `ALLOW_HTTP=false`), `:704` (`tls://` NATS), `:1076` (OpenFGA https + preshared key/OIDC), `chart/templates/services.yaml:101-102` and `:302` (lineage DSN sslmode), `infra-credentials.yaml:43` / `external-secrets.yaml:53` / `openbao.yaml:171` (`sslmode=disable` → `verify-full`), `openbao.yaml:34` + `dapr-component.yaml:283,303` (real certs, skipVerify false), `values.yaml:2125` (https Dex); then add the missing TLS-on-store-hops test.

**XC-008 · `rask-age` serves TLS-off Postgres for the lineage graph and OpenFGA, and the fix — the AGE→CNPG cutover — is built and unblocked but never taken**
`lineage, catalog, chart` · **HIGH** · **blocked:** owner decision — it is a data migration of the lineage graph and OpenFGA's tables, not a values toggle

- *Why open:* `SHOW ssl` on the running `rask-age-0` answers **off**, so the server offers no TLS at all and putting `sslmode=require` on the client would be an outage — a server change before it is a connection-string one. All four stated blockers are now met (K8s v1.36.2, CNPG operator 1.29.1 leader-elected and logging hourly `pki: Periodic TLS certificates maintenance`, containerd 2.3.2-k3s2, and `age-cnpg-ext:1.7.0-18` built and verified from the registry manifest, 1,497,689 B of `/lib/age.so`), but the graph and OpenFGA's tables live in the StatefulSet's PVC.
- *Closes when:* Set `age.cnpgCluster.enabled: true` (`chart/templates/age-cluster.yaml` fails the render if both stores are on), move the lineage AGE graph and OpenFGA tables off the `rask-age` StatefulSet PVC into the CNPG Cluster and retire the StatefulSet, proving the round-trip with `scripts/age_restore_drill.sh`; this also delivers server TLS plus physical backups and PITR.

**XC-009 · No Dapr `accessControl` policy in any chart template, and the actor + workflow invocation planes were never characterised**
`medallion, notifications, gateway, annotator, ingest, chart` · med

- *Why open:* `accessControl`/`defaultAction` appear in zero chart templates (re-measured true 2026-09-10), so any of the 16 sidecars may invoke any app-id, and `networkPolicy.enabled` is still false (`values.yaml:559`). The row's original 11-entry plan was sized from the HTTP `/v1.0/invoke` callers grep can see, but the estate invokes over three planes — HTTP (gateway, notifications), ActorProxy (annotator, notifications; 28 references), and Dapr Workflow (flows, ingest, medallion) which Dapr documents as EXCLUDED from service-invocation access control and needing a `WorkflowAccessPolicy` this estate has never heard of — so a `defaultAction: deny` written from the HTTP count alone would ship as an outage.
- *Closes when:* Characterise actor-to-actor and workflow invocation on the live estate first (no document answers it), then write `policies:` (not `appPolicies:`) plus `defaultAction: deny` and `trustDomain` into the one shared `lance-tracing` Configuration (`chart/templates/observability.yaml:50-56`), add a `WorkflowAccessPolicy`, validate with a live drive of every gateway route rather than a render, and add the missing test. NetworkPolicy is a separate prod-hardening half — it is a no-op on k3s flannel and needs a policy-enforcing CNI.

**XC-010 · OpenFGA subjects are raw-interpolated (`f"user:{user}"` in `service_kit/governed/fga.py`) — user IDs are never URL-encoded, and OIDC subjects here are emails**
`service-kit, catalog` · med

- *Why open:* The Lakekeeper study ruled this mandatory before prod OIDC if subjects can contain `@`/`+`/`:`, and email subjects always do; the interpolation is still present at `fga.py:487`, `:532`, `:617` and `:1312`. No verdict on it appears in `docs/DECISIONS.md` §9, so it is neither fixed nor knowingly accepted.
- *Closes when:* URL-encode the subject when serializing to OpenFGA in `packages/service-kit/src/service_kit/governed/fga.py` (all four interpolation sites), with a test covering `@`, `+` and `:` in the subject.

**XC-011 · Estate bootstrap is still check-then-write, and `fga.provision()` rewrites the authorization model on every unpinned boot**
`chart, service-kit, catalog` · med · **blocked:** owner decision (C-Q2) — whether `provision()` gates on a model-content hash or `RASK_FGA_MODEL_ID` is the accepted pin

- *Why open:* Verified 2026-09-10: `chart/templates/bootstrap-admin.yaml` contains no `bootstrap.json` / `records.create_json`, and `packages/service-kit/src/service_kit/governed/fga.py::provision` (line 319) still documents 'The model is (re)written each time'. The latch shape — observe unconditionally, act only when enabled, treat 409 as success — is unimplemented, and the pin it should gate on has never been chosen.
- *Closes when:* In `chart/templates/bootstrap-admin.yaml`, write `_control/bootstrap.json {subject, store_id, model_id, at}` with `records.create_json` (create-iff-absent) after the seed and read it before the seed, treating 409-on-exists as success; then gate `fga.py::provision` (~lines 319-349) on that latch using whichever pin the owner picks.

**XC-012 · The dev cluster runs `auth.enabled=false`, so all seven rendered user doors sit in their dev-open state**
`chart, catalog, lineage, medallion, ingest` · med · **blocked:** owner decision — whether to turn auth on in the dev cluster

- *Why open:* The chart gap is closed — 7 of 7 deployments render a user door keyed on the per-service `governedAuth` flag — but the deployed release never turns auth on, which is why the in-cluster proofs had to arm lineage's door by hand from the chart's own values.
- *Closes when:* Set `auth.enabled=true` on the k3s release and re-verify each of the seven doors answers 401/403 without a bearer.

**XC-013 · pg-dump backups land in the same RustFS bucket the VolumeSnapshot protects, nothing prunes `_backups/` or old snapshots, and `snapshotClassName` is empty**
`chart, lineage` · med · **blocked:** owner decision on the off-cluster backup destination

- *Why open:* `chart/templates/backup-pg.yaml` writes the lineage and openfga dumps to `s3://lance-catalog/_backups/pg/` — the same store a PVC loss would take out — nothing prunes either artefact kind, and `snapshotClassName: ""` fails on clusters with no default class. The restore half of this row is closed (`docs/runbooks/RUNBOOK-restore.md` exists).
- *Closes when:* Point the pg-dump CronJob at an off-cluster bucket in `chart/values-prod.yaml`, add retention pruning for `_backups/pg/` and for old VolumeSnapshots in `chart/templates/backup-snapshot.yaml`, and set a real `snapshotClassName`.

**XC-014 · Bootstrap on a fresh machine is not chart-complete: the Ray head is hand-applied `deploy/ray-lance-demo.yaml` and OpenBao's k8s auth backend/policy/role + KV values are seeded by runbook**
`chart, compute, medallion` · med

- *Why open:* The hand-applied Ray head has diverged from the chart's own RayService, and re-applying an older copy silently reverted the scoped S3 credential to the root key once. Until the head is reconciled and the OpenBao bootstrap is a Job, 'it is all in the chart' is false — and the gap sits exactly where the security posture lives.
- *Closes when:* Reconcile the hand-applied Ray head with the chart's RayService and delete `deploy/ray-lance-demo.yaml`; turn the OpenBao k8s auth backend/policy/role and KV seeding into a chart-owned Job.

**XC-015 · Residual duplicated storage seams: ingest hand-maps its own 409, a dead line in `storage/client.py`, and three boto3 constructors outside `s3_client`**
`storage, ingest, catalog, service-kit` · med

- *Why open:* The conflict-classifier half of both rows is CLOSED in the code — `packages/service-kit/src/service_kit/lancekit/commit_verdict.py` is the shared five-verdict classifier and both `services/catalog/src/catalog/services/dataplane.py:552` and `lancekit/writer.py:76` map it onto their own error type, `INCOMPATIBLE` included, so nothing leaks an `OSError` as a 500 any more (verified 2026-09-10). What is untouched is ingest's hand-mapped 409, the no-op line I5 names at `storage/client.py:102` (line numbers have shifted — that offset now falls inside the `s3_client` docstring, so re-locate before deleting), and the boto3 constructors that bypass `storage.s3_client`.
- *Closes when:* Route `services/ingest`'s 409 handling through `classify_commit_failure`, re-locate and delete the no-op line in `packages/storage/src/storage/client.py`, and collapse the remaining direct boto3 constructors onto `storage.s3_client`.

**XC-016 · OpenFGA's pgxpool defaults (MaxOpenConns 30 / MaxIdleConns 10) are untuned against the AGE Postgres running on the default `max_connections=100`**
`openfga, chart, lineage` · med · **blocked:** the OpenFGA telemetry row (Observability) — there is no FGA metric to measure against today

- *Why open:* The same Postgres carries the lineage AGE graph, the `lance-statestore` Dapr state store and the backup Job, and v1.10.4/v1.11.0 was a breaking switch from `database/sql` to pgxpool — 30 of 100 connections to one consumer needs measuring, and the row is deliberately not set blind.
- *Closes when:* Measure observed connection counts against the AGE Postgres, then set `datastore.maxOpenConns` / `datastore.maxIdleConns` (and evaluate the experimental `datastore_throttling`) from that measurement.

**XC-017 · The zero-trust posture is a claim, not a measured target — 1 control missing and 8 partial with nothing re-deriving them**
`catalog, lineage, maintenance, medallion` · med · **blocked:** owner acknowledgement of R11

- *Why open:* The zero-trust diff scored rask matching or beating Lakekeeper on 9 of 19 §F controls, missing 1 outright and partial on 8; nothing re-measures those rows, so the posture is asserted rather than gated, and §F's controls are catalog/lineage/maintenance governance.
- *Closes when:* Encode the 19 §F controls as a checked list (a test or `make` target) that re-derives matched/partial/missing, then close the 1 missing and 8 partial rows against it.

**XC-018 · `model.fga.yaml` has no `check` case with a userset subject or the self-referential userset — exactly the behaviours `weighted_graph_check` alters**
`service-kit, catalog` · med · **blocked:** the `fga` CLI — GitHub release downloads 403 in the authoring sandbox; `make bootstrap` installs it into `.localbin` on a normal dev box

- *Why open:* All 21 cases / 18 `check` blocks use a concrete `user:<id>` subject while the flag is already ON in chart values, and the catalog's access simulator and admin endpoints pass usersets with `qualify=False` — so a future flag or version flip would be caught by an operator reading a wrong TRUE rather than by CI.
- *Closes when:* Add `check` cases with `role:x#assignee` and `team:eng#member` as the check user plus the self-referential userset case, and `list_users` coverage for the admin-console rungs, then run `fga model test`.

**XC-019 · The Lance writer seam has no create verb, so a fresh estate's first annotation save has nothing to write into**
`service-kit, annotator` · med

- *Why open:* Verified still true: `packages/service-kit/src/service_kit/lancekit/writer.py` exposes only `merge_upsert` / `merge_insert_only` / `delete` (the Protocol at :49-51 and all three implementations), the only `create_table` in the annotator is the publish saga's (`projects/lakehouse.py`), and `annotations/save.py` and `tags.py` still call `reader.table_version()` unguarded. The sibling credential defect filed with it IS fixed — `open_reader`/`open_writer` now take `caller_token`.
- *Closes when:* Add a create-if-absent verb to the `TableWriter` protocol and its three implementations in `lancekit/writer.py`, call it from `annotator/annotations/save.py` before the first merge, and pin it with a RED test that saves into an estate where the annotations table does not exist. Do not widen an except clause instead.

**XC-020 · `transaction.can_set_property` and `transaction.can_cancel` are defined in `model.fga` and referenced by no relation and no string in `services/` or `packages/`**
`service-kit, catalog` · low · **blocked:** the `fga` CLI — not installable in the authoring sandbox

- *Why open:* They are leaf permissions so nothing inherits through them, and `api/fga_deps.py` picks only between `can_describe` and `can_set_status`; the removal cannot ship without `fga model test` green.
- *Closes when:* Run the dependency scan, delete both relations from `packages/service-kit/src/service_kit/governed/auth/model.fga` (or grow `alter_transaction` into the property/cancel actions), run `fga model test`, regenerate `model.json`.

**XC-021 · The OpenFGA subchart is pinned at 0.3.9 while the running image is v1.18.3, which is 0.3.12's appVersion**
`openfga, chart` · low

- *Why open:* Every value key the chart sets was verified present in 0.3.12, but `Chart.lock` carries a digest over the dependency set, so hand-editing the version desyncs it and the `helm dependency build` run by `make k3s-install` and both `scripts/*_e2e_stack.sh` fails outright.
- *Closes when:* Run `helm dependency update ./chart` to move the openfga dependency 0.3.9 → 0.3.12 and commit the regenerated `Chart.lock`.

**XC-022 · NATS HA, a nack operator under GitOps, and a query engine are owner-parked with no ruling**
`chart, nats` · low · **blocked:** owner decision — the park has never been lifted

- *Why open:* Parked by the owner and re-parked by the merge plan's proposed decision 5, which notes rask's JetStream is on but streamless and that lance-ns's stream-job is its first real consumer. Nothing is authorised until the park lifts, and the event plane's correctness is a named lakehouse concern.
- *Closes when:* An owner ruling on whether to run NATS HA plus a nack operator under GitOps and whether a query engine is in scope; until that ruling, no work.

**XC-023 · The Dapr retreat (D5) has not started: 40/480 source files import the SDK, 2,481 LOC of actors, 3,407 LOC of workflows, 36/53 chart templates**
`medallion, notifications, ingest, lineage, service-kit, chart` · low · **blocked:** sequenced after §A–§D

- *Why open:* Nothing in the sequence has started; the replacement map per block lives in `dapr-coupling-analysis.md` and each sweep's §5, and the whole programme is sequenced behind §A–§D. It serves the lakehouse's decoupling from a specific workflow engine.
- *Closes when:* Work the stated order: secrets (OpenBao direct) → pub/sub (JetStream durable consumers behind a `Publisher` protocol replacing `dapr_publish`) → state (JetStream KV with CAS; notifications' actors become KV rows with revision CAS) → bindings (in-process scheduler + KV lease) → invocation (plain HTTP + mTLS) → workflow last (BYO engine; `promotion_review` becomes a record + door + scheduled message).

### Serves compute (phase 2)

_One cross-cutting row lands on the inference path: with the zones' OIDC off, the serve proxy's 401 guard never fires and anonymous callers reach GPU inference on an otherwise governed estate._

**XC-024 · With `frontend.oidc.enabled: false`, `locals.authEnabled` is false and `@rask/api/serve-proxy`'s 401 guard never fires, so anonymous callers reach GPU inference**
`chart, frontend, compute` · med · **blocked:** owner decision — enable zone OIDC vs fail closed and break the UI until OIDC is configured

- *Why open:* `auth.enabled: true` paired with `frontend.oidc.enabled: false` renders the zones with no OIDC env while the backends demand bearers — the chart's own values warn about the pair. Left open deliberately, because failing closed breaks a working UI until OIDC is configured.
- *Closes when:* Either enable the zones' OIDC (`frontend.oidc.enabled: true`, the intended state), or inject `auth.enabled` into the zone env and make `@rask/api/serve-proxy` fail closed on it.

### Serves the controlplane (phase 3)

_These are the estate's edge and its identity plane — the IdP every governed service verifies tokens against, TLS at the ingress, pod hardening, and the Dapr actor surface that trusts whoever can reach the sidecar port._

**XC-025 · `chart/templates/dex.yaml` still ships in-memory storage, bcrypt('password') static users and a plaintext client secret, and `values-prod.yaml` has no dex stanza at all**
`chart, catalog, lineage, gateway` · **HIGH** · **blocked:** owner decision on which real IdP prod federates to

- *Why open:* Verified 2026-09-10: the template still has `storage: type: memory`, `staticPasswords`, `staticClients` and an in-cluster HTTP issuer, and `grep '^dex:' chart/values-prod.yaml` returns nothing — the prod overlay does not touch it, yet the whole governed auth layer rests on this issuer and every governed service verifies tokens against it.
- *Closes when:* Add a `dex:` block to `chart/values-prod.yaml` setting an externally-reachable HTTPS issuer, a Postgres storage backend (AGE is already there) and an org-IdP connector; remove the static demo users from the template's prod path and move the client secret out of the ConfigMap into the infra-credentials/OpenBao path.

**XC-026 · Every actor method accepts a caller-supplied `actor` field; the only guard is pod-network topology, not the app**
`annotator, notifications, service-kit` · med · **blocked:** owner decision — dapr-api-token vs netpol tightening vs a sidecar-only guard

- *Why open:* `service_kit.governed.dapr_auth.require_dapr_token` now exists and gates e.g. `annotator/api/v1/endpoints/jobs.py:47`, but the `/actors/*` invocation routes themselves are not behind it, so anything that can reach the sidecar port can name an arbitrary actor id — true for every actor method in the plane.
- *Closes when:* Pick one of the three named postures — a `dapr-api-token` on the actor routes, a NetworkPolicy limiting who may reach the sidecar, or a sidecar-only guard like the gateway's lineage rows — and apply it estate-wide across the annotator and notifications actor hosts rather than per service.

**XC-027 · `chart/values-prod.yaml` sets `ingress.enabled/className/host` but no `tls:` block, so OIDC tokens and vended S3 credentials traverse plaintext at the edge**
`chart, gateway` · med · **blocked:** owner decision on the prod hostname / certificate issuer

- *Why open:* Verified 2026-09-10: `chart/templates/ingress.yaml` renders `.Values.ingress.tls` when present, and `chart/values-prod.yaml:201-204` supplies only `enabled`, `className` and an empty `host` — no tls entry, no cert-manager annotation. In-cluster Dapr mTLS covers service invocation only.
- *Closes when:* Add an `ingress.tls` block plus the cert-manager issuer annotation to `chart/values-prod.yaml`, and re-run `bash scripts/prod_render_check.sh` to pin it.

**XC-028 · `security.serviceAccounts`, `security.infraContexts` and `dapr.sidecarRestricted` are all false by default and `values-prod` flips none of them**
`chart` · med

- *Why open:* Verified 2026-09-10: `chart/values.yaml:696` `serviceAccounts.enabled: false`, `:703` `infraContexts.enabled: false`, `:2449` `sidecarRestricted: false`, and none of the three appears in `chart/values-prod.yaml` — so every app pod runs as SA `default` with token automount on. The assessment records all three as live-proven on 2026-07-13, so the omission reads as an oversight rather than a decision.
- *Closes when:* Set `security.serviceAccounts.enabled`, `security.infraContexts.enabled` and `dapr.sidecarRestricted` true in `chart/values-prod.yaml` (with the `dapr_sidecar_injector.sidecarDropALLCapabilities=true` companion), verify `dapr mtls -k` still passes, and extend `scripts/prod_render_check.sh` to assert them.

**XC-029 · The ingress carries no `nginx.ingress.kubernetes.io/proxy-read-timeout`, so the controller's 60 s cut severs every idle `query.live` SSE feed**
`chart, gateway` · med

- *Why open:* Confirmed live: the running controller has `proxy_read_timeout 60s`, no override annotation exists, and SvelteKit's SSE transport emits no keepalive (kit 2.70.1 — `runtime/server/remote.js:90` is the only `enqueue`, no timer in `runtime/server`). Each reconnect re-primes the whole 200-event window and writes an audit record, so replicating `query.live` 15× without this makes the estate slower while looking faster.
- *Closes when:* Add the `nginx.ingress.kubernetes.io/proxy-read-timeout` annotation to the chart's ingress template before any `query.live` expansion.

### Build, test and release infrastructure

_This is the machinery that decides whether a change can be proved and shipped — the test tree's honesty, the live e2e suites nothing runs, image provenance, and the prod install path that has no ordered procedure._

**XC-030 · Images carry no signature, and cosign alone would be decoration because the estate runs zero signature verifiers**
`chart, catalog, gateway, compute, notifications` · med · **blocked:** owner decision — a key custodian plus an admission-time verifier, without which signing is decoration

- *Why open:* `provenance()` in `.dagger/images.go` emits three OCI labels (`BUILD_DATE`, `VCS_REF`, `VERSION`) that 13 dockerfiles turn into `org.opencontainers.image.*`, and nothing more — no signature, no in-toto/SLSA attestation. The estate has no Kyverno, no sigstore policy-controller and no Gatekeeper (its five validating webhooks are CNPG, external-secrets and Kueue), so a signature nothing verifies is a control whose name is present and whose enforcement is not. The SBOM half shipped 2026-09-10: `dagger call sbom --name=<stem>` / `zone-sbom --zone=<zone>` emit CycloneDX 1.7 from the same tarball seam trivy scans.
- *Closes when:* Owner names a key custodian and approves an admission-time verifier (Kyverno or sigstore policy-controller); then add cosign signing to the `dagger call image … publish` path in `.dagger/images.go` and the verifying admission policy to the chart.

**XC-031 · No ordered prod install runbook exists — the FGA seed / OpenBao unseal / PSA-label ordering footguns are documented only as warnings in values-prod**
`chart` · med

- *Why open:* `docs/runbooks/` holds only `RUNBOOK-oncall.md`, `RUNBOOK-restore.md` and `llm-cluster.md`; `docs/DEPLOY.md` is the local/k3s framing and `docs/OPERATORS.md` is strategy. values-prod itself warns that flipping `medallion.fgaEnabled` before running `scripts/seed_medallion_fga.sh` fails the stage runners closed.
- *Closes when:* Write `docs/runbooks/RUNBOOK-prod-install.md` with the ordered sequence (secrets → `scripts/seed_medallion_fga.sh` → OpenBao init+unseal → flip governance → verify), and consider a seed Job / helm hook for the FGA grants.

**XC-032 · `imagePullSecrets` is plumbed into only 3 of 56 chart templates and no first-party image is digest-pinned in values-prod**
`chart` · med

- *Why open:* Verified 2026-09-10: `grep -rl imagePullSecrets chart/templates/` matches 3 files (controlplane.yaml, frontends.yaml) of 56, so most pod specs cannot pull from a private registry at all. The plumbing exists; it is simply not applied everywhere, and it blocks any non-k3s cluster.
- *Closes when:* Add the `{{- with .Values.imagePullSecrets }}` block to every remaining pod spec in `chart/templates/` (or hoist it into `_helpers.tpl` and call it once per template), and set registry-qualified `image.*.repository` values in `chart/values-prod.yaml`.

**XC-033 · Two `test_lineage_e2e.py` cases fail, and nothing routinely points the 111 e2e functions at k3s**
`lineage, e2e` · med

- *Why open:* Two cases fail against current code, and the wider class is that 111 e2e functions across 30 files are excluded from `make test` (`-m "not e2e"`). The `make e2e-live` / `scripts/e2e_live.sh` half of the row has since been built, so what remains is the failing cases and a suite that only ever runs by hand.
- *Closes when:* Fix the two failing `test_lineage_e2e.py` cases and wire `make e2e-live` into a routine (CI or scheduled) run so 'verified live' stops resting on a manual terminal invocation.

**XC-034 · Seven seams have no test at all: `objectfs.py`, `lakehouse/blobs.py`, `lancekit/store.py`, `lancekit/reader.py`'s REST path, `audit.py`, `middleware.py`, and `submit_or_reattach`'s delete branch**
`service-kit, catalog, medallion` · med

- *Why open:* Q3-38 closed only the `ray_kit.submit` clause — the kernel moved to `medallion.services.ray_jobs_api.submit_or_reattach` and its deterministic-id/reattach/resubmit branches are tested — while the six modules and the DELETE branch named here still have none.
- *Closes when:* Write tests for `objectfs.py`, `lakehouse/blobs.py`, `lancekit/store.py`, the `lancekit/reader.py` REST path, `audit.py`, `middleware.py`, and `submit_or_reattach`'s delete branch.

**XC-035 · 111 single-component test files sit in `tests/unit` instead of their service's own testpath, so a catalog change cannot be verified against catalog tests**
`catalog, service-kit, medallion, maintenance, lineage, annotator` · med · **blocked:** owner decision on relocating 111 files

- *Why open:* All 329 files were classified by how many components each imports: 53 render the chart, 119 import two or more, 46 import none — all correct where they are — and 111 import exactly one (33 catalog, 25 service-kit, 19 annotator, 14 medallion, 9 maintenance, 6 lineage, 2 ingest, 2 search, 1 storage). `services/catalog/tests` holds 72 files while 33 more catalog-only files sit in the shared directory, so the per-commit selection is dishonest by construction.
- *Closes when:* Read the 25 `service_kit` files first to separate shared fixtures from misplaced tests, then relocate the remaining single-component files into each service's own `tests/` directory with import fixes, keeping the per-commit selection always including the invariant and integration layers.

**XC-036 · A subchart names `{{ .Release.Name }}-x` while this chart's Secrets use `lance.fullname` — they agree only when the release is named `rask`**
`chart` · low

- *Why open:* Any release not named `rask` silently points the subchart at Secrets that do not exist.
- *Closes when:* Make the subchart reference `lance.fullname` (or template the Secret names it expects) so the two agree under any release name.

**XC-037 · `pytest-xdist` is still not a dependency and 17 test files roll their own `subprocess.run` helm render**
`chart, service-kit` · low

- *Why open:* The parse half landed (memoised `_rendered_docs` + libyaml, 36 `yaml.safe_load_all` sites converted: 726.9 s → 308.3 s, `make test` now 8 m 36 s). Two residuals stand: `-n` is unsafe until the parallel-unsafe suites are grouped with `--dist loadfile` (the `lance.audit` process-global logger, `configure_audit`'s level, the registry CAS markers), and of 44 files touching helm, 17 roll their own render — 105 uncached calls for 162 tests, ≈76 redundant renders ≈ 34 s.
- *Closes when:* Add `pytest-xdist` and group the three parallel-unsafe suites with `--dist loadfile`; convert the 13 cleanly-convertible of the 17 hand-rolled renders onto one cached `render(*flags)` keyed on the verbatim flag tuple, leaving the 4 that need a real subprocess (`check=False` ×3, `CalledProcessError` in `test_invariants`) and the two deliberate variants (`test_chart_gitops_ready._render` omits `image.localImages=true`; `test_prod_ha_posture` renders with `-f chart/values-prod.yaml`) alone.

**XC-038 · The infra subcharts (GreptimeDB, NATS, Perses, the Dapr control plane) still get no requests/limits from values-prod**
`chart` · low

- *Why open:* `chart/values-prod.yaml` carries only two `resources:` blocks (one of them OpenFGA's, which half-closed the assessment's gap #4); the remaining infra subcharts pass none through, and they are the shared bus and telemetry the cascade rides. Not re-measured against every subchart's own defaults.
- *Closes when:* Add per-component `resources` keys under the greptimedb-standalone, nats, perses and dapr subchart stanzas in `chart/values-prod.yaml`, then re-render and confirm no container is unbounded.

**XC-039 · Three live e2e legs skip on a 5 s `/livez` timeout while the medallion producer is up and serving the cascade**
`medallion, e2e` · low

- *Why open:* The probe is too tight for an estate under load, and a timeout that reads as 'not reachable' turns a pass into a skip.
- *Closes when:* Raise (or retry) the `/livez` probe timeout in the live e2e runner so a loaded-but-healthy producer does not read as unreachable.

**XC-040 · The dangling-locator gate checks pointers INTO a file but nothing gates a register's SIDECAR (e.g. an orphaned `*.findings.json`)**
`e2e` · low

- *Why open:* `test_no_locator_names_a_deleted_register.py` would not have caught a `.findings.json` that nobody cited — that one was found by a reader asking why a file was still there.
- *Closes when:* Extend the gate to fail when a sidecar file (`open_*.findings.json` and similar) outlives the register it indexes.

**XC-041 · `make seed-dev` chmods the whole corpus root, is pinned to one release name/node/ports, and seeds doc ids with an already-wrong `dataset_version`**
`scripts, chart` · low

- *Why open:* The seventh wave hardened the seeder's failure reporting but not this: `_make_world_readable` chmods the corpus ROOT rather than what the run wrote (and does nothing for the re-seed case its own header claims to cover), the script is pinned to one release name, one node and fixed local ports, and the labeling seed hard-codes fixture-internal doc ids with a `dataset_version` that is already wrong.
- *Closes when:* Chmod only the paths the run wrote and handle the re-seed case; take release name, node and ports as parameters; derive the labeling seed's doc ids and `dataset_version` from the live fixture the way the corpus half already reads `MEDIA_DB` and the catalog's `/v1/me`.

**XC-042 · `scripts/dev-micro.sh` never starts `:8103`, the annotations plane, and `/capi/v1/me` 502s without a catalog**
`annotator, home-zone` · low

- *Why open:* dev-micro starts `:8101`/`:8804`/`:8820`/`:8888` only, so the annotations plane must be started by hand; the `/capi/v1/me` 502 was the one console error in the otherwise-verified annotator run.
- *Closes when:* Add the `:8103` annotations plane to `scripts/dev-micro.sh`'s process list, and make `/capi/v1/me` degrade rather than 502 when no catalog is reachable.

**XC-043 · The P7a/P7b dead-name docs sweep: six nav-served docs still describe the orchestrator, `core_api`/`search_api`/`volumes_api`, `packages/htr` or `/default/<zone>` bases**
`docs` · low

- *Why open:* F1 closed the first systematic cause of staleness (the dead `common` package); this is the second and was never executed. The work-list is exactly `architecture/microservices.md`, `architecture/deployment.md`, `architecture/layout.md`, `DECISIONS.md`, `packages/htr.md` and `reference/htr.md` — and `packages/htr` is not a package at all, it is the sealed `runners/htr`, outside every workspace glob.
- *Closes when:* Rewrite those six files so `grep -rl "core_api\|search_api\|volumes_api\|packages/htr\|/default/" docs/ --exclude-dir=superpowers --exclude=lance-ns-merge.md --exclude=OPEN-WORK.md` returns only files whose mention is an explicit tombstone, with the zensical nav gate still green.

**XC-044 · `fga model validate` is not in the `ms-authz` CI job, so weighted-graph compatibility stays a hand audit**
`service-kit` · low · **blocked:** upstream OpenFGA shipping `fga model validate`

- *Why open:* The 2026-08-08 audit found nothing to migrate, but the legacy-algorithm fallback will be removed upstream and an incompatible model then fails to build; the machine check waits on the announced command shipping.
- *Closes when:* When `fga model validate` ships in the CLI, add it to the `ms-authz` CI job beside the existing `fga model test`.

**XC-045 · Decide whether the deferred infra/app chart split becomes the home for rask-operator's chart**
`chart` · low · **blocked:** owner decision

- *Why open:* The deferred chart split (`docs/DECISIONS.md:812, 834-849`) — infra (operators + CRDs, rarely installed) vs app (upgraded constantly) — has no verdict, so there is no decided home for a future rask-operator chart and its CRD.
- *Closes when:* An owner ruling recorded in `docs/DECISIONS.md` saying whether rask-operator's chart (and its gated, `helm.sh/resource-policy: keep` CRD) lands in a split infra chart or elsewhere.

**XC-046 · Remote branch `claude/flyte-2-dapr-audit-19cyc2` is still not deleted**
`—` · low · **blocked:** owner action (push rights)

- *Why open:* Decided: delete — everything is on `main`. The sandbox proxy refuses `git push --delete`, so nobody in-session can perform it.
- *Closes when:* Run `git push origin --delete claude/flyte-2-dapr-audit-19cyc2` from a machine with push rights.

### Observability

_The telemetry plane is what turns 'it looks fine' into a measurement — and today the authz layer emits nothing, prod telemetry is mislabelled, and foreign log noise is loud enough to hide something that matters._

**XC-047 · OpenFGA exports no traces or metrics: `telemetry.trace.otlp.endpoint` is unset and the estate scrapes no Prometheus endpoint**
`openfga, chart, catalog` · med

- *Why open:* Every governed catalog/lineage request pays an FGA check and there is no FGA panel at all. v1.18.3 supports `OTEL_*`, `OPENFGA_TRACE_SAMPLER`, `datastore_item_count` and `openfga_iter_query_duration_ms`, but the subchart only exposes a Prometheus `/metrics` endpoint while this estate is OTLP-push (zero `prometheus.io/scrape` in `chart/templates/`).
- *Closes when:* Point the subchart's `telemetry.trace.otlp.endpoint` at the OTel Collector with the `x-greptime-pipeline-name=greptime_trace_v1` header, decide how its metrics reach GreptimeDB, and add an FGA panel to `chart/templates/perses-dashboards.yaml`.

**XC-048 · `request_id` + actor propagation has zero hits in `services/`, and the verdict on whether OTel plus the audit trail supersede it was never recorded**
`service-kit, catalog, lineage` · med · **blocked:** owner decision — record supersession by OTel + audit trail, or build it

- *Why open:* The register says it is possibly superseded by OTel tracing plus the audit trail, but nobody ever recorded that verdict — so it is neither built nor knowingly dropped. (Related evidence from the audit-sink row: `CorrelationFilter` already stamps request_id/trace_id on the root handler, which is the strongest argument for supersession.)
- *Closes when:* Either record the supersession verdict in `docs/DECISIONS.md` §9, or add `request_id` + actor propagation through the service-kit middleware and the downstream clients.

**XC-049 · Two Kueue controllers reconcile one set of CRDs — the resulting `kueue-ca` handshake spam runs ~450/min, plus two failing otel-collector scrape targets**
`chart` · low · **blocked:** cluster-operator decision about the foreign `kueue-system` install, which arrived with the `htr-batch` namespace this repo does not own

- *Why open:* The chart-owned Kueue CRDs carry `app.kubernetes.io/managed-by: Helm` / `helm.sh/chart: kueue-0.18.1` with their conversion webhook pointing at `rask-kueue-webhook-service` in `default`, while `kueue-system/kueue-controller-manager` — 43 days old — is owned by nothing in this repo; a CA bundle written by whichever reconciled last is exactly what produces the `x509: certificate signed by unknown authority ... 'kueue-ca'` spam. Neither source is rask code, but the first is loud enough to hide something that is. (The spam rate rests on a refuter's measurement — `journalctl -u k3s` returned zero lines for the auditor.)
- *Closes when:* The cluster operator removes the non-chart `kueue-system` Kueue install (or the chart-owned one) so a single controller writes the CRD conversion-webhook CA bundle; separately repair the two failing otel-collector scrape targets.

**XC-050 · `chart/values-prod.yaml` never sets `observability.environment`, so every OTel resource attribute labels prod telemetry with the chart default**
`chart` · low

- *Why open:* Verified: `chart/values.yaml:2779` defaults `environment: rask` with the comment 'override per deploy (dev / staging / prod)', and `grep environment chart/values-prod.yaml` returns nothing — so every trace and metric in prod, the cascade's included, carries the wrong `deployment.environment.name`.
- *Closes when:* Add `observability.environment: prod` to `chart/values-prod.yaml`.

**XC-051 · Undecided whether the estate shares one GreptimeDB or runs one per workload**
`chart` · low · **blocked:** owner decision

- *Why open:* Listed as unresolved open decision #4 in the merge checklist. De facto the estate has one (`rask-greptimedb-standalone`), so the decision is being made by default rather than taken — which matters once a runner's telemetry volume competes with the cascade's RED metrics and lineage-adjacent traces.
- *Closes when:* Record the ruling in `docs/DECISIONS.md` (one shared GreptimeDB, or per-workload) and make `chart/values.yaml`'s observability stanza state it.


---

## PHASE 2 · COMPUTE

**compute · ingest · ray-kit · maintenance's Ray half.** BYO workflow engine, BYO distributed engine.

### compute

_The compute service (:8804) is the estate's only door onto the Ray cluster; what it holds (a wildcard S3 key, one shared dashboard token, an unnarrowed dashboard surface) is what an attacker or a mis-scoped reader gets._

**CP-001 · The Ray/compute S3 identity enumerates 105 buckets, and one shared dashboard token reads every job's `runtime_env` past the vended credential's 900s TTL**
`compute, catalog, medallion` · med

- *Why open:* Re-measured 2026-09-09 through the running Ray head: `list_buckets()` with the pod's own `S3_KEY`/`S3_SECRET` returns 105 buckets. Withholding `s3:ListAllMyBuckets` does not withhold the list — RustFS falls back to per-bucket `ListBucket`, which `arn:aws:s3:::*` grants. The identity is scoped (`rask-ray-compute`, not root) but the breadth is not, and the vended triple sits in a `runtime_env` that any holder of the single dashboard token can read after the credential's TTL has expired for everyone else.
- *Closes when:* Vend per-table credentials on the Ray lane (same root as Q5-2) so the Ray pod never holds a wildcard key, and stop putting the vended triple in `runtime_env` where a shared dashboard token can read it.

**CP-002 · `services/compute` still runs an image without `DiagnosticFormatter`, so its `extra=` diagnostics are dropped**
`compute, service-kit` · low · **blocked:** owner go-ahead for one image build

- *Why open:* `DiagnosticFormatter` lives in `service_kit.app.setup_logging`, so it ships per IMAGE, not per commit. The nine lakehouse services rolled on `main-d6dc0e3c` and were verified safe (zero `Traceback`, zero `--- Logging error ---` across all nine in a 40-minute window); `compute` builds from its own dockerfile and still runs an older image. It is the only in-scope service left (gateway/notifications are fleet services the scope ruling does not name, controlplane is not a lakehouse service, flows is struck).
- *Closes when:* Build and roll one image: `dagger call image --name=compute publish …` from `.docker/compute.dockerfile`, then confirm an `extra=`-carrying line renders in the pod's logs.

**CP-003 · The compute zone's actual Ray Serve / dashboard call set is unknown, so the dashboard exposure cannot be narrowed**
`compute` · low · **blocked:** D1

- *Why open:* The dashboard surface stays as wide as `ray-kit` makes it because nobody has enumerated which endpoints the compute zone genuinely calls; this is the API-surface half of the same exposure Q6-1 measures the credential half of.
- *Closes when:* Enumerate the compute zone's dashboard/Serve calls, keep only those on `services/compute`'s router, and put each behind FGA.

**CP-004 · Nothing in the FGA model governs execution — no zone, compute-job or run type — and whether that boundary is deliberate is undecided**
`compute, catalog, service-kit` · low · **blocked:** owner ruling on whether execution/zone access becomes a governed dimension at all

- *Why open:* Grepping `packages/service-kit/src/service_kit/governed/auth/model.fga` for `zone|compute|submit|job|run` returns no type or relation: the ten types are a data hierarchy plus labeling, and nothing models a right to execute. `services/compute` IS authenticated today (its whole router is gated), so the plane is not unguarded — but a door is not a governance model, and the file's §9 lead explicitly says this is an open question, not a finding.
- *Closes when:* Run the §9 pass and record the ruling in `docs/DECISIONS.md`: either add zone/run types to `model.fga` with gates in `services/compute`, or state that execution rights are expressed as data rungs on what the surface reads and that a zone is a deployment surface, never a governed object.

### ingest

_Ingest is the estate's only door for external bytes; each row here is a way a run can land the wrong thing, strand units, or read a bucket with a credential nobody scoped._

**CP-005 · `ensure_dataset` runs before enumeration, so a mis-pointed source leaves a registered empty bronze table and the run reports COMPLETE**
`ingest, medallion, catalog` · **HIGH** · **blocked:** owner decision — the shipped code records that a zero-unit enumeration is deliberately NOT a refusal, so the register's "refuse at the enumeration seam" ask contradicts it; someone must say whether a mis-pointed source is distinguishable from a quiet one

- *Why open:* Two of the three halves have since landed and the row was not updated: `finalize_run` treats an empty fragment list as a no-op (`committed_version: None`), and `workflow.py:629`'s `units_total == 0` short-circuit now returns `RunOutcome(status="COMPLETE", rows=0)` on purpose, with a comment recording the ruling that an empty prefix is a legitimate state of the world and must not alert. What survives is the ORDERING: `ensure_dataset` is still the first activity of every run (`workflow.py:473`, noted at `catalog_service.py:85`), so a wrong prefix still registers a bronze table that will never hold a row, and the run still reports success.
- *Closes when:* Move `ensure_dataset` after enumeration in `services/ingest/src/ingest/workflow.py` (or roll it back when `units_total == 0`), pinned by a test that points a source at an empty prefix and asserts no table is registered.

**CP-006 · `drain_chunk` batches all redeliveries in one fetch regardless of origin batch, so two crashed batches' remainders merge into an undecidable fragment**
`ingest` · med

- *Why open:* The finalizer's exact-cover search narrowed the refusal to the genuinely undecidable case (0% of resolvable inputs, down from 18.2%), but the state is still reachable and still strands units. Removing it means preserving the original batch grouping on redelivery, which `drain_chunk` currently discards.
- *Closes when:* Carry the origin batch id through redelivery in `drain_chunk` (`services/ingest/src/ingest/queue.py` / `staging.py`) and group redeliveries by it so the finalizer never sees a fragment spanning two batches; delete `StagingOverlapError` once it is unreachable.

**CP-007 · Ingest's lineage OUTBOX write has no credential and is denied the bucket, and its reads of the estate-default store and of secretless registered stores have none either**
`ingest, catalog, chart` · **HIGH** · **blocked:** owner/operator decision on paths 2 and 3 — which source buckets, and which scoped identity each gets. The OUTBOX half is unblocked.

- *Why open:* The headline is closed (2026-09-09: the root pair is gone from all five pods; ingest's governed writes and staging ledger are catalog-vended), but three paths remain. **Path 1, the lineage-outbox staging WRITE, is LIVE and was measured firing 2026-09-10** — not latent, which is what this row said until a real run proved otherwise: `_outbox_storage_options` returns `{endpoint}` and no keys, so `stage_event` dies `OSError: When testing for existence of bucket 'lance-catalog': AWS Error ACCESS_DENIED during HeadBucket`, twice in one ingest run. It is the RECOVERY path for a refused lineage emit (§E1), so the run that needed it lost its event outright and still reported COMPLETE. Its docstring claims the options are 'resolved the way every other S3 caller in this service resolves it', which is false — every other caller vends from the catalog. Paths 2 and 3 (`objectstore._s3_prefix`'s `is_estate_default` branch; any registered store declaring no secret) read buckets an operator registers at runtime, so no deploy-time policy can enumerate them, and narrowing them before path 3 is closed would break ingestion from every secretless store; those two remain latent (`rask-ingest` registers no stores).
- *Closes when:* PATH 1 FIRST and on its own. **The MECHANISM is settled and only the AUTHORIZATION is open** (re-measured 2026-09-10). The standing rule decides it — *"STS for STORAGE… a scoped static key is not a fix"* — so the medallion's scoped-identity precedent is OUT for this, and confirmed live: ingest carries no S3 key at all (`RASK_INGEST_SECRETS_FROM_DAPR=true`, no access key in the pod), which is the stronger posture and worth keeping.
  **The primitive already supports it.** `catalog/core/vending.py::build_session_policy(bucket, prefix, tier, bases)` is prefix-GENERIC, not table-keyed, so `(lance-catalog, _lineage_outbox, write)` needs no change to it; as an STS session policy it can only restrict the catalog's role, never widen it. What is missing is a DOOR — the only vending route is `POST /v1/table/{id}/credentials`, and a control prefix is not a table.
  So the remaining decision is one question, not a design: **who may vend write on `_lineage_outbox`?** It is every service that stages an event (medallion, maintenance, ingest) and no one else, which is neither a table rung nor a warehouse rung — the same shape `can_maintain` and `publisher` were minted for. Answer that, add the door, point `_outbox_storage_options` at it, and prove it by staging from ingest and watching the drain — which now works (LH-086, `drained=1`). Then prove it by staging an event and watching the reconcile cron drain it — which also unblocks LH-086. THEN paths 2 and 3: register the source buckets as stores that DECLARE a `secret` naming a scoped identity in the Dapr secret store, so `objectstore._own_store_for` stops falling back to `without_credentials`; the machinery landed 2026-09-08 (`06f0ab21`) and is inert until an operator aims it.

**CP-008 · Enumeration has no BYTE ceiling (the unit and time ceilings landed and gate A15's enforcement half now exists)**
`ingest, chart` · low

- *Why open:* The register's headline is stale in its load-bearing claim: `RASK_INGEST_MAX_RUN_HOURS` and `RASK_INGEST_MAX_UNITS` are both read (`ingest/config.py:102-103`), carried into `RunLimits` and enforced in `ingest/workflow.py` — `max_units` refuses at enumeration before the fan-out and `max_run_hours` is a real run deadline (the module comment names it "A15's other half, which nothing enforced", past tense) — and `chart/values.yaml:265,281` sets both. Gate A15 therefore asserts a relation whose other side IS enforced. What genuinely does not exist is a byte ceiling: `grep max_bytes services/ingest/src` returns nothing, so one enormous object still enters unbounded.
- *Closes when:* Add a `max_bytes` limit to `RunLimits` in `services/ingest/src/ingest/workflow.py` + `config.py` (env `RASK_INGEST_MAX_BYTES`, chart default), refused at enumeration alongside `max_units`; then strike the run-hours half of this register row.

**CP-009 · CLOSED IN CODE: `runs.py`'s outcome-status promotion now accepts terminal FAILED and TERMINATED**
`ingest` · low

- *Why open:* Not open. Verified today at `services/ingest/src/ingest/runs.py:361`: the promotion set is `("COMPLETE", "COMPLETE_WITH_ERRORS", "FAILED", "TERMINATED")`, with a comment naming this exact defect ("THE ALLOWED SET INCLUDES 'FAILED', and leaving it out was a latent defect that the run DEADLINE made reachable") and `_RUNTIME_STATUS` itself mapping `FAILED -> FAILED`. It is pinned by `services/ingest/tests/test_terminated_is_not_a_failure.py`. It is listed here only so the register row it blocks is not left waiting on it.
- *Closes when:* Strike the row from the register and lift it from the empty-source item's `blocked_on`; no code change.

### ray lane (ray-kit / Ray jobs / the Ray head)

_Every batch job the platform runs goes through a Ray head that today accepts unauthenticated job submission, keeps its job history only in memory, emits no metrics or logs anywhere, and is driven by a submission path that bypasses the CRD the chart already grants RBAC for._

**CP-010 · dev-kuberay.ra.se's job-submission API answers 200 with no token and with a wrong token**
`external-kuberay, compute` · **HIGH** · **blocked:** the operators of the KubeRay cluster at dev-kuberay.ra.se; plus an answer on whether that host is reachable from outside the network and whether anything already fronts it — that answer decides urgency

- *Why open:* Measured from inside rask's cluster: `GET /api/version`, `/api/jobs/`, `/api/cluster_status` and `/nodes?view=summary` all return 200 with NO Authorization header and with a WRONG one. rask's half is correct — `ray.auth.enabled` is true in the live release, the token secret holds 32 bytes, and compute sends `Authorization: Bearer` — the cluster simply does not check it. `/api/jobs/` is the same door that POSTs and DELETEs jobs, so anything that can route to that host can enumerate the cluster's work and run code on it. Only read paths were exercised; no write was attempted. rask cannot fix this from this repo.
- *Closes when:* The cluster's operators enable token verification (or front it with ingress auth / a network policy); then re-run the three probes — no-token and wrong-token must 401 on `/api/version`, `/api/jobs/` and `/api/cluster_status`.

**CP-011 · `runners/htr` is not re-cut as stage runners: the prefetch pipeline and the loader/writer endcaps remain, and `main.py` has no `stage` subcommand**
`runners/htr, medallion, compute` · **HIGH**

- *Why open:* Verified still present today: `runners/htr/src/runner/pipeline.py` imports `PrefetchActor`, `PageLoaderActor` and `AltoWriterActor` from `htr.actors.io`, defines `prefetch_pipeline` (line 143) and registers it as the `"prefetch"` entry (line 230); `runners/htr/src/runner/main.py` declares no subparsers at all. The register's cited seam is itself stale — `medallion/schemas/htr.py::GOLD_CONTRACT_COLUMNS` was deleted 2026-08-17 — so the re-cut must be re-anchored on `medallion/schemas/tier.py`'s opaque `{id, payload, stage, lineage, source_rowid}`.
- *Closes when:* Give `runners/htr/src/runner/main.py` a `stage` subcommand, run layout/lines and transcribe as `medallion.bronze`/`medallion.silver` stage runners driven by `MEDALLION_RAY_ENTRYPOINT`, delete the prefetch and endcap actors, and get the bronze→silver→gold cascade e2e green with lineage populated.

**CP-012 · The RayJob CRD path is built but off the deployed path — submission goes through the Jobs API, zero RayJob/RayCluster/RayService CRs exist, and Kueue admits nothing**
`medallion, compute, ray-kit, chart` · **HIGH** · **blocked:** owner decision — chart-owned RayCluster vs ephemeral per-job clusters vs delete the adapter (a job-record durability question); Q17-54 was additionally deferred by instruction until the lakehouse phase finished, which it now has

- *Why open:* One question wearing four register ids. The dependency-graph half is done (catalog/lineage/maintenance/service-kit/medallion carry zero ray imports; `32ff50cb` converged three job programs plus `runners/dummy` onto `WorkOrder.to_env()`), and `engine_registry.executor_for('ray', …)` constructs the adapter — but the deployed path still calls `medallion.services.ray_submit` (verified: `workflow.py:486,527,707,977` import it; `executor_for` is defined at `engine_registry.py:52` and called nowhere on that path). `kubectl get rayjobs,rayclusters,rayservices -A` answers "No resources found"; the live Ray is `ray-lance-head`, a hand-applied plain Deployment with no ownerReferences. Because nothing goes through the CRD, Kueue's two chart-owned ClusterQueues (`rask`, `htr-batch-cq`) are structurally bypassed, and job history lives only in the head's memory — a host `ray stop --force` took the dashboard from 7 jobs to 0.
- *Closes when:* Owner picks chart-owned `RayCluster` vs ephemeral per-job clusters vs deleting the adapter. If kept: add the CR to the chart, select it from `services/medallion/src/medallion/services/rayjob_executor.py`, route the medallion's submission through `service_kit.lakehouse.executor.executor_for` instead of `ray_submit`, bring the hand-applied `ray-lance-head` under the chart, configure the Ray history server so job records survive a head restart, and set Kueue gang + priority policy on the `rask` ClusterQueue. If dropped: delete that module and `chart/templates/medallion-rayjob-rbac.yaml`.

**CP-013 · `RayJobExecutor.status()` maps only `status.jobStatus`, so a RayJob whose cluster never came up reports PENDING forever and can never be resubmitted**
`medallion, ray-kit` · med

- *Why open:* Verified in `services/medallion/src/medallion/services/rayjob_executor.py`: line 152 reads `status.jobStatus` alone, and `jobDeploymentStatus` is consulted only at line 161 to fill a message once `jobStatus` already says FAILED. KubeRay records a cluster that never came up, an `activeDeadlineSeconds` expiry or a Kueue eviction in `jobDeploymentStatus` and leaves `jobStatus` empty, which `_JOB_STATUS` maps to PENDING — and `DURABLE_RECORD` then forbids resubmitting it. Real today as library code; reaches production only if the adapter is kept.
- *Closes when:* Map `jobDeploymentStatus` in `RayJobExecutor.status()` so a cluster-provision failure, deadline expiry or Kueue eviction reports FAILED, pinned by a test feeding a status with an empty `jobStatus`.

**CP-014 · `RayJobExecutor` treats any 409 as REATTACHED without reading the CR, and the CR name omits `code_version`**
`medallion, ray-kit` · med

- *Why open:* Verified in the same file: line 136-137 returns `SubmitOutcome.REATTACHED` on any `409` without fetching the object. A 409 may mean a DIFFERENT job holds that name, and omitting `code_version` from the derived name makes that reachable — a same-token re-run after a deploy reattaches to the previous build's job.
- *Closes when:* Read the CR on 409 and compare identity before declaring REATTACHED, and include `code_version` in the RayJob CR name derivation.

**CP-015 · `POST /train` on the medallion producer does not resolve the `$n` form of `features[].dataset`**
`medallion` · med

- *Why open:* Classed read-from-wrong-target: the `$n` dataset reference is passed through unresolved into the submitted training job, so training reads from a target the caller did not name.
- *Closes when:* Resolve `features[].dataset`'s `$n` form against the catalog inside the `/train` door, refusing an unresolvable reference with a 400 rather than forwarding it.

**CP-016 · `tests/unit/test_ray_auth.py` asserts only `helm template` output, never that a token is honoured**
`compute, chart` · med · **blocked:** nothing external; needs a reachable Ray endpoint or a Dagger-run Ray fixture to assert against

- *Why open:* All eight tests are render-time assertions — they prove the chart emits the secret, the env and the fail-closed prod guards, and they pass. None makes an authenticated call, so the gap between "we send a token" and "the token is honoured" was invisible to the whole suite; that is exactly how the unauthenticated dashboard above went unnoticed.
- *Closes when:* Add a test that calls a Ray dashboard endpoint (`/api/version`, `/api/jobs/`) with no token and with a wrong token and asserts 401 — against a live endpoint or a Ray container brought up with `dagger core container … as-service up`, never docker.

**CP-017 · Nothing scrapes the external Ray cluster — zero `ray_*` / `ray_serve_*` / `ray_data_*` / `autoscaler_*` series reach GreptimeDB**
`external-kuberay` · med · **blocked:** the operators of the KubeRay cluster at dev-kuberay.ra.se

- *Why open:* rask's half is landed and verified end-to-end against a real KubeRay cluster, but the external half is unapplied, so an alive-but-wedged head, a Serve app with zero healthy replicas and a cascade stalled on backpressure are indistinguishable from an idle healthy cluster. This can never be fixed from the rask chart: the Collector's service discovery is namespace-scoped and in the documented production posture (`observability.otelCollector.externalEndpoint` set) `chart/templates/otel-collector.yaml` renders nothing at all. Ray metrics are per-node PULL endpoints — the gap cannot be pushed and does not self-heal.
- *Closes when:* Add a `job_name: ray-pods` scrape block to the collector beside that cluster (keep on `__meta_kubernetes_pod_label_ray_io_is_ray_node="yes"` and container port name `metrics`; relabel `ray_io_cluster`, `ray_node_type`, `namespace`, `pod`), confirm the head and every worker group declares `containerPort: 8080, name: metrics`, and export to `http://<greptimedb-host>:4000/v1/otlp` with `x-greptime-db-name` only. Carry rask's `metric_relabel_configs` drop first — enabling the scrape took the estate from 3,250 to 7,349 series via 113 `ray_data_*` families and OOMKilled the store 13 times. Done when `ray_node_cpu_utilization` returns a series on `:4000/v1/prometheus`.

**CP-018 · Ray core logs never leave the pod — driver/task/actor logs are files under `/tmp/ray` that nothing mounts and nothing tails**
`external-kuberay` · med · **blocked:** the operators of the KubeRay cluster at dev-kuberay.ra.se

- *Why open:* Measured on rask's head pod: ~490 log files in-container against 28 stdout lines in six days; the container's stdout carries only `ray start`'s plain-text CLI banner. JSON encoding sharpens the records but does not change WHERE Ray writes, so the core half still needs a mounted path or a sidecar.
- *Closes when:* Add a `ray-logs` emptyDir (sizeLimit 2Gi) mounted at `/tmp/ray` on the ray-head container plus a `ray-log-agent` sidecar (otel/opentelemetry-collector-contrib) mounting it read-only, whose filelog receiver reads `/tmp/ray/session_latest/logs/**/*.{log,out,err}` with `include_file_path: true`, `start_at: end` and a json_parser, exporting otlphttp to the same GreptimeDB as the metrics. Poll frequently at first — the directory does not exist until Ray creates it. Done when a `job-driver-*.log` line from a medallion stage job is queryable. Consider `RAY_DEDUP_LOGS=0`: the 5-second pattern buffering reorders records against their timestamps.

**CP-019 · Serve-plane tracing is switched on nowhere external and no proxy/router/replica span has ever been observed**
`external-kuberay, compute, service-kit` · med · **blocked:** the operators of the KubeRay cluster at dev-kuberay.ra.se; an image recent enough to carry `service_kit`; and a Serve application that actually comes up to send a request to

- *Why open:* One gap in two register rows: the switch is unset on dev-kuberay, and even on rask's rebuilt reference head — where the env var is set and the import resolves — rask's own Serve application never came up (`applicationStatuses` empty on both the active and pending clusters), so there was no endpoint to send a traced request to. Core tracing is verified end-to-end (5 tasks in, 5 PRODUCER + 5 CONSUMER spans out); the Serve plane is the higher-value half, since it is the segment that joins a gateway-originated trace to the model call and honours an inbound traceparent, and Ray's own monitoring docs never mention the switch exists. Both tracing planes fail soft by design, so a healthy pod proves nothing — only an observed span does.
- *Closes when:* On the head AND every worker group container set `RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH=service_kit.ray_tracing:serve_span_processors` and `RAY_SERVE_TRACING_SAMPLING_RATIO=1.0` (the default 0.01 yields zero spans on a ten-request smoke test), with `OTEL_EXPORTER_OTLP_ENDPOINT` present on the same containers; on the HEAD ONLY set `rayStartParams.tracing-startup-hook: service_kit.ray_tracing:setup_tracing` (on a worker group it is a silent no-op — the hook propagates via GCS internal KV). Verify the image carries it first: `kubectl exec <ray-head> -- python -c "import service_kit.ray_tracing"`. Do not swap the two functions — `setup_tracing` takes no args and returns None, `serve_span_processors` returns a list of SpanProcessor; crossing them fails soft with nothing unhealthy. Then, on a cluster with a live Serve application, send a request through the gateway and query `opentelemetry_traces` for ONE trace_id carrying both the gateway span and a Serve proxy/replica span.

**CP-020 · The chart ships Ray telemetry wiring that no install renders — decide between rendering `rayservice.yaml`, adopting the orphaned `rask-ray`, or dropping it**
`chart` · med · **blocked:** owner decision — the handover file names this as "the one decision this raises for rask"

- *Why open:* `chart/templates/rayservice.yaml` renders only under `ray.enabled AND singleTenant.enabled`, and `singleTenant.enabled` is false, so a normal install deploys no Ray and the tracing/log env the template carries reaches nothing. The live `rask-ray` RayService carries Helm labels from an older revision but appears in no current release manifest — verified, `RAY_SERVE_TRACING_*` appears 0 times on it. External-only by accident rather than by choice; the same caveat applies to the §3 log-encoding env vars.
- *Closes when:* Owner rules among the three, then the matching edit: flip the gate in `chart/templates/rayservice.yaml` so it renders, bring `rask-ray` under the Helm release, or delete the `RAY_SERVE_TRACING_*` / `RAY_*_LOG_ENCODING` wiring from the template and keep the handover as its only home.

**CP-021 · Ray GCS is not fault-tolerant — a head restart kills in-flight jobs, and the only supported fix needs an external Redis the estate forbids**
`compute, chart` · med · **blocked:** owner decision — accept job loss on head restart, or grant a no-Redis exception for the GCS store

- *Why open:* Marked Owner in §O2. The platform now degrades in one poll interval instead of 24h, but fault tolerance itself requires an external Redis for the GCS store, and the estate's standing rule is "No Redis".
- *Closes when:* An owner ruling: accept job loss on head restart (and say so in `docs/DECISIONS.md`), or grant a scoped exception to the no-Redis rule for the Ray GCS store and wire it in the chart.

**CP-022 · `ray-kit` imports the heavy `ray` SDK solely for `JobSubmissionClient`, forcing `compute` onto its own 1536Mi tier**
`ray-kit, compute, chart` · low

- *Why open:* G2's deployment defect is fixed; this survives because it is load-bearing elsewhere. `compute` is the only fleet service importing the Ray SDK, was OOMKilled on the shared 512Mi tier, and now carries a 1536Mi limit (`chart/values.yaml:222-224`). `JobSubmissionClient` is itself a REST wrapper over `/api/jobs/`, so going httpx-only removes the fleet's sole Ray SDK dependency and that tier together.
- *Closes when:* Reimplement `ray-kit`'s job-submission client over httpx against `/api/jobs/`, drop the `ray` SDK dependency from `packages/ray-kit/pyproject.toml`, and revert `compute` to the shared memory tier in `chart/values.yaml`.

**CP-023 · `RAY_LOGGING_CONFIG_ENCODING` / `RAY_SERVE_LOG_ENCODING=JSON` are set on no cluster that runs, and no Serve replica line has been seen to land**
`external-kuberay` · low · **blocked:** the operators of the KubeRay cluster at dev-kuberay.ra.se

- *Why open:* Both env vars are rendered on the rask RayService, but that template renders for nobody, and the external cluster has neither. The belief that Serve replica logs are already collected is explicitly UNVERIFIED: zero rows from any Ray pod reached `opentelemetry_logs` in a measured 6h window (of 1,110,457 rows total), and the head had emitted 28 stdout lines in six days. That cluster's Serve app was idle, so it may have had nothing to log — but nobody has seen a Serve replica line arrive.
- *Closes when:* Add `RAY_LOGGING_CONFIG_ENCODING: JSON` and `RAY_SERVE_LOG_ENCODING: JSON` to the head container env and every worker group container (they must be set before `import ray`, which a container env satisfies by construction), then confirm a Serve replica exception appears in `opentelemetry_logs` with a populated `severity_text` and queryable deployment/replica fields. Do NOT use `RAY_LOG_TO_STDERR=1` as a shortcut — it stops Ray writing log files at all and breaks the driver-log reader `ray-kit` uses for `/api/ray/jobs/{id}/logs`; `RAY_BACKEND_LOG_JSON=1` converts only the Job Supervisor.

**CP-024 · Confirm `ray_gcs_*` survives token auth before any GCS alert rule is written**
`external-kuberay, chart` · low · **blocked:** needs the §1 scrape applied, or an auth-enabled cluster head to curl

- *Why open:* `chart/values-prod.yaml:132-134` sets `ray.auth.enabled=true`, and ray-project/ray#59361 reports token auth plus the OTel metrics backend dropping the entire `ray_gcs_*` / `ray_object_store_*` family with "Authentication required but no authorization header provided". Unverified against this estate, so any GCS alert written now could be a green gate over nothing.
- *Closes when:* Curl the head's `:8080/metrics` in-cluster with auth enabled and confirm `ray_gcs_update_resource_usage_time_bucket` is present; only then add GCS rules to `chart/alerting/rules.yml`.

### BYO workflow engine

_Dapr Workflow currently runs the cascade with no versioning seam, a status metric that lies, and an operator door that answers for the wrong instance — and the seams a bring-your-own engine would plug into (submit, outcome, plan) do not exist on any service._

**CP-025 · Dapr Workflow has no versioning seam — two replay divergences already shipped and "drain before deploying" is the only safe answer**
`medallion, ingest` · **HIGH** · **blocked:** §K — the Dapr retreat, sequenced after §A–§D

- *Why open:* Marked High in §O2 and untouched. Any in-flight instance replays against new code on deploy, and the estate has already shipped two replay divergences. §K sequences the retreat off Dapr Workflow, so this is the standing cost of staying meanwhile.
- *Closes when:* Either a version-pinning seam for the medallion/ingest workflow definitions (versioned workflow names plus a drain gate in the deploy), or the BYO-engine cutover §K puts last.

**CP-026 · `GET/POST /stage-runners/{stage runner}/stages/{instance_id}` ignores both path parameters, so the wrong stage runner answers**
`medallion` · med

- *Why open:* Classed silently-weaker: an operator asking about one instance can be answered by a different stage runner, so terminate and status act on the wrong workflow — the worst direction for an operator door to be wrong in.
- *Closes when:* Resolve and verify `{stage runner}` + `{instance_id}` before answering or forwarding in the `stage_runner_ops` router (`services/medallion/src/medallion/…/stage_runner_ops.py`), refusing a mismatch with a 404/409 rather than serving it.

**CP-027 · The workflow status metric reports SUCCESS on a dying path**
`medallion` · med

- *Why open:* Listed Medium in §O2 and untouched — a control that reports success while the workflow is failing makes the metric unusable as a gate, and anything built on it (an alert, a dashboard, a promotion decision) inherits the lie.
- *Closes when:* Emit the workflow status metric from the terminal state the engine actually records, and pin it RED with a test that drives the dying path.

**CP-028 · 1,367 orphan rows in `daprstate` with no TTL and no alert**
`medallion, notifications, chart` · med

- *Why open:* Measured and unaddressed: the Dapr state store accumulates rows nothing removes, and nothing pages when it grows.
- *Closes when:* Set a TTL on the Dapr state store component (or add a prune), and add a vmalert rule in `chart/alerting/rules.yml` on `daprstate` row growth.

**CP-029 · `compute` is an introspection shell — no submit door with vended credentials, no idempotent outcome door, no plan document on a control lane**
`compute, medallion` · med

- *Why open:* None of the three BYO seams exists on any service. `submit_or_reattach` exists only as library code called in-process by the medallion, so nothing outside the medallion can submit work, reattach to a running job, or read a plan — the compute service exposes only Ray dashboard introspection (`/api/ray/*`) and the `/api/serve/*` proxy.
- *Closes when:* Build the two BYO artefacts specified in `lakehouse-analysis.md` §11 D and expose them on compute's management API: a submit door that vends credentials via the catalog's `POST /v1/table/{id}/credentials`, and an idempotent outcome door backed by `submit_or_reattach`, with the plan document published on a control lane.

**CP-030 · The `Transform` CRD is deferred to `rask-operator`, so a lane declaration cannot live in git as a CR with the catalog record as a projection**
`chart, catalog, medallion` · med · **blocked:** `rask-operator` existing

- *Why open:* Deferred, not abandoned: a CRD without its controller renders unreconciled CRs as objects stuck mid-provision (verified live, `open_estate-verification.md` row 21), so it must not ship in this chart.
- *Closes when:* Ship the CRD together with its controller in `rask-operator` (DECISIONS.md "The compute plane is decoupled" §7.4 step 5), not in this chart.

**CP-031 · A stage runner row still carries `stageJob` / `ray_entrypoint` / `ray_job_params` beside the declaration that supersedes them, with `engine_choice` arbitrating**
`medallion, catalog, chart` · med · **blocked:** the Transform CRD row above — it needs `rask-operator` first

- *Why open:* Two sources of truth for what a lane runs, arbitrated at runtime. Not removable before there is a seeding path for lane declarations — without one the default deploy could run no cascade at all. It dies with the Transform CRD row above.
- *Closes when:* Build a seeding path for lane declarations, then delete `stageJob`, `ray_entrypoint`, `ray_job_params` and `engine_choice` from the stage runner row.


---

## PHASE 3 · CONTROLPLANE

**controlplane · gateway · notifications.** Last. Do not start these while phase 1 is open.

### gateway (edge / Gateway API)

_Every browser and SSR call reaches the fleet through this one edge, so what it fails to guard is exposed estate-wide today, and the planned move to Gateway API drops four properties (token guard, sentry mTLS, Dapr resiliency, the no-cluster dev origin) that nothing has yet replaced._

**CTL-001 · The edge exposes sidecar-only paths — invert lineage_sidecar_guard's blocklist to a per-row allowlist**
`gateway, chart` · **HIGH**

- *Why open:* gateway/__init__.py:517 blocks only lineage-events and lineage-reconcile-cron, so the root rewrites still expose /api/lineage/lineage-dlq, /api/catalog/control-events, both /dapr/subscribe, /ui/* and /demo/*, and with APP_API_TOKEN unset the route guards no-op. The allowlist is required of nginx, kgateway and the Python gateway alike, so it waits on neither phase; only its HTTPRoute form rides on gateway/P2-dapr-token.
- *Closes when:* Replace the blocklist with a per-row allowlist in gateway/__init__.py — catalog /v1/*; lineage /runs, /events, /v1/* — so anything unlisted 404s at the edge, with a test covering the currently-exposed paths; when the HTTPRoutes land, express the same allowlist there (no /bronze-arrival, no /dapr/subscribe, no sidecar-only lineage routes), delete lineage_sidecar_guard, and prove the sidecar paths 404 at the edge while still reaching the service from its sidecar.

**CTL-002 · Decide how the north-south path authenticates once the edge calls Services directly instead of via dapr-api-token**
`gateway, service-kit, chart` · **HIGH** · **blocked:** Owner decision — how the north-south path authenticates once Dapr service invocation leaves it; the plan states Phase 2 cannot proceed until this is decided.

- *Why open:* service_kit/governed/dapr_auth.py rejects any request whose token does not match the app's APP_API_TOKEN, and an edge->Service backendRef sends no such header, so the guard either rejects every call or stops guarding. Phase 2's structural-allowlist argument rests on this guard, so no /api row can move until the answer is chosen.
- *Closes when:* Record the owner decision in the plan — keep dapr-api-token and have the edge mint it, replace it with a different edge-injected credential, or drop it and re-argue the allowlist — then make the matching change in service_kit/governed/dapr_auth.py and the chart.

**CTL-003 · Nothing replaces the dapr-sentry mTLS that an edge->Service backendRef drops**
`gateway, chart` · **HIGH** · **blocked:** gateway/P2-dapr-token; gateway/P1-edge browser-proven

- *Why open:* Today's chain is browser -> Ingress -> rask-gateway:8888 -> the gateway's own daprd:3500 -> service (gateway/__init__.py:155-157), so caller->service is mTLS issued by dapr-sentry. An HTTPRoute backendRef straight to a Service is plaintext, and the Phase 2 table does not name this.
- *Closes when:* Choose and wire the replacement for in-cluster transport security on the edge->service hop — mesh/Envoy TLS origination, or an explicit accepted-plaintext ruling written into the plan — before any /api row moves off the Python gateway.

**CTL-004 · Dapr invocation resiliency (retries, timeouts, invokeBreaker) leaves with the gateway's sidecar and has no edge equivalent**
`gateway, chart` · **HIGH** · **blocked:** gateway/P2-dapr-token; gateway/P1-edge browser-proven

- *Why open:* chart/templates/dapr-resiliency.yaml:60-62 scopes retries, timeouts and circuit breakers to the gateway app-id and every app-id it routes to; three breakers were observed latched, so they are load-bearing rather than theoretical, and removing Dapr from the north-south path removes them.
- *Closes when:* Express the equivalent retry/timeout/outlier-detection policy on the kgateway HTTPRoutes (or a kgateway policy CRD) for each absorbed backend, and update chart/templates/dapr-resiliency.yaml to drop the now-dead gateway scope.

**CTL-005 · `make dev-micro` loses its stable /api origin when the gateway dissolves — derive a dev proxy from the chart's HTTPRoutes**
`gateway, chart, scripts` · **HIGH** · **blocked:** gateway/P2-routes

- *Why open:* HTTPRoutes exist only in a cluster, while the no-k8s dev loop (the fleet as plain processes behind one stable /api origin, scripts/dev-micro.sh) is a hard requirement. Nothing has been built, and this blocker shapes all of Phase 2.
- *Closes when:* Build a thin dev-only proxy whose route table is RENDERED from the chart's HTTPRoutes (never a second hand-kept table), wire it into scripts/dev-micro.sh in place of rask-gateway:8888, and add a contract test that fails when the rendered table and the chart's HTTPRoutes disagree.

**CTL-006 · The edge has no body cap, rate limit, X-Forwarded handling, access log or coded errors**
`gateway, service-kit` · med

- *Why open:* The gateway builds its own FastAPI and runs neither service_kit.middleware.register_middleware nor a body cap, rate limit, access line or coded 404/502 (gateway/__init__.py:280,332,341,347). RequestIDMiddleware is already mounted (line 513), so that half alone is done.
- *Closes when:* Add streaming body-size middleware, a token bucket per subject/IP, stripping of inbound X-Forwarded-* with injection at the edge, one structured access line per request, and problem+json carrying a `code` for 404/413/429/502 — added through service_kit.middleware.register_middleware rather than per route.

**CTL-007 · No Gateway/HTTPRoute exists — install kgateway behind a values toggle and render the edge one rule per future backend (nginx stays default)**
`gateway, chart` · med

- *Why open:* chart/templates/ingress.yaml is still the only edge template and grep finds gateway.networking.k8s.io in chart/ only in _helpers.tpl and vendored CNPG CRDs, while chart/values-prod.yaml:167 still calls kgateway 'the intended future edge'. Phase 1 is untouched, and writing a single /api rule now forces a restructure in Phase 2 instead of just adding backendRefs.
- *Closes when:* Add a kgateway toggle to chart/values.yaml on the cnpg.enabled pattern (toggle gates operator AND resources, nginx default); render Gateway + HTTPRoute with routing identical to chart/templates/ingress.yaml — /api -> rask-gateway:8888, /<zone> -> rask-web-<zone>:3000 specific-first, / -> home last, NO path rewriting — emitting one rule per future backend service (catalog, lineage, produce, train, explorer, ray/serve, projects) rather than one /api rule; prove both edges with `helm template` in each toggle position and `make k3s-up` green with the toggle ON plus a real browser reaching /, /lakehouse, /compute and /api/catalog; leave OpenFGA ClusterIP-only and the rask-gateway Deployment untouched; commit chart/ paths only.

**CTL-008 · kgateway has no equivalent of nginx's 3600s proxy-read-timeout, so query.live streams would be cut**
`gateway, chart` · med · **blocked:** gateway/P1-edge

- *Why open:* chart/values.yaml:975 sets nginx proxy-read-timeout: 3600 because every zone holds query.live streams open (bell, admin console feed, FGA live canvas); the kgateway equivalent does not exist yet and Envoy's default counts stream silence as death.
- *Closes when:* Set the HTTPRoute timeout / kgateway policy equivalent of proxy-read-timeout: 3600, then watch a notification-bell query.live stream stay connected for more than 90s through the new edge (not just `kubectl get gateway`).

**CTL-009 · Move longest-prefix /api routing out of gateway/__init__.py::_routes() into per-service HTTPRoute rules**
`gateway, chart` · med · **blocked:** gateway/P1-edge browser-proven; gateway/P2-dapr-token

- *Why open:* services/gateway still exists and still owns the routing table; Gateway API is specific-first natively, so each row becomes a backendRef once Phase 1 has shaped the routes.
- *Closes when:* For each row in gateway/__init__.py::_routes(), add the matching HTTPRoute rule + backendRef in chart/, and delete the row from the Python table.

**CTL-010 · Zones reach the gateway server-side through two different env vars (RASK_GATEWAY_URL vs LANCE_GATEWAY_URL)**
`gateway, frontend` · med · **blocked:** gateway/P2-routes

- *Why open:* compute/studio/models read RASK_GATEWAY_URL while home/lakehouse read LANCE_GATEWAY_URL; post-dissolution both must point at services directly (as the admin plane already does with CATALOG_API) or at the Gateway's in-cluster address, and unifying the split is explicitly in Phase 2's scope.
- *Closes when:* One SSR base-URL env var across all seven zones' server-side fetches, set in chart/templates/frontends.yaml and in the zones' server code, with .claude/skills/rask-frontend updated to drop the two-var gotcha.

**CTL-011 · Deleting the gateway removes the north-south OTLP span the Perses 'Fleet — RED' panels read**
`gateway, chart` · med · **blocked:** gateway/P2-routes

- *Why open:* services/gateway emits OTLP spans today via service_kit.setup_otel; removing it removes the north-south span and the RED panels that read it unless kgateway/Envoy access logs and metrics are piped in first.
- *Closes when:* Wire kgateway/Envoy access logs and metrics into the collector->GreptimeDB pipe, then confirm the Fleet — RED dashboard in chart/templates/perses-dashboards.yaml still shows request rate, errors and duration for the north-south path after the gateway is gone.

**CTL-012 · Envoy path-normalization parity with _normalize_path (merge_slashes, `..` segments) is assumed, not checked**
`gateway, chart` · med · **blocked:** gateway/P2-routes

- *Why open:* Hop-by-hop stripping, streaming and normalization move to Envoy natively, but the plan requires parity to be proven rather than assumed — merge_slashes and dot-segment handling in particular.
- *Closes when:* A test (or a documented comparison) showing Envoy's merge_slashes/path-normalization settings on the new edge produce the same paths as gateway/__init__.py::_normalize_path for slashes and `..` segments, with the chosen Envoy settings pinned in the chart.

**CTL-013 · services/gateway, its dockerfile, chart Deployment and dev-micro.sh entry all still exist**
`gateway, chart, scripts` · med · **blocked:** all other Phase 2 items

- *Why open:* services/gateway/ (pyproject.toml, src, tests) is still present along with its .docker image, chart Deployment and dev-micro.sh process, so the Phase 2 exit criteria are unmet.
- *Closes when:* Remove services/gateway, .docker/gateway.dockerfile, the chart's gateway Deployment/Service/resiliency scope and the dev-micro.sh entry; verify every zone's /api/* works in-cluster through the edge AND through the derived proxy in `make dev-micro`; update docs/architecture/system-overview.md, docs/architecture/deployment.md and .claude/skills/rask-services-fleet in the same commits; delete open_gateway.md.

**CTL-014 · The Dapr helper comments' "routes become HTTPRoutes" note is neither acted on nor deferred**
`gateway, chart` · low · **blocked:** gateway/P1-edge

- *Why open:* Phase 1 condition 4 requires that note to be either honoured or deferred with a comment written at the site; neither has happened.
- *Closes when:* Update the Dapr helper comments in chart/templates/_helpers.tpl (and the dapr-annotation sites they serve) to reflect Gateway API, or leave an explicit deferral comment at the site saying why it stays as-is.

**CTL-015 · Name the 502-with-detail -> Envoy 503 change for unreachable upstreams**
`gateway` · low · **blocked:** gateway/P2-routes

- *Why open:* The Python gateway returns 502-with-detail on an unreachable upstream while Envoy returns 503; frontends branch on ok/not-ok so it is cosmetic, but the plan requires the change to be NAMED rather than dropped silently.
- *Closes when:* Record the status-code change in docs/architecture/system-overview.md (or deployment.md) and in .claude/skills/rask-services-fleet, and check that no frontend or client asserts on 502 specifically.

**CTL-016 · Decide the fate of the merged /docs and fleet-wide openapi.json**
`gateway` · low

- *Why open:* gateway/__init__.py:615-632 still fetches each upstream's openapi.json and serves a merged Swagger UI, and nothing else can host it once the gateway dissolves; the plan requires an explicit decision rather than a silent drop.
- *Closes when:* Either re-home the merged /docs + openapi.json aggregation onto one service endpoint, or retire it with the decision recorded in the plan/commit, then delete the aggregation code from gateway/__init__.py.

### controlplane

_The controlplane owns the Project boundary the home zone renders and the CRD-shaped contract the 2026-08-16 "no CRD without its controller" ruling constrains, so an unenforced invariant or an untyped status is what lets that boundary drift unnoticed._

**CTL-017 · The "no CRD without its controller" ruling is enforced by nothing but the absence of a file**
`chart, controlplane` · med

- *Why open:* Verified 2026-09-10: the only CRD-aware scan in tests/unit/test_invariants.py (~line 1678-1681) SKIPS documents whose kind is CustomResourceDefinition, so a templated Project CRD would render unnoticed. grep over chart/ finds platform.rask.io only in templates/controlplane.yaml RBAC — the invariant holds by accident.
- *Closes when:* Add a test to tests/unit/test_invariants.py that renders the chart and asserts no rendered document has kind: CustomResourceDefinition with an apiGroup/spec.group of platform.rask.io, leaving the RBAC apiGroups reference in chart/templates/controlplane.yaml allowed.

**CTL-018 · controlplane ProjectStatus carries only `phase` and `namespace` — no conditions[], observedGeneration or catalogProjectId**
`controlplane, home` · low · **blocked:** Owner decision (C-Q3): which fields of the controlplane Project DTO freeze once conditions[] exists, and the matching home-zone render contract.

- *Why open:* Verified 2026-09-10 in services/controlplane/src/controlplane/schemas.py: ProjectStatus has exactly `phase: str = ""` and `namespace: str = ""`, so `Pending` covers both "no status yet" and "status with an empty phase" and nothing separates "not yet reconciled" from "reconciled and failed". The change cannot be finished until the owner names which DTO fields freeze, because `phase` is what the home zone renders.
- *Closes when:* Get the owner ruling on the frozen fields, then add to ProjectStatus a typed conditions[] carrying observedGeneration plus catalogProjectId and namespace as external facts — additive, keeping `phase` for the home-zone render fed by services/controlplane/src/controlplane/service.py:186-197.

**CTL-019 · No managed surfaces for roles and identities over the FGA model**
`controlplane, catalog` · low · **blocked:** Owner ruling that the estate goes long-lived shared

- *Why open:* Section I item 7 marks it CONDITIONAL — it is only wanted if the estate becomes long-lived and shared, and that call has not been made.
- *Closes when:* An owner ruling that the estate is long-lived/shared, then build role and identity management surfaces over the FGA model.

**CTL-020 · The models registry has no MLflow-parity feature set**
`controlplane, models zone, catalog` · low · **blocked:** C2 (the product-works pass), then an owner decision naming the MLflow capabilities to match

- *Why open:* Owner ruling deprioritized it until AFTER the product pass, and the product pass (C2) has not run, so the gate has not opened.
- *Closes when:* Run C2 (the product-works pass), then get an owner decision naming which MLflow capabilities the models plane must match before any is built.

### notifications

_This is the estate's targeted inbox — its credential, its per-subject state and its skill contract decide whether a person is told about their own work and whether the state holding that answer can ever be erased._

**CTL-021 · notifications cannot hold a dedicated service credential — daprd overwrites dapr-api-token on service invocation**
`notifications, lineage` · med · **blocked:** Owner decision — move the reconciler's lineage call off Dapr service invocation, or accept that sidecar-invoked hops authenticate as the estate

- *Why open:* Three of four dedicated credentials are proven on the wire (service-web bf273f07; service-maintenance and service-ingest 9405b732, where ingest's own token answers 200 at lineage while the same name with the shared bearer answers 401). The fourth landed and was REVERTED live 2026-09-07 (release 107 -> 108): the client half is correct yet the reconciler still 401'd with `the presented credential may not claim 'notifications'`, because it reaches lineage through DAPR SERVICE INVOCATION and the sidecar overwrites the token — ingest works only because it calls lineage directly over HTTP.
- *Closes when:* Take the owner ruling, then either move the notifications reconciler's lineage call off Dapr service invocation onto direct HTTP (matching ingest) so its own credential survives, or record sidecar-invoked hops authenticating as the estate as the boundary; then re-drive both directions at lineage's service door exactly as ingest's was.

**CTL-022 · No subject-erasure door, no TTL on watches/prefs/cursor, and no reverse index for a subject**
`notifications` · med

- *Why open:* watch_actor.py:1-7, models.py:322-335 and inbox_actor.py:444-446 hold per-subject state that nothing sweeps or expires, and there is no way to enumerate what a given subject holds. Unlike G2/G3 this row was never struck by the lakehouse-first ruling, but it is controlplane, which the owner ordered LAST.
- *Closes when:* Add a delete-subject door in services/notifications that sweeps inbox, prefs, watches and the sent ledger for one subject; TTLs on the watch/prefs/cursor state; and the reverse index (WatchIndexActor) that makes the sweep enumerable.

**CTL-023 · Sidecar proxies return bare 500s and block per call instead of reusing a lifespan-built proxy**
`notifications` · low

- *Why open:* proxies.py:98-119 maps no transport error to a problem body, so a sidecar failure surfaces as an opaque 500, and each call waits on the sidecar rather than using a proxy built once.
- *Closes when:* In services/notifications/.../proxies.py:98-119, map transport errors to 503 problem+json bodies and build one proxy factory in the service lifespan instead of per call.

**CTL-024 · .claude/skills/rask-notifications/SKILL.md contradicts the code in eight named places**
`notifications` · low

- *Why open:* It was listed as a rider on G6 with the instruction 'fix the skill in the same commit as G1'; G1 closed 2026-09-09 and nothing records the skill being corrected, so all eight contradictions stand.
- *Closes when:* Edit .claude/skills/rask-notifications/SKILL.md against services/notifications for the eight items — reason count, line refs, lease_expired, the feed grant, render-on-control-rows, the delivery membership check, named_subjects, the missing WatchIndexActor — and record the GET /events/projection / can_observe_events rung that G1 added.


---

## FRONTEND — opportunistic only

Fix in the same change as the service it pairs with. Never a frontend-only campaign.

### Pairs with the lakehouse (phase 1)

_The lakehouse plane's backend doors (lineage's governed event feed, the catalog's undrop/trash and maintenance-policy APIs, the explorer viewer's atlas) all shipped, but the surfaces that reach them are missing, hand-rolled or wasteful — so governed data is unreachable, stale, or moving hundreds of MB per hour._

**FE-001 · No in-tab browser memo for `/api/atlas/points` — 6,679,228 bytes refetched on every mount and every Text/Visual toggle**
`viewer, explorer` · **HIGH**

- *Why open:* Measured as the biggest waste in the estate: 25.5 MiB in ~30 s of ordinary clicking, which OOM-killed the viewer and took the plane to 502. Named as the highest user-visible gain per unit of work, needing no infra, chart or backend change — it is purely unwritten on the client side.
- *Closes when:* Memoize the atlas projection, the content-addressed thumbnails and the descriptor in the BROWSER, keyed on the `v=6` token already in the URL so invalidation is free — wire it at `frontend/microfrontends/explorer/src/lib/atlas/mount-atlas.svelte` / `AtlasMap.svelte` over the existing `frontend/packages/explorer-api/src/memo.ts`. Note the server half already exists (`explorer-api/src/server-cache.ts` + `explorer/src/lib/server/atlas-points.ts`), so scope is the client half only; the item's `/media/api/...` path is stale — the media zone is gone, the route is the explorer zone's.

**FE-002 · 13 hand-rolled poll/loading/401/offline fetch triples instead of SvelteKit `load`/`query` — two lineage pages keep rendering governed rows after the session dies**
`lineage, lakehouse-zone` · med

- *Why open:* SvelteKit data `load` is deployed but used by exactly one page. The four-way drift across the 13 hand-rolled triples is a correctness bug, not just duplication: two lineage pages leave governed rows on screen once the session has ended.
- *Closes when:* Replace the 13 hand-rolled poll/loading/401/offline triples in the lakehouse zone with data `load` functions plus `query`, so 401 handling is uniform (the `ApiResult` status pattern the lineage client already returns) and the two lineage pages drop governed rows post-session.

**FE-003 · Nothing subscribes to lineage's per-subject `GET /events` keyset cursor — 8 of 13 lakehouse pollers still poll and four admin surfaces make zero requests ever**
`lineage, lakehouse-zone` · med · **blocked:** The Traefik proxy-read-timeout item in this group (recorded as step 0) — the k3s edge has no measured guard for a long-lived stream, so live feeds should not be relied on until it does.

- *Why open:* The TypeScript client already implements the `after` cursor (`frontend/packages/api/src/lineage/client.ts:117-126`) and no caller passes it; the feed is already per-subject governed (an event shows only if the caller `can_get_metadata` on every referenced dataset), so no backend and no Dapr change is needed. The earlier 'blocked on event scoping' claim was true of the catalog control feed only.
- *Closes when:* Point `admin.remote.ts` at lineage's `GET /events` with the `after` cursor and add one `query.live` per feed, deleting the remaining `setInterval` timers and giving the four dead admin surfaces a data source.

**FE-004 · A lineage jobs render does not pass `summary: true` — 464,318 bytes per render instead of 46,980**
`lineage, lakehouse-zone` · med

- *Why open:* A measured 10x payload reduction on a page moving 528 MB/hour to render one job; the flag is already passed one file over, so this is a one-line omission. Caveat before working it: both current lakehouse `fetchEvents` call sites already pass it (`src/lib/lineage/store.svelte.ts:124` and `routes/lineage/jobs/[...job]/+page.svelte:51`), so confirm which render is still unflagged — it may already be closed.
- *Closes when:* Find the remaining `fetchEvents(...)` without `summary: true` under `frontend/microfrontends/lakehouse/src/routes/lineage/` and pass it, matching `+page.svelte:51`; if none remains, strike the row as closed and record the measurement.

**FE-005 · No UI reaches `undrop` on either rung — neither `POST /v1/table/{id}/undrop` nor the plural `POST /v1/namespace/{id}/undrop`**
`catalog, lakehouse-zone` · med

- *Why open:* #96 closed 2026-08-05 with the whole detach/undrop/trash-record backend driven live over HTTP, but the recovery path is API-only: the trash deadline (`GET /v1/namespace/{id}/tasks`) and the undrop action are unreachable from the lakehouse zone, so a dropped table can only be recovered with curl.
- *Closes when:* Add trash/undrop surfaces to the lakehouse zone for both rungs — list trashed objects with their deadline from `GET /v1/{table,namespace}/{id}/tasks` and call the two undrop doors — done alongside the catalog service.

**FE-006 · The project-scoped maintenance policy has an API but no UI**
`catalog, lakehouse-zone` · med

- *Why open:* Section I ranks it open under Maintenance and records that the API shipped — only the surface is missing, so a project's maintenance policy can be set by HTTP call and by nothing else.
- *Closes when:* Build the project-scoped policy screen in the lakehouse zone over the shipped `set_project_policy` API, done alongside the catalog service.

**FE-007 · The chart ships only the ingress-nginx proxy-read-timeout annotation, inert on k3s's Traefik — the zones' live SSE bell has no edge-level guard there**
`chart, gateway` · low

- *Why open:* Verified 2026-09-10: `chart/values.yaml:2235` sets `nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"` and `chart/templates/ingress.yaml:22-24` states outright that k3s ships Traefik and 'an unknown annotation is inert to a controller that does not own it'. The 269.6 s / 0-severed proof was measured against nginx; no equivalent was ever measured on Traefik. Mitigated but not closed by the app-level 20 s keepalive in the runs feed.
- *Closes when:* Add the Traefik equivalent (a `ServersTransport` / `traefik.ingress.kubernetes.io/*` annotation, or `respondingTimeouts`) to `chart/values.yaml` ingress.annotations, then run `HOLD_S=270 node scripts/verify_live_stream_timeout.mjs` against the k3s ingress and record the result.

**FE-008 · Six mutation sites still do a trailing `await load()` after every write instead of `form` + single-flight + `withOverride`**
`lineage, viewer` · low

- *Why open:* SvelteKit's `command`/`form` invalidate dependent queries and return refreshed data in the same round trip, so the extra round trip after each write is pure waste; none of the six sites has been converted.
- *Closes when:* Convert the six mutation sites to `form` + single-flight + `withOverride`, deleting the trailing `await load()` at each — sequence this after the `load`/`query` adoption item above, which supplies the queries these writes invalidate.

**FE-009 · The lakehouse zone's `/governance` route was removed and nothing decided whether the catalog layer should have one**
`lakehouse-zone, home-zone, catalog` · low · **blocked:** Owner ruling: does a catalog-scoped governance page return to the lakehouse zone, or does `home/settings/access` stay the single governance surface? The history trace is unblocked and is step one.

- *Why open:* Unrated in the register and never audited: confirmed removed — `frontend/microfrontends/lakehouse/src/routes/` has no `governance` directory and `git ls-files` tracks none (only stale `.svelte-kit/types` build residue names it). The surviving surface is `home`'s `/settings/access`, the estate-admin-gated raw tuple editor. What was removed, from where and why was never traced.
- *Closes when:* Trace the removal (`git log --diff-filter=D -- frontend/microfrontends/lakehouse/src/routes/governance`), then implement whichever the owner rules; record the ruling in `docs/DECISIONS.md`.

### Pairs with the controlplane (phase 3)

_Both rows are information-architecture questions about the project/estate hierarchy the controlplane serves — the routes cannot be built until the owner says what they are for, so they sit as decisions, not code._

**FE-010 · Nobody has decided what `/projects/<id>` is FOR, or whether the hierarchy graph belongs on it**
`home-zone, lakehouse-zone, catalog` · low · **blocked:** Owner IA ruling naming what `/projects/<id>` shows, and whether the project > warehouse > namespace > table hierarchy graph lives there.

- *Why open:* Section I item 7 lists both under 'IA (need an owner ruling, not code)'; the route exists in the IA but its purpose and contents are undecided, so it cannot be built.
- *Closes when:* Take the ruling, then implement the route in the owning zone over `/api/projects`.

**FE-011 · There is no Estate Settings zone — estate-level settings have no home in the UI**
`home-zone, controlplane` · low · **blocked:** Owner IA ruling: is Estate Settings its own zone, or a section under an existing zone (home)?

- *Why open:* Section I item 7 lists it under 'IA (need an owner ruling, not code)' — the shape of an Estate Settings surface has not been decided, so no zone owns it.
- *Closes when:* Take the ruling, then build the routes in whichever zone it lands in (adding an eighth zone means a `frontend/microfrontends/<zone>` with its own `package.json` plus a `microfrontends.json` row).

### Standalone zone work

_Cross-zone frontend plumbing that pairs with no single backend phase — one blocks a second zone from reusing a built surface, the other makes the same `/api/*` call succeed in one zone and fail in another._

**FE-012 · Kill the zones' VIEWER_BACKEND :8888 vs LANCE_BACKEND :8001 dev-proxy split**
`frontend` · med · **blocked:** gateway/P2-devproxy — the single derived dev-proxy origin must land first.

- *Why open:* Verified live in the vite configs: `home/vite.config.ts:5` and `lakehouse/vite.config.ts:5` proxy `^/api(/.*)?$` to `LANCE_BACKEND` (`http://localhost:8001`, which `scripts/dev-micro.sh` does not start), while `compute`, `models` and `studio` proxy to `VIEWER_BACKEND` (`http://localhost:8888`, the gateway); `explorer` and `annotator` have no `/api` proxy at all. The same `/api` call therefore works in one zone and fails in another.
- *Closes when:* Point every zone's vite dev `/api` proxy at the one derived dev-proxy origin (delete `LANCE_BACKEND` from `home/vite.config.ts` and `lakehouse/vite.config.ts`), and update the `rask-services-fleet` + `rask-frontend` skills and the CLAUDE.md conventions bullet that document the split.

**FE-013 · The WebGPU atlas is still zone-local in explorer instead of hoisted to `frontend/packages/atlas`**
`explorer, annotator` · low

- *Why open:* Verified: `frontend/microfrontends/explorer/src/lib/atlas/` holds AtlasMap, gpu-scatter, cross-filter, legend/geometry/colors, and `frontend/packages/` has no `atlas` (api, config, dockview, engine, explorer-api, flow, labeling, media-api, ui, zone-contract). Until it hoists exactly as the pixi engine did, the annotator's bulk-labeling view cannot embed the same selection surface. Nothing technical blocks it — a day of careful extraction plus zone rewiring.
- *Closes when:* Create `frontend/packages/atlas` (zone-agnostic props, no `$app/*` imports, transport injected), move `explorer/src/lib/atlas/*` into it, re-point the explorer zone, and add the annotator's bulk-labeling embed.


---

## LOW PRIORITY — parked

**flows · search · viewer · annotator.** Recorded so nothing is lost. Do not work these.

### annotator (deprioritised)

_The whole annotation plane — auth, entities, FGA, publish schema, actors, HTTP surface, zone — is unbuilt above a live media-plane write path that keeps writing Lance; parked, but the live half keeps costing versions and keys ownership on a client-settable header._

**LOW-001 · services/annotator keys ownership on a client-settable `X-User` header — no OIDCVerifier, `get_author` defaults to "anon"**
`annotator, service-kit, catalog` · **HIGH** · **blocked:** owner decision — actors hosted in catalog vs an OIDCVerifier in annotator

- *Why open:* Every project/task/draft/assignment entity is keyed on who owns or claims it, and a client-settable header can answer for anyone. Flagged decide-BEFORE-S6 and still undecided, so every later slice would build on it.
- *Closes when:* Owner picks: (a) host `ProjectActor`/`TaskActor` in services/catalog (already has `CurrentToken` and `lance-statestore` scope), or (b) give services/annotator its own `OIDCVerifier`; then build against the verified subject, never `X-User` in `service_kit/media/deps.py::get_author`.

**LOW-002 · Consensus replicas mint unaddressable `{gid}-r{k}` task ids that wedge publish, and all N replicas share one per-task draft doc**
`annotator` · med

- *Why open:* Two seventh-wave findings recorded and never fixed: a client-chosen `task_id` with `consensus_n > 1` can exceed the task route length so that project's publish can never complete, and the draft is keyed by task rather than `(task, author)`, so replicas overwrite each other — the one thing consensus must not do.
- *Closes when:* Bound or hash the generated replica id in the send path so `{gid}-r{k}` always fits the task route, and key the draft document on `(task_id, author)`; RED-first, with the wedged-publish case as the failing test.

**LOW-003 · S10 — the media-plane Lance write path is still the annotator's live write: `annotations/{save,tags,versions}.py` + `commit.py:check_base_version_value`, and the canvas still writes it (615 versions / 9.8 MB for 3 rows)**
`annotator, explorer` · med · **blocked:** S7 and S8 proven live

- *Why open:* Half landed (a `?task=`-opened canvas snapshots saved rows into the task draft) but `annotations/save.py:55` still serves `POST /annotations/{doc_id}/{speech_id}/{chunk_id}`, so one state flip is still one dataset version + data file + manifest, and shapes drawn on the canvas do not travel into a publish. Sequenced last on purpose — deleting the write path before the replacement is driven is the same mistake reversed.
- *Closes when:* Point the canvas save at `PUT /tasks/{id}/draft`, run `frontend/microfrontends/annotator/drive2.tmp.mjs` (draw → draft → publish-with-shapes) against the cluster, then delete `annotations/save.py`, `annotations/tags.py`, `annotations/versions.py` and `check_base_version_value` (keeping the Arrow-IPC read); retires #99.

**LOW-004 · S1 — `services/annotator/projects/{__init__,schema,machine}.py`: the entities and the pure `apply(entity, event, *, actor, rungs, now)` do not exist**
`annotator` · med

- *Why open:* Unblocked, needs no store/chart/cluster, but nothing landed — the transition tables stay prose that every later endpoint would re-derive.
- *Closes when:* Write the Pydantic entities and `apply()` raising `IllegalTransition`(409)/`NotLeaseHolder`(403)/`SelfReview`(403), with `tests/unit/test_annotation_projects_machine.py` RED first: all 72 `TaskState × TaskEvent` pairs (14 legal + field effects, 58 illegal), the six §5.2 rules, and a subprocess guard that `common.lancekit` never enters `sys.modules`.

**LOW-005 · S2 — the `annotation_project` FGA type and `can_create_annotation_project` are absent from `service_kit/governed/auth/model.fga`**
`service-kit, annotator` · med

- *Why open:* Unblocked and store-free, but until it lands the publish two-door rule (`can_publish` + `can_create_table`, plus conditional `can_promote`) is ungradeable and any handler can get the privilege math wrong.
- *Closes when:* Add the §6.1 type, `fga model transform --file packages/service-kit/src/service_kit/governed/auth/model.fga`, and add `model.fga.yaml` cases (tenant member = viewer not annotator; annotator cannot `can_review`; reviewer can `can_annotate`; manager `can_publish`; foreign tenant resolves nothing); `tests/unit/test_fga_model_contract.py` is the RED.

**LOW-006 · S3 — `services/annotator/projects/publish.py`: `PUBLISHED_LABELS_SCHEMA` (34 columns) and `build_published_table()` do not exist**
`annotator` · med

- *Why open:* Unblocked and store-free, but unwritten, so §7.1's "deliberately absent" list (task state, assignee, leases, drafts, transitions) is a paragraph instead of an enforced schema a training consumer can rely on.
- *Closes when:* Write the schema + builder with `tests/unit/test_annotation_publish_table.py` RED first: 3+1 shapes and one skipped → exactly 5 rows; skipped = one row `shape_type=="none"`/`task_outcome=="skipped"`; empty project → 0 rows, schema-identical; `reviewed_by==""` never None; field set intersects none of `{state, assignee, lease_expires_at, revision, review_notes, transitions}`.

**LOW-007 · S6 — `ProjectActor` (project doc, claimable queue, tenant index) and `TaskActor` (task state + lease-expiry reminder) are unwritten**
`annotator` · med · **blocked:** the §10 verified-subject decision — do not build against `X-User`

- *Why open:* S5 landed (`lance-statestore` on AGE Postgres with `actorStateStore: "true"`, three consumers), so the fence moved to S6 — but without single-threaded-per-entity actors the queue index is a lost-update race and lease expiry needs a sweeper cron.
- *Closes when:* Implement both actors on `lance-statestore`, reading `packages/service-kit/src/service_kit/governed/user_state.py` first (key rules, fail-closed posture, the `||` separator trap); prove with two concurrent claims → one 200 + one 409, and a fired reminder returning the task to `unassigned` with its draft intact.

**LOW-008 · S7 — no HTTP surface for annotation projects/tasks/drafts behind the §6.1 FGA doors**
`annotator` · med · **blocked:** S6

- *Why open:* The first slice a human can drive, and none of it exists; it gates S9 (zone) and S10 (the deletions).
- *Closes when:* Add project/task/draft endpoints in `services/annotator/projects/` behind the `annotation_project` doors, with S1's `apply()` as the only mutator and the draft etag as the only concurrency control.

**LOW-009 · S8 — the publish workflow (freeze → snapshot accepted drafts → Arrow → exists/create → tag → record → emit `annotation.project.published`) does not exist**
`annotator, catalog, lineage` · med · **blocked:** S3 and S4

- *Why open:* The annotator has zero Dapr references today; this is its first pub/sub usage and what would unblock `query.live` for #102 and give #125 a source. Must be idempotent because Dapr activities run at least once.
- *Closes when:* Build the durable Dapr workflow in `services/annotator/projects/`: `POST /{id}/exists` then create, compare `annotation.publish_id` to no-op on replay or fail on a different publish, reuse the id after `publish_failed`, tag `publish-<publish_id>`, emit the control event; prove by killing the worker mid-workflow and asserting one table and one tag.

**LOW-010 · `publish.py:435` stamps the PROJECT template instead of each task's captured one, invents `template_kind`, and publishes client-supplied shape provenance under a server-stamped comment**
`annotator` · low

- *Why open:* Three recorded-not-fixed seventh-wave findings still standing in live code; `modality`/`kind` are captured, stamped and displayed but cross-checked against nothing, so an `audio` template can be sent image items.
- *Closes when:* In `annotator/projects/publish.py` read each task's captured template when stamping table properties and the run facet, drop the invented `template_kind`, cross-check `modality`/`kind` against `MediaRef.kind` at send, and either server-stamp shape `source`/`model_version`/`confidence` or correct the comment.

**LOW-011 · Batch-labeling submit has no runner behind it — `runners.jobsUrl: ""` in every default deploy**
`annotator, chart, explorer` · low

- *Why open:* Verified: `chart/values.yaml:1789` is empty and `explorer.yaml:250` / `frontends.yaml:352` only set `MEDIA_JOBS_URL` when non-empty, so the submit path is a mock wherever the chart ships.
- *Closes when:* Point `runners.jobsUrl` at the compute service's Ray job door and replace the mocked batch submit with a real submission + status read, witnessed with one submitted batch job in-cluster.

**LOW-012 · The annotator's wire and state still say `project` where the owner ruled the unit of work is a labeling TASK**
`annotator, service-kit` · low

- *Why open:* Owner ruling 2026-07-31 made 'project' platform-level; layer 1 (UI language) shipped that day, layer 2 never did — the `annotation_project` FGA type, `AnnotationProjectActor`/`AnnotationTaskActor` ids, the `/projects` + `/tasks` paths and `can_create_annotation_project` are unchanged, so the annotator's grouping still collides with the estate tenant.
- *Closes when:* Run the rename as its own red-first slice with a state migration for existing actor ids and FGA tuples plus a `test_actor_proxy_names.py` update — not a find-and-replace.

**LOW-013 · S9 — the annotator zone still lands on the refused `DataSelection.svelte` dataset→document→chunk gallery**
`annotator` · low · **blocked:** S7

- *Why open:* The owner ruled the landing page is your projects and their progress with items arriving by being SENT from search/atlas/saved views; the zone implements the refused gallery, and `statusStyle.ts`'s four statuses disagree with the six `TaskState` values.
- *Closes when:* Replace `DataSelection.svelte` with a projects-and-progress landing, add send-to-project, make the canvas read/write drafts, map `statusStyle.ts` onto the six `TaskState` values, and turn `e2e/zone.spec.ts:131` into a projects-landing assertion (screenshots, looked at).

**LOW-014 · No character-span tool, no audio item has ever gone through the loop, no relation/reading-order tools**
`annotator, engine, labeling, explorer` · low

- *Why open:* Named as the honest residue of the seventh wave and untouched: W4's surfaces all exist (`AudioViewer`, waveform lane, `Shape.t_start/t_end`, `MediaRef.kind`) but no send surface emits audio items, so claim→submit→publish has never carried one.
- *Closes when:* Add `char_start`/`char_end` to `Shape` plus a span tool in `@rask/engine`; make the explorer send surfaces emit audio items and drive one end to end; add `relation` and `order` to the task template tool set with editors behind them.

**LOW-015 · `GET /projects/{project_id}/tasks` ignores `limit` and `cursor`**
`annotator` · low · **blocked:** owner decision — annotator is out of scope

- *Why open:* The register itself marks the row "(annotator, out of scope)" — recorded, not carried.
- *Closes when:* Honour `limit`/`cursor` on the annotator tasks door — only if the annotator re-enters scope.

**LOW-016 · Export serializers (COCO/YOLO/CSV/HF) and a managed label taxonomy are unscheduled, and the export half belongs to the P7c exporter service**
`annotator, exporter (P7c)` · low · **blocked:** owner decision — scheduling, plus the P7c exporter service existing

- *Why open:* Owner to schedule. Ruling R4 puts serialization in a separate microservice, so building COCO/YOLO/CSV/HF inside the annotator would build them twice.
- *Closes when:* Owner schedules it; then add the projection functions to the P7c exporter service (ALTO 4.4 first) and a managed label taxonomy in the annotator — no second export path.

### flows / studio (deprioritised)

_The flow plane runs, but its upload path buffers whole files in a rendering pod, its node library asserts callability it cannot verify, and the durable executor it was built for has never run a flow._

**LOW-017 · `proxyServeInfer` does `await request.arrayBuffer()`, buffering every uploaded byte in a SvelteKit pod sized for rendering HTML**
`flows, frontend, storage` · med

- *Why open:* `BODY_SIZE_LIMIT=32M` bounds the damage but not the shape, and it rules out audio and video entirely; the presigned lane the row names has never been built even though `PageLoaderActor` already does read-through properly.
- *Closes when:* Add a TTL scratch bucket, presign a PUT straight from the browser to RustFS (creds from the Dapr secret store, never env), and add an `objectRef` payload kind to `services/flows`.

**LOW-018 · The studio node library marks every Ray-dashboard-reported Serve app RUNNING, so an app with no ingress route appears callable**
`flows, compute, frontend` · med

- *Why open:* The library reads `/api/serve/applications/` through the compute service and the dashboard knows nothing about the ingress that decides reachability, so the badge asserts something it cannot verify.
- *Closes when:* Probe each Serve app's ingress from `services/flows` and mark the unreachable ones in the studio node library, or drop the RUNNING badge's implication of callability.

**LOW-019 · flows runs still take the inline executor; the Dapr Workflow path has never been observed end to end**
`flows` · low

- *Why open:* The sidecar holds the actor runtime (`lance-statestore` scoped to app-id `flows`), so the wiring is in place and unexercised.
- *Closes when:* Drive a flow through the Dapr Workflow executor against the cluster, observe the instance's state transitions end to end, then make it the default lane.

**LOW-020 · `services/flows` declares the node catalog and the studio zone still ships its own registry**
`flows, frontend` · low

- *Why open:* The seam exists and is unused, so two node vocabularies can drift apart silently.
- *Closes when:* Have the studio node library fetch the catalog from `services/flows` and delete the duplicate frontend registry.

**LOW-021 · No IIIF loader node, no bucket attach, and no streaming — a long run shows nothing until it completes**
`flows, frontend, storage` · low · **blocked:** the presigned-upload lane (uploads item above) for the upload half of bucket attach

- *Why open:* All three unimplemented: the IIIF node needs an allowlisted fetch proxy, bucket attach needs the presigned lane, and every run is one-shot — a CPU HTR page measured 66 s of silence, which reads as nothing happening.
- *Closes when:* Add an allowlisted IIIF fetch proxy plus a manifest→canvas→image node; wire bucket load over the governed `/api/explorer/object*` read and upload over the presigned lane; add SSE to `services/flows` with a per-node progress surface in the studio zone.

**LOW-022 · `https://dev-kuberay.ra.se/htr/transcribe` answers a bare `text/plain 404` — the Serve HTTP ingress exposes no `/htr` route**
`flows, compute` · low · **blocked:** owner decision / ops on the external dev-kuberay cluster

- *Why open:* The app is RUNNING 1/1 on a GPU replica and studio already calls it with `app=htr, path=/transcribe`; the vLLM apps got explicit routes and `htr` did not. Port 8000 on that host is closed, so the fix is an ingress change on an external cluster, not code in this repo.
- *Closes when:* Add the `/htr` prefix route to the dev-kuberay Serve ingress (as `dev-kuberay.ra.se/gemma-31b/v1` has), then re-request `POST /htr/transcribe` with raw image bytes and expect ALTO XML.

### search (deprioritised)

_Two frozen Lance tables from the dead search plane serve nobody, and the surviving search door drops parameters and binds to a single table._

**LOW-023 · D2b — no lines FTS surface at all; the governed lines table was never built**
`search, catalog, medallion, viewer` · med · **blocked:** D2c (the P7b gold wave)

- *Why open:* Verified dark — `grep 'lines' services/search/src services/viewer/src` returns nothing. P7a deleted the indexer and the R6/R20 wave deleted `search_api`, so the "existing indexed data keeps serving" clause ended and `s3://images-batch-search/lines` is a corpse.
- *Closes when:* Land a catalog-governed lines table (text/geometry/confidence as gold contract columns) in the P7b gold wave plus a `DatasetRegistry` descriptor, served at `/api/explorer/search?dataset=lines&mode=fts`, with thumb crops riding as a blob column — no raw-S3-key proxy.

**LOW-024 · D2d — the EAD `archive_catalog` Lance table is frozen and unserved, with no governed re-land**
`search, catalog, ingest` · med

- *Why open:* `scripts/index_catalog.py` and `make catalog-index` are deleted and nothing in search/viewer references `archive_catalog`, so the table at `s3://images-batch-search` is unreachable; only `scripts/harvest_ead.py` (download only) survives.
- *Closes when:* Write an ingest job that lands the EAD table THROUGH the catalog plus a dataset descriptor, so `/api/explorer/search?dataset=archive_catalog&mode=fts` serves it — the dynamic filterable params already cover `archive_code`/`date_*`.

**LOW-025 · `GET /api/search` (search :8102) silently ignores `dataset` and `mode`**
`search` · low

- *Why open:* Two dropped parameters on the search door — one reads from the wrong target (`dataset`), one silently answers weaker (`mode`). Unrated in the register; parked under the scope ruling.
- *Closes when:* Honour `dataset` and `mode` in `GET /api/search`, or refuse them 400.

**LOW-026 · Search binds to ONE declared `row_table` keyed by the identity triple — no table chooser, no non-identity joins, no external-pointer declaration**
`search, viewer, explorer` · low

- *Why open:* Named unfinished in the seventh wave and not revisited; the descriptor cannot express more than one row table or an external pointer.
- *Closes when:* Extend the dataset descriptor to declare multiple row tables plus external pointers, add a table chooser to the explorer search surface, and support a join key other than the identity triple.

### explorer / viewer (deprioritised)

_The corpus the deprioritised trio reads is a mounted volume rather than catalog-governed tables — exactly the coupling the lakehouse exists to avoid._

**LOW-027 · The media/explorer corpus is read off a mounted volume — `explorer.yaml:109-111` still derives `MEDIA_DB_ROOT`/`MEDIA_DB`/`MEDIA_DESCRIPTOR_DIR` from `explorer.corpusMountPath`**
`explorer, viewer, search, annotator, chart` · med · **blocked:** owner decision — PVC vs a rustfs corpus bucket, taken against the destination cluster (merge plan P4); explorer/viewer is explicitly deprioritised

- *Why open:* The hostPath half is fixed (`explorer.corpus.mode` defaults `emptyDir`, `pvc` named as prod, `explorer.yaml:263` records 'NO hostPath ships by DEFAULT'), so the volume branch of the decision was taken — but the portable half, #103 registering the corpus as catalog-governed project tables, was never built, so three services still read a volume, not the catalog.
- *Closes when:* Either land the corpus as catalog-registered project tables, re-point the `MEDIA_DB`/`MEDIA_DESCRIPTOR_DIR` readers in viewer/search/annotator at the catalog and drop `explorer.corpus.mode`; or record in `docs/DECISIONS.md` that `pvc` is the permanent answer and #103 is dropped.

### model registry (out of scope by the model/train ruling)

_The model registry has no rung of its own, so it borrows the `table` FGA type — the one lane the LANCE-ONLY ruling says must stay closed._

**LOW-028 · K-F9 — no generic/opaque asset rung; the model registry squats on the `table` FGA type**
`catalog` · low · **blocked:** owner ratification of the rung's shape

- *Why open:* Excluded by the model/Ray-train scope exclusion and constrained by the 2026-08-15 LANCE-ONLY ruling: if the rung lands it must be a governed opaque BLOB with no format tag, schema interpretation or data ops, because a second TABLE lane carrying a format tag is what that ruling forbids. The ruling constrains the shape; it does not authorize the rung.
- *Closes when:* Owner ratifies the rung as a governed opaque blob type (not scoped by naming any workload's file formats), then add a distinct FGA type for it so the model registry stops using `table`.
