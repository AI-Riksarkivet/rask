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

**210 open items**, deduped from 325 raw rows mined out of the seven files above. A further 98 rows
are CLOSED and still rendered — struck through, keeping the measurements that made them worth
opening — and are not counted here.

| Phase | Items | High |
| --- | --- | --- |
| **1 · Lakehouse** (catalog, lineage, medallion, maintenance) | 70 | 14 |
| **1 · Cross-cutting** (service-kit, storage, chart, build, tests) | 46 | 10 |
| **2 · Compute** (compute, ingest, ray-kit) | 29 | 6 |
| **3 · Controlplane** (controlplane, gateway, notifications) | 24 | 5 |
| **Frontend** (opportunistic) | 13 | 1 |
| **Low priority** (flows, search, viewer, annotator) | 28 | 1 |

Counts are re-derived by `tests/unit/test_the_backlog_counts_itself.py`, which counts OPEN rows and
checks the HIGH column too, so neither can drift from the rows below.

**7 of the 14 phase-1 lakehouse HIGH rows are decision-gated** (2026-09-17), leaving `LH-094`,
`LH-159`, `LH-172`, `LH-004`, `LH-018`, `LH-019` and `LH-137` workable. The denominator moved 13 -> 14
on 2026-09-17 when [[LH-137]] was REOPENED: it was closed on "the current stage-runner pod records 0
refusals", and driving a real publication — the thing its own closure said had not been done —
reproduced the refusal twice within a second of a silver write. Three rulings moved it the same day:
`LH-172`'s remedy was measured inert and the owner ruled "accept it for now" (a decision that CLOSES a
question rather than unblocking work), and **R1-R11 were acknowledged as standing**, which cleared
`LH-004`'s marker and makes the other eight R-citing rows workable as written. Two of those were then
picked up and their markers dropped on evidence rather than on the ruling alone: `LH-018` (R1 — the
`CREATE_TABLE_VERSION` emit shipped; replay marker and the `/commit` retirement remain) and `LH-019`
(R2 — the protection code decided ON THE SPEC, `spec.yaml:2431`, not on the owner tie the row assumed:
table 19, containers 3). Both stay OPEN with their remaining clauses named, because a row whose gate
lifts is not a row that closed. That number is the one worth watching: the column above says how much
is written down, and this says how much of the priority anyone can pick up without a ruling. It moved
here by measurement rather than by attrition — four rows that were decision-gated in their bodies
carried no `**blocked:**` marker, so the workable count read optimistic until they were marked. Gated
too, for the same reason the CLOSED count is: a progress number nobody re-derives is a claim.


## Owner rulings and the evidence behind them (2026-09-16)

**R1–R11 STAND — owner, 2026-09-16.** The nine rows citing them are workable as written, five of them
HIGH (`LH-004`, `LH-018`, `LH-019` among them). No re-litigation; the R-numbers are the contract.

**The FGA vocabulary: NARROW THE ROWS, not one edit — evidenced against Lakekeeper.** `LH-055`,
`LH-056` and `LH-058` ask `model.fga` to grow `branch` / `column` / `base` / `estate` types. Read the
reference implementation rather than deciding by taste: Lakekeeper — the Iceberg catalog whose
OpenFGA integration is the closest thing to a standard — declares exactly
`Role, Tag, Server, Project, Warehouse, Namespace, Table, View, GenericTable` (+ `User`), and
`crates/authz-openfga/src/relations.rs` contains **zero** occurrences of `column`, `branch`,
`base_path` or `storage_location`. A mature catalog authorizes at CONTAINER and OBJECT granularity
only. `estate` has a precedent there (`Server`); the other three do not.
**AND THAT RECOMMENDATION IS WITHDRAWN — it was derived from the wrong reference, which the owner
caught: "we are using lance, does it still apply according to the lance_docs".** It does not, and the
spec rask implements is the reason. `lance_docs/ns_catalog/spec.yaml` makes branches, tags and columns
FIRST-CLASS OPERATIONS — `CreateTableBranch`, `DeleteTableBranch`, `ListTableBranches`,
`CreateTableTag`/`DeleteTableTag`/`UpdateTableTag`/`GetTableTagVersion`/`ListTableTags`, and
`AlterTableAddColumns`/`AlterColumns`/`DropColumns`/`BackfillColumns`. Iceberg has no such doors, which
is precisely why Lakekeeper's FGA needs no such types: **it is not authorizing operations it does not
have.** rask has the doors. A door whose granularity the authorization model cannot express is
authorized on something coarser, which is what `LH-055`/`LH-056`/`LH-058` say.
*So `branch` and `column` are JUSTIFIED for rask on the spec's own evidence*, and the Lakekeeper
comparison argues the opposite of what I first drew from it. `base` is the one that does not follow:
base paths are a FILE-FORMAT concept (`file_format.md`, shallow clones / multi-base), not a namespace
door, so it needs its own argument rather than this one. `estate` keeps the `Server` precedent.
*The lesson is the standing rule, applied to myself:* idiomatic to lance-ns, never Iceberg — and I
reached for an Iceberg catalog because it was the nearest analogue rather than the governing spec.

**The permanently-refused ack: Lakekeeper does NOT settle it, and saying so is the finding.** Its event
plane is fire-and-forget — `service/events/dispatch.rs:390-391`: *"If the listener fails, it will be
logged, but the request will continue to process."* There is no consumer, no ack vocabulary and no DLQ;
`lakekeeper-events-nats` is a publish backend only. rask's bus door AUTHORIZES what it records, which is
strictly more than the reference does, so this question is rask's own and has no borrowed answer.

**The governed-tier home: the spec answers it, and the answer is CREATED-OR-REGISTERED rather than
"vended path only".** Read off `lance_docs/ns_catalog/spec.yaml` rather than by analogy:
* `CreateTableRequest` (:3739-3742), verbatim — *"The table location and any credential vending
  behavior are **determined by the implementation** and returned in the response, rather than specified
  in this request."* So on the CREATE door the catalog owns the location and a client cannot choose it.
* `RegisterTable` (:373) — *"Register an existing table at a given storage location as `id`."* So the
  spec DOES support a governed table that already lives somewhere the catalog did not mint.
*Therefore the rule is not "only the vended path".* A table elsewhere is legitimate **iff it is
registered**; what is illegitimate is a dataset that is neither created by the catalog nor registered
with it — which is exactly rask's composed `medallion/<tier>` population, and where three of the four
[[LH-141]] crossings sit. The remedy is unchanged in shape (register or reap, [[LH-164]] measures the
set) and better founded: the spec has a door for the registering half.

## The five conditions, MEASURED against the running estate (2026-09-15)

Row counts say how much is written down; they do not say how close the goal is. Every line below was
driven against the live estate or read from its authoritative store — never inferred from a setting or
a docstring, which is how three claims had to be retracted the same day.

| # | Condition | Status | The measurement |
| --- | --- | --- | --- |
| 1 | Provenance survives a write | **verified END TO END, one residual** | Driven literally: a real `POST /v1/table/{id}/create` (3 rows, Arrow IPC) answered 200 at `s3://lakehouse-wh/916cee62_lakehouse$prov-probe`, and the graph then held `(:Run {operation:'create_table'})-[:WROTE]->(:Dataset {name:'lakehouse$prov-probe'})` — the write's provenance survived the write. Probe table purged afterwards. Standing totals: 7,107 `Run`, 1,444 `Dataset`, 7,093 `WROTE`. `/runs` is governed on read: **0** rows for an identity holding no rung on the outputs, **10** for one that does. Residual: [[LH-166]]. **ADVANCED 2026-09-16:** the in-pod maintenance lanes now record the versions they mint ([[LH-153]] closed, OBSERVED — a real compact on `aud1ns$t1` put `(:Run {operation:'compact_table', author:'CiQwOGE4…'})-[:WROTE {version:7}]->` in the graph, where the sweep's own run carries an EMPTY author), and `lineage-kit` no longer returns in silence when nothing will recover an event. |
| 2 | Catalog correct for lance-ns + authz | **partial** | 381 catalog tables across 97 warehouse roots, 0 unreadable. `ungoverned_tables` = 0 against a denominator of 381 — **and that parenthesis is load-bearing: it counts every table the CATALOG KNOWS, so a dataset that exists on storage and in lineage but was never REGISTERED is outside the count entirely** (re-measured 2026-09-16: `research-bronze$events` and `bind86-bronze$events` answer 404 at the catalog while carrying lineage nodes — see [[LH-144]]). `maintainer` now on all 97 warehouses — repaired by the boot backfill, `warehouses=96 tuples=1344 failures=0`. **Spec conformance DRIVEN, not merely test-covered**: all **54/54** operations in the vendored `spec.yaml` are served by the running catalog (zero missing), beside 106 rask control-plane operations that are correctly not spec ops; and a stubbed one answers the spec's own status — `POST /v1/table/{id}/backfill_column` -> **406 `UnsupportedOperationError`** in RFC 9457 form. Residuals: [[LH-037]], [[LH-164]]. **AND A LIVE AUTHZ DEFECT THIS ROW NEVER COUNTED, found and fixed 2026-09-16:** the vend door honoured only ONE of the estate's two approved-base allowlists, so it dropped `s3://lance-catalog/models/` — a base the create door accepts — from every session policy it issued: **1,836 `vend_base_path_unsanctioned` refusals in three hours** — re-measured later the same day at **1,254 in thirty minutes**, i.e. ~2,500/hour and ONGOING — each surfacing later as a read denial at the object store with nothing naming the base. Fixed `6d923a40`, built as `main-38b85684`, and NOT YET DEPLOYED: the running catalog is `main-9e5ff5b3`, so the refusals continue until the stem is rolled ([[LH-169]]). |
| 3 | Not coupled to a workflow engine or Ray | **DONE and pinned** | In the running `rask-medallion-producer`: `dapr-ext-workflow` is absent from the unconditional `Requires-Dist` and present only as `extra == 'workflow'`; `import ray` FAILS in the image; zero module-level `import ray` across the four services; none declares `ray`/`ray-kit`. Both halves gated by `test_the_lakehouse_does_not_depend_on_a_workflow_engine.py`. |
| 4 | Events are correct | **partial, and the plane itself is healthy** | The bus DELIVERS, measured over two windows: **586 deliveries / 1 parked** in 20 min, **1,313 / 53** in 60 min — every one answering HTTP 200, because a park is an ACK (the DLQ route acks after logging, so 200 is the correct status for both outcomes and cannot be read as success on its own). Streams: LINEAGE 1,311 / MEDALLION 190 / CATALOG_CONTROL 908 / INGEST 32, all with recent traffic. DLQ is 9,887 msgs / 29 MiB, `dlq.lineage.events` 9,856, dominated by TEST identities (`data_eng` 30, `e2e` 14, `ray` 10 over 3 h against `service-stage-runner` 3 + `service-maintenance` 2). So the failure is a CLASS of message, not the transport. **And the DLQ's GROWTH is not a loss rate at all (measured 2026-09-16): the ingest consumer is ephemeral with `deliverPolicy: all`, so every lineage RESTART re-presents the retained stream, the gate refuses the same unrepairable events again, and each refusal appends a NEW DLQ message about an event already in it.** Driven deliberately — rolling lineage produced 49 parks inside two minutes of pod start and zero before it; run `188ab99f…` sits at seq 5000 parked 2026-09-10 and was parked again 2026-09-15. **No production event newer than 2026-09-14T19:14 has ever been parked.** So DLQ depth is restart-count x backlog-size over a roughly FIXED, OLD set — not an accelerating bleed, **and it is BOUNDED: the stream is `max_age` 168 h / `discard: old`, so it holds a rolling 7-day window and has gone DOWN across today's four rolls (10,011 -> 8,255)**. Residual: [[LH-166]], [[LH-148]]. **ADVANCED 2026-09-16:** a failed outbox stage no longer skips the publish it was meant to protect ([[LH-154]] closed, driven against the deployed pod), and the loop's MECHANISM is finally attributed — [[LH-170]] records that `lineage-pubsub-lineage` is the one subscription of eight with no `durableName` and `deliverPolicy: all`, so the replay is idempotent for events the graph ACCEPTS and not for events the gate REFUSES. One ruling on refusal-ack semantics closes that, [[LH-166]] and [[LH-151]] together. |
| 5 | Resilient | **partial** | Zero restarts across the whole lance plane. The cascade retry window is the shape that actually works — `pubsubDeliveryRetry` is `constant`, `duration=120s`, `maxRetries=4` (8 min), live in the CR and gated by `test_the_cascade_retry_window_is_the_one_the_chart_states.py`; the sidecars carry it because every stage runner was restarted after it applied. **RE-MEASURED 2026-09-16 over a 6h window: 305 distinct datasets processed, 135 refused, 0 skipped, 0 errors — and every sampled refusal is the shallow-clone protection** (*"another dataset resolves its files through X (shallow clone / multi-base) — compacting or reclaiming here would break it"*). **Only THREE refusals are authorization** (`maintenance_vend_denied`), and all three are one cause: `lakehouse$gold`, `lakehouse$silver`, `lakehouse$silver-media` — the medallion COMPOSED paths under `s3://lance-catalog/medallion/<ns>`, whose derived ids are namespace-shaped and name no table door. That is the double-home ([[LH-137]], [[LH-164]]), owner-blocked, not a governance gap in the sweep. Residual: [[LH-148]]'s replay gap. **ADVANCED 2026-09-16:** the reconcile loop gained the orphan pass it could never have ([[LH-127]]) — keyed on the durable set the CHART RENDERS, because the app-id form the row asked for would have deleted the dead-letter consumer and both control lanes; dry-run against the live release's own values removes exactly `lance-ray-durable`, `pages-to-gold-htr-durable` and `maintenance-durable` and spares all eight chart-owned. Also: control-root backups now prune ([[LH-110]]), prod stops running every app pod as SA `default` ([[XC-028]]), and every first-party pod can pull from a credentialed registry ([[XC-032]]). NOT YET DEPLOYED — see [[LH-169]]. |

**One of five is finished. The other four are each one or two named rows from being finished**, and
three of those rows wait on an owner decision rather than on work. That is a more useful statement of
"how much is left" than the item count above, and it is the first time the goal itself has been
measured rather than the backlog that serves it.

**AND THE BINDING CONSTRAINT IS A DECISION QUEUE, not capacity — measured 2026-09-16 by a 165-agent
audit that re-drove all 220 open rows against HEAD and the live estate.** `NEEDS_OWNER` came back as
the joint-largest verdict (51 rows). Two rulings dominate: whether the catalog-vended path is the ONLY
legitimate home for a governed tier (closes the sole named residual on two of the five conditions), and
what a PERMANENTLY refused bus message acks as (closes [[LH-166]], [[LH-151]] and [[LH-148]] together —
the named residual on conditions 1 and 4 both). Neither needs engineering to decide.

**THE ROWS THEMSELVES ARE THE OTHER RISK, and it is larger than the count suggests.** Of the rows
picked up on 2026-09-16, **eleven named a fix the estate could not take as written** — three of them
destructively: [[LH-127]]'s orphan rule would have deleted the dead-letter consumer, [[XC-010]]'s
URL-encoding would have denied every principal whose id carries a special character, and [[LH-023]]
asked for the exact authz-drift `docs/DECISIONS.md` row 6 refuses. The others named columns, files or
call sites that do not exist ([[LH-075]]'s `dataset` column, [[LH-096]]'s already-converted services,
[[CP-013]]'s deleted module). **Re-measure before working a row is not ceremony here; it is the
difference between advancing the estate and regressing it.**

**THE STALE-ROW SEAM IS DRAINED, measured 2026-09-15 — do not plan another sweep on the old base
rate.** The standing instruction cites 2026-09-09, when 8 of 17 settled rows turned out already fixed,
and that number has been used since to justify re-measuring before working anything. It is still the
right instinct per ROW and it is no longer a good bet in BULK: two adversarial sweeps re-measured **24
unblocked lakehouse rows against the code** and found exactly **one** already satisfied ([[LH-053]],
closed) plus one whose first half had landed ([[LH-149]], since finished and closed). Every other row
came back OPEN or PARTIAL, with its remaining half named.

So the cheap wins are gone: what is left in phase 1 is real work or an owner decision, and the
sweeps' value from here is narrowing a row's ask rather than closing it outright. Re-measuring the
single row you are about to touch stays mandatory — that is how [[LH-157]] was found closed and how
two wrong root causes were caught the same day. Items are numbered continuously; the ids in brackets are the source rows they came
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

**LH-127 · Four orphaned Dapr durables sit on the estate's streams, and the reconcile loop cannot remove any of them**
`lineage, compute, chart` · med

- *Why open — RE-MEASURED TWICE ON 2026-09-13.* The row was filed on one consumer; the first re-measure found four on two streams; a census across EVERY stream found **eight, on six streams**, and the two largest were in neither earlier count. Listing only the streams somebody had already looked at is how a census undercounts:

      stream            consumer                    unprocessed   last delivery   app-id live?
      LINEAGE           lance-ray-durable                 1,572   29d             NO
      LINEAGE           maintenance-durable                 529    5d             yes (gated off)
      MEDALLION         pages-to-gold-htr-durable           296   28d             NO
      CATALOG_CONTROL   lance-ray-control-durable           872   37d             NO
      TRAINING          lance-ray-durable                    26   never           NO
      DLQ               lance-ray-durable                     0   never           NO
      DLQ               pages-to-gold-htr-durable             0   27d             NO
      MAINTENANCE_WORK  maintenance-work-durable              0    5d             yes (gated off)

  `lance-ray` alone holds FOUR durables and `pages-to-gold-htr` two. **`lance-ray-control-durable`'s
  872 is the ENTIRE `CATALOG_CONTROL` stream** — every control event the estate has published, held by
  a consumer for an app that does not exist.
- **AND THAT ONE IS BEYOND THE DRIFT LOOP'S REACH ENTIRELY**, which sharpens the root cause below
  rather than repeating it: `nats-stream-job.yaml` walks `LINEAGE MEDALLION TRAINING DLQ` and nothing
  else, so `CATALOG_CONTROL` and `MAINTENANCE_WORK` sit outside even the pass that WOULD delete a
  config-drifted durable. The blind spot is two-dimensional — the streams it does not walk, and, on the
  streams it does, the orphans whose config matches by construction.
- *`pages-to-gold-htr` is also a WORKLOAD-NAMED app-id*, which the estate no longer permits anywhere
  (R23, "the platform knows NO workload"). Dead metadata rather than a live violation, but it dates the
  residue: pre-R23, and nothing has removed it since.
- *(The `ingest-r*` consumers on INGEST are a different defect with its own row — [[LH-131]], one
  durable leaked per run — and are deliberately not counted here.)*
  All eight report `Active Interest: No`. The live app-ids are annotator, bronze-to-silver, catalog,
  compute, controlplane, flows, gateway, ingest, lineage, maintenance, medallion-producer,
  media-to-silver, notifications, search, silver-to-gold, viewer — `lance-ray` and `pages-to-gold-htr`
  are not among them, and neither appears in the repo as an app-id (`lance-ray` occurs only as the name
  of the Data-integration library).

- **Retention is NOT pinned.** LINEAGE is `Retention: Limits, Discard: Old, Maximum Age: 7d`, so messages
  age out regardless of any consumer's position — the "a consumer holds the stream" behaviour belongs to
  WorkQueue/Interest retention, which this stream does not use. `lance-ray-durable`'s unprocessed fell
  2,445 -> 1,590 *because* retention pruned beneath it, and 1,590 is now exactly the stream's whole
  message count: it has consumed nothing that still exists.

- *So the cost is a FALSE SIGNAL, not disk.* `nats consumer ls LINEAGE` presents an orphan exactly as it
  presents a broken subscription — bound, durable, queue-grouped, far behind. Separating the two took
  reading the deployed pod's `/dapr/subscribe`. That is the one distinction an operator needs from this
  view, and the only one it cannot make.

- **`maintenance-durable` is residue, NOT a dead subscription** — the distinction above, checked the hard
  way. `chart/values.yaml:1518` ships `workTopic: ""`, the release sets no override (`helm get values
  rask` -> `maintenance: {trashPurgeDryRun: false}`), so `register_arrival_route` returns `None` at
  `services/maintenance/src/maintenance/api/arrival.py:78` and `GET /dapr/subscribe` **404s** on the
  running pod: the service registers no Dapr subscription at all. The cron lane is doing the work — a
  sweep at 18:49 on 2026-09-13 logging per-dataset `maintenance_dataset_outcome` with
  `indices_optimized=1`/`2`. The consumer dates from the 2026-09-03 -> 09-08 window when `workTopic` was
  set; the `MAINTENANCE_WORK` stream that implies still exists, empty, created 2026-09-03 17:17:03.

- **ROOT CAUSE — the chart's reconcile loop cannot remove an orphan, by construction.**
  `chart/templates/nats-stream-job.yaml:223-234` walks `LINEAGE MEDALLION TRAINING DLQ`, matches
  `*-durable`, and deletes a consumer only when its `max_deliver`/`backoff` differ from
  `EXP_MAXD`/`EXP_BOFF`. An orphan was created by a Dapr sidecar from those same templated values, so it
  matches forever — measured, both LINEAGE orphans report `MaxDeliver 3, Backoff 12m0s,12m0s`, exactly
  `EXP` for `resiliency.enabled`. The loop reconciles CONFIG drift and has no notion of "no app
  subscribes to this", so the one class of stale durable it cannot see is the class that never changes.

- **NOT the same shape as lineage's own consumer, which was filed beside this and STRUCK.** Lineage's is
  ephemeral BY DESIGN — the chart states it ("a durable cursor would defeat its replay-rebuilds-the-graph
  recovery story") and `_is_replay` accepts what the replay re-presents, logging at INFO. Confirmed live
  2026-09-13: lineage's ingest consumer is `NrOCxPF3`, ephemeral, created 2026-09-11 20:58:00 at the pod
  restart, `Unprocessed 0`. These four are the opposite: durable, queue-grouped, attached to nothing.

- **PART OF THE BLIND SPOT CLOSED 2026-09-14, and it was a different defect than this row names.**
  The row calls the unwalked streams a second dimension of the orphan problem. One of them was not an
  orphan problem at all: CATALOG_CONTROL carries TWO LIVE chart-owned durables —
  `medallion-producer-control-durable` (`dapr-component.yaml:88`) and `notifications-control-durable`
  (`:139`), both reporting Active Interest when re-measured 2026-09-14 — and the loop's own promise to
  repair them sits at `dapr-component.yaml:103` ("nats-stream-job reconcile compares
  (maxDeliver/backOff) and repairs"). The job excluded the stream on the written ground that
  "Consumers are EPHEMERAL (no durableName)", which is true of the catalog's ring-buffer consumer and
  false of those two. Two files asserting opposite things, the live stream agreeing with neither, and
  the exposure is the 2026-07-13 failure this loop exists for: a values change to the resiliency flip
  would leave both durables unbindable and every subscribe failing while the pods stay Ready.
  Their components render maxDeliver/backOff from the SAME conditional the loop templates EXP from, so
  walking it is a no-op today and catches the next drift. `82d9f6f2`.
- *And NOT by walking every stream, which is why the other two stay out.* `maintenance-work-durable`
  renders `720s,720s,720s,720s` with resiliency OFF against EXP_BOFF's `30s,60s,120s,300s` — its
  backoff is sized by the WORK, not the fleet convention (the index lane's is `indexAckWait` repeated).
  Walking those would find a mismatch every run and delete the work queue out from under in-flight
  units, the documented reason INGEST is excluded. The exclusion now carries that reason instead of an
  incorrect one, and the new gate pins the RULE — a durable whose config the comparison understands
  must be walked, a bespoke one must not — mutation-proven in both directions, including against the
  naive over-fix.
- **THE ROW'S OWN RULE IS DESTRUCTIVE AS WRITTEN — measured 2026-09-16 before implementing it.** It says
  "a durable whose app-id is not in the release's rendered app-id set is pure residue and is deleted".
  The live release renders 12 app-ids (`annotator bronze-to-silver catalog compute flows ingest lineage
  maintenance medallion-producer media-to-silver notifications silver-to-gold`) and THREE chart-owned
  durables are not named `<app-id>-durable`: `lineage-dlq-durable` (`lineage-pubsub-lineage-dlq`),
  `medallion-producer-control-durable` and `notifications-control-durable`
  (`catalog-control-pubsub-*`). Strip `-durable` from those and you get `lineage-dlq`,
  `medallion-producer-control`, `notifications-control` — none of them an app-id, so the rule as stated
  deletes all three, including the dead-letter consumer. That is the 2026-07-13 failure this loop was
  built to prevent, caused by the loop.
- *THE SAFE FORMULATION IS SIMPLER AND THE CHART ALREADY HAS IT:* compare against the set of
  `durableName` values the CHART RENDERS, not against app-ids. The chart writes every durable it owns —
  eight of them today, readable straight off the rendered Components — so "not in that set" is exactly
  "nothing here created this", with no name-shape inference in between. It also fixes the row's other
  half for free: `maintenance-durable` names a LIVE app-id and so survives an app-id test, while it is
  absent from the rendered set the moment `workTopic` is empty, which is the conditional the row asks
  for.
- *AND THE SET MUST BE RENDERED ONCE, NOT TWICE.* `dapr-component.yaml` derives durable names FIVE
  different ways — `{{ .appId }}-durable` (:223), `<producer>-control-durable` (:88),
  `notifications-control-durable` hardcoded (:139), `<lineage>-dlq-durable` (:273) and
  `<maintenance>-work-durable` (:381). A Job that rebuilds that set inline is a second file computing
  the same truth, which is precisely how this loop already shipped one defect: the stream was excluded
  on the written ground that "Consumers are EPHEMERAL (no durableName)" while two chart-owned durables
  sat on it, two files asserting opposite things and the live stream agreeing with neither. The set
  belongs in one named template both consume — a new `_durables.tpl` rather than `_helpers.tpl`, which
  carries unrelated uncommitted edits.
- *The gate is already half-written:* `tests/unit/test_a_durable_the_chart_owns_is_a_durable_the_drift_loop_walks.py`
  has `_durables(rendered)` returning `{durableName: (maxDeliver, backOff)}` off a real `helm template`,
  which IS the expected set. An orphan-pass assertion reuses it rather than restating it.
- **LANDED 2026-09-16.** `chart/templates/_durables.tpl` emits `lance.chartDurables` — every durable
  this chart renders, under each component's own conditional — and `nats-stream-job.yaml` templates it
  into `EXP_DURABLES`, deleting any `*-durable` on a walked stream that is not in it. DRY-RUN AGAINST
  THE LIVE RELEASE'S OWN VALUES: it removes `lance-ray-durable`, `pages-to-gold-htr-durable` and
  `maintenance-durable`, and spares all eight chart-owned durables — including the three a naive
  app-id rule would have destroyed. `maintenance-durable` falls out for free, because it leaves the
  rendered set exactly when `workTopic` empties, which is the conditional this row asked for.
- *Gated by four assertions in `test_a_durable_the_chart_owns_is_a_durable_the_drift_loop_walks.py`*,
  the load-bearing one being that `EXP_DURABLES` equals the durable set read off the rendered
  Components — so a component that grows a durable the helper does not emit fails a test rather than
  losing its consumer at the next release. (The first version of the existence check passed against a
  chart with no orphan pass at all: it matched `MAINTENANCE_ORPHAN_SCAN_ENABLED` from an unrelated
  template. It is anchored on `EXP_DURABLES` now.)
- *STILL OPEN:* not yet deployed, so the eight orphans are still on the streams — the pass runs on the
  next release. `MAINTENANCE_WORK` and the index lane stay excluded for the reason recorded above. **Still fully open:**
  re-measured 2026-09-14, all EIGHT orphans are present and unchanged in kind (`lance-ray` x4,
  `pages-to-gold-htr` x2, `maintenance-durable`, `maintenance-work-durable`), with retention moving
  the depths beneath them — LINEAGE/`lance-ray-durable` now reads 1,330 unprocessed, which is the
  stream's ENTIRE message count, i.e. it has still consumed nothing that exists. A durable whose app-id is
  not in the release's rendered app-id set is pure residue and is deleted (`lance-ray`,
  `pages-to-gold-htr`). A durable for a LIVE app whose subscription is merely gated off must be rendered
  under the SAME conditional the subscription's pubsub component renders under — the discipline the file
  already applies to `EXP_MAXD`/`EXP_BOFF` — so `maintenance-durable` is removed exactly when `workTopic`
  is empty and recreated when it is set, rather than surviving to replay 523 stale events into `plan_one`
  on the day someone enables the lane. Then surface per-consumer depth and `Active Interest` wherever the
  maintenance sweep already reports, so the next one is visible without a NATS client.

**LH-149 · ~~The cascade head hard-imports the workflow engine, so the lakehouse DEPENDS ON Dapr Workflow rather than being driven by it~~ — CLOSED 2026-09-15, observed live**
`medallion` · med · found 2026-09-13 while re-measuring condition 3 · **refines an earlier verdict that measured only two of the import sites**

- *Condition 3 in the owner's words:* "Dapr Workflow and Ray are things the lakehouse can be driven BY,
  never things it depends ON." A module-level import is a dependency.
- **MEASURED 2026-09-13, in a subprocess rather than by reading:** importing `medallion.producer` — the
  cascade head — pulls in `dapr.ext.workflow` and its whole durabletask stack (`_durabletask.client`,
  `.deterministic`, `.internal`, the generated protobufs). The producer cannot be imported without the
  workflow engine present.
- *The chain, named so it can be cut:* `producer.py:33` imports `medallion.api.promotions` at module
  level — one of the six routers the producer always mounts, human-facing control rather than an
  opt-in lane — and that module imports the engine twice over: `promotions.py:23`
  `from dapr.ext.workflow.workflow_state import WorkflowStatus`, and `promotions.py:32`
  `from medallion.workflow import PromotionSpec, promotion_review`, where `workflow.py:54` is a
  module-level `import dapr.ext.workflow as wf`. `services/promotion_hold.py:19` carries the same second
  edge. Confirmed per-module: promotions, promotion_hold and workflow each pull it; `stage_runner_ops`
  does not.
- *Why the earlier verdict missed it, recorded so the next re-measure is not fooled the same way:* the
  two sites it checked — `producer.py:121` and `stage_runner.py:93` — ARE lazy, and the cascade's
  workflow dispatch IS gated by `transform.py`'s `if use_ray`. Both true, and neither is the import
  graph. The question "is the engine optional?" is answered by importing the module, not by reading the
  call sites.
- *`workflow.py` itself is NOT the defect and must not be 'fixed':* it is the engine ADAPTER —
  `ACTIVITY_RETRY: Final = wf.RetryPolicy(...)` at :76, `register(runtime: wf.WorkflowRuntime)` at :827,
  `DaprWorkflowContext` generators throughout. An adapter may import the thing it adapts. The defect is
  that an always-mounted HTTP router imports FROM the adapter.
- **WHY med AND NOT HIGH, stated so the severity is not mistaken for the goal's verdict.** The
  RUNTIME gating is correct and its comment is true: `producer.py:118` starts the engine only
  `if settings.quality_review_enabled or settings.ray_enabled`, and says "with neither feature on, this
  app hosts no workflow and should run no engine" — no threads, no client. And `dapr-ext-workflow` is
  already an unconditional entry in `services/medallion/pyproject.toml`, so the import adds no
  dependency the manifest did not already carry. Nothing is operationally broken. What it costs is the
  claim: condition 3 cannot be declared met while the cascade head cannot be IMPORTED without a
  workflow engine.
- *Which makes the full fix larger than moving imports:* real decoupling means `dapr-ext-workflow`
  becomes an optional extra that medallion runs without, and the import edges below are what has to be
  cut first for that to even be possible. Cutting them is worth doing on its own; declaring condition 3
  met needs both.
- **BOTH MEDALLION ENTRYPOINTS WERE AFFECTED, not just the producer.** `services/transform.py:51`
  imports `promotion_hold` at module scope, and that module took `PromotionSpec` from the engine
  adapter — so `import medallion.stage_runner` and `import medallion.services.transform` pulled the
  engine too, by a second route through the same adapter.
- **LANDED 2026-09-13 (`25fc6905`).** `PromotionSpec` moves to `medallion/schemas/promotion.py` beside
  the other wire and state contracts — plain pydantic, no engine — so the adapter imports the payload
  instead of the payload living inside the adapter. `promotion_review` is resolved at the scheduling
  call, where the workflow client is already built lazily; `WorkflowStatus` becomes `_is_live()`,
  comparing against the enum MEMBERS rather than their names, since the names are the library's to
  change. `medallion.workflow` still imports the engine and must — it IS the adapter — which is pinned
  as its own test so the fix cannot decay into lazy-importing everything until the module means
  nothing. No backward compat: all five importers moved in the same change.
- *The test imports each module in a SUBPROCESS rather than reading source*, because by the time a
  suite reaches it an earlier test has already pulled the engine into the interpreter and an in-process
  check would pass regardless. That is also exactly why the earlier verdict was wrong.
  **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live.
- *What remains for condition 3 to be declared met:* `dapr-ext-workflow` is still an unconditional
  entry in `services/medallion/pyproject.toml`. Cutting the import edges is what makes an optional
  extra POSSIBLE; making it optional is the other half, and it is a packaging decision rather than a
  defect.
- **COSTED 2026-09-13 so the decision is cheap, and the trap named so it is not taken blind.** The
  mechanics are ordinary — `[project.optional-dependencies] workflow = ["dapr-ext-workflow>=1.18"]`
  beside the precedent `service-kit` already sets, then `uv lock`, then `--extra workflow` on both
  `uv sync` steps in `.docker/rest-catalog.dockerfile`. Probed: `uv sync --extra` resolves an extra
  defined by ANY selected package ("Extra `X` is not defined in any project's optional-dependencies
  table"), so `--extra workflow` binds unambiguously to medallion among the seven `--package` flags.
  **THE TRAP IS THAT `--all-packages` DOES NOT IMPLY EXTRAS.** Nothing in this repo passes `--extra` or
  `--all-extras` today, and `uv sync --all-packages` is what the Makefile runs three times, what
  `.dagger/test.go` runs, and what three CI jobs run as `--frozen --all-packages --all-groups`. Move
  the dependency without updating all of those and the engine is absent from every developer and CI
  environment, so `medallion.workflow` — the adapter, which MUST import it — fails at import and takes
  its suite with it. Loud rather than silent, which is the only good news in it. Six-plus call sites
  across the Makefile, a Go module and CI config is a deliberate change, not an opportunistic one.
- *Closes when:* the roll observes the decoupled image, and the dependency becomes optional.
  The shape is already visible: `PromotionSpec` is a plain pydantic model (`workflow.py:1112`) with no
  engine in it and belongs beside the other schemas, so `promotions.py` and `promotion_hold.py` can take
  the domain without the adapter; `promotion_review` is referenced only inside the scheduling call
  (`promotions.py:169`), where `wf` is ALREADY imported lazily at :125; and `WorkflowStatus` builds one
  module-level tuple (`_LIVE`, :70) that can be resolved where it is used. No backward compat, so every
  importer of `PromotionSpec` moves in the same change.
- **RE-MEASURED 2026-09-15: the IMPORT half is landed and green; the PACKAGING half is not, and that
  alone keeps the row open.** Verified by hand after a re-measurement sweep flagged it:
  * `services/medallion/src/medallion/workflow.py:54` is the ONLY module-level `import dapr.ext.workflow`
    left in the service — the adapter, which the row permits. `producer.py`, `stage_runner.py`,
    `api/promotions.py` and `services/dapr_saga.py` all import it inside function bodies.
  * The pinning suite `test_the_cascade_head_does_not_import_a_workflow_engine.py` passes (6 tests), and
    it proves this by SUBPROCESS IMPORT rather than by reading source, so it cannot be satisfied by a
    comment.
  * **But `services/medallion/pyproject.toml:30` still lists `"dapr-ext-workflow>=1.18"` in the
    unconditional `dependencies` block, and medallion declares no `[project.optional-dependencies]`
    table at all.** So the engine is still a hard install-time dependency of the lakehouse's cascade
    head: condition 3 is met in the import graph and not in the package metadata.
- **THE PACKAGING HALF LANDED 2026-09-15, and the feared blast radius was measured away rather than
  paid.** The concern above was that `uv sync --all-packages` (Makefile ×3, `.dagger/main.go`, CI ×3)
  would lose the engine and take medallion's suite down, making this a 7-site build change. Measured
  with the real resolver, neither half of that holds:
  * `--extra <name>` is a bare NAME, so `uv sync --all-packages --extra workflow` could only ever apply
    to members declaring an extra of that name — it cannot drag in service-kit's
    `governed`/`lakehouse`/`lancekit`, which the architecture rules keep out of a base install.
  * And no whole-workspace change was needed at all: `ingest` and `flows` declare the engine
    unconditionally for their OWN workflows, so `--all-packages` still installs it. Verified by driving
    the sync both ways.
  So the change is TWO sites, both in `.docker/rest-catalog.dockerfile`, plus the manifest.
- *Proven with the resolver rather than by reading the manifest:* `uv export --frozen --no-dev
  --package medallion` names `dapr-ext-workflow` **0** times; the same command with `--extra workflow`
  names it **2**. A per-package sync — which is exactly what the image does — now drops the engine
  unless asked, so condition 3 holds in the package metadata and not only in the import graph.
- *The two halves drift apart silently, so both are pinned* by
  `tests/unit/test_the_lakehouse_does_not_depend_on_a_workflow_engine.py`: making the dependency
  optional WITHOUT teaching the image to ask for it yields a build that succeeds and a service that
  dies at import, because `medallion/workflow.py` imports the engine at module scope. The gate parses
  the dockerfile's sync COMMANDS across their line continuations — two earlier drafts counted
  substrings and failed on a correct file, once on a comment and once on the prose explaining the flag.
- *Scope pinned too:* `ingest` and `flows` keep their own unconditional pins. They are FLEET services,
  and the goal statement names the lakehouse as catalog/lineage/medallion/maintenance — moving theirs
  would be a change nobody asked for.
- **OBSERVED LIVE on `main-926f9b16`, read out of the running `rask-medallion-producer`'s own
  installed metadata rather than from the repo:**

      dapr-ext-workflow installed  : 1.18.3          (the adapter still works)
      medallion.workflow imports   : True
      unconditional dep in METADATA: NONE            (the lakehouse no longer DEPENDS on it)
      gated behind the extra       : ["dapr-ext-workflow>=1.18; extra == 'workflow'"]

  Ten workloads rolled, zero failed rollouts — and the three that import the adapter at module scope
  (the producer and both stage runners) starting at all is itself the proof the extra reached the image.
- **AND THE RAY HALF OF CONDITION 3 WAS ALREADY CLEAN, measured the same way rather than assumed.**
  The condition reads "a workflow engine OR RAY", and nobody had checked the second noun: none of
  `catalog`, `lineage`, `medallion`, `maintenance` declares `ray`/`ray-kit` unconditionally, there is
  not one module-level `import ray` across the four, and **`import ray` fails outright in the deployed
  image** — the Ray lane reaches the cluster over the Jobs REST API rather than by importing a client.
  Both halves are now pinned by `tests/unit/test_the_lakehouse_does_not_depend_on_a_workflow_engine.py`,
  the Ray half starting green so its whole job is to stay that way: that coupling is one `uv add` away
  and would break nothing visible.
**LH-148 · The terminal-provenance-loss metric counts restarts, not losses — and the payload it parks can never be read back**
`lineage, chart` · med · found 2026-09-13 while re-measuring [[LH-127]] · **not currently bleeding**

- *Measured live 2026-09-13.* The DLQ stream holds 8,612 messages / 25 MiB, of which **8,515 are on
  `dlq.lineage.events`** — the lineage service's own ingest DLQ (`chart/templates/services.yaml:488`
  sets `LINEAGE_DLQ_TOPIC=dlq.lineage.events`; the other subjects are `dlq.bronze-to-silver` 75,
  `dlq.silver-to-gold` 18, `dlq.notifications` 3, `dlq.maintenance.work` 1). Retained window
  2026-09-06 18:57:14 -> 2026-09-11 21:02:08, and **zero since**: `dapr_dead_letter_parked` over the
  last 48 h of `rask-lineage` logs counts 0, and compaction provenance is landing today (`MATCH (r:Run)
  WHERE r.operation='compaction'` groups to 84 on 2026-09-13, 14 on 09-12). Sampled payloads are real
  cascade provenance — `embed_features` from `bronze-to-silver`, `aggregate_gold` from
  `silver-to-gold`, a `compaction.m2proof_silver$…` COMPLETE from `maintenance`.

- **THE METRIC OVERCOUNTS, AND THE ARITHMETIC PROVES IT WITHOUT SAMPLING.** The LINEAGE stream was
  created 2026-08-05 and its **last sequence is 5,860** — so at most 5,860 messages have EVER been
  published to `lineage.events.v1` in the stream's whole life. The DLQ holds **8,515** parked deliveries
  of that one topic from a FIVE-DAY window. 8,515 > 5,860, so events were parked repeatedly; the excess
  is not explainable by volume. *How repeatedly:* 31 evenly-spread `stream get`s across the DLQ's whole
  sequence range yield only **21 distinct run ids**, which for a uniform population puts the distinct set
  near 35-40 — so the parkings outnumber the events behind them by roughly two orders of magnitude, not
  by a factor of six.

- *The mechanism is the ingest consumer's replay, and it is deliberate.* `chart/templates/dapr-component.yaml:172`
  registers lineage as `deliverPolicy: "all"`, and line 219 renders a `durableName` only for
  `deliverPolicy: "new"` — so lineage's ingest consumer is EPHEMERAL + deliver-all by design (confirmed
  live: it is `NrOCxPF3`, created 2026-09-11 20:58:00 at the pod restart). Every restart therefore
  re-reads up to 168 h of retained stream and re-parks every event that still fails. So
  `record_outcome(Outcome.DEAD_LETTERED)` counts *restarts x still-failing events*, not lost events.

- **This is the hazard the code says it fixed, arriving by the other door.** `services/lineage/src/lineage/api/dapr.py:101-105`
  explains that the parking route was moved onto its own `deliverPolicy=new` durable because riding the
  ingest component "made this route re-park up to 168h of already-parked dead letters on every pod
  restart, spiking the terminal-loss metric with no new loss". That fix landed `c316d744` (2026-08-08)
  and it is real — but it stops the DLQ route re-reading ITS OWN parked copies. It does nothing about
  the INGEST consumer re-reading the SOURCE stream and parking the same events again, which produces the
  identical spike. The comment's claim that the metric now means terminal loss does not hold.

- **AND THE PARKED PAYLOAD IS UNREACHABLE BY CONSTRUCTION — all three backstops miss this class, each in
  its own words.**
  - The **outbox** stages the event before the publish and deletes it once the publish returns
    (`packages/service-kit/src/service_kit/lakehouse/outbox.py`). A dead letter happens AFTER the publish
    returned, so the object is already gone. The module states the boundary itself: *"The DLQ only
    catches events that were published then failed delivery."*
  - **Replay-from-stream** is the documented recovery (`dapr.py:63`) — but it reads LINEAGE, whose
    `Maximum Age` is **7d**, so it can only recover a dead letter younger than 168 h.
  - The **DLQ** is a log line: `on_dead_letter` ERROR-logs, records the outcome and acks, and its own
    docstring says it *"adds operator VISIBILITY, not a second path"*. Nothing re-ingests the DLQ stream;
    `/admin/dlq` reads the OUTBOX object store, not this stream.

  So the one place the FULL payload still exists is the one place nothing reads.

- **BUT MOST OF THE PARKED PROVENANCE IS NOT LOST, AND THAT IS WHY THIS IS `med` AND NOT HIGH.** Of the
  21 distinct parked run ids sampled above, **17 are present in the AGE graph and 4 are not** (absent:
  `2780f402`, `7dc47e08`, `845c63a5`, `f8d26794`). The healing mechanism is that the run ids are
  deterministic — every one sampled is a UUIDv5 — and `handle_cloud_event` is idempotent on `run_id`, so
  when the producing stage runs again it re-emits the SAME id and the node lands. Terminal loss is
  therefore confined to runs that never ran again, which is the ~19% measured here. The gap above is
  real and the recovery path is still missing; it is simply much narrower than the parked count suggests,
  and the parked count is the number an operator would have reached for.

- **(a) LANDED 2026-09-13 — the metric now means what its comment claims.** `on_dead_letter` asks the
  graph before calling a parking a loss (`repository.run_status`, the point read documented as "is this
  run in the graph?"): a run the graph already holds records the new `Outcome.PARKED_ALREADY_RECORDED`
  and logs at WARNING; a run it lacks still records `DEAD_LETTERED` and logs at ERROR, carrying
  `run_id` and `already_recorded` as fields so a dashboard can filter on them rather than on severity.
  Every fallback leans toward reporting loss — no run id, no repository, or a graph that raises all
  record `DEAD_LETTERED`, because a route that cannot ask must not answer "nothing was lost". Because
  the re-parks are what inflated the count, they stop counting as loss the moment the run heals, which
  is what collapses the two-order-of-magnitude gap. RED-first, 7 tests in
  `tests/unit/test_a_park_the_graph_already_holds_is_not_terminal_loss.py`. **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live.

- *Closes when:* (b) the DLQ stream becomes replayable — re-presenting `dlq.<appId>` to the ingest
  handler is idempotent on `run_id`. **It must NOT go through the outbox relay**, which was the obvious
  reuse and is a trap: `reconcile_cron._drain_outbox` re-PUBLISHES after ingesting (deliberately — the
  cascade's `/bronze-arrival` reacts to that announcement), so staging a dead letter there would put it
  back on `lineage.events.v1`, re-park it, and manufacture the flood this row measured. The replay must
  ingest without re-publishing. Until (b),
  `dapr.py`'s "recovery story stays replay-from-stream" should say what it actually means: a dead letter
  older than the stream's retention is lost.
- **"NOT CURRENTLY BLEEDING" IS FALSE AS OF 2026-09-15 — see [[LH-166]].** The DLQ has grown from the
  8,612 this row measured to **9,887**, with `dlq.lineage.events` at 9,856 and a delivery 11 minutes
  before that reading. The cause is events naming an output that carries no FGA tuples, which no
  principal can be authorized for, so they park on every redelivery. That is tracked separately
  because it is a PRODUCER defect rather than the replay gap this row is about — but it also raises
  this row's stakes: a replay door that re-presents those events would park them again.
- **RE-MEASURED 2026-09-15: the interim doc clause is DONE, so (b) is the whole remainder.**
  `on_dead_letter`'s docstring now states it outright — *"That bounds what recovery can reach: a dead
  letter older than the stream's retention has no path back, because nothing re-ingests the DLQ stream
  itself."* Nothing else in the row has moved.
- *So what is left is a FEATURE, not a correction, and it is worth saying so before someone picks it up
  expecting a small one:* a replay door that ingests a parked `dlq.<appId>` delivery WITHOUT
  re-publishing, idempotent on `run_id`. The trap is already documented above and is the reason the
  obvious reuse is wrong — `reconcile_cron._drain_outbox` re-publishes after ingesting, deliberately,
  because the cascade's `/bronze-arrival` reacts to that announcement, so routing a dead letter through
  it would re-park the event and manufacture the very flood this row measured.

**LH-002 · ~~The sweep's whole `storage_loss` population is UNGOVERNED residue — 3 of 3, and the row's original 32 were two other things~~ — CLOSED 2026-09-15: `storage_loss` no longer lumps benign residue with real loss**
`lineage, maintenance` · **HIGH**


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15: `STORAGE_LOSS_STATES` no longer exists anywhere. `reconcile_cron.py:98-100` reports
  MISSING_ON_STORAGE, UNGOVERNED and GRAPH_AHEAD as three separate fields with three distinct WARN bodies
  (:140/:146/:153), so an operator can filter the benign classes without silencing loss. UNGOVERNED is a real
  `ReconcileState` (`schemas.py:31`) produced at `core/reconcile.py:405`.
  *The residue-cannot-be-pruned caveat belongs to [[LH-144]], not here:* `cypher.py:345`'s `nc = 0` still
  disqualifies any Dataset carrying a `CREATED` edge from the orphan prune.

- **ROOT-CAUSED 2026-09-11: `storage_loss` IS TWO STATES, AND THE BENIGN ONE DOMINATES.**
  `STORAGE_LOSS_STATES = (GRAPH_AHEAD, MISSING_ON_STORAGE)` (`reconcile.py:272`), and `summarize_sweep`
  reports both under one name. `GRAPH_AHEAD` means the dataset is READABLE and merely sits at a lower
  version than the graph records — which is precisely this row's own re-measurement ("29 of the 32 are
  LIVE, catalog-registered, readable tables ... `count_rows` answers 3"). A table an e2e run dropped and
  recreated is at v1 while the graph still holds v3: benign, expected, and indistinguishable in the
  report from data destruction.
- *So the WARN is ~91% benign by this row's own count, and the label is what makes it unreadable.* The
  constant's comment argues the grouping deliberately ("the graph claims a version/dataset that on-disk
  Lance no longer has"), and that argument holds for MISSING_ON_STORAGE; for GRAPH_AHEAD it describes a
  rollback that a recreate also produces. One name for both is why the line reads as 32 destroyed
  datasets every tick.
- *The names live in GreptimeDB, which settles the composition question this row could not:* the
  `lineage_reconcile_storage_loss` WARN carries the full list, and it is e2e residue by name —
  `probe$nonexistent`, `e2e-ns$t178b2dda`, `tracka$…`, `trackansd…`, `cli07837ns$t2`. Measured stable at
  `storage_loss=32, unreadable=2, stale=268, checked=356` across every tick sampled.
- **DEPLOYED AND OBSERVED 2026-09-11 — the alarm was 91% false and the real number is THREE.** The
  split shipped in `main-b641103f`; the first sweep on the new image reports:

      before (old image)   storage_loss=32   unreadable=2                     (no graph_ahead field)
      after  (deployed)    storage_loss=3    unreadable=26   graph_ahead=31   provenance_holes=0

  So of the 32 datasets this row is named for, **3 are genuine loss**. 31 are `graph_ahead` — readable
  tables at a version below the graph's, which is what a drop-and-recreate leaves and is not loss at all.
  `unreadable` rising 2 -> 26 is the malformed-URI fix (`4b3bfa64`) doing its job: a relative
  `source_uri` now reads as "cannot say" instead of "destroyed".
- *And `provenance_holes=0` on the same tick* is the compaction fix (`5eb73751`) observed — no phantom
  `WROTE` edge planted for either version a compaction commits.
- **OBSERVED ON THE ROLL 2026-09-14, and the row's own headline is now PROVEN rather than argued.**
  The first sweep on `main-94e88b35`:

      before (main-b641103f)  checked=356 storage_loss=3 graph_ahead=31 unreadable=26 stale=320   (no ungoverned field)
      after  (main-94e88b35)  checked=356 storage_loss=0 ungoverned=11 graph_ahead=31 unreadable=24 stale=317

  `ungoverned` APPEARED, exactly as this row predicted the roll would show — and `storage_loss` went to
  **ZERO**. So the "3 real losses" this row reserved for itself were ungoverned residue too, and the
  title ("the whole `storage_loss` population is UNGOVERNED residue") is right about 3 of 3 in the
  strongest sense: there is no residual real-loss population at all.
- *What this row still owns:* the residue itself — 11 ungoverned datasets — which [[LH-144]] explains
  cannot be pruned, because an ungoverned table cannot be dropped by anyone. The loss half is closed.
- **THE SPLIT LANDED 2026-09-11.** `storage_loss` now means MISSING_ON_STORAGE alone; `graph_ahead` is
  its own report field and its own `lineage_reconcile_graph_ahead` WARN body, so an operator can filter
  the benign class without silencing real loss. `STORAGE_LOSS_STATES` is deleted — it had exactly one
  consumer and two tests pinning it, and the property those tests actually guarded (neither state is
  back-fillable) is re-pinned against the REPORT rather than against a tuple, which is what an operator
  reads. Pinned by `tests/unit/test_a_readable_dataset_is_not_reported_as_storage_loss.py`.
  UNDEPLOYED: the numbers above are from the running estate, which predates this.
- *HOW NOT TO MEASURE THIS, learned by doing it wrong 2026-09-11:* classifying the 32 by opening their
  graph URIs from a `kubectl exec` subprocess inside the lineage pod does NOT work and does not fail
  loudly. A fresh `LineageSettings()` there carries `aws_secret_access_key` of length **0** — the running
  app hydrates it from the Dapr secret store at boot, and a subprocess does not — so every open fails on
  authentication and reads as UNREADABLE. The attempt returned 32/32 unreadable against a sweep
  reporting `storage_loss=32, unreadable=2`, and the contradiction is the only reason it was caught.
  Classifying these requires the sweep's own process, i.e. the deployed split.
  (Worth noting as a positive: the secret being absent from a fresh settings object is the estate's
  "never secret through envs" rule observably holding — the value exists only after the store fetch.)
- *What is still open here:* the residue itself. The split makes the question answerable — once deployed,
  `graph_ahead` vs `storage_loss` says how many of the 32 are recreated tables and how many are real —
  but it prunes nothing and this row's `prune_orphan_datasets` remedy is still measured not to work.
- *Smaller, truer fix than the row asks for:* split the two states into their own report fields and
  their own WARN lines, so `graph_ahead` (benign after a recreate) stops being spelled as loss. That is
  a `SweepReport` change plus `summarize_sweep`/`log_sweep`, no new probe and no extra I/O, and it makes
  the residue question answerable instead of arguing about pruning first.
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
- **THE REMAINING THREE ARE ALL UNGOVERNED, measured 2026-09-11 — so the wait for retention is not the
  answer and this row has a mechanism instead.** The sweep's `storage_loss` line now names exactly
  `aud1ns$sub3$tt`, `e2e-ns$t178b2dda` and `probe$nonexistent`, and asking OpenFGA about each one
  DIRECTLY (not by set difference) returns **zero tuples**, against a control `acme-bronze$events` that
  returns 2. All three carry ABSOLUTE `s3://` URIs, so they are correctly in the loss path and not the
  relative-URI class [[LH-141]] owns — the bytes really are gone. What is wrong is the NAME: the class
  means "a bad restore, a wipe... the data is gone; only a human can answer for it", and a table nobody
  holds a single tuple on is not a governed table whose data was lost. After the `graph_ahead` split
  fixed 29 of 32, the remaining 3 are a SECOND miscategorisation of the same kind.
- **IMPLEMENTED 2026-09-11, and the helper is verified against the LIVE store rather than a mock.**
  `fga.governed_objects` run against the real OpenFGA returns **1232 governed tables in 0.31 s**, bare
  ids, and classifies the probes exactly as predicted — `acme-bronze$events` governed,
  `probe$nonexistent` and `research-bronze$events` not. `ReconcileState.UNGOVERNED`,
  `SweepReport.ungoverned` with its own WARN line, `reconcile_all(governed=...)` and
  `reconcile_cron.governed_tables` complete it; the check sits AHEAD of the storage read, so residue
  now costs no object-store round-trips.
  *The fail-safe is the part that matters:* a store that cannot be enumerated returns `None` (unknown),
  never an empty set — an empty set would classify every dataset ungoverned and silence the loss axis at
  exactly the moment authorization is in trouble. Pinned by its own test, as is the paging bound: a
  cursor that never empties RAISES rather than answering short, because this set is asked what is ABSENT
  from it and a partial read inverts that answer instead of degrading it.
- **THE POST-DEPLOY PREDICTION, stated before the roll so it can falsify the change:** `storage_loss`
  **3 -> 0**, `unreadable` **26 -> 24** (`acme-bronze$zzprobe8926` and `uiproof-gold$catalog` move),
  `ungoverned` **0 -> 11**, `checked` unchanged at 356.
  **THE PRE-ROLL BASELINE, measured 2026-09-13 and stable across consecutive ticks**, so the comparison
  needs no memory of what it used to say:

      lineage_reconcile_sweep checked=356 backfilled=0 storage_loss=3 graph_ahead=31 unreadable=26
        dangling_blobs=0 stale=320 contract_violations=0 provenance_holes=0 outbox_drained=0
        outbox_stranded=0 pruned_runs=0 pruned_events=0

  Note what is ABSENT: there is no `ungoverned` field at all, because the state ships in `1ba4156e` and
  that has not rolled. Its APPEARANCE is the first thing to look for; a rolled image whose sweep still
  has no such field did not take the change.
  The 11, not the 58: the drop stamp is checked FIRST, so the 47 purge-dropped datasets never reach the
  governed branch — which is correct, a deliberate drop is already handled. I had written 58 and the
  arithmetic corrected it; the 11 that remain are exactly [[LH-144]]'s real population, which is the
  convergence that makes both rows one finding.
- *The fix, specified and costed rather than sketched:* a dataset carrying no authorization tuple is not
  a live governed table, and the sweep should say so in its own words.
  1. `service_kit.governed.fga` has `check`, `batch_check`, `list_objects` and **no tuple read**, so it
     needs one guarded helper that pages `Read` with NO `tuple_key`. Measured against the live store:
     **51 pages, 5027 tuples, 0.1 s** for the whole estate — so this is one call per SWEEP, not the 356
     per-dataset checks the naive shape would cost. (A `tuple_key` with an empty object id is refused,
     which is what makes the no-`tuple_key` form the one that works.)
  2. `ReconcileState.UNGOVERNED`, beside `UNREADABLE` and for the same stated reason: neither loss nor
     health, and collapsing it into loss is what sends an operator after data nobody lost.
  3. `reconcile_all(..., governed: set[str] | None = None)` — it is already pure over injected
     callables, so this needs no new repository method; a dataset absent from the set classifies
     UNGOVERNED and skips the downstream axes exactly as the `dropped` stamp does.
  4. Its OWN report field and WARN line, never folded into `storage_loss` — the [[LH-143]] lesson, which
     this row is otherwise about to repeat.
  *The id mapping is exact and was checked:* `canonical_object_id` joins segments with the delimiter, so
  a graph dataset name and its `table:` object id agree byte-for-byte — which is the property that
  docstring insists on, and the reason the measurement above matched at all.
- **AND THE PRUNER CANNOT FIRE AT ALL — measured across the whole graph, not sampled.** This row says
  `prune_orphan_datasets` "leaves 31 of the 34 nodes in place because they carry a CREATED edge". The
  structural version is worse: asked of AGE directly, **`prune_orphan_datasets` matches 0 datasets
  today**, and **1145 of 1247** Dataset nodes carry a CREATED edge, so they can never satisfy its
  `nw = 0 AND nr = 0 AND nc = 0`.
  *And it can never change*, which is the part no row states. Every one of the 1150 CREATED edges comes
  from a **`User`** node, not a `Run` — asked directly, `MATCH (x)-[:CREATED]->(:Dataset) RETURN
  labels(x)[0]` returns `User|1150`. `prune_runs` deletes Run nodes; nothing deletes a User's creation
  record, nor should it, because that edge IS the creator provenance. So the coupling comment on the
  prune — "a dataset becomes prunable exactly when its last run goes" — is false for 1145 of 1247
  nodes, and waiting for retention prunes none of them on 2026-09-30 or ever.
- *THE GUARD IS DELIBERATE, WHICH IS WHY THIS IS A SCOPE PROBLEM AND NOT A BUG.* `nc = 0` is pinned by
  `test_a_DECLARED_table_is_not_residue` — "a declared-but-unwritten table would be pruned as residue"
  — and for that case it is exactly right. What nobody measured is that a CREATED edge is written for
  EVERY table a user makes and is never removed, so a guard meant for the narrow declared-but-unwritten
  case permanently covers the whole user-created estate. The fix is a decision about how to tell
  "declared and never written" from "written, then its runs aged out" once the runs are gone — which is
  the owner's call, not a predicate to change unilaterally.
- *AND IT LANDS ON [[LH-011]], which closed today deferring to exactly this.* That row records
  `stale=268` of `checked=356` as "a retention problem with its own row and its own date
  (2026-09-30)". Retention cannot reclaim those nodes, so the deferral points at a remedy that
  structurally cannot fire. 265 of the 268 are properly GOVERNED catalog tables written once by a test,
  so the ungoverned split above does not reach them either. Recorded here rather than reopening LH-011,
  because the remedy belongs to this row.
- *What that costs, measured on one tick:* 296 of the 356 swept datasets — **83%** — are named in at
  least one WARN line every 300 s, `stale` alone carrying 268 names in a single ~6 KB record, and 29 of
  the 31 `graph_ahead` datasets double-reported as stale as well.
- *Closes when:* the sweep stops naming ungoverned residue as storage loss — which the tuple check
  settles NOW rather than on 2026-09-30 — and a real `storage_loss` line names only datasets that are
  both governed and gone. Retention may still tidy the nodes; it was never what made the warning wrong.

**LH-003 · ~~The `parent`, `processingEngine` and engine-version run facets are neither emitted nor stored, and the graph has no version/branch/tag/clone nodes~~ — THE CONDITION-1 HALF CLOSED 2026-09-11, THE REST STRUCK**
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
`lineage, catalog, maintenance, medallion, service-kit` · **HIGH** · **R10 ACKNOWLEDGED 2026-09-16 (owner): the R-series stands**

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
  **AND THE OUTBOX IS NOT EMPTY — measured on the live estate 2026-09-16, which is what re-measuring
  this row found.** `lineage_outbox_drained drained=0 stranded=6`, unchanged across every tick. Six
  events were staged — so the write happened and its provenance WAS captured — and the graph refuses
  every one of them:

      lineage_outbox_event_unauthorized  outbox_key='36944760-…@COMPLETE'  author='e2e'
        reason='can_write_data required to amend run 36944760-…: e2e_outbox_ds'

  42 refusals across 6 distinct `run_id`s, all `author='e2e'`, against two datasets — `e2e_crash_ds`
  (35) and `e2e_outbox_ds` (7). **The drain is behaving exactly as designed**: a refusal is not poison
  and not transient, so it strands rather than drops, because destroying the only durable copy of a
  committed write's provenance is the wrong answer to "you may not record this".
  **THE OBVIOUS REMEDY IS NOT AVAILABLE, measured 2026-09-16 before building it.** "Move a
  permanently-refused event aside to a `_refused/` prefix so it leaves the drain and the gauge" is the
  natural design — `_staged_infos` lists with `recursive=False`, so a sub-prefix is invisible to
  `backlog`, `list_events` and the drain, and the same outbox prefix keeps it inside the STS scope. It
  cannot be done BY THE RELAY: a copy is a PutObject, and the relay's identity is granted only
  `s3:DeleteObject` on its own outbox (`rustfs-scoped-users.yaml`), pinned by
  `test_the_lineage_plane_writes_nothing_it_does_not_own.py`, which asserts it gets no `PutObject`
  anywhere. Widening that grant to tidy a gauge would trade a real least-privilege boundary for a
  cosmetic one, and it is the same shape as the 2026-09-10 defect where `delete_file` re-created a
  directory marker and the drain had never once succeeded.
  **SHIPPED 2026-09-16 — `refused` IS ITS OWN NUMBER, AND THE ALERTS NO LONGER LIE.** `DrainOutcome`
  and `SweepReport` carry `refused` beside `drained`/`stranded`; the `PermissionDeniedError` branch
  counts it instead of folding into `stranded`; `outbox.events.refused` is emitted every tick (zero
  included, so the series exists before it is needed). The event is still LEFT STAGED — this split the
  reporting, not the handling.
  **THE ALERT HALF IS THE PART THAT MATTERED.** `chart/alerting/rules.yml` carried
  `LineageOutboxNotDraining: max(outbox_depth) > 0` at severity CRITICAL, and depth has a permanent
  floor equal to the refused count — so the moment `observability.alerting.enabled` was turned on it
  would have fired forever with a false description, making a genuinely stuck relay indistinguishable
  from a settled answer. (It is NOT firing today: there is no vmalert and no alertmanager pod on this
  estate, and the toggle is off — `values.yaml:3006` records enabling it as a separate open decision.)
  Both rules are now guarded on "nothing is moving" rather than "something is staged", and a
  `LineageOutboxEventsRefused` WARNING keeps the condition visible without paging.
  *`or vector(0)` is load-bearing and `make alert-rules-check` proved it:* an `and` against an EMPTY
  vector yields empty, so the first guarded version silently never fired at all — a fix for a false page
  turning into a permanent no-page, which is strictly worse. Pinned by three new promtool cases: stuck
  with counters ABSENT still pages, the measured live scenario (depth 6, refused each tick, aging 2.1
  days) does not, and a deep-but-draining backlog does not.
  **OBSERVED ON THE LIVE ESTATE 2026-09-17 — all three halves, none of them inferred.** Rolled as
  `main-bba6b3a3` to `rask-lineage` and `rask-catalog` (they share the `lance-rest-catalog` image and
  lineage was two commits behind). (1) The first tick after the roll read
  `lineage_outbox_drained drained=0 stranded=0 refused=6`, against `drained=0 stranded=6` before it,
  and it has held every 300 s since (09:41 through 10:06 sampled). (2) `outbox_events_refused_total`
  exists in the live GreptimeDB `information_schema` — RE-QUERIED rather than assumed, because the
  OTLP exporter mangles the name and `outbox.events.refused` could have landed under any suffix — and
  reads `6` for `service_name='lineage'` while `outbox_events_stranded_total` reads `0`.
  (3) **THE COUNTER ACCUMULATES, and that is the leg that decides whether the alert guard works at
  all:** it read `6` at 22:06 and `12` at 22:11, so `rate(outbox_events_refused_total[15m]) > 0` and
  the `and ... == 0` guard genuinely suppresses `LineageOutboxNotDraining`. Had `record_refused`
  reported a per-tick CONSTANT instead, the rate would be 0, the guard would pass, and the alert would
  page on the settled-refusal condition exactly as before the fix — a guard that looks right in the
  rules file and is inert in the store. Measuring the second tick is what separated the two.
  *So the fix is to change what COUNTS, not to move the object:* report `refused` as its own number and
  its own metric rather than folding it into `stranded`, which needs no write permission, destroys
  nothing, and leaves the event recoverable if the grant ever lands. The drain already NAMES the
  refusal separately (`lineage_outbox_event_unauthorized`) and then folds it into `stranded += 1`
  (`reconcile_cron.py:493`) — that fold is the whole defect, because `stranded` is documented as "a tick
  failed while it recovered everything else" and a governance refusal is not a failed tick.
  *What the estate has no answer for is the AFTER.* These six will strand forever, be re-refused every
  tick, and hold `outbox.events.stranded` permanently non-zero — which a dashboard reads as an ongoing
  fault rather than a settled one. That is [[LH-148]]/[[LH-151]]/[[LH-166]]'s question — what a
  permanently-refused message resolves to — seen from the OUTBOX side, and it now has live evidence
  instead of an abstraction: six events, 42 refusals, no door that can retire them.
  *They are e2e residue rather than tenant provenance* (`author='e2e'`, `e2e_*_ds`), which lowers the
  severity and does not change the shape: an unprivileged producer staged provenance and the estate
  cannot clear it.

  **THE CP-007 BLOCKER IS CLEARED (2026-09-11).** Ingest can write the outbox — the credential is vended through the catalog's outbox door and the whole path was observed end to end (`lineage_outbox_drained drained=1 stranded=0`), so staging is worth something now. What remains is the CONSOLIDATION, and it is a LATENT trap rather than present loss: `lineage_kit/emitter.py` swallows a transport failure with `log.warning("lineage_emit_failed")` and stages nothing, while the backstop that saves ingest (`_stage_undelivered`) lives in ingest rather than in the kernel. Today that costs nothing, because ingest is the kernel's only service importer. It costs a silently-lost event the day a second HTTP producer is added by someone who reasonably expects the emitter to be durable — which is precisely the duplication R10 exists to end.
- *Closes when:* Delete `service_kit.lancekit.openlineage`/`lineage_emit` and the per-service `lineage_emit.py` copies, route every producer through `packages/lineage-kit`'s emitter and one `RunEvent` builder, and stage each event in an outbox before transport so a failed emit is retried rather than dropped.
- **MEASURED 2026-09-16 AT HEAD, AND R10's PRESCRIPTION IS WRONG FOR THIS ESTATE.** R10 reads "every
  producer stages before transport", and the row derived from it that the kernel should carry the
  backstop. It cannot: ingest's `_stage_undelivered` stages through `_outbox_storage_options()`, which
  VENDS an STS credential from the catalog because "this service holds no key"
  (`services/ingest/src/ingest/lineage.py:253-262`). Moving that into `lineage-kit` couples the kernel
  to the catalog, and `lineage-kit` runs inside sealed Ray runners that have no catalog access. The
  kernel's own comment already states the hook is injected "NOT BUILT IN" for this reason
  (`test_an_undelivered_event_reaches_a_recovery_hook.py:13-15`). So the durable half is a
  PER-PRODUCER obligation, not a kernel one, and any consolidation must keep the hook injectable.
- **THE DEFECT THE SILENCE HID, found while measuring and fixed.** `runs.py` read
  `if self.emitter.emit(event) or self._on_undelivered is None: return` — a producer with no recovery
  hook took a SILENT return, byte-identical in behaviour to a successful emit. Four of the five
  production construction sites pass no hook (`actor.py:52`, `actor.py:83`, `stage.py:48`,
  `runs.py:276` — the actor mixin, the `@stage` decorator and the offline `run()` block, i.e. every
  Ray-runner path); only ingest passes one. That path now logs `lineage_event_unrecoverable` at ERROR
  with run id, job and state. Deliberately NO new counter: the emitter already records the TRANSPORT
  drop, so a second would double-count one event — what was missing was severity and finality.
- **THE ROW IS TWO ROWS WITH DIFFERENT BLOCKERS** (3-lens adversarial verification, 2026-09-16, all
  three refuters independently reaching the same split):
  * *The CONSOLIDATION half is UNBLOCKED and workable at M-L effort* — retire
    `service_kit.lancekit.{openlineage,lineage_emit}`, repoint their one live consumer
    (`services/annotator/src/annotator/annotations/commit.py`), collapse the 805-line catalog and
    337-line maintenance kernels and the three `RunEvent` builders. Note `lineage_kit.emitter` has NO
    Dapr transport at HEAD, so "route every producer through it" is a rewrite, not a move.
  * *The DURABILITY half needs a ruling*, and it is not a procedural ack: does the catalog's
    commit->publish crash window stay permanently residual, or does it get a transactional producer?
    The only design written down is "make the Ray job the durable producer" (`docs/RESILIENCE.md:83`),
    which condition 3 forbids, and LANCE-ONLY leaves no relational store to replace it with.
- *RECOMMENDED ANSWER, not yet ratified by the owner:* neither WONTFIX nor a new producer. Treat the
  **Lance commit log as the source of truth and the lineage graph as a projection of it** — the
  log-first / change-data-capture shape — which makes a lost publish VIEW LAG rather than data loss,
  needs no Ray and no second format, and turns the storage->graph reconcile sweep from a backstop into
  the documented view maintainer. The work that follows is a COMPLETENESS gate on that sweep (every
  committed version eventually in the graph, with a measured upper bound on lateness), which is
  assertable in a way "we accept the window" is not.

**LH-169 · ~~A `kubectl set image` on part of an image stem arms the next `helm upgrade` to revert it, and nothing fails when a stem is split~~ — CLOSED 2026-09-16 (stem converged and the upgrade now refuses a split)**
`chart, catalog, lineage, medallion, maintenance` · **HIGH** · found 2026-09-16 by the backlog audit's completeness critic, which looked for defects no row covered

- *Why open:* TEN deployments share the `lance-rest-catalog` image stem, and the dev loop rolls them with
  `kubectl set image` one service at a time. Measured live 2026-09-16 after exactly that: `rask-catalog`,
  `rask-lineage`, `rask-maintenance` and `rask-medallion-producer` ran `main-9e5ff5b3` while
  `rask-annotator`, `rask-bronze-to-silver`, `rask-media-to-silver`, `rask-search`, `rask-silver-to-gold`
  and `rask-viewer` ran `main-3803cc1d`.
- **THE HAZARD IS THE UPGRADE, not the split itself.** The deployed release (rev163, decoded from
  `sh.helm.release.v1.rask.v163`) pins `image.tags['lance-rest-catalog'] = 'main-3803cc1d'` for ALL ten. So
  the next `helm upgrade` — *even one with byte-identical values* — silently reverts the four that were
  rolled forward. This is the same shape the chart already records for hand-deployed images, applied to a
  stem rather than a service.
- *The estate cannot capture its way out of it either:* `scripts/k3s-pins.sh:61-70` REFUSES to generate a
  pin file while any stem runs more than one tag, by design ("no correct pin file exists until they
  converge"). That refusal is correct and is a stderr exit nobody reads, so the split persists silently
  until an upgrade reverts something.
- *RE-MEASURED 2026-09-16, and the split has not closed:* `main-3803cc1d` on six (annotator,
  bronze-to-silver, media-to-silver, search, silver-to-gold, viewer) against `main-9e5ff5b3` on four
  (catalog, lineage, maintenance, medallion-producer), while the image carrying the fixes is
  `main-38b85684` and is deployed nowhere. The cost is not hypothetical while it lasts: the catalog is
  logging ~2,500 `vend_base_path_unsanctioned` refusals an hour that `6d923a40` fixes.
- *Closes when:* The stem converges (one image from a commit carrying every change, all ten rolled), and a
  GATE makes a split loud rather than leaving it to a script's exit code — the natural home is beside the
  pin generation the chart already depends on, so an upgrade cannot be attempted from a split estate.
- **CLOSED — both halves done and observed 2026-09-16.**
  * *The stem converged.* `main-fd3999f4` built with Dagger (`scripts/dagger-image.sh --name rest-catalog
    --push`) from a commit carrying BOTH pending changes — `6d923a40`'s vend-door sanctioned-bases fix and
    [[LH-141]]'s crossing guard — and all ten stem-mates rolled to it by container NAME, never `*`
    (a wildcard `set image` overwrites init containers too, which is how `rask-lineage` was wedged
    `Init:0/1` earlier today). `kubectl get deploy` now reads **10 x main-fd3999f4**, and
    `scripts/k3s-pins.sh` answers *"stems converged: 25 first-party images, one tag each"* where it
    refused before. `chart/values-live-pins.yaml` regenerated, so an upgrade carries the new tag.
  * *The gate landed in front of the destructive operation.* `make k3s-up` now has `k3s-stem-check` as a
    PREREQUISITE, which runs `scripts/k3s-pins.sh --check-only` — the same detection the pin generation
    already had, reachable before only by asking for a pin file. Pinned by
    `tests/unit/test_an_upgrade_cannot_be_attempted_from_a_split_image_stem.py`, which drives the script
    against a fake `kubectl` so the rule is exercised without a cluster, and asserts the refusal names
    both tags and both owner sets — an operator cannot act on "something diverged".
- **What the roll bought, measured on the live estate immediately after:**
  * `vend_base_path_unsanctioned` in the catalog: **~2,500/hour -> 0**. The maintenance sweep's
    *"not permitted to read the base at `s3://lance-catalog/models/`"* refusal is gone from every tick.
  * Sweep refusals fell **320 -> 255** on the first tick.
  * **Zero rewrites now sign with the ambient key**: 220 SCOPED vend decisions, 0 AMBIENT, against 137
    SCOPED + 3 silent-ambient writes before. See [[LH-141]] for the four crossings this exposed.

**LH-006 · ~~`UPSTREAM`/`DOWNSTREAM`/column-lineage Cypher is unbounded `*1..`, and Dataset nodes carry no `latest_version`~~ — CLOSED 2026-09-16**
`lineage` · was med

- **CLOSED BY THREE CHANGES, AND THE THIRD IS NOT THE ONE THIS ROW ASKED FOR.** The walks gained a
  reachable bound (2026-09-15); every traversed EDGE label gained endpoint indexes (`16dd2da6`); and the
  39 indexed LOOKUPS were rewritten into the form AGE can serve from an index (`17e41ffb`). The
  `latest_version` property the row prescribed was NOT added, deliberately — see below.
- *Why the original ask:* The `/producers` and retention/index clauses closed; traversal depth did not. The query
  CONSTANTS are `*1..`, and `age.py:44` names the unbounded path over a grown graph as why a pooled
  connection cannot be pinned.
- **RE-MEASURED 2026-09-11 — "no door applies a ceiling" is FALSE, and the ask is a semantic change
  rather than a missing bound.** The machinery is all present and named differently than this row says:
  the helper is `cypher.bounded_walk` (not `with_depth`), the ceilings are `cypher.MAX_WALK_DEPTH` (20)
  and `cypher.MAX_COLUMN_DEPTH`, and doors already use them — `datasets.py:103` takes
  `depth: Annotated[int | None, Query(ge=1, le=MAX_WALK_DEPTH)]`, and the column-graph walk validates
  against `MAX_COLUMN_DEPTH` (`repository.py:598`) and iterates a bounded frontier with a visited set.
  What is genuinely unbounded is the four NEIGHBOURS doors: `/datasets/{name}/upstream`, `/downstream`
  and the column pair pass no depth, so `bounded_walk` receives `None` — which its own docstring calls a
  DELIBERATE unbounded walk, not an oversight.
- *Which makes the remaining work a decision, not a fix:* `/upstream` answers "what this was derived
  from", and bounding it by default would silently TRUNCATE an ancestry a caller may depend on. Adding
  an optional bounded `depth` changes nothing by itself; changing the DEFAULT changes answers. That is
  the call this row actually needs, and it is not one to make from the resilience argument alone.
- *Attempted and reverted 2026-09-11:* a ceiling constant added in `repository.py` — a duplicate of
  `cypher.MAX_WALK_DEPTH` with a different value (25 vs 20), caught before commit. Two definitions of
  one bound is worse than the unbounded walk.
- **THE FOUR STATEMENTS ARE BOUNDED AS OF 2026-09-15, and the two halves failed differently — which is
  why "no door applies a ceiling" read as false while the defect was real.**
  * `/upstream` and `/downstream` never DECLARED a `depth`, so although `repository.upstream/downstream`
    have accepted one and applied `bounded_walk` since the helper landed, every request through the
    door passed `None` and reached the unbounded `*1..`. The bound existed and was unreachable.
  * `column_upstream`/`column_downstream` took no `depth` at all and handed `cy.COL_UPSTREAM` /
    `cy.COL_DOWNSTREAM` to `fetch` RAW — so the column plane had no ceiling to reach, not a default to
    override. That is the inert-argument shape, and no signature or type check can see it.
  Both now take `Annotated[int | None, Query(ge=1, le=...)]` on the door and route through
  `bounded_walk`, mirroring `/graph` and the column-graph walk exactly.
- *THE DEFAULT IS DELIBERATELY UNCHANGED:* omitted, all six walks are still unbounded — the previous
  behaviour and what an un-rooted caller wants. Silently truncating a provenance answer is a worse
  failure than a slow one, and this row asked for a reachable bound rather than a new default.
- *Why it is worth doing at all, rather than tidiness:* `age.py` names the unbounded walk over a grown
  graph as the reason a pooled connection cannot be pinned, and an unbounded correlated walk against
  the live graph OOM-killed the AGE container on 2026-09-15 (restart 0->1, recovered). Column lineage
  is the walk most able to multiply, since it fans out per FIELD rather than per dataset — and it was
  the one with no ceiling at all.
- *Pinned by `tests/unit/test_every_lineage_walk_can_be_bounded.py`*, which checks the DOOR offers the
  parameter, the repository accepts it, AND the column methods route it through `bounded_walk` — the
  third assertion existing precisely because the second passes for an argument the query ignores.
- **THE REMAINING HALF IS CLOSED 2026-09-15, AND NOT BY THE PROPERTY THIS ROW NAMED.** The ask was a
  `latest_version` on the Dataset node maintained on write, so the tip is read off the node instead of
  aggregated from WROTE history. Measuring the aggregate first found a better root cause, and the
  denormalization is no longer the right fix.
  * `EXPLAIN ANALYZE` of `LATEST_WRITE_VERSION` against the estate's hottest dataset
    (`acme-silver$features`, 457 WROTE edges) read **all 7,095 WROTE rows and all 7,109 Run rows**, in
    8.3 ms. The cost was O(whole graph), not O(that dataset's edges) — and it runs INSIDE the ingest
    transaction on every event, via `repository._schema_is_current`.
  * `pg_indexes` over the deployed `lineage` schema returned **seven rows: five functional indexes on
    VERTEX labels plus the two `_ag_label_*` primary keys. No edge label carried an index at all**,
    while all seven are traversed and none is bounded — WROTE 7095, OF_JOB 6394, HAS_COLUMN 4236,
    READ 1424, CREATED 1324, DERIVED_FROM_COLUMN 380, DERIVED_FROM 70.
  * Verified on a prod-shaped graph (1,445 datasets / 7,109 runs / 7,095 edges, the same 457-edge skew)
    built in a throwaway `apache/age` via Dagger, so the live graph was never written to:
    **6.778 ms seq-scanning 7,095 edges -> 1.783 ms on a bitmap index scan touching the node's own 457.**
    The change of ORDER is the point, not the 3.8x: the first plan grows with the estate, the second
    with the node's degree.
- *Why the index and not the property:* a maintained `latest_version` is the last-writer-wins stamp this
  module already refused once — `cypher.py` records that a mutable `dropped` flag was rejected in review
  because a redelivered event re-stamps a live dataset, and a back-filled or reordered write would stamp
  a stale tip the same way. An index adds no write path and no drift, and it fixes EVERY walk rather
  than one query. `latest_version` still appears nowhere, and `MERGE_DATASET` still sets only
  `d.namespace` — deliberately.
- **THE SECOND INDEX SHAPE IS ALSO CLOSED (`17e41ffb`), as its own change rather than a rider on the
  first.** The vertex indexes that DO exist were unreachable from the query form the code emitted: AGE
  compiles the inline `MATCH (d:Dataset {name:$name})` into a `properties @>` containment filter, which
  no B-tree on an extracted property can serve. Measured live: inline Seq Scan 0.420 ms against
  `MATCH (d:Dataset) WHERE d.name = $name` **Index Scan using `lineage_dataset_uniq` 0.053 ms**. All
  **39** statements are rewritten; the 6 `MERGE` are untouched because there the pattern IS the merge
  key, and `:User` keeps its inline form because it carries no index.
- *Plans on the live graph after the roll:* `RUN_BY_ID` Seq Scan 2.086 ms -> Index Scan **0.124 ms**;
  `SOURCE_URI` 0.291 -> **0.019**; `GET_DATASET_GOVERNANCE` 0.686 -> **0.264**; `DATASET_COLUMN_NODES`
  1.943 -> 0.958; `PRODUCERS` 18.945 -> 16.287.
- *EQUIVALENCE WAS MEASURED, NOT ARGUED,* because a semantic slip here damages provenance: the 17
  read-only statements were driven against the LIVE graph in both forms and returned identical result
  sets (16 over real data; `CREATOR` was empty for that dataset and was re-driven against one holding a
  `CREATED` edge — `service-ingest` both ways). The 22 writing statements were driven in a throwaway
  `apache/age` via Dagger, old form into one graph and new into another from an identical seed, then
  compared by `EXCEPT` over the raw agtype properties of every label: **TOTAL DIFFERENCES 0**, every
  label non-empty so no row passed vacuously.
- *Three shapes were checked rather than assumed:* no `OPTIONAL MATCH` is among the 39 (the module has
  nine, and that is the one shape where lifting a predicate out of the pattern changes the ANSWER, not
  just the plan); none of the 39 contains `WITH`, `UNION`, `CALL` or `FOREACH`, so every one is a
  single-part query where the `WHERE` can only bind to its own `MATCH`; and the `MERGE` exemption has
  its own test, so a future tightening cannot turn the rule into one `MERGE` cannot satisfy.
- *Observed on the roll:* 1,314 `POST /lineage-events` answered 200 — the ingest path runs the
  rewritten `LATEST_WRITE_VERSION` inside every transaction — with graph totals unchanged.
- *Gated by `tests/unit/test_an_indexed_lookup_uses_the_form_age_can_index.py`*, which imports the
  module and inspects its constant VALUES rather than the source, so a statement split across literals
  or built by `.replace()` (as `RUN_BY_ID` is) is checked as the string the database receives.
- *Closed by `16dd2da6`*, gated by `tests/unit/test_every_traversed_edge_label_has_an_index.py` — a
  DERIVED assertion reading the traversed labels out of `cypher.py`'s own source (a new edge label fails
  the gate rather than silently shipping a full-table walk), plus a seam test driving
  `ensure_graph_constraints` and reading the DDL it really emits, which is the only one of the four that
  catches a builder-only mutation such as indexing one endpoint.

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

**LH-142 · ~~Credential vending 401s on EVERY maintenance rewrite, so the STS path is inert and every rewrite is signed by the ambient process credential~~ — FIXED AND OBSERVED 2026-09-11**
`maintenance, catalog, chart` · **HIGH** · found 2026-09-11 by reading the running estate, not by a review

- **CLOSED BY THE DEPLOY, observed on the live estate 2026-09-11.** The row predicted this: "the deploy
  may close this by itself... re-measure immediately after". Deployed `main-b641103f` (the image carrying
  the corrected token derivation from `344e9763`); maintenance restarted and its own log window shows:

      before   300 x `credential vending unavailable (401)`   300 x AMBIENT   0 x SCOPED
      after      0 x 401                                                     41 x SCOPED

  So every rewrite now signs with a table-scoped 900-second vended credential instead of the ambient
  process credential. The STS path the estate's storage rule is built on is doing its job for the first
  time in this window.
- *Why it was broken is now readable:* both ends derive the dedicated token from the chart helper, and
  the helper was hashing a value no values file defined (`%!s(<nil>)`). Deploying the corrected
  derivation and restarting both the presenter and the verifier is what made the two agree.
- *Measured, and the ratio is exact.* 600 `maintenance.services.credentials` records since 20:00 on the
  live estate split **300 / 300**: every one is either `credential vending unavailable for <table> (401)`
  or `write credential AMBIENT for <table> — nothing vended; this rewrite is signed by the root key`.
  A perfect 1:1 pairing and **zero** successful scoped vends. So the STS machinery the estate's
  zero-trust storage rule is built on is inert, and every rewrite uses the process credential instead.
  **NOT the tenant root, and this row said so at first by quoting the deployed log.** That message is
  the OLD wording; `credentials.py` has since corrected it in place — "NAME THE KEY, do not rank it.
  'the root key' is an assertion this function cannot make" — and measured on the running pod,
  `MAINTENANCE_S3_ACCESS_KEY_ID=rask-maintenance`, a scoped identity. The defect is real and narrower
  than "root": the blast radius is the maintenance identity's whole scope instead of one table prefix
  for 900 seconds, rather than the tenant's everything.
- *401, not 403, and the distinction is the whole diagnosis.* 403 is "this identity may not"; 401 is
  "the presented credential may not CLAIM this identity" — the refusal `service_principal` raises when a
  privileged subject arrives with the SHARED bearer instead of its dedicated token. `openbao.yaml`'s own
  seeding comment predicts exactly this for this identity: "`maintenance.catalogServiceIdentity` is on
  this list because the catalog's `LANCE_PRIVILEGED_SUBJECTS` names it: a privileged subject whose token
  is not seeded resolves to nothing, falls back to the shared bearer, and is refused — so the SEED is a
  third half of this control." The control and its prediction are both present; what is missing is the
  token actually reaching the door.
- *Why nothing caught it:* the fallback is INFO-level and per-dataset, so it reads as routine. LH-078
  tracks its milder ancestor (8 vends per tick 403ing) and rates it LOW on the strength of "207 AMBIENT
  → 8". That ratio no longer holds: it is now every vend.
- *It also invalidates a closure.* LH-134 records "after: 0 records/min" as evidence its lazy-vend fix
  worked. That fix (`27ce13f3`) is NOT deployed — the running maintenance image `main-8c229296` has no
  `_may_write_anything`, checked in the pod — so the observed halt is vends FAILING, not vends being
  skipped. Same number, opposite cause, and the worse one.
- **NARROWED 2026-09-11 to the store, by eliminating everything else.** `service_headers`
  (`catalog_identity.py`) presents the dedicated token when `secrets_from_dapr` is on AND the resolver
  finds one, else the shared bearer. Checked in the RUNNING pod rather than the chart:
  `MAINTENANCE_SECRETS_FROM_DAPR=true` and `MAINTENANCE_CATALOG_SERVICE_IDENTITY=service-maintenance`.
  Both chart lists carry the identity — minted (`openbao.yaml:218`) and demanded
  (`services.yaml:243,302`). So the toggle is on, the identity is right, and the two lists agree.
  What remains is the token's PRESENCE or VALUE in the store at one end or the other, which needs
  secret-store access to settle and is the next step for whoever has it.
- *A hypothesis that was wrong, recorded so it is not retried:* grepping `services.yaml` for
  `MAINTENANCE_SECRETS_FROM_DAPR` finds nothing and suggests a missing toggle. It is set in
  `maintenance.yaml:248` and `maintenance-worker.yaml:204` — maintenance has its own template. The
  running pod is what settled it.
- *The deploy may close this by itself, which is a reason to try it first:* `344e9763` changes how every
  dedicated token is DERIVED, and the upgrade re-runs the seed. If the 401 is a stale or absent seeded
  value, re-seeding both ends consistently is exactly what fixes it — so re-measure this row immediately
  after the deploy before doing anything else to it.
- **THE VISIBILITY HALF IS DONE** (`eb564cd6` + `4fcba209`). The fallback is now a counter and an
  alert rather than an `info` line per dataset, so "every rewrite is ambient" cannot be the quiet state
  again — which is how this went unnoticed long enough to be cited as evidence for an unrelated fix. The
  AUTHENTICATION half is still open and still needs the secret store.
- *Closes when:* The maintenance identity presents its dedicated token at the catalog's vend door — root
  cause the seed/fetch path rather than the door — and the AMBIENT fallback is surfaced as a counter or
  refusal rather than an INFO line, so "every rewrite is root-signed" cannot be the quiet state again.

**LH-143 · ~~The cascade-lag detector cannot report a hop that NEVER RAN — the one case it exists for — because an absent destination is indistinguishable from a forbidden one~~ — CLOSED 2026-09-16**
`medallion, lineage` · was low · found 2026-09-11 by reading the estate's own warnings

- **THE MECHANISM IS FIXED, DEPLOYED AND OBSERVED; THE WORKED EXAMPLE THAT MOTIVATED THE ROW IS
  REFUTED.** Both halves were re-measured 2026-09-16 against the running estate.
- *The detector works.* Live ticks, stable across consecutive runs:

      cascade_lag_tick edges=270 published=14 failed=0 unmeasurable=255 skipped=0 destination_invisible=0 stores_disagree=1
      cascade_lag_edge_blind edge='silver->gold' project='bind86' reason='stores_disagree' published=3

  (`unmeasurable=255` / `skipped=0` is the `AbsentEdgeMemo` re-probing after a pod restart — this row
  already records that inversion, and it is not a change in what is known.)
- **THE `advref31` CLAIM IS FALSE, and it was the row's whole worked example.** The row said *"The
  tenant's silver is published and its gold was never written — a real, reportable first-hop lag that
  the detector reports as nothing."* Driven through `published_reader` itself, inside the medallion pod,
  rather than read off a neighbouring store:

      advref31   published('silver->gold') = None
      bind86     published('silver->gold') = 3

  The catalog answers **200** for `advref31-silver$features` with `{'tags': {}}` — readable, and
  carrying no `published` tag at all. So there is no lag: publishing never happened, the gold hop was
  never due, and `unmeasurable` is the CORRECT answer rather than a lost one. The detector's own live
  reads show the path taken — `tags/list` 200 then `/producers` 403 — which is the
  `published_version is None` short-circuit, reached before the 403 can matter.
  *An earlier attempt at this measurement read `d.tags` off the lineage graph and would have reported
  the same conclusion for the wrong reason:* `published_reader` reads the CATALOG's `tags/list`, and
  the graph property is a neighbouring representation ([[feedback-verify-what-the-code-receives]]).
- *What remains TRUE and is now precisely bounded:* `destination_invisible` still cannot be reached for
  a destination that was never written, because `/producers` is gated before existence resolution. It
  is unobserved because reaching it needs a tenant with a PUBLISHED source AND an absent destination,
  and advref31 — the only candidate this row ever named — is not one. That is a latent gap in a control,
  not a live loss, and it is recorded here rather than left as an open row asserting a loss that is not
  happening.
- *The residual this refutation exposes is [[LH-167]]:* `advref31-silver$features` carries **8 `WROTE`
  edges** and zero tags — written repeatedly, never published.

- *Original filing follows.*

- **NOT CLOSED, AND THE DEPLOY MADE IT QUIETER RATHER THAN BETTER.** This row was marked closed on
  2026-09-11 on the strength of `cascade_lag_edge_unreadable` going to zero. It went to zero because
  the deploy shipped `625e3b06`, which translates a 403 from the producers door into
  `EdgeNotMeasurable` — the same edge, moved from a bucket that WARNS into one that is silent by
  design. Measured on the tick either side of the restart:

  | | pre-deploy | post-deploy |
  |---|---|---|
  | edges | 267 | 267 |
  | published | 13 | 14 |
  | unknown | 2 | 1 |
  | **failed** | **1** | **0** |
  | unmeasurable | 0 | **252** |
  | skipped | 251 | 0 |

  `failed` was ONE, not the 19 the closure claimed — 19 was a count over a window, and one edge per
  tick is what the records show. `advref31 silver->gold` has never once appeared in `published`.
  (`skipped` 251 -> 0 and `unmeasurable` 0 -> 252 are the restart clearing `AbsentEdgeMemo`, which
  re-probes every cell until three consecutive misses; not a change in what is known.)
- **THE SHARPENED FINDING: `lag_for_edge`'s first-ever-hop branch CANNOT FIRE in production.** Its
  `if not consumed: return EdgeLag(lag=published, known=True)` is the shape the module's own docstring
  calls "the case this detector most needs to report". Reaching it requires `consumed_reader` to return
  an EMPTY sequence — but a destination that was never written has no Dataset node at all, lineage's
  `/datasets/{name}/producers` is gated router-level by `require_metadata_access` which runs BEFORE
  existence resolution, and `consumed_reader` maps that 403 to `EdgeNotMeasurable`. So the only way to
  an empty sequence is a dataset that EXISTS and has no producing run. A hop that never happened is
  dropped, counted, and never published. That is a control that cannot fire, and `625e3b06` — a fix for
  audit-log noise — is what closed the last path to it.
- *Measured end to end, 2026-09-11:* (1) `advref31-gold$catalog` has NO Dataset node — asked of AGE
  directly, `MATCH (d:Dataset)` filtered to that tenant returns exactly `advref31-bronze$events` and
  `advref31-silver$features`; (2) the producers route's 403 is therefore absence, not forbiddance;
  (3) the edge sat in `failed` before the deploy and sits in `unmeasurable` after it. The tenant's
  silver is published and its gold was never written — a real, reportable first-hop lag that the
  detector reports as nothing.
- **THE CREDENTIAL DEFECT WAS NOT THE CAUSE, and the check that would have shown that was cheap.**
  The closure credited [[LH-142]]'s broken `dapr.appToken` derivation. But 13 other edges read
  lineage successfully with the SAME credential on the SAME tick, and a broken privileged token
  answers 401, not 403 — the sampled records say `403 Forbidden` on one dataset. One edge failing
  while thirteen succeed was never consistent with a credential that cannot be presented.
- *Measured:* `cascade_lag_edge_unreadable` fires every tick, and every sampled record is the same
  thing — `403 Forbidden` on `GET http://rask-lineage:8000/datasets/advref31-gold$catalog/producers`,
  `project=advref31`, `edge=silver->gold`. 8 of 8 sampled, one project, one edge. Not estate-wide: every
  other project's edges read fine.
- **BUILT AND WAITING ON THE ROLL.** `lance-rest-catalog:main-24edb624` is built with Dagger and
  verified present in the registry (283 tags; the tag is there, not merely a zero exit). The roll is
  blocked — `kubectl set image` is refused here as a Shared Cluster Mutation — so the fix is not yet
  observed live. Ten workloads share that image and the pins file has ONE key for it, so they must move
  together or `values-live-pins.yaml` stops being true; the roll is no longer a medallion-only change: measured 2026-09-13, **38 commits** separate
  `main-b641103f` from HEAD, touching 8 medallion source files, 7 in lineage, 5 in catalog and
  service-kit besides. All ten workloads share the image and the pins file has ONE key for it, so
  they move together either way.
  **THE PRE-ROLL BASELINE, measured 2026-09-13 and stable across consecutive ticks:**

      cascade_lag_tick edges=267 published=14 unknown=1 failed=0 unmeasurable=0 skipped=252

  **OBSERVED 2026-09-14, AND THE PREDICTION WAS HALF RIGHT — the divergence is the finding.** `unknown=`
  is gone, `blind` is populated, and the named edge is NOT the one this row forecast:
  `cascade_lag_edge_blind edge='silver->gold' project='bind86' reason='stores_disagree' published=3`.
  Predicted was `advref31` / `destination_invisible`; observed is **bind86** / **stores_disagree**, with
  `destination_invisible=0`.
  *That is not a miss, it is a join.* bind86 is [[LH-137]]'s tenant — the one whose silver->gold hop was
  DROPped with `unconfined_uri` because its silver was published at a COMPOSED path
  (`s3://bind86-wh/medallion/silver`) while the catalog vends
  `s3://bind86-wh/78de8931_bind86-silver$features`. "The two stores disagree" is that same fact seen
  from the lag detector: the lineage graph and the catalog answer differently for one dataset. So this
  row's mechanism works and it is now pointing at LH-137's defect rather than at advref31's absent gold.
  *The original prediction for advref31 remains unrefuted and unobserved* — `destination_invisible=0`
  means no edge reported it this tick, which is consistent with that project's lane simply not running.
  The old text said the tick should report
  `medallion_cascade_lag_blind{lance_medallion_edge="silver->gold",lance_medallion_project="advref31",lance_medallion_reason="destination_invisible"}`
  series appearing. A rolled image still printing `unknown=` did not take the change.
  If `blind` instead comes back EMPTY while the 252 stay `skipped`, then that tenant's silver has no
  `published` tag and the state is a different one — an unpublished mid-cascade tier — which is worth
  its own row rather than a patch to this one. (The 252 are `skipped`, not `unmeasurable`: both fields
  exist and the deployed tick moves the population between them, so reading the wrong one would answer
  this question backwards.)
  *The ALERT cannot be observed here at all, by design:* `observability.alerting.enabled` defaults false
  (dev has no on-call) and no vmalert pod exists in this cluster — verified, `helm get manifest` names
  it zero times. The chart's own note makes promtool the bar for a rule and the prod drill the bar for
  the round-trip; `make alert-rules-check` passes, 42 rules.
- **FIXED IN CODE 2026-09-11.** The two reads are now separate calls,
  because which store refused is the whole discriminator: both refusing means the project does not run
  that lane (251 of 252 cells — correct, and silent); a source that HAS published into a destination
  this subject cannot read is a lane that is running and unmeasured, and is reported as its own state.
  It publishes no fabricated lag — absent and forbidden answer alike, and this estate holds gold tables
  that exist with zero tuples ([[LH-144]]), so a guessed first-hop lag could be a confident number for
  a hop that had in fact run. The cell is deliberately NOT memoized into silence: it is the estate's
  only evidence of that lost hop, so it pays one audit record a tick and stays in the population.
  `medallion.cascade.lag_blind{reason="destination_invisible"}` carries it and `MedallionCascadeLagBlind`
  pages on it after 30m. Gated by `test_a_running_lane_is_not_dismissed_as_unmeasurable.py` (5 tests,
  RED first) and two promtool cases, one of which pins that `MedallionCascadeLag` stays SILENT on the
  same input — an operator must not be able to silence one and believe the other covers it.
- **AND THE ALERT'S OWN PROSE WAS FALSE, which is how this stayed invisible.** `MedallionCascadeLag`
  described itself as firing "for a hop that NEVER ARRIVED — which no counter can see". It cannot: a lag
  is arithmetic over two reads, and a hop that never arrived usually has no destination to read. The
  description now says what the rule does and names the sibling that covers the rest.
- *What it costs:* the silver→gold lag for that project is not measured. The cascade may be healthy or
  stalled and the monitor cannot say, which is the failure mode a lag monitor exists to prevent.
- **THE CAUSE IS NOT A MISSING GRANT — the dataset does not exist.** Read from OpenFGA and the graph:
  `table:advref31-gold$catalog` has ZERO tuples, and `advref31-gold$catalog` has no Dataset node at all.
  Its siblings do (`table:advref31-silver$features` and `advref31-bronze$events` both carry owner +
  parent, and `namespace:advref31-gold` exists with an owner and a warehouse parent but no `child`). So
  the cascade never reached gold for that project, and the monitor is correctly unable to read producers
  for a dataset that was never produced. A 403 is what "no tuples" looks like from a door.
- *And it is NOT a second instance of [[LH-137]]'s composed-path refusal, checked rather than assumed:*
  `advref31-bronze$events` does sit at a composed `s3://advref31-wh/medallion/bronze`, which is the
  bind86 shape — but sampling every `medallion_stage_from_uri_refused` since 2026-09-09 returns 8 of 8
  for bind86 and none for advref31. Same path shape, different story.
- **THAT QUESTION IS ANSWERED — there is no grant gap to reach anything.** This row asked whether the
  same gap reached beyond the monitor, on the theory that a 403 on `/producers` meant the medallion
  identity was missing a rung for `advref31`. It is not: the dataset does not exist, so the 403 is
  ABSENCE wearing forbiddance's status code, and a rung nobody is missing cannot affect a second door.
  Two independent measurements say so, and both are in this row: 13 other edges read lineage with the
  SAME credential on the SAME tick, and `advref31-gold$catalog` has no Dataset node.
  **Re-measured against AGE 2026-09-14** — `MATCH (d:Dataset) WHERE d.name STARTS WITH 'advref31'`
  returns exactly `advref31-bronze$events` and `advref31-silver$features`, so the state is unchanged
  three days on and this is a monitoring row after all.
- *Why it went unseen:* it is a WARN line in a channel that is 91% two permanent, designed refusals (see
  the audit's warning-composition section), so a 19-per-tick signal is 0.1% of the volume.
- **THE BLAST RADIUS IS NOT ANSWERABLE FROM LOGS, and that is a finding of its own.** Checked: the
  lineage service emits NO warning for the refusals it issues — its only WARN-or-worse bodies in the
  window are the three reconcile classes. So a 403 is visible ONLY from the caller that chose to log its
  own failure. Any other consumer hitting the same missing rung would fail quietly, and the gap would be
  invisible from both ends.
  That is defensible per-request — a door refusing a read is doing its job, not reporting an incident —
  but it means "how far does this grant gap reach" cannot be answered by reading more logs. It needs the
  tuples. Recorded so the next step is an FGA check rather than another query.
- *Closes when:* the roll observes `destination_invisible` reported for that edge. The grant half of
  this criterion is struck rather than carried: it offered "granting the rung if the tuple is missing",
  and the tuple is not missing — the destination was never written, which is a lane that never ran and
  exactly the state the new `blind` report names. What is left is that a blind edge is a distinct
  state rather than an unreadable-edge warning, so "not measured" cannot look like "not lagging".

**LH-146 · ~~Run retention and the provenance back-fill undo each other — every real author and EVERY input edge becomes a synthetic `author='reconcile'` record, starting 2026-09-16~~ — FIXED, DEPLOYED AND OBSERVED 2026-09-15**
`lineage` · **HIGH** · found 2026-09-11 by reading the two halves together; both are deployed and running


- **CLOSED 2026-09-15 (`f6cda4b0`, corrected by `dad86232`), deployed on `main-3e858a70` and observed.**
  Retention now refuses to prune any run belonging to a dataset whose newest write predates the cutoff,
  so a dataset can never be left with zero `WROTE` edges, is never classified UNTRACKED, and the
  back-fill that would mint `author='reconcile'` is never reached. Hole recovery is untouched.
  *THE FIRST FIX HAD A HOLE AND IT IS RECORDED HERE BECAUSE THE SHAPE IS SEDUCTIVE.* `f6cda4b0`
  exempted "a run that is some dataset's only writer", which asks about the graph as it stands rather
  than as the batch will leave it: when every writer of a dataset is past the cutoff they each see two
  or more writers and all are prunable, so one `DETACH DELETE` still took them. Measured on the
  deployed graph at a probe cutoff — 10 datasets, 57 runs still stripped. Caught before the built image
  was deployed; `dad86232` moved the question to the DATASET, which no batch composition can change.
  *WHAT THE LIVE OBSERVATION DOES AND DOES NOT SHOW, stated because this row's own text warns that an
  idle pruner and a broken one both report 0.* Confirmed: the exact committed COUNT and DELETE strings
  execute against the deployed AGE (COUNT read-only, DELETE inside a rolled-back transaction), the
  running pod carries the per-dataset predicate and not the per-run one, and the first reconcile tick
  after the roll logged `pruned_runs=0 pruned_events=0` with no error. NOT yet seen: the exemption
  firing on a real prune — the oldest run is `2026-08-16T14:24` against a cutoff of ~`12:30`, so
  nothing is old enough yet. The next tick that prunes is the one that shows it.
  *A note for whoever reads the query next:* the shapes that keep exactly ONE run per quiet dataset
  need each run correlated against its siblings' timestamps, and that form OOM-KILLED the AGE container
  on this graph rather than running slowly. A quiet dataset therefore keeps its whole history, which
  costs rows and costs the 30-day window nothing — that window protects alert signal-to-noise, which is
  driven by Dataset nodes, and an exempted dataset keeps its node either way.
  *And the premise was checked rather than assumed:* the 30-day cutoff is NOT a compliance requirement.
  `chart/values.yaml` records it as an owner ruling made so the reconcile's `storage_loss`/`unreadable`
  warnings converge instead of firing every tick over dead rows; nothing in `docs/` states a compliance
  basis. The window is unchanged by this fix.

- *The mechanism, each step read out of the code rather than inferred:*
  1. `PRUNE_OLD_RUNS_TEMPLATE` is `MATCH (r:Run) WHERE r.event_time < $cutoff … DETACH DELETE r` —
     by AGE alone, with no guard for a run that is a dataset's only provenance. `DETACH` takes the
     run's `WROTE` **and `READ`** edges with it.
  2. A dataset that loses every run has no versioned write, so `latest_write_version` answers `None`
     while storage still has a version — which `classify` calls **UNTRACKED**.
  3. `BACKFILLABLE_STATES = (STORAGE_AHEAD, UNTRACKED)`, so the sweep back-fills it, and
     `_recover_holes` then back-fills every version below the tip, 25 per dataset per tick.
  4. `backfill_write` MERGEs `reconcile-<name>-v<version>` with `author='reconcile'` and — its own
     docstring — "no inputs".
- **SO THE GRAPH DOES NOT SHRINK; IT IS REWRITTEN.** The run id is deterministic per (name, version),
  so the synthetic run is recreated identically each time, and it carries a fresh `event_time`, so it
  ages out and is re-created on the next cycle. Retention deletes real runs once and they never return;
  what persists is a skeleton that says THAT each version was written and nothing about who or from
  what.
- *Measured on the live graph 2026-09-11:* **1294 READ edges** — the entire input/derivation half —
  against 6139 WROTE edges over 6153 Run nodes. **685 runs (11%) already carry `author='reconcile'`**
  from the outbox-gap back-fills, so the mechanism is not hypothetical; retention takes it to 100%.
- *And it has DATES, because nothing has been pruned yet.* The oldest run is 2026-08-15 (27 days) and
  `pruned_runs` is 0 across 40 sampled sweeps, which is consistent with nothing being old enough — idle,
  not broken. It does NOT settle the half [[LH-011]] left open ("confirm the pruner actually deletes Run
  nodes — I did not query the live graph"): an idle pruner and a broken one both report 0, and the first
  cutoff that would tell them apart falls on ~2026-09-14.
  Taking each swept dataset's newest run + 30 days:

  | date | datasets with NO real provenance left |
  |---|---|
  | 2026-09-16 | 1 of 333 |
  | 2026-09-30 | **180 of 333** |
  | 2026-10-07 | 290 of 333 |
  | 2026-10-11 | **333 of 333** |

- **AND THERE IS NO RECOVERY SOURCE, which settles whether any of this is reversible.** The durable
  `/events` feed is retained 7 days (`LINEAGE_EVENTS_RETENTION_DAYS=7`, deployed) and `lineage_events`
  holds 4051 rows reaching back only to 2026-09-07, while the graph holds runs back to 2026-08-15. So
  the raw events for everything written before early September are ALREADY gone, and that provenance now
  exists solely in the Run nodes retention begins deleting. Once a run is pruned there is nothing in the
  estate to rebuild who wrote a version or what it read — provenance arrives with the write event and
  never again.
- **WHAT IS INTENDED AND WHAT IS NOT, because the first is an owner ruling and only the second is the
  defect.** Retention aging out old lineage IS intended — 30 days, owner, 2026-09-08. What is not is
  the sweep then REFILLING the gap with edges that look like provenance: a deliberate 30-day forget
  becomes an estate whose every dataset reports a complete write history authored by the reconciler.
  The loss is invisible precisely because the back-fill is good at its job.
- *This is condition 1 of the goal — "a write's provenance survives it" — and it is the one that stops
  holding on a known date.* The fact of the write survives; the actor and the derivation do not.
- **THE CLOCK STARTED, MEASURED 2026-09-14 — this row stops being a forecast.** The pruner's first
  deletions are on the board: `pruned_runs` went non-zero for the first time (9 ticks in a 6h window
  deleting 1-4 runs each, against 63 at zero), and the oldest run is `2026-08-15T15:17` — exactly the
  30-day cutoff. The graph read directly: 6,506 Run nodes, 6,492 `WROTE`, 1,336 `READ`.
  *The destructive half has begun and the rewriting half has not caught up:* `backfilled=0` across all
  72 sampled ticks and `author='reconcile'` is STATIC at 685 — the same 685 measured on 2026-09-11, so
  those are the historical outbox-gap back-fills and not the retention cycle. That gap is the window in
  which a ruling costs nothing; once a swept dataset loses its last real run the back-fill mints the
  synthetic one and the original author and inputs are gone with no source to rebuild them from.
  Consistent with this row's own table (1 of 333 by 2026-09-16), so the forecast is holding rather than
  being overtaken.
- *Closes when:* an owner ruling on which of the two yields, and the code matches it. The shapes are:
  exempt a dataset's last surviving run from retention; or stop treating UNTRACKED as back-fillable
  when the runs were deliberately pruned (the two are indistinguishable today — a lost event and an
  aged-out one both leave no edge); or keep retention as-is and accept a synthetic graph, in which case
  say so in `docs/DECISIONS.md` so nobody reads `author='reconcile'` as a defect later. Pin whichever
  lands, because the current pair is a cycle no test covers.

**LH-145 · ~~The lag tick's `unknown` count names no cell and reaches no metric — a store disagreement is counted, unactionable and unpageable~~ — CLOSED 2026-09-14 (OBSERVED ON THE ROLL)**
`medallion` · low · found 2026-09-11 while fixing [[LH-143]]

- **CODE LANDED `5ad5e62d`** — `unknown` is replaced by `blind: list[BlindEdge]`, each naming its edge,
  project and a reason from a closed vocabulary (`destination_invisible` / `stores_disagree`) validated by
  `BlindEdge`.
- **OBSERVED ON THE ROLL 2026-09-14 — this row is closed.** The first tick on `main-94e88b35`:

      before  cascade_lag_tick edges=267 published=14 unknown=1 failed=0 unmeasurable=0   skipped=252
      after   cascade_lag_tick edges=267 published=14           failed=0 unmeasurable=252 skipped=0 destination_invisible=0 stores_disagree=1
      WARNING cascade_lag_edge_blind edge='silver->gold' project='bind86' reason='stores_disagree' published=3

  `unknown=` is gone and the cell is NAMED, which is the whole ask: an operator can now see which edge,
  which tenant and why. The `unmeasurable`/`skipped` populations also swapped fields, which is the
  restart clearing `AbsentEdgeMemo` and re-probing every cell — not a change in what is known.

- *Measured:* the post-deploy tick reports `unknown: 1` — one declared cell where both stores answered
  and DISAGREED. `lag_for_edge` returns `known=False` for exactly two shapes, and both are real
  inconsistencies rather than absences: a frontier AHEAD of the source's published version (a tag moved
  backwards, or a lineage run outlived the table it names), and a source reporting nothing published
  while the destination has consumed something.
- *What it costs:* `record_edge_lag` publishes NO point for an unknown lag, which is correct — every
  sentinel a gauge could carry is also a real lag. But the consequence is that the cell has no series,
  `MedallionCascadeLag` fires on `> 0` and so cannot see it, no other rule mentions `unknown`, and the
  tick's log line carries the COUNT without the identity. So an estate where every edge disagreed would
  publish nothing, page nobody, and look exactly like a cascade with no lag.
- *This is the same class as [[LH-143]] one notch milder,* and worth fixing the same way: the state is
  real and named internally, and the gap is that it stops at the report. The fix is symmetric with
  `destination_invisible` — carry the identities, export a series, alert on persistence.
- *Sized on its own evidence, then folded after all,* and the reversal is the interesting part. The
  intent was to keep it separate because [[LH-143]] earns its scope from a measured live loss and this
  was found by reading. Re-measuring settled it: `unknown: 1` is on EVERY tick, stable, and the tick at
  21:38 UTC shows the same. Two states that both mean "this lane is running and its lag cannot be
  stated", reported through two different fields, is the exact shape that produced [[LH-143]] — two
  sibling readers classifying one identical refusal differently. So they became one closed vocabulary.
- **FIXED IN CODE 2026-09-11, in the same image as [[LH-143]] and not yet observed.** `unknown: int`
  is gone; both states are `LagTickReport.blind: list[BlindEdge]` carrying `edge`, `project` and a
  reason from a closed set — `destination_invisible` and `stores_disagree` — exported as
  `medallion.cascade.lag_blind{reason}` and paged by one `MedallionCascadeLagBlind`, the shape
  `medallion.stage.refused` already uses for its four refusals. The tick line reports the two reasons
  SEPARATELY rather than as a sum, so one rising while the other falls cannot hide.
  *`ty` earned its keep here:* it flagged `pydantic-discarded-extra-argument` at four test sites still
  passing `unknown=`, each of which would otherwise have asserted against a silently dropped kwarg.
- *Closed by:* the roll, on both halves and to the bar this row set for each. NAMING is observed live —
  `cascade_lag_edge_blind edge='silver->gold' project='bind86' reason='stores_disagree'`, with `unknown=`
  gone from the tick. PAGING is the rule `MedallionCascadeLagBlind`
  (`max by (lance_medallion_edge, lance_medallion_project, lance_medallion_reason) (medallion_cascade_lag_blind) == 1`,
  `for: 30m`), fed by a real gauge (`metrics.py:96,295` sets `medallion.cascade.lag_blind` with exactly
  those three labels) and gated by `make alert-rules-check` — which is the bar this row set, because it
  records that firing "cannot be observed here at all, by design": alerting is off and no vmalert pod
  exists in this cluster.

**LH-144 · Three live tables are ungoverned — but 47 of the 58 were simply DROPPED, and the row said otherwise**
`catalog` · medium · found 2026-09-11 by reading OpenFGA directly, RE-MEASURED the same day

- **RE-MEASURED 2026-09-11, and the headline above was two-thirds wrong.** The count survives: paging
  OpenFGA's whole tuple set and subtracting it from the graph's datasets carrying a `source_uri` gives
  **exactly 58 of 1162** — the same number by a different method, so the sweep is sound. What the sweep
  could not see is WHY a table has no tuples. `drop_table` REVOKES a table's tuples (`tables.py:460`,
  "then revoke its FGA tuples") while its lineage node persists, because provenance has to outlive the
  table it describes. So a dataset node with zero tuples is the EXPECTED state of a dropped table, and
  reading it as "created, ungoverned and unreachable" reads a working mechanism as a defect.
  Classifying all 58 by their latest COMPLETE run — which is exactly how `repository.dropped_at`
  derives a drop, rather than a flag anyone stamped:

  | latest completed operation | count |
  |---|---|
  | `drop_table` — tuples correctly revoked, nothing wrong | **47** |
  | `deregister_table` — which ALSO "revoke[s] its FGA ownership", so equally expected | **1** |
  | something else (compaction x2, aggregate_gold, insert, update, create_table) | 6 |
  | no completed run at all | 4 |

  So 48 of the 58 are a mechanism working, and the defect population is **10**.

- **TWO OF THE THREE GOLD TABLES THIS ROW IS NAMED AFTER WERE DROPPED**, both on 2026-08-23:
  `durproof-gold$catalog` and `gateprobe-gold$catalog`, alongside their silver siblings. They are not
  stranded; they are deleted. Only `uiproof-gold$catalog` survives the check — last written by
  `aggregate_gold` at 2026-08-23T18:02, never dropped, zero tuples — while its own silver WAS dropped
  23 minutes later. That one is real.
- **THE REAL POPULATION IS 10, AND THREE OF THEM MATTER.** `uiproof-gold$catalog` above, plus
  `research-bronze$events` (last op `compaction`, 2026-08-30) and `bind86-bronze$events` (`compaction`,
  2026-09-02). Both are CASCADE-HEAD tables — `bronze$events` is the `bronze` lane's declared source —
  in projects whose other tiers ARE governed (`research-silver$features`, `research-gold$catalog`, and
  nine bind86 tables including seven other bronze ones). So this is not "a project nobody governed"; it
  is one table per project, at the head of the cascade, missing.
- **RE-MEASURED 2026-09-16, AND THE POPULATION IS NOT WHAT THE RECONCILER COUNTS.** The sweep's
  `ungoverned_tables` category reports **0** on the live estate against a denominator of 381 registered
  tables — so it is not vacuous, and every table the CATALOG knows does carry tuples. That is a
  different set from this row's, and the difference is the finding: asked of the catalog directly,

      research-bronze$events    404   not a registered table
      bind86-bronze$events      404   not a registered table
      uiproof-gold$catalog      403   the no-existence-oracle answer
      bind86-silver$features    200   registered and readable

  Two of the three tables this row says "matter" are **not catalog tables at all**. They have lineage
  `Dataset` nodes and bytes on storage, and the catalog has never heard of them.
- *Which means the control cannot see the defect it looks like it covers.* `_ungoverned_tables` reads
  the object MANIFEST (`object_type == 'table'`, `lance_docs/namespace.md:411,658`) and subtracts the
  FGA tuple set, so its question is "does a REGISTERED table lack tuples". This row's question is
  "does a dataset that exists lack a registration", and nothing answers that one — a 0 from the first
  reads as an answer to the second. The sweep meets the same population from the other side and says so
  in its refusal text ("or no such table is registered and this dataset is ungoverned"), which is a
  message, not a report.
- *So the scorecard line for condition 2 is true and narrower than it reads:* "`ungoverned_tables` = 0
  (every table the catalog knows carries tuples)" — the parenthesis is doing load-bearing work, because
  a dataset the catalog does NOT know is outside the count entirely.
- **AND IT COSTS A MEASUREMENT, which is how the two rows connect.** An ungoverned source refuses
  exactly as an absent one does, so the cascade-lag detector reads those lanes as lanes nobody runs.
  Eight declared cells have an ungoverned source — but three of those sources were DROPPED (silence is
  correct there) and three were never written, so the true cost is **two cells**: `research` and
  `bind86`, whose bronze heads are actively compacted and which appear nowhere in the
  `medallion_cascade_lag` series. Counting all eight would have been this row's own mistake repeated.
  [[LH-143]]'s fix cannot recover them — no reading of a 403 separates "ungoverned" from "absent" — so
  governing these tables is what restores the measurement.
- **THE MECHANISM, narrowed by elimination rather than guessed — and none of the row's three original
  candidates survives.** Each step is a read of the code or the graph:
  1. *Not a failed create.* `register_written_dataset` runs BEFORE the write and before the emit — the
     producer's own comment is "nothing has been written and nothing has been emitted at this point".
     So a lineage node's existence PROVES the registration succeeded. These tables were governed.
  2. *Not a drop that was seen.* Neither `drop_table` nor `deregister_table` appears anywhere in their
     run history: `research-bronze$events` has `lance_ray_ingest` x2 then `compaction`;
     `bind86-bronze$events` the same; `uiproof-gold$catalog` has `aggregate_gold` x2.
  3. *So the tuples were revoked AFTER the fact*, and the only paths that revoke are the drop's two
     branches — graceful (trash) and purge. Both revoke, and both emit `DROP_TABLE` from OUTSIDE the
     `if not trashed:` branch, so a drop normally leaves a run behind.
  4. *And that emit is explicitly BEST-EFFORT* — "so it never fails the drop" (`tables.py`). A drop
     whose emit does not land revokes the tuples and records no run.
  **Leading explanation: these are DROPPED tables whose drop event was lost.** It predicts all three
  observed symptoms at once — no tuples, no `drop_table` run, and a lineage node that `dropped_at`
  cannot recognise, which is also why the reconcile sweep and the lag detector both still treat them as
  live. And it makes this an EVENTS-correctness defect, not a governance-seeding one.
  *Unproven, and the test is named:* a `lineage_emit_failed` record carrying `operation=drop_table` for
  one of these ids. `catalog_lineage_emit_failed_total` does not exist as a series at all (its sibling
  `catalog_control_emit_failed_total` does), so no lineage emit has failed inside the retained window —
  but these drops predate it, so absence there proves nothing either way.
  *Consistent with the compaction:* a graceful drop keeps the bytes, and the maintenance sweep
  "discovers datasets by walking storage for a `_versions/` marker, not by reading the registry", so a
  dropped-but-retained table is still compacted afterwards — which is exactly what both bronze heads show.
- **AND THE LOSS WINDOW HAS A DATE, which makes the explanation testable rather than merely tidy.**
  The staged-then-published transactional outbox is inert until a deployment sets
  `LANCE_LINEAGE_OUTBOX_URI`, and the chart began setting it on **2026-08-31** (`d58ffaff`). Before
  that the drop's lineage emit was a plain best-effort publish — exactly the window a lost drop event
  needs. Two of the three that matter fit it completely: `uiproof-gold$catalog` last did anything on
  2026-08-23 and `research-bronze$events` on 2026-08-30, both before the outbox. The third does not —
  `bind86-bronze$events` was compacted 2026-09-02 — so its drop is not bounded by that window and the
  explanation is temporally consistent for two of three, not all. Said plainly rather than rounded up,
  because a prediction that only mostly holds is the kind that gets quoted later as if it held.
- **"They predate the compensation" is REFUTED.** `seed_ownership_or_compensate` landed 2026-08-15
  (`8c947640`). Dating each of the 58 by its earliest producing run: **zero** predate it, 55 were first
  written after it, and the newest is `bronze$lh018probe` from 2026-09-11T13:15 — hours before this was
  written. Whatever leaves a table ungoverned is still doing it. (The proxy is "first written", not
  "created"; for probe tables created and written in one test the gap is negligible.)
- **AND THE SWEEP IS CHEAP, which the cost argument below gets wrong.** Its premise is verbatim correct
  — a `Read` carrying a `tuple_key` with an empty object id is refused, "the object type field is
  required and both the object id and user cannot be empty", reproduced live. But omitting `tuple_key`
  ENTIRELY is a different call, and it pages the whole store: measured, **51 pages, 5027 tuples, 0.1
  seconds**. A periodic sweep costs 51 reads, not 1162, so "the kind of price that gets an axis
  switched off" does not apply and a sweep is a live option again.
- *Measured.* Of 14 gold datasets carrying a `source_uri` in the live graph, **3 have no FGA tuples at
  all** — `durproof-gold$catalog`, `gateprobe-gold$catalog`, `uiproof-gold$catalog`. Not a missing
  grant: no owner, no parent, nothing. Their healthy siblings carry both
  (`table:acme-gold$catalog` → `namespace:acme-gold parent`, `user:service-silver-to-gold owner`).
- *This is the exact state `seed_ownership_or_compensate` exists to prevent*, and `tables.py` describes
  it in its own words: "a failed seed left a declared-only table that its declarer could not see, could
  not drop, and could not re-declare (native `AlreadyExists`), reserving the id against everyone,
  permanently (F3)." Three tables are in it.
- *Why it is HIGH rather than tidy:* a table absent from the authorization graph cannot be read,
  maintained, dropped or re-created by anyone, including the identity that made it. Every door gated on
  any relation refuses, and the refusals are invisible — the lineage service logs none of the 403s it
  issues, so nothing reports the condition from either end.
- **THE POPULATION IS MEASURED: 58 of 1162, 5.0%, and it is not gold-specific.** Every dataset name in
  the live graph carrying a `$` was checked against an OpenFGA `Read` — 1162 calls, zero read errors, so
  this is a complete sweep rather than a sample. By tier segment: `ns` 33, `bronze` 10, `silver` 6,
  `gold` 3, plus a handful of others. Examples are dominated by probe and e2e names
  (`acme-bronze$lance_s3_probe`, `acme-bronze$zzprobe8926`, `acme-bronze$should_refuse`), and one is
  `aud1ns$sub3$tt` — a THREE-segment id, the malformed shape [[LH-137]] traces separately.
- **AND THIS IS WHY THE RESIDUE CANNOT BE PRUNED, which [[LH-002]] has been stuck on.** Its remedy is
  measured to "close nothing" because `prune_orphan_datasets` leaves most nodes in place. A table with
  no tuples cannot be dropped BY ANYONE — that is the F3 state `tables.py` describes — so the estate's
  test residue is undroppable by construction, not by oversight. Cleaning it requires either seeding
  ownership first or a path that does not authorize against the table being removed.
- *What is still NOT known:* whether compensation failed or was never reached for each of the 58. That
  needs the create/register history per table, not another sweep.
- *DETECTION IS CHEAPER AT THE SEAM THAN AS A SWEEP, and that is measured rather than preferred.*
  OpenFGA's `Read` cannot enumerate "every table carrying any tuple" in one pass: with an empty object
  id it requires a `user` ("the object type field is required and both the object id and user cannot be
  empty"), so enumeration is per-user or per-namespace. A periodic sweep therefore costs one call per
  table — 1162 per tick on today's estate — which is the kind of price that gets an axis switched off.
  The seam is where it is cheap — but the seam ALREADY DOES THIS, which corrects the suggestion above.
  `seed_ownership_or_compensate` undoes the native create when the seed fails, and its docstring names
  precisely this failure: "the object exists on storage with no owner and no `parent` edge, so per-item
  list filtering hides it from every caller including its creator, and the obvious retry hits native
  `AlreadyExists` and never reaches the seed again." So a verification read-back would largely duplicate
  a compensation that is already there and carefully argued.
  *Which makes the real question narrower:* how did 58 tables get PAST it. Three candidates, none yet
  distinguished — they predate the compensation; or they were created by a path that does not go through
  that door; or the caller passed `undo=None`, which the docstring describes as a deliberate choice
  where a native delete is unsafe. Answering that needs per-table create history, and it decides whether
  anything needs building at all.
- *Closes when:* the THREE live ones are resolved — each either governed to its real owner or, if the
  drop-event explanation holds, recorded as dropped so `dropped_at` can see it — and a drop whose
  lineage emit does not land stops being invisible. The 47 dropped ones need nothing; naming them as a
  defect was this row's own error. The seed-compensation pin the row originally asked for is NOT the
  work: `seed_ownership_or_compensate` is measured here to be doing its job, and the 58 are not evidence
  against it.

**LH-140 · ~~A manifest-declared base path is granted READ with no check that the caller may read it~~ — CLOSED 2026-09-16**
`catalog` · was med · filed 2026-09-11 · code landed 2026-09-13 · **deployed and DRIVEN 2026-09-16**

- *Why open:* `build_session_policy` appends a READ grant for every base path the table's manifest
  declares, and a base may legitimately live in its own bucket — so the grant is not confined to the
  caller's tenancy by anything. The input is writer-chosen: bases come off the table's own manifest
  (`_dataset_facts` → `manifest_base_path_refs`), and a write-tier vend grants `PutObject` on
  `<prefix>/*`, which covers `_versions/` and is enough to commit a manifest client-side. So a writer on
  ONE table can declare a base naming another tenant's table prefix and read it with their next
  credential.
- *What is already closed (`803171d8`+):* the bucket-root case. A base with no prefix collapsed the
  statement to `arn:aws:s3:::<bucket>/*` — measured 2026-09-11, `s3://lakehouse` granted READ on the
  whole lakehouse bucket and `s3://rask-observability` on a bucket the table has nothing to do with.
  That is refused now, and refusing it cannot narrow a real table because a base points at a dataset
  root or a file directory, never at a bucket (`file_format.md`, Base Path System).
- *Why the rest could not be closed in the same change:* a base naming a specific prefix is
  indistinguishable here from a legitimate cross-bucket base. Telling them apart needs the base resolved
  to a catalog object and the SAME read authorization the caller would need to read that object
  directly — a per-base FGA check on the vend path, not a shape rule.
- **LANDED 2026-09-13 — the vend door now applies the sanction the CREATE door already enforced.** A
  declared base is granted READ only when it is inside the table's own vended scope, or on the
  operator's `LANCE_MULTIBASE_DATA_BASES` allowlist; anything else is DROPPED (never raised — a
  poisoned manifest must not make a table permanently un-vendable) and logged
  `vend_base_path_unsanctioned`. `sanctioned_bases` defaults to empty, so a call site that has not
  wired the allowlist sanctions nothing foreign. Wired through both STS plugs as deployment config
  rather than a `vend` argument, because which buckets belong to this lakehouse is not a per-request
  question — and a per-request one would be answerable by the caller whose manifest is the untrusted
  input. RED-first, 16 tests in
  `services/catalog/tests/test_a_declared_base_cannot_reach_a_table_the_caller_never_opened.py`,
  including both near-misses (`lakehouse-evil`, `mine$t-evil`) that a bare prefix test would admit.
  **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live.

  **And DRIVEN, not merely present.** `build_session_policy` was called inside the running catalog with
  the pod's own empty `LANCE_MULTIBASE_DATA_BASES`:

      own base       s3://lakehouse-wh/medallion/bronze/base    KEPT
      foreign base   s3://other-tenant-wh/secret/table          DROPPED  (+ vend_base_path_unsanctioned)
      same base, with sanctioned_bases=['s3://other-tenant-wh'] KEPT

  The third line is what makes the second mean something: it shows the drop is a sanction TEST rather
  than a blanket refusal, which a foreign-base-only probe would have passed either way.

- *Why the allowlist rather than the per-base FGA check this row originally prescribed:* there is no
  location->table index in the catalog, so "resolve each base to a table id" costs either a walk of the
  estate on a 900 s-TTL hot path or a reversal of the backend's own layout convention. The allowlist is
  not a shape rule — it is the estate's existing sanctioning mechanism for foreign bases, and
  `config.py:70-77` already states the rule it encodes ("a caller can never point a base at an
  arbitrary bucket (data-exfil / rogue-write door)"). The defect was the ASYMMETRY: the create door
  enforced that list and the vend door ignored it.

- *Measured while fixing, and both facts matter:* `LANCE_MULTIBASE_DATA_BASES` on the deployed catalog
  carries neither a `value` nor a `valueFrom` — it is empty in the running pod, so `has_external_bases`
  is never consulted and declared bases reached the policy wholly unchecked. And the bases live tables
  actually declare are `<table-root>/tree/work` (shallow-clone/branch shapes, manifest feature flag 16),
  i.e. inside the table's own scope — so the new gate narrows nothing that is running.

- **THE WIRING WAS PINNED BY NOTHING, found by the same pass.** Every test called
  `build_session_policy` DIRECTLY with an explicit `sanctioned_bases=`, so the rule was covered and
  nothing that carries it was: deleting `sanctioned_bases=self._sanctioned_bases` from a vendor, or
  `sanctioned_bases=settings.multibase_data_base_list` from the catalog's lifespan, drops the parameter
  to its empty default — every allowlisted foreign base refused on every real vend, the §H12 compaction
  refusals back, and not one test red. Now driven through a real `StsVendor` with a capturing
  `assume_role` plus a negative twin, and mutation-tested: it fails with the forwarding removed.
- *Residual, stated rather than accepted:* a base on the allowlist is granted to any caller vending any
  table, without checking that THIS caller may read THAT location. That is an operator's explicit
  sanction of a shared multi-base bucket rather than a writer's choice, which is why it is no longer
  HIGH — but it is still not a per-caller check. Closing it needs the location->table resolution above,
  and is worth doing only if a deployment ever populates the allowlist with a bucket whose contents are
  not uniformly readable by everyone who can vend.
- *Note while this is open:* a refused base currently propagates a `ValueError` out of the vend rather
  than a typed refusal, the same shape `_reject_iam_metacharacters` already had. Fail-closed and
  consistent, but an operator sees an opaque error for a poisoned manifest.
- **THE CLOSURE CLAIM ABOVE WAS FALSE, AND THE GATE DID NARROW SOMETHING RUNNING.** It read "the bases
  live tables actually declare are `<table-root>/tree/work` … i.e. inside the table's own scope — so the
  new gate narrows nothing that is running". Measured on the deployed catalog the same day (image
  `main-9e5ff5b3`): 1,836 `vend_base_path_unsanctioned` warnings in three hours, every one for
  `s3://lance-catalog/models/`, against tables in unrelated buckets (`tracka-wh`, `cslens0d7def-wh`,
  `acme-bucket`), all with `sanctioned_count=0`. The estate had already approved that base — the pod
  carries `LANCE_EXTERNAL_BLOB_BASES=s3://lance-catalog/models/` — but `main.py` gave the vendor only
  `multibase_data_base_list`, so the gate this row installed dropped a base the create door accepts.
  FIXED `6d923a40` (`Settings.vend_sanctioned_bases` unions both allowlists), with the four tests the
  drop's remoteness demands: `vending.py:271-281` says a dropped base "surfaces later as a read denial at
  the object store … with nothing naming the base", so a regression is invisible at the vend.
  **Not yet deployed** — the fix is in HEAD and the running image predates it.

**LH-141 · A wrong `lineage.dataset_id` stamp is repaired only by a WRITE, so a dataset that stopped being written keeps a false name forever**
`medallion, maintenance, service-kit` · **HIGH** · filed 2026-09-11 · measured on the live estate · **blocked:** [[LH-146]]'s ruling on whether a synthetic assertion is acceptable — the GUARD half landed 2026-09-16, the REPAIR half cannot start without it

- **THE CONSEQUENCE IS LIVE AND IT WRITES — measured 2026-09-16, and this is no longer a latent row.**
  A dataset the catalog governs as one table is being MAINTAINED under the identity of another, in a
  different warehouse, and the rewrite lands:

      sweep    dataset='s3://bind86-wh/medallion/silver'  table_id='bronze$events'
                                                          mode='distributed'  indices_optimized=1
      catalog  silver$features  ->  s3://bind86-wh/medallion/silver      <- the governed id for that path
      catalog  bronze$events    ->  s3://lance-catalog/medallion/bronze  <- a DIFFERENT bucket

  So a maintenance pass recorded against `bronze$events` wrote to a dataset belonging to
  `silver$features` — `indices_optimized=1` says a write landed.
  *The id came from the STAMP, not from the path, which is what makes this LH-141 rather than an id-
  derivation bug:* `table_id_from_location` answers **None** for every composed medallion path
  (`s3://bind86-wh/medallion/silver`, `…/medallion/bronze`, `s3://lance-catalog/medallion/bronze` — run
  directly), so the only source for `bronze$events` is the dataset's own declared
  `lineage.dataset_id`.
- **THE CROSSING IS AT THE PLAN DOOR, NOT THE CREDENTIAL DOOR — resolved 2026-09-16 and it changes both
  the mechanism and the severity.** Reading the sweep's own lines around that dataset:

      POST /v1/table/bronze$events/compaction_plan          -> 200
      compaction_distributed_nothing_to_do uri='s3://bind86-wh/medallion/silver'
      maintenance_index_findings          uri='s3://bind86-wh/medallion/silver'
      maintenance_dataset_outcome         dataset='s3://bind86-wh/medallion/silver'

  Over 12 hours: **30 calls to `table/bronze$events/compaction_plan` and ZERO to
  `table/bronze$events/credentials`.** So no credential was ever vended for the wrong id — the estate's
  1,233 SCOPED / 0 AMBIENT decisions in that window are all for correctly-named datasets.
- *What this makes it: a NEAR-MISS, latent, and one empty plan away from data corruption.* The catalog
  answers **200** to a compaction plan for `bronze$events` while the sweep holds a dataset governed as
  `silver$features` in a different warehouse. The plan comes back EMPTY every time
  (`compaction_distributed_nothing_to_do`), so nothing is executed and no fragment is rewritten. Had
  `bronze$events` carried pending compaction work, those tasks — computed against ITS manifest, in
  `s3://lance-catalog` — would have been executed against another tenant's dataset. The only thing
  standing between this and a cross-tenant rewrite is that one table happening to be at target.
- *The index optimize DID write* (`indices_optimized=1`), and that half is a genuine write under a
  mis-recorded identity; it is not planned by the catalog and so is not covered by the plan door's
  answer.
- *WHERE THE GUARD GOES IS NOT OBVIOUS, and the two obvious places are both wrong — recorded so the fix
  is not attempted at the first site that looks right:*
  * `catalog_compaction.plan_via_catalog(table_id, policy, settings)` receives ONLY the id. The catalog
    cannot detect the mismatch because the sweep never tells it which dataset it is holding, so the
    door answering 200 is not a catalog bug — it answered the question it was asked.
  * a sweep-side `describe` before each plan would cost one extra catalog round trip per dataset, and
    the tick already walks **552** of them. That is the shape the `_may_write_anything` probe exists to
    avoid (it was added because vending per PLANNED dataset was minting 280 STS records a minute).
  *The cheap discriminator is probably the vend itself* — a credential is already scoped to the table's
  own bucket+prefix, so options that do not cover `item.uri` ARE the crossing, detectable with no extra
  call. That needs one more measurement first: why this dataset produced 30 plan calls and ZERO
  credential calls, when `_may_write_anything` returns True for a dataset whose index optimize commits.
  Until that is answered the guard cannot be placed, and placing it by analogy is how this estate gets
  controls that cannot fire.
- *Closes when:* a declared id that does not name the dataset being maintained stops the unit, and the
  refusal says which two locations disagreed.
- *Why the existing guard does not catch it, read off the code rather than assumed:*
  `credentials.write_options_for` keys the vend on `declared_table_id or table_id_from_location(uri)`
  (`credentials.py:79`) and its docstring states the intended safety — *"A DECLARED id is never repaired
  or second-guessed here. If a producer stamps a wrong one the vend fails on a table that does exist,
  which is a visible 403 in the log."* That reasoning holds only when the wrongly-named table is one
  this identity may NOT maintain. Here it is one it may, so there is no 403 and nothing is visible: the
  guard's failure mode assumes the stamp names a table the caller cannot reach, and a stale CASCADE
  stamp names a table the caller reaches every tick.
- *Why open:* `ensure_declared_dataset_id` self-heals a stale stamp — its docstring is explicit that a
  `merge_insert` does not carry schema metadata, so the tier above would otherwise keep its parent's
  name — but all five call sites are inside `compute.py`'s WRITE paths. Nothing outside a write ever
  re-stamps, so a dataset no longer being written keeps whatever name it was last given. The reconcile
  sweep repairs missing lineage EDGES and never touches this.
- *Measured, and it is live rather than theoretical:* the maintenance sweep sends
  `declared_table_id(ds)` to `/compaction_plan`, and in one 400-row sample the two ids that 404 are
  `lakehouse$bronze$events` (134, bucket `lance-catalog`) and `lakehouse-bronze$events` (94,
  `lakehouse-wh`). The first is the `f"{project}${dataset}"` spelling that
  `warehouse_registry.project_namespace`'s own docstring records as the ingest plane's old composition —
  three segments, which the catalog parses as namespace/namespace/table and resolves to nothing.
- *The code that produced it is FIXED and that is exactly the point.* Ingest imports `project_namespace`
  now, and the deployed ingest image (`main-141f6199`) carries that fix — verified by ancestry against
  `14db0444`. The bad name survives in the DATASET, not in the code, and no code change repairs it
  because repair is write-triggered.
- *What it costs, per tick, forever:* those datasets 404 the compaction plane so they are never
  compacted distributively, and every maintenance lineage event they produce files against a Dataset
  node that names another table — a condition-1 defect on live data that no re-run will clear.
- *THE REPAIR SOURCE IS AN OPEN QUESTION, and the obvious answer is measured NOT to work.* This row
  first proposed repairing from `table_id_from_location`. Driven against the two affected URIs, it
  cannot supply the value:

      s3://lance-catalog/medallion/lakehouse$bronze  ->  'lakehouse$bronze'   (missing the table half)
      s3://lakehouse-wh/medallion/bronze             ->  None

  Neither is the correct `lakehouse-bronze$events`, so a repair built on that crossing would be a
  control that cannot fire. Naming it here so the next reader does not implement it.
- *AND THE TWO 404 IDS ARE TWO DIFFERENT DEFECTS, which the aggregate hid.* `lakehouse$bronze$events`
  (134) is a stale STAMP — the old `f"{project}${dataset}"` spelling, three segments, resolving to
  nothing. `lakehouse-bronze$events` (94) is the CORRECT spelling and 404s anyway, which is not a stamp
  problem at all but a table the catalog does not hold under that id. Only the first belongs to this
  row.
- **THE SAME DEFECT HAS A GRAPH-SIDE TWIN, measured 2026-09-11, and it shares this row's design
  question.** `register_table` ECHOED the caller's relative path and the door emitted it as the CREATED
  edge's `source_uri` (fixed forward by `cf040fff`, which now resolves via `describe_table`). Counted
  over every Dataset node in the live graph:

      1162 datasets carry a source_uri
      1102 absolute (all s3://)
        60 RELATIVE — e.g. `silver/loop-1785786423_cae1f8ffb5a1`, `transcripts_v2.lance/chunks.lance`,
           `probe-relative-loc`

  A relative URI opens as nothing, so each of those 60 classifies MISSING_ON_STORAGE and is reported as
  storage loss every tick. One of them is named `acme-bronze$zzprobe8926` pointing at
  `probe-relative-loc` — someone probed exactly this and it was never carried further.
- *THE OTHER EMITTERS WERE CHECKED AND ARE FINE,* so the sweep for producers of relative URIs is
  bounded rather than open. The shared write-door trailer (`api/lineage_deps.py:73`) takes its location
  from `dataplane.read_version_and_schema`, which returns `str(dataset.uri)` off the OPENED dataset —
  necessarily absolute. `declare_table` mints its location absolute. The medallion emitters pass composed
  `{root}/medallion/{tier}` URIs, absolute whenever the root is. Of the 60 relative nodes, the remaining
  shapes (`transcripts_v2.lance/chunks.lance`, `silver/loop-1785786423_…`) are media-plane and older
  stage residue, not a live producer in scope.
- *The fix is FORWARD-ONLY, which is what makes it this row's problem too:* `cf040fff` stops new ones,
  and the 60 keep their URI because nothing rewrites a Dataset node's `source_uri` after the fact. Same
  shape as the stale stamp above — a wrong value repaired only by a write that may never come — with the
  same authoritative source available (the catalog resolves the location) and the same open question
  about where the repair belongs.
- **THE REPAIR'S REACH IS MEASURED, 2026-09-11: 58 of the 60 are still GOVERNED.** "The catalog is the
  authoritative source" is a design statement until someone checks the catalog can still answer, and for
  58 of them it can — they carry live FGA tuples, so `describe_table` resolves a location for each. That
  turns the closes-when from an open question into a bounded job. The other TWO hold no tuple at all, so
  no door will answer for them and no repair can source a location: they are removals, not repairs, and
  a fix that assumes the catalog answers for all 60 will stall on exactly those two.
  *And 36 of the 60 are already dropped*, so they never reach the sweep — the live cost is the 24 that
  do, which is most of the `unreadable` line rather than all of it (the sweep reports 26).
- **THE STAMP HALF HAS A LIVE COUNT NOW, 2026-09-13.** Across 20 minutes of maintenance sweep
  outcomes, 205 distinct datasets: **186 carry a `table_id` matching their own path, 19 do not** — and
  the 19 are two different things, which a count alone would hide. Most are a SPELLING difference
  (`silver/consensus-live-…` stamped `silver$consensus-live-…`, `/` where the id uses `$`) and name the
  same table. The genuine stale stamps are the composed `medallion/<tier>` paths, and one is worse than
  the ids this row already names: `dataset='s3://bind86-wh/medallion/silver'` stamped
  `table_id='bronze$events'` — a SILVER dataset claiming to be a BRONZE table, in a tenant warehouse.
  The sweep sends that id to `/compaction_plan` every tick.
- **RE-MEASURED 2026-09-13: the 60 are unchanged, and a SECOND candidate repair source is now
  eliminated.** The graph still reports `1162 datasets carry a source_uri / 1102 absolute`, so the
  forward-only fix holds and nothing has repaired one. Two sources have now been driven and neither
  can supply the value:
  - `table_id_from_location` — measured 2026-09-11, above.
  - **lineage's own durable events feed** — it carries the FULL event JSON, so an earlier absolute URI
    for the same dataset would have repaired these without any new dependency. It does not: the feed
    holds 4,174 rows spanning 2026-09-07 -> 2026-09-13 only, and a search across the affected set
    returns ONE marker. These datasets stopped being written before the retention window, which is the
    same fact that makes them unrepairable by a write.

  The relative URIs are also now known to carry no recoverable information: they are the table id with
  `$` swapped for `/` (`silver$consensus-live-41965_…` -> `silver/consensus-live-41965_…`), so what is
  missing is the ROOT, and the root is a warehouse binding only the catalog holds.

- *THE REPAIR PATH IS NARROWED TO THREE, and the cheap one is blocked on an owner question.* Lineage
  has NO catalog client — no `describe_table`, no catalog URL, nothing — so:
  1. *Give lineage a catalog client.* Authoritative, and it puts a cycle between two lakehouse
     services: the catalog already emits INTO lineage.
  2. *Carry the id/location on the registration* so the graph never needs to ask. Forward-only again —
     it does not repair these 60.
  3. *Have the CATALOG re-assert the location*, which needs no new coupling direction and no new door:
     `_merge_dataset` already rewrites `source_uri` via `SET_DATASET_SRC` on any ingested event that
     carries one (`repository.py:409`), so a catalog-side assertion would restamp through the path that
     already exists. **This is the cheap one and it is NOT free to choose:** it means emitting events
     no run produced, which is precisely the "accept a synthetic graph" half of [[LH-146]]'s pending
     owner ruling. Implementing it now would pre-empt that decision.

- *Closes when:* the [[LH-146]] ruling settles whether a synthetic assertion is acceptable — if yes,
  option 3 repairs all 58 governed datasets through existing seams; if no, option 1 is the remaining
  authoritative answer and the catalog↔lineage cycle has to be accepted or broken deliberately. The
  two ungoverned of the 60 are removals either way: no door will answer for them. Pin that a corrected dataset keeps its `_rowid`s
  (`update_schema_metadata` is metadata-only, so that is provable), and pin separately that the
  94-count id's absence from the catalog is diagnosed rather than folded in here.
- **RE-MEASURED 2026-09-15 on the live estate. Stated as MEASUREMENT, not as a root cause — two root
  causes published earlier today had to be retracted, and this row is one where the obvious reading is
  already recorded as wrong once.**
  * Both stamps confirmed, with their exact values:
    `s3://lance-catalog/medallion/lakehouse$bronze` carries `lineage.dataset_id = 'lakehouse$bronze$events'`
    and `s3://lakehouse-wh/medallion/bronze` carries `'lakehouse-bronze$events'`. 8 rows each.
  * **NEITHER NAME IS A CATALOG TABLE** — checked against the 381 tables `_tables_across` enumerates.
    So the 404 is not "the id is malformed"; it is "the id names nothing", which is a different repair.
  * Corroborated independently by the sweep: in a 30-minute window those two ids are the ONLY
    `/compaction_plan` 404s (4 and 3 respectively), which is the same pair this row measured in a
    400-row sample on 2026-09-11. The condition is stable, not drifting.
- *And the estate carries TWO live naming conventions, which is what makes "the correct value" hard:*
  `lakehouse$silver$features` and `lakehouse$silver-media$features` (nested, `$`-separated) sit beside
  `lakehouse-silver$features` and `lakehouse-gold$catalog` (hyphen-qualified project prefix) — all four
  are real tables. `lakehouse-bronze$events` follows the convention its OWN warehouse's siblings use
  exactly, so it does not read as a malformed leftover; the table it names simply was never registered.
- *What that does to this row's open question:* the repair source cannot come from the location
  (already measured here), and it cannot be validated against the catalog either, because the target
  does not exist. That points at the same unregistered-`medallion/<ns>`-dataset population as
  [[LH-164]]'s remaining owner decision, so the two may resolve together — **may**, on today's
  evidence, and this row does not assert it.
- **THE OPEN QUESTION IS ANSWERED AND THE GUARD HAS LANDED — 2026-09-16.** This row said the guard
  could not be placed until one thing was measured: *why this dataset produced 30 plan calls and ZERO
  credential calls.* Because **the sweep carries two table identities per dataset and asks a different
  door about each**:
  * the CREDENTIAL door gets `item.table_id`, which `plan_sweep` fills from `table_id_from_uri` —
    `None` for every composed `medallion/<tier>` path;
  * the COMPACTION-PLAN door gets the dataset's own `lineage.dataset_id` stamp, read inside
    `compact_one`.

  So a stamp the vend never sees reaches the catalog. `write_options_for` returns the ambient fallback
  at `table_id is None` on a `logger.debug` line and makes **no HTTP call**, which is why the condition
  left no trace: measured over 90 minutes on the live estate, **1,310 SCOPED vend lines, 0 AMBIENT, 0
  `maintenance_vend_skipped_unresolvable_location`**. Every observable read as total coverage.
- **THE LIVE COUNT, one tick 2026-09-16: 59 datasets wrote, 56 under a vended table-scoped credential
  and 3 under the AMBIENT one with no vend decision at all** — and all three are composed paths:

      s3://bind86-wh/medallion/silver      stamped bronze$events     indices_optimized=1
      s3://lance-catalog/medallion/silver  stamped silver$features   indices_optimized=1
      s3://lance-catalog/medallion/gold    stamped bronze$events     indices_optimized=1

  Two datasets in two buckets both claim to be `bronze$events`; `s3://lance-catalog/medallion/gold` is
  a THIRD crossing this row did not have. None of them is a one-off.
- **THIS ELIMINATES THE THIRD CANDIDATE FIX, the one this row proposed itself.** "The cheap
  discriminator is probably the vend itself" cannot work: the crossing datasets are *precisely the ones
  that never vend*. A guard placed there would be a control that cannot fire on the population it was
  built for — the same failure this row already recorded for `table_id_from_location` and for lineage's
  durable feed.
- **WHAT THE DISCRIMINATOR ACTUALLY IS: a field already on the wire and being discarded.**
  `CredentialResponse` carries `location` — the catalog's own answer for where that table lives —
  and `_vend` read only `credentials.storage_options`. So the check needs no extra call, which is what
  the tick's 552-dataset walk could not have afforded.
- **LANDED**: the probe that already opens each about-to-vend dataset now also reads its stamp
  (`sweep._probe_before_vending`), the path still wins wherever it can answer — this row is a catalogue
  of wrong stamps, so a stamp must never displace a layout-derived id — and
  `credentials.write_options_for` refuses the unit when the catalog's location for that id does not
  **cover** the URI being maintained, naming both locations. Containment, never equality: several live
  datasets are swept at `<root>/tree/<branch>` while the catalog answers with the root, and equality
  would refuse the healthy majority. Gated by
  `services/maintenance/tests/test_a_vended_credential_must_cover_the_dataset_it_signs.py` and
  `tests/unit/test_the_sweep_vends_for_the_dataset_it_is_holding.py`.
- **BLAST RADIUS BOUNDED WITHOUT DEPLOYING: no dataset that vends today would be refused.** A vended
  credential that did not cover its dataset would already be failing the rewrite at the object store,
  and the tick recorded **zero errored datasets holding one** (the 20 `AccessDenied` strings in the
  window are all inside the `s3://lance-catalog/models/` shallow-clone refusal, on `table_id=None`
  datasets that vend nothing). The three composed datasets above WILL be refused every tick — which is
  this row's Closes-when working: three silent ambient writes become three visible refusals until the
  stamps are repaired.
- *NOT CONFIRMED IN-CLUSTER, stated rather than glossed.* A host-side dry-run against the live catalog
  answered 401 — *"the presented credential may not claim 'service-maintenance'"* — which is the
  zero-trust design working: maintenance holds a dedicated token from the secret store and
  `APP_API_TOKEN` cannot claim its identity. The bound above is from log evidence, not from firing the
  guard in-cluster, and the deploy is behind [[LH-169]]'s split stem either way.
- **WHAT IS LEFT OF THIS ROW IS THE REPAIR, AND ONLY THE REPAIR.** The guard stops a crossing from
  landing; it does not correct a single stamp or a single relative `source_uri`. The 58 governed
  relative-URI datasets and the composed stale stamps still need option 1 or option 3 above, and
  option 3 is still blocked on [[LH-146]]'s ruling about a synthetic assertion.
- **DEPLOYED AND OBSERVED FIRING 2026-09-16** — built with Dagger as `main-fd3999f4`, rolled to all ten
  stem-mates ([[LH-169]], now closed), first sweep tick on the new image:

      the dataset at s3://bind86-wh/medallion/silver           declares 'bronze$events'       catalog: s3://lance-catalog/medallion/bronze
      the dataset at s3://lance-catalog/medallion/gold         declares 'bronze$events'       catalog: s3://lance-catalog/medallion/bronze
      the dataset at s3://lance-catalog/medallion/silver       declares 'silver$features'     catalog: s3://bind86-wh/medallion/silver
      the dataset at s3://acme-bucket/4750a5b9_acme-bronze$events  declares 'acme-bronze$events'  catalog: s3://acme-bucket/medallion/bronze

  **The fourth is new and it is a FLAT layout**, which the row's own framing did not predict: its id is
  derived from the path and parses perfectly, and the catalog holds that table somewhere else in the
  same bucket. So two directories claim one table id, and the crossing is not confined to the composed
  `medallion/<tier>` population — checking the vend where the path CAN answer is load-bearing, not
  belt-and-braces.
- **THE ESTATE NO LONGER SIGNS A REWRITE WITH THE AMBIENT KEY.** Same tick: **220 SCOPED vend decisions,
  0 AMBIENT**, against 137 SCOPED + 3 silent ambient writes before. 56 datasets optimized an index,
  exactly the 56 that were properly vended before — so nothing that used to be maintained stopped being
  maintained, and the three that were being written under the root-reaching key are now refused by name.
- *Also connects [[LH-137]]:* that row's half (b) residue is these exact two ids, so it is this defect
  seen from the compaction door rather than a separate fault.

**LH-134 · ~~Credential vending accumulates one STS identity record per vend, and at ~100k the store cannot restart~~ — CLOSED AND OBSERVED 2026-09-11**
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
  being outpaced roughly fifty to one.
  **THIS CLOSURE IS WRONG, re-measured 2026-09-11: the fix it credits is NOT DEPLOYED.** `27ce13f3` is
  not an ancestor of the running image, and the deployed `sweep.py` has no `_may_write_anything` —
  checked inside the pod. The halt is vends FAILING (401 on every one, see [[LH-142]]), not vends being
  skipped. Same number, opposite cause. Reopen on the deploy and re-measure then. The sweep is unaffected — policies loaded, 95 registry buckets
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

**LH-132 · ~~The cascade's delta lane and its full lane DISAGREE about a deleted row~~ — CLOSED 2026-09-13 (LANDED, both guards, three tests)**
`medallion` · was med · filed 2026-09-11

- **LANDED, and the row is closed by reading the code rather than by argument.** `scripts/ray_stage_job.py`
  now calls `retracted = _retract_deleted(upstream, to_uri, so)` inside the delta lane, and every clause
  this row specified is there:
  *Guard A — retraction BEFORE the early return.* The call sits above the `rows_in == 0` branch and the
  comment states this row's own reasoning: "RETRACTION FIRST, because a deletion-only change IS an empty
  delta … Run after the early return below and a delete would leave through the `delta_empty` door
  reporting that nothing changed." The lane's log carries `retracted=` alongside `delta_empty=1`.
  *Guard B — refuse rather than retract on an empty key set.* `UpstreamVanishedError` (:602, raised at
  :656) — "retracting against it would empty {to_uri}" — the `StagedOutputEmptyError` shape this row
  asked for.
  *The join is the one specified:* upstream side `source_rowid` where present else the reserved
  `_rowid`, downstream always `source_rowid`; a NULL `source_rowid` is left alone because "unjoinable"
  must not read as "orphaned"; deletes are chunked. The 1:N-deeper-in under-deletion is documented in
  the docstring rather than left to be discovered.
- *Tested, three ways* (`tests/unit/test_ray_stage_job.py`, 43 passed):
  `test_a_row_DELETED_upstream_is_retracted_from_the_tier_below`,
  `test_the_two_lanes_AGREE_about_a_deleted_row` — this row's exact defect — and the guard at :1186-1196,
  which deletes every upstream row and asserts the tier still holds 3, "the tier was emptied by a
  refusal that did not hold".
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

**LH-011 · ~~three lineage knobs ship off in the prod render~~ — CLOSED 2026-09-11: the owner's number is 48**
`lineage` · was med

- *THE NUMBER IS 48 HOURS (owner, 2026-09-11)*, shipped as `services.lineage.freshnessBudgetHours` and
  observed live as `LINEAGE_FRESHNESS_BUDGET_HOURS=48`. A lane that has landed nothing in two days has
  stopped; a quiet weekend day has not.
- *WHAT TURNING IT ON REVEALED, and it belongs to [[LH-002]] rather than here.* The first sweep with the
  axis armed reported **`stale=268` of `checked=356`**. The number is not wrong — those datasets really
  have not been written in 48 h — but the population is dominated by the same e2e residue LH-002
  describes, so ~75% of the finding is dead test data nobody will write again. Raising the budget to
  quiet it would hide a stalled lane to avoid naming dead data, which is the wrong trade: the budget is
  a policy about LIVE lanes and the residue is a retention problem with its own row and its own date
  (2026-09-30). Recorded here so the first operator to see 268 knows it was measured, expected, and
  whose row it is.

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

**LH-137 · Half (a) REOPENED 2026-09-17 — the silver→gold refusal reproduces on the live estate, and the drive the closure asked for is the thing that found it**
`medallion, catalog` · **HIGH** · found by the 2026-09-11 audit; closed 2026-09-16; REOPENED 2026-09-17 by driving a real publication

- **THE CLOSURE RESTED ON TWO CLAIMS AND A DRIVE NOBODY HAD DONE. The drive has now been done, and it
  refutes both claims.** The 2026-09-16 entry closes (a) on *"the current stage-runner pod records 0
  `unconfined_uri` refusals"* and on `supplied` and `root` being *"now measured to AGREE"*, while
  recording that the remaining condition *"is not reproducible without driving a real publication…
  Named rather than done."* Driving one is exactly what produced the evidence.
- *Measured 2026-09-17, on pods created that morning from `main-e3139b71`:* two `/produce` cascades, and
  each silver write was followed within ~1s by

      medallion_stage_from_uri_refused transition='silver->gold' project='bind86'
        supplied='s3://bind86-wh/medallion/silver'
        root='s3://bind86-wh/78de8931_bind86-silver$features'

  then `dapr_dead_letter_parked app='silver-to-gold' dlq_topic='dlq.silver-to-gold'`. 10:57:02.792
  followed a silver write at 10:57:02.617; 10:58:58.182 followed one at 10:58:57.972. A pod that had
  existed for two minutes carried one, so "0 refusals on the current pod" measured a pod that had not
  yet seen a publication rather than a fault that had stopped.
- **`supplied` AND `root` DO NOT AGREE, and the shape is the one the closure said was falsified.**
  `supplied` is the TIER DIRECTORY (`…/medallion/silver`); `root` is the TABLE
  (`…/78de8931_bind86-silver$features`). The confinement check cannot pass, so this is not a staleness
  window between two catalog answers — it is two different KINDS of path, which is the composed-path
  reading the row originally had.
- *WHAT IS NOT ESTABLISHED, stated so the next person does not inherit a guess:* the refused trigger
  names project `bind86` while the driving producer is configured `MEDALLION_PRODUCE_ADMIN_PROJECT=acme`
  and neither `/produce` passed `?project=`, so the refused event is NOT demonstrably the one my
  cascade emitted. The correlation is tight (two drives, two refusals, ~1s each) and the most likely
  mechanism is that a publish wakes a relay that redelivers a permanently-stuck `bind86` trigger — but
  that is a HYPOTHESIS. What is measured is that the refusal is live today, not historical.
- *Also not established:* whether the driving cascade's OWN silver→gold hop ran at all. No
  `medallion_stage_moved transition='silver->gold'` appeared for either token. Either the gold hop did
  not fire or it fired somewhere this drive did not read.
- **ROOT-CAUSED 2026-09-17 by following the refused event back to its publisher. The chain is
  complete and every link is measured.**
  1. `/produce` → bronze→silver completes and publishes the lane table `silver$features`
     (`medallion_publication_trigger dataset='silver$features' from_version=326 to_version=328`, then
     `328 -> 331` on the second drive).
  2. The catalog's `publication_extra` resolves the publication's TENANT through the registry binding
     (namespace → warehouse → project) and answers **`bind86`** — for a table whose id carries no
     tenant prefix at all.
  3. The trigger therefore travels as `project='bind86'` + `location='s3://bind86-wh/medallion/silver'`
     (`publication_trigger.py:147` carries `extra["location"]` verbatim).
  4. The gold runner takes the LANE (`silver$features`) and the project (`bind86`) and composes the
     catalog identity `bind86-silver$features` — which EXISTS and is a **different dataset**.
  5. Confinement then compares the published table's path against the composed table's path and
     refuses, correctly, because they are not the same table.
  *Catalog locations, read through the service's own helper:*

      silver$features         -> s3://bind86-wh/medallion/silver
      bind86-silver$features  -> s3://bind86-wh/78de8931_bind86-silver$features
      gold$catalog            -> None

  **`gold$catalog` resolving to `None` is the confirmation that this hop has never once completed**
  for the lane the deployed cascade actually runs — not a window, a permanent state.
- **A SECOND, INDEPENDENT DEFECT FOUND IN THE SAME READ, and it is the sharper one because it makes
  the `from_uri` feature unable to do anything but no-op or refuse.** `transform.py:636` is
  `from_uri = read_root = vended`, so `read_root` is narrowed from the tenant's WAREHOUSE to the single
  vended TABLE. `_confine_from_uri`'s own docstring states the opposite invariant — *"That root is the
  TENANT'S WAREHOUSE for a project trigger, which is what makes I2 work — the vended
  `<root>/<hash>_<ns>$<name>` sits directly under it."* With the window narrowed to one exact string,
  `uri_within(read_root, supplied)` can pass ONLY when `supplied` is byte-identical to `vended` — in
  which case honouring the trigger's `from_uri` changes nothing. So I2's permissive branch is
  unreachable: every case where the trigger's location DIFFERS (the only case the feature exists for)
  is refused. Code and its documented invariant disagree, and the docstring is the one describing the
  behaviour that would work.
- *Closes when:* a driven publication's silver→gold hop is observed COMPLETING — the positive
  observation, not the absence of a refusal, since absence is what closed this row wrongly once. Half
  (b) stays closed; its residue is [[LH-141]] and nothing here touches it.
- **ESTATE AUDIT 2026-09-17 (owner asked for the blast radius before any write; NOTHING was changed).**
  The cause is one row in the warehouse binding registry, and the tier namespaces are inconsistent:

      bronze        -> (unbound)        silver       -> bind86-wh      gold       -> (unbound)
      bronze-media  -> lakehouse-wh     silver-media -> lakehouse-wh   gold-media -> (unbound)

  `silver` bound to a TENANT's warehouse is what makes `project_for_namespace('silver')` answer
  `bind86`, which is the first link of the chain above. It is accidental: `alpha`, `beta`, `media` and
  `zz-probe` are bound to `bind86-wh` the same way and are plainly test residue.
  * *Scanned all 195 bindings against 90 warehouses.* Suspect = the namespace does not start with its
    warehouse's project. Only TWO suspects are medallion tier names: `silver` -> `bind86-wh` and
    `silver-media` -> `lakehouse-wh`. The rest are test namespaces (`sec*`/`gov*` -> `acme-bucket`,
    `track*`, `zz-probe`) that name no tier and drive no cascade.
  * **A PREDICTION WAS MADE AND REFUTED, which is why the rule is now stated correctly.** If a
    tier bound to a project were sufficient, the media chain would fail identically — `silver-media`
    is bound to `lakehouse-wh`. It does not: `rask-media-to-silver` has ZERO `from_uri_refused` in its
    whole life. The reason is that no `gold-media` stage runner is deployed, so nothing CONSUMES a
    silver-media publication. **The defect needs BOTH a tier namespace bound to a project AND a
    deployed downstream consumer**, and only the tabular silver→gold hop has both today. Deploying a
    `gold-media` runner would light up the second instance.
  * *A second, independent difference the audit exposed, and it dates the two registrations to
    different code paths:* `silver-media$features` sits at a catalog-VENDED location
    (`s3://lakehouse-wh/a76d1ca5_silver-media$features` — the `<hash>_<ns>$<name>` shape) while
    `silver$features` sits at a COMPOSED one (`s3://bind86-wh/medallion/silver`). That is
    `catalog_register.py:301`'s own recorded defect — "the stage runner composed `{root}/medallion/{tier}`"
    — visible in one lane and not the other.
- **WHICH FIX IS AN OWNER DECISION, because the three candidates are not variations of one change:**
  (i) `publication_extra` must not infer a tenant for an id carrying no tenant prefix — the tenant
  would then be absent and the gold runner would read the lane table it was actually told about;
  (ii) `transform.py:636` must stop narrowing `read_root`, restoring the invariant the guard documents
  — this alone does not fix (i), it stops the guard being a one-string window;
  (iii) estate config — `silver$features` is registered under `bind86-wh` while the runner's own
  `MEDALLION_TO_URI` is `s3://lance-catalog/medallion/silver`, so the registration may simply be stale
  dev residue and neither code path is wrong. (ii) is defensible on its own merits regardless of (i)
  and (iii), since a documented invariant and its code disagreeing is a defect either way.

- **THE 2026-09-16 READING, KEPT BECAUSE ITS EVIDENCE IS STILL EVIDENCE — but (a)'s conclusion is
  overturned by the drive above and only (b) remains settled.**
  * **(a) — the gold table EXISTS, and that part holds.** `bind86-gold$catalog` is at
    `s3://bind86-wh/c1c79484_bind86-gold$catalog`, beside eight other projects' gold tables — so the
    silver→gold hop this row says is stopped has run. The current stage-runner pod records **0**
    `unconfined_uri` refusals. And the two sides of the confinement check are now measured to AGREE:
    `read_root` comes from `describe_table_location(identity.from_dataset)` =
    `describe_table_location('bind86-silver$features')`, and the sweep reads that table at
    `s3://bind86-wh/78de8931_bind86-silver$features`; `supplied` is `publication_trigger`'s
    `extra["location"]` — the catalog's vended location for the SAME id. Two catalog answers for one
    table, which is what the row's own narrowing said they should be.
  * *The STALENESS reading it drew from that is what 2026-09-17 refutes:* the drive shows `supplied`
    and `root` are different KINDS of path, not one path read at two times. That a gold table exists
    means the hop ran at SOME point, which is compatible with it being broken now — an existing
    artefact is not a working pipeline, and reading it as one is what closed this row early.
  * *Recurrence is already loud, which is why this closes rather than waiting for a drive.*
    `chart/alerting/rules.yml:160` fires on every `medallion_stage_refused_total` reason except
    `routing_disabled`, `unconfined_uri` included; and `DiagnosticFormatter` now renders `extra`, so the
    next refusal names its own `supplied` and `root` on the line — the diagnosability blocker this row
    records is gone and the alert will say when to look.
  * **(b) is not a route fault and its residue belongs to [[LH-141]].** The row says "0 distributed
    commits against 24 in-pod fallbacks in 24 h". Measured: **980 `mode='distributed'` outcomes against
    1,124 `in_pod` estate-wide, and 28 of the 124 composed `medallion/<tier>` datasets run
    distributed** — the door drives medallion tier ids fine. Every `/compaction_plan` 404 in the window
    is one of exactly TWO ids, `lakehouse$bronze$events` and `lakehouse-bronze$events`, both
    `TableNotFoundError` — the stale-stamp pair [[LH-141]] already names, tracks and now guards. A
    second row for the same two ids is how one defect gets fixed twice and closed neither time.
- *Why open:* Both were seen on the live estate and neither is tracked. (a) Every one of the 8
  silver→gold triggers for project `bind86` on 2026-09-10 was DROPped with
  `medallion_stage_from_uri_refused` and parked on `dlq.silver-to-gold` within a second of publish —
  `medallion_dlq_parked_total` steps in lockstep with `medallion_stage_refused_total{reason=unconfined_uri}`.
  So a whole project's cascade is stopped by a confinement check nobody has diagnosed. (b)
  `/compaction_plan` answers 404 for medallion tier ids, so the distributed compaction door cannot be
  driven for exactly the datasets the cascade writes (0 distributed commits against 24 in-pod fallbacks
  in 24 h).
- **THE DIAGNOSABILITY BLOCKER IS GONE — verified in the RUNNING POD 2026-09-13, not inferred from a
  commit date.** This row records that the branch "could not be named" because the deployed text format
  renders no `extra`. That is no longer true of the estate: `DiagnosticFormatter` renders every `extra=`
  field after the message, and `rask-silver-to-gold` was read in place —
  `inspect.getsource(DiagnosticFormatter.format)` carries the diagnostics tail. So the NEXT silver→gold
  refusal names its own `from_uri` on the line, and half (a) needs one hop DRIVEN rather than a new
  mechanism.
- *And the 2026-09-11 avenue is closed off rather than left hanging.* That note leaves "GreptimeDB's
  `opentelemetry_logs` was not queried" as untried; it is tried now and yields nothing recoverable.
  Those records reached GreptimeDB through the collector's FILELOG receiver, which tails pod stdout, so
  their `log_attributes` carry only `log.file.path` / `log.iostream` / `logtag` and the body is the bare
  string `medallion_stage_from_uri_refused`. The refusing URI was never written anywhere that survives —
  the OTLP copy that rescued other diagnostics this week does not exist for this one.
  *For whoever re-drives it:* `medallion_stage_refused_total` runs 2026-09-10 12:56 → 16:03 and then
  stops, max counter 6 in the retained window (this row says 8 triggers; retention may have clipped the
  earlier samples, so the two are not necessarily in conflict).
- *A SECOND FAULT sat in the same window and is recorded because it was in front of me:* that same
  stage-runner pod logged repeated `UNAVAILABLE: … ipv4:127.0.0.1:50001: Connection refused` — it could
  not reach its own Dapr sidecar. Cause, consequence or coincidence is NOT established; it is named so
  the next person driving this hop checks the sidecar before the confinement branch.
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
- *HALF OF (b) NARROWED BY MEASUREMENT 2026-09-11, and it is an identifier crossing rather than the
  route.* `/compaction_plan` resolves its `{id}` with `parse_identifier` + `describe_table`, so a 404
  means the id names no table. The id maintenance sends is `declared_table_id(ds)` — the
  `lineage.dataset_id` a producer STAMPED IN THE SCHEMA (`stage_stamp.py:50`), which is an
  OpenLineage dataset name; nothing asserts it is also a catalog identifier. The function that
  exists to produce a catalog identifier answers `None` for the nested medallion layout, measured by
  calling it: `table_id_from_location('s3://lakehouse/medallion/bronze')` -> `None`, likewise
  `medallion/silver-media`, while `4750a5b9_acme-bronze$events` -> `acme-bronze$events` and
  `medallion/bronze$bind86` -> `bronze$bind86`. That is deliberate — its docstring calls `None` a
  first-class answer for "a nested layout whose namespace is a parent directory rather than part of
  the leaf" — so for a tier laid out that way NO code path yields a catalog-valid id, and the
  stamped lineage name is the only thing left to send. Which is a 404 for exactly the medallion
  datasets and for nothing else, matching what was observed.
  STILL UNMEASURED, and it decides the fix: whether the stamped id resolves for the tiers that DO
  carry a `$` leaf. That needs the live catalog's table list beside what the sweep sends, so the
  drive below still stands.
- **(a) IS ROOT-CAUSED 2026-09-11 WITHOUT A DRIVE — the `extra` the deployed text formatter drops is in
  GreptimeDB, which this row listed as unqueried.** All 8 refusals, byte-identical:

      root     = s3://bind86-wh/78de8931_bind86-silver$features   (catalog-VENDED, uuid8 layout)
      supplied = s3://bind86-wh/medallion/silver                  (the COMPOSED tier path)
      project  = bind86   transition = silver->gold   at transform.py:682-683

  **It is candidate (1), in its STALE form; (2) and (3) are refuted.** The refusal itself proves
  `from_uri` was PRESENT — `_confine_from_uri` checks nothing when it is absent (`if supplied:`), so an
  omitted location produces no refusal at all. And `publication_trigger.py:147` sets that field from
  exactly one source, `extra["location"]`. So the catalog's publish control event carried
  `s3://bind86-wh/medallion/silver` as the location of a table it now vends at
  `s3://bind86-wh/78de8931_bind86-silver$features`. (3) is refuted because the composed value arrived
  THROUGH the publication rather than being composed by the consumer — the consumer's own resolution was
  correct, and `read_root` is the vended location. (2) is refuted because the 10 publication triggers
  that day all name `dataset=silver$features`, the same object `read_root` resolves to.
- *So the defect is one location recorded at publication and a different one vended at trigger time*,
  and the guard is working exactly as designed: `transform.py:667` already says the composed layout is
  "a path no catalog-written table has ever occupied". The fix belongs where `location` is STAMPED on the
  publish control event, not in the confinement check.
- **THE WRITER IS IDENTIFIED, and it is tenant-specific rather than structural.** The same day's
  SUCCESSFUL stage moves, read from the same table:

      acme          to_uri = s3://acme-bucket/e41135a5_acme-silver$features      (vended uuid8)
      lakehouse-wh  to_uri = s3://lakehouse-wh/a76d1ca5_silver-media$features    (vended uuid8)
      acme gold     to_uri = s3://acme-bucket/3c099c25_acme-gold$catalog         (vended uuid8)
      bind86        to_uri = s3://bind86-wh/medallion/silver                     (COMPOSED)

  So the write half is NOT generally broken — every other tenant's stage writes to the catalog-vended
  location. bind86's bronze→silver stage alone fell back to the composed path, wrote its silver there,
  and published that path as the location; the gold stage then resolved the vended location and
  correctly refused the composed one. `_resolve_roots` composes first and lets
  `describe_table_location` override, so the fall-back means the override found nothing for THIS tenant
  at THAT moment — the likeliest reading being that `bind86-silver$features` did not yet exist in the
  catalog on the first hop. That is consistent with `catalog_register` logging
  `written_dataset_already_registered` with `location: medallion/<tier>` for the older tenants, whose
  writer and registration agree because `_require_same_location` checked them.
- *So the remaining question is narrow and tenant-shaped:* why the vended override was empty for
  bind86's silver on its first bronze→silver hop, and whether a stage that cannot resolve a vended
  WRITE location should compose one at all — composing is what makes the next hop unreachable, and the
  read side already refuses to do it.
- *What a drive is still needed for:* which writer stamped the composed location — whether the silver
  stage registered its output at its own composed write path (`to_uri = {root}/medallion/{to_namespace}`,
  `transform.py:601`, the write half the read-side I2 fix deliberately left alone), or the table was
  re-registered later. That decides whether the fix is at registration or at publication.
- **(b)'s HEADLINE IS REFUTED — the distributed plane is NOT unused.** The row says "0 distributed
  commits against 24 in-pod fallbacks in 24 h". `maintenance_dataset_outcome` carries `mode` per
  dataset, and its own docstring supplies the query; run over 600 outcomes since 2026-09-11 12:00:

      distributed  253
      in_pod       347
      fragments_removed  0

  So the door is driven for roughly two datasets in five, and "cannot be driven for exactly the datasets
  the cascade writes" is false as written. (600 is a LIMIT rather than a random draw, so the RATIO is
  indicative; any distributed outcome at all refutes "0", and 253 is not a boundary case.)
- *And `fragments_removed = 0` is the number that actually matters here,* matching LH-134's
  "`fragments_removed_total=0` across 785 ticks": compaction runs, distributed and in-pod alike, and
  removes nothing — because nothing needs compacting. So the 404/403 populations are a correctness
  defect worth fixing on their own terms, not the reason the estate is uncompacted. It is not.

- **(b) LARGELY CLOSED BY THE DEPLOY, observed 2026-09-11.** On `main-b641103f`,
  `compaction_plane_unavailable_falling_back` fell from hundreds per window to **6 in five minutes**, and
  the composition is now pure:

      3x 404 lakehouse$bronze$events     (the stale 3-segment stamp — [[LH-141]])
      3x 404 lakehouse-bronze$events     (correct spelling, absent from the catalog)
      0x 403

  **The AUTHORIZATION half is gone** — the 403s were the credential path failing ([[LH-142]]), and with
  vending working those datasets either vend or refuse cleanly via `maintenance_vend_denied` (27 in the
  window) instead of silently falling back. The distributed plane now reports
  `compaction_distributed_nothing_to_do` (400 in four minutes), i.e. it runs and finds nothing to
  compact — which is the `fragments_removed = 0` picture, not a broken door.
- *What remains of (b) is two ids and nothing else,* both already tracked: a stamp only a write repairs,
  and a table the catalog does not hold under that name.
- **(b) ROOT-CAUSED 2026-09-11 FROM THE SAME SOURCE, and it is TWO causes rather than one — the row's
  framing is wrong.** `compaction_plane_unavailable_falling_back` carries `uri`, `table_id` and
  `reason`; sampling 40 from the live estate:

      228/400  404 — the id names no table, across just TWO ids
      172/400  403 — `can_maintain required on table:<id>`, across NINE tables

  BOTH causes are real and neither dominates the way a first 40-row sample suggested — that sample read
  26/40 in favour of 403 and the 400-row one reverses it, so any proportional claim here needs the larger
  draw. 543 `maintenance_rewrite_denied` in the same window are the refusals that stop a dataset outright.
  The populations are what matter more than the ratio, because both are a handful of datasets retried
  every tick:

      404: lakehouse$bronze$events (134, bucket lance-catalog), lakehouse-bronze$events (94, lakehouse-wh)
      403: 9 tables, entirely in lane-wh / e2e-iso-a / e2e-iso-b / bind86-wh / research-bucket

  So the IDENTIFIER half hits the platform's OWN medallion datasets, while the AUTHORIZATION half is
  confined to test and e2e residue buckets — the same residue population LH-002 tracks, and the reason
  LH-078's per-warehouse `maintainer` tuples do not cover them (a warehouse the registry does not
  account for grants nothing).
- *The 404 half is an identifier defect, and a provenance one underneath it.* The failing ids are
  `lakehouse$bronze$events` — THREE segments, which the catalog parses as namespace `lakehouse` then
  `bronze` then table `events`, naming nothing — and `lakehouse-bronze$events`. Worse, ONE id is sent
  for three different datasets:

      table_id lakehouse$bronze$events  <-  s3://lance-catalog/medallion/lakehouse$bronze
      table_id lakehouse$bronze$events  <-  s3://lance-catalog/medallion/lakehouse$silver
      table_id lakehouse$bronze$events  <-  s3://lance-catalog/medallion/lakehouse$gold

  The id is `declared_table_id(ds)`, the `lineage.dataset_id` stamped in schema metadata — so silver and
  gold each claim to BE the bronze dataset. That is a condition-1 defect in its own right, independent
  of compaction: every lineage edge those tiers carry attaches to the wrong Dataset node.
- **CORRECTION, same day: the mis-stamped tier id is ALREADY FIXED AT HEAD, and the live data is stale
  evidence.** `ray_stage_job.py:464` reads `RASK_DEST_TABLE` and stamps it, and its own comment names
  this exact symptom as what it closed — "this job never read it, so every derived tier inherited its
  UPSTREAM's name through schema metadata — silver's compactions and silver's per-dataset FAIL events
  filed against bronze's node". `work_order.to_env` emits the key (`work_order.py:151`) and the
  in-process lane passes `dataset_id` too. So the three tiers sharing one id are datasets written before
  that fix, not proof of a live defect — which is the same "a verdict is not evidence" trap, applied to a
  finding measured twenty minutes earlier in this session.
- *What the live estate therefore cannot yet show:* whether the deployed Ray image carries that fix. The
  Ray head image is not chart-owned, so the stamp landing correctly is exactly what a deploy would
  settle, and nothing short of one will.
- *Still unmeasured on this half:* why `lakehouse-bronze$events` 404s as well, since it is the correct
  two-segment shape — warehouse scoping is the obvious candidate and the catalog's table list would
  settle it.
- *Closes when:* Drive one `bind86` silver→gold hop (owner-authorised 2026-09-11), print `supplied` vs
  `read_root` at the refusal, and fix whichever of the three the values name; reproduce (b) by calling
  `/compaction_plan` with a medallion tier id and fixing whichever of the id resolution or the route is
  wrong.

- **RE-MEASURED 2026-09-15, and half (b)'s HEADLINE IS STALE IN THE ESTATE'S FAVOUR.** This row says
  the distributed door "cannot be driven for exactly the datasets the cascade writes", citing 0
  distributed commits against 24 in-pod fallbacks in 24 h. Measured over 30 minutes on the running
  sweep: **655 `mode='distributed'` against 1288 `mode='in_pod'`**, with 604 of the distributed ones
  reporting `compaction_distributed_nothing_to_do` — so the door is driven continuously and answers.
- *And the in-pod majority is explained rather than suspicious:* **1112 of the 1288 in-pod lines carry
  NO `table_id` at all, while 0 of the distributed ones lack one.** The distributed door is addressed
  by catalog table id, so a dataset discovered by BUCKET SCAN with no catalog table behind it cannot
  use it and falls back in-pod. That is the mechanism, not a fault.
- *The residue of (b) is 7 refusals in that window, and they are [[LH-164]]'s class exactly:* `404` for
  `lakehouse$bronze$events` and `lakehouse-bronze$events` — derived ids naming no registered table.
  So (b) is not "the cascade's datasets are undrivable"; it is "a handful of derived ids name nothing",
  and the fix belongs with the id derivation, not with the door.
- *For scale, measured the same day:* **381 catalog tables across 97 warehouse roots, 0 unreadable, and
  the reconciler's new `ungoverned_tables` category reports 0.** Every table the catalog knows is
  governed.
- **HALF (a) IS NOT ROOT-CAUSED. An earlier revision of this row claimed it was, and that claim was
  produced by reading the wrong value — it is corrected here rather than left standing.**
  The mistake: `uri_within(read_root, supplied)` was driven with `read_root` taken from
  `MEDALLION_FROM_URI`, the pod's env. That is only the root for a trigger carrying NO project.
  `_resolve_roots` RESOLVES the root per tenant — `if project: read_root = project_root(...)` — so the
  env value is a neighbouring representation, not what the code receives.
- *Driven properly, in the running `rask-silver-to-gold` with its secrets spliced the way boot splices
  them:*

      project=<none>    read_root=s3://lance-catalog/medallion/silver
      project=bind86    read_root=s3://bind86-wh        uri_within(bind86 table) -> True
      project=research  read_root=s3://research-bucket

  A bind86 trigger that CARRIES its project resolves to bind86's own warehouse root and passes. So
  "every warehouse-backed project is structurally refused" is false, and per-tenant routing works.
- *What the refusal therefore requires:* a trigger whose `from_uri` is in a tenant bucket while the
  trigger's `project` is absent or resolves elsewhere. Both publishers reference `project`
  (`publication_trigger` states it always carries one; `ingest_trigger` names it too), so WHICH
  publisher emitted the 8 refused triggers, and what they carried, is still unmeasured.
- **SO THE ROW'S ORIGINAL ANSWER STANDS: it needs ONE bind86 silver→gold hop DRIVEN.** The refusal line
  already carries `project` and `root` in its `extra`, and the deployed `DiagnosticFormatter` renders
  extras — so a single new refusal names the project, the resolved root and the supplied URI together,
  which is exactly the triple that settles it. The 2026-09-10 evidence cannot be recovered; a fresh
  hop can.

**LH-138 · ~~The reconcile TIP axis still stamps a `reconcile` edge on a maintenance version~~ — CLOSED 2026-09-11**
`lineage` · was low

- *Closed by:* `5eb73751`. The tip resolves DOWNWARD to the newest version that wrote data
  (`_newest_data_version`), so a compaction at the tip is not drift at all: the comparison converges on
  the graph's own tip and classifies `in_sync` with no back-fill. Paid only when the two maxima disagree,
  bounded by `MAX_TIP_PROBE_VERSIONS`, falling back to the raw tip rather than to a guess. That is the
  coherent fix this row asked for, and it avoids the permanently-red failure withholding the back-fill
  would have caused.
- *THE ROW WAS THE SMALLER HALF, found by MEASURING a compaction instead of enumerating operation names.*
  One `compact_files()` commits TWO versions, not one: an unmodelled one whose counters are identical to
  the version below it, then the `Rewrite`. `MAINTENANCE_OPERATIONS` excluded the `Rewrite` and not the
  other, so `_recover_holes` — the axis this row assumed was already correct — back-filled a phantom
  `WROTE` edge on every compaction in the estate, below the tip as well as at it.
- *WHY IT HID:* `type(op).__name__` returns `BaseOperation` for an operation pylance models no subclass
  for — the ABC's own name, which is the word "unknown" wearing an operation's clothes. The module's
  comment already reasoned that unknown must be REPORTED, and was right; but reporting and back-filling
  were one list, so that reasoning silently authorised a fabrication it never argued for. The two are now
  separate: every unknown is reported, and only a NAMED data operation is recovered.

**LH-014 · The DIY provenance recipe (`stamp_stage`, `source_rowid`, the tier contract) is written down nowhere**
`medallion, lineage` · low

- *Why open:* Marked Doc in §O1 — anyone writing a new lane has to reconstruct the contract from the code.
- *Closes when:* Write the recipe (`stamp_stage`, `source_rowid`, the `{id, payload, stage, lineage, source_rowid}` tier contract) into `docs/architecture/` or the `rask-lance-catalog` skill.
- **RE-MEASURED 2026-09-16: the premise HOLDS.** `stamp_stage` appears in no document at all — only in
  `.claude/skills/rask-lance-catalog/SKILL.md`, and there inside an incident narrative rather than as a
  recipe; `source_rowid` is scattered across seven files; and the tier contract string
  `{id, payload, stage, lineage, source_rowid}` appears in none of them.
- **WRITTEN 2026-09-16** as `docs/architecture/medallion-data-flow.md` § 6a, beside § 6 "What a stage
  actually writes", which is where a lane author is already reading. One function and three rules, each
  with the measurement that makes it a rule rather than a style: an absent value DROPS rather than
  inherits (a child publishing its parent's id is a claim about the wrong object); `source_rowid` is
  minted once at the first derive off bronze and never re-minted (re-minting reroots the chain one tier
  down, so gold would name silver); and the STAMP owns column order, because `lance_ray` casts by
  POSITION and two hand-built orders for one dataset killed every tabular cascade at gold on
  2026-08-30.
- *It also carries the trap the stamp alone does not close* — `merge_insert` does not carry schema
  metadata, so `ensure_declared_dataset_id` is the metadata-only repair that lets the estate self-heal
  — and the divergence that is still real: rule 3 holds for the Ray driver, while the in-process blob
  path builds the order by hand, so one media lane can yield two silver schemas depending on
  `MEDALLION_RAY_ENABLED`. Stated rather than glossed, so the document does not read as more settled
  than the code.

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
`catalog` · **HIGH** · *unblocked 2026-09-16 — R1 acknowledged and the lineage half shipped; the replay marker and the `/commit` retirement remain*

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
- **A DIFFERENT DEFECT ON THE SAME DOOR, FOUND AND FIXED 2026-09-11 while re-measuring this row.**
  `create_table_version` MOVES the file at `manifest_path` into the table's version slot — not a copy —
  and nothing checked that the path belonged to the table. Driven against a real `dir` namespace with no
  privileged access: a holder of `can_write_data` on ONE table named a manifest inside ANOTHER table's
  `_versions/` and both destroyed that version and grafted its rows into their own table
  (victim `[1, 2, 3]` -> `[1, 3]`; attacker `[1]` -> `[1, 2]`). The FGA gate is sound and never reached
  it: it authorises the table in `id`, and the reach came from a field nothing inspected. The version CAS
  (`latest + 1` only) is a real guard and is not this one — aimed at the version it demands, the move
  succeeds. Fixed by requiring the spec's own relative shape (`namespace.md`, "Table Version Metadata
  Schema", example `_versions/<n>.manifest`), which is confined by construction; pinned by
  `services/catalog/tests/test_a_version_entry_cannot_adopt_another_tables_manifest.py`, and the same
  guard is applied to `batch_create_table_versions`, whose entries carry the identical field.
- *Why open:* Version routes at `endpoints/versions.py` are mounted and FGA-gated (`_BATCH_PATHS`, `_action_relation` → `can_write_data`) but nothing else runs on them, and `managed_versioning` is never advertised in `DescribeTable`, so a stock Lance client's commit bypasses the whole governance chain. `batch_commit_tables` is `UnsupportedOperationError` on the dir backend and always will be.
- *Closes when:* Owner acknowledges R1 (the governed commit path IS the spec's managed-versioning path); then attach lineage emit, the quality gate, the replay marker and protection to `CreateTableVersion` in `endpoints/versions.py`, advertise `managed_versioning=true` in `DescribeTable`, alias then remove `/commit` (data.py:326-364, dataplane.py:556-637), and back `batch_commit_tables` with rask's own staged-manifest KV.
- **R1 ACKNOWLEDGED 2026-09-16 (owner: the R-series stands), and the LINEAGE HALF LANDED.**
  `POST /v1/table/{id}/version/create` now emits `CREATE_TABLE_VERSION` after the native call, pinned to
  the version just minted — the same shape as `restore_table`, for the same reason: the version state
  changed and the graph has to be able to say who changed it. Until this, a version minted through the
  SPEC's own commit door left no provenance while the same table's `/commit` door emitted: two doors
  onto one table, one of them silent.
  *The gate is route-derived* (`services/catalog/tests/test_a_version_door_that_mints_one_records_who_did_it.py`):
  every route in the module is classified as minting a version or not, so a ninth cannot be added
  without someone deciding which kind it is — and `batch_create_table_versions` is deliberately in the
  NO column, because it is 406 on this backend and an emit there would be provenance for work that
  never happened.
- **DEPLOYED `main-b2d08df6` and the door was DRIVEN — but the emit itself is NOT yet observed, and the
  difference matters.** Against the deployed catalog: a table created, `version/describe` answered 200
  with `manifest_path` `…/_versions/18446744073709551614.manifest`, and `version/create` with the spec's
  relative form reached the BACKEND — answering `400 InvalidInputError: Staging manifest not found …
  for version 2`, which is a real domain answer rather than a routing or signature failure. So the async
  door with its new dependencies is live and behaving; what is unproven is the emit, because it runs
  only on SUCCESS and a successful version-create needs a genuinely staged manifest (the client-direct
  write path), which this drive did not construct. Catalog logs: 0 errors since the roll.
- **THE 2026-09-11 GUARD BROKE THE DOOR IT PROTECTED, found and fixed 2026-09-16 while working the
  replay clause.** `manifest_path` is resolved by the backend as an **ABSOLUTE** path. Measured against
  a real `dir` namespace, driving every spelling against ONE staged manifest present on disk each time:
  `_versions/<n>.manifest-<uuid>` -> `InvalidInput "Staging manifest not found"`;
  `t.lance/_versions/<n>.manifest-<uuid>` -> the same; `/<root>/t.lance/_versions/<n>.manifest-<uuid>`
  -> **OK, version 2 committed**. The guard refused every absolute path and demanded the relative form,
  so it did not confine this door — it CLOSED it: the only spelling that can commit was the one being
  rejected 400, and the relative form it insisted on can never succeed. The earlier live drive recorded
  exactly this symptom (*"answering 400 InvalidInputError: Staging manifest not found … a real domain
  answer rather than a routing failure"*) and it was read as the drive's own missing manifest rather
  than as the door being shut.
  **CONFIRMED ON THE LIVE S3 ESTATE**, not only on a local `dir` namespace. Against the deployed
  catalog (`main-b2d08df6`), `acme-bronze$events` (real location `s3://acme-bucket/medallion/bronze`):
  an absolute `manifest_path` answers **400** with the guard's own sentence, and the relative form
  driven at the version the CAS demands (186) answers **400 `Staging manifest not found at
  '_versions/…'`** — the same refusal the local measurement produced with the file present. Both
  spellings are therefore dead on the deployed door: one refused before the backend, one refused by it.
  A version below the CAS answers `409 ConcurrentModification … requested 2, expected 186 (latest 185)`,
  which is the CAS firing first and is why an earlier probe looked like it "reached the backend".
- **AND THE FIRST REMEDY STILL DID NOT CONFINE — the field resolves against the STORE, not the table,
  measured 2026-09-16 by staging one real manifest in the estate's own S3 and driving every spelling at
  it.** The first fix admitted absolute paths and compared them, and passed anything without a scheme or
  a leading slash through as "relative, therefore confined by construction". On S3 that is exactly the
  spelling that commits:

      s3   '_versions/<n>.manifest-<uuid>'                   -> InvalidInput "Staging manifest not found"
      s3   '<prefix>/t.lance/_versions/<n>.manifest-<uuid>'  -> OK, version 2 committed

  A bare `other-project/other.lance/_versions/x` carries no scheme and no leading slash, so it went
  through unchecked, and the backend resolves it against the BUCKET — another project's manifest moved
  into the caller's table. **This hole is OLDER than either fix, not introduced by one**: the 2026-09-11
  guard refused `startswith("/")`, `"://"`, `"\\"`, a `..` segment and control characters, and that
  path carries none of them. It was never considered because the attack that motivated the guard was
  driven with an ABSOLUTE path on a `dir` namespace, where the bucket-relative spelling does not exist.
  So the deployed estate has been reachable this way the whole time; this is the first fix that closes
  it.
  **DEMONSTRATED AGAINST THE RUNNING CATALOG**, not argued: `POST /v1/table/acme-bronze$events/version/create`
  with `manifest_path` `'some-other-project/other.lance/_versions/probe.manifest-0'` passes the guard
  and reaches the BACKEND, which answers `Staging manifest not found at
  'some-other-project/other.lance/…' for version 186 of table at 's3://acme-bucket/medallion/bronze'`.
  It says "not found" only because that object does not exist — the path was looked for **in the bucket**,
  not in the table, and an object that did exist there would have been moved. **The deployed catalog is S3-backed, so this was the production case, not an
  edge**, and the local `dir` measurement hid it: there the store root is the filesystem, so only a
  fully-qualified path commits and "absolute" happened to coincide with "store-relative".
  Every spelling is now compared against the table's own location as the OBJECT STORE sees it — scheme
  and authority must match when present, and an unqualified path is compared key-only because "no
  scheme" means "this store", never "this table". The spec's own table-relative example is refused
  with the rest, which costs a caller nothing: it commits on NEITHER backend, and pinned by a
  real-backend test so that a backend which starts honouring it fails that test first.
  *Provenance for the shape of the guard:* traversal and control characters are refused on SHAPE with
  the backend untouched; everything else costs exactly one `describe_table` READ, and
  `create_table_version` must not appear in the call log of any refusal.
  **DEPLOYED `main-b42bed5d` AND OBSERVED ON THE RUNNING CATALOG** — the acceptance test is the door,
  not the suite. Driven against `acme-bronze$events` (location `s3://acme-bucket/medallion/bronze`):

      medallion/bronze/_versions/probe.manifest-0                 -> reaches the backend
      s3://acme-bucket/medallion/bronze/_versions/probe.manifest-0 -> reaches the backend
      some-other-project/other.lance/_versions/probe.manifest-0    -> REFUSED by name
      medallion/bronze-evil/_versions/x.manifest                   -> REFUSED (near-miss prefix)
      s3://acme-bucket-evil/medallion/bronze/_versions/x           -> REFUSED (near-miss bucket)
      _versions/probe.manifest-0                                   -> REFUSED (store key outside)

  The third row is the whole point: that exact request passed the guard on `main-5775970a` an hour
  earlier and was resolved against the bucket. The first two rows are the other half — a guard that
  refused everything would look identical on the attack row and is how this defect started.
  **The remedy's own premise was the error**: "confinement by construction, no `describe_table`
  round-trip to get wrong" only holds if relative paths resolve inside the table, and they resolve
  nowhere. Confinement is now BY COMPARISON — shape refusals first (traversal, control characters) at no
  cost, then one `describe_table` for the table's own location, and only when the path is absolute.
  Near-miss containment is tested against `<location>/`, the same shape `vending._location_within`
  documents, so `attacker.lance-evil` does not pass for `attacker.lance`. The relative form is passed
  through to the backend's own answer rather than refused, because the SPEC documents it
  (`namespace.md`, "Table Version Metadata Schema") and the divergence is not this door's to settle.
  **The attack is real and was re-driven** with an absolute path into a sibling: the victim's slot took
  the attacker's staged manifest and the victim's dataset then failed to open at all ("Not found"), its
  manifest naming data files in a directory it does not own. Pinned by 10 legs in
  `services/catalog/tests/test_a_version_entry_cannot_adopt_another_tables_manifest.py`, now split by
  what a refusal COSTS (a traversal is refused with the backend untouched; an absolute path costs one
  location read, and `create_table_version` must not appear).
- **THE REPLAY-MARKER CLAUSE IS CLOSED BY MEASUREMENT, NOT BY BUILDING IT.** The version CAS already
  converges a replay, and the spec says so: `lance_docs/namespace.md:1772` declares this operation's
  whole error set as **1 (NamespaceNotFound), 4 (TableNotFound), 14 (ConcurrentModification)**. Driven
  2026-09-16 — a staged manifest committed at version 2, then the identical request replayed —
  `attempt 1 -> OK, version 2`, `attempt 2 -> ConcurrentModificationError (code 14)`. That meets the
  `catalog.api.idempotency` bar without the seam: the replay is non-destructive (the slot is occupied,
  nothing moves), it maps to 409 rather than a bare 500, and the caller can tell its own commit from a
  competing writer's by reading `DescribeTableVersion` and comparing the `e_tag` of the manifest it
  staged. Contrast `create_table`, which the seam DOES wrap and must: there a replay's `AlreadyExists`
  is indistinguishable from a name collision and the caller cannot learn whether its write landed.
  Wiring the seam here would add a second answer to a question the spec has settled. Rationale recorded
  at the door.
- *What is left of this row, with the dangerous clauses struck:* ~~the replay marker~~ **(closed
  2026-09-16 — the CAS converges it; see above)**, and the
  `/commit` alias-then-remove. **`managed_versioning=true` is NOT to be advertised** — the row's own
  re-measure says it invites clients onto a catalog-mediated commit pointer, the Iceberg shape the
  permanent LANCE-ONLY ruling exists to avoid — and `batch_commit_tables` stays 406 rather than being
  backed by a rask-only staged-manifest KV.

**LH-019 · rask-only governance side effects still run inside spec handlers (warehouse-scoped namespace refusal, trash soft-delete, protection 409, lineage keys in schema metadata, implicit BTREE, insert pre-coercion, maintenance 503 on POST reads)**
`catalog` · **HIGH** · *unblocked 2026-09-16 — R2 acknowledged and the protection code decided; three clauses of four remain*

  **THE PROTECTION CLAUSE IS CLOSED 2026-09-16, and it closed on the spec rather than on taste.**
  `lance_docs/ns_catalog/spec.yaml:2431` defines code 19 as *"InvalidTableState: Table is in an invalid
  state for the operation"* — which is what protected IS — so the 19-vs-3 "undecided tie" below was
  never a tie: one of the two options is the spec's own answer for this exact rung. `require_not_protected`
  now raises `InvalidTableStateError` for `kind="table"` and keeps `NamespaceNotEmptyError` for the
  container kinds, and the scope line is written at the site: `warehouse` and `project` are rask's own
  hierarchy, absent from the spec, so every code is an approximation there.
  **THE HTTP STATUS DID NOT MOVE** — `ns_errors._STATUS` maps both codes to 409, so nothing reading
  status alone observes this; what changed is the `code` a generated client dispatches on. The cascade
  path (`namespaces.py::_require_descendants_unprotected`) was the sharpest case and is covered by the
  same dispatch: there the namespace genuinely IS non-empty, so code 3 was a well-founded instruction to
  empty it — a client would drop every unprotected sibling and still never pass the protected table.
  Pinned by `services/catalog/tests/test_a_protected_table_refuses_as_a_TABLE_not_a_namespace.py` (10
  legs, incl. the scope guard and the unchanged-status leg); four legs of `tests/unit/test_drop_protection.py`
  moved with it.
  **STILL OPEN, and this row does not close on the above:** the management-API carve (R2 is acknowledged,
  so the carve is sanctioned — but moving the 25 route groups is LH-021's work, unstarted), the remaining
  refusals' spec codes, and `branch` on the eight refusing ops.

  **RE-MEASURED 2026-09-11 — SIX of the seven side effects confirmed, and the remedy is aimed past
  its doors.** Confirmed executing inside spec handlers: the warehouse-scoped namespace refusal
  (`fga_deps.py:877` via `namespaces.py:132`), trash soft-delete (`tables.py:508-516`,
  `namespaces.py:481`), protection refusals inside drop/deregister/rename (`tables.py:476,597,899`),
  and the rest. The "Closes when" is partly aimed at doors that already answer and partly at a
  mechanism that does not do what it claims; its first clause is LH-021/R2 — a whole-surface carve
  needing owner acknowledgement — not work this row can do. The protection-code question (3 vs 19)
  remains an undecided owner tie.
- *Why open:* Only the `branch`-honouring clause closed; eight ops still refuse `branch` and the side effects change what a spec client observes — 7 of the 8 conformance blockers. Four refusals were measured as typed spec errors, but the protection refusal mints `NamespaceNotEmptyError` code 3 for a protected TABLE, so a generated client empties-and-retries forever; the recorded reason for not using code 19 was measured false and the 19-vs-3 split is an undecided tie.
- **THE TIE'S FACTUAL HALF IS SETTLED 2026-09-11 — the stated reason for code 3 is false.**
  `require_not_protected` (`fga_deps.py:1006`) raises `NamespaceNotEmptyError` for EVERY `kind`,
  including `table`, and its docstring justifies that as "reused rather than minting a status the client
  SDKs do not map". Checked against the installed `lance_namespace`: `InvalidTableStateError` IS
  exported and carries code 19. The SDK maps it. So what remains is a preference between two mappable
  codes, not a constraint — and one of the two is a NAMESPACE error minted for a TABLE.
- *Why the direction matters more than the number:* a generated client that receives "namespace not
  empty" on a protected table is being told to empty it. For a table, emptying means deleting rows — so
  the error invites a destructive retry against exactly the object protection exists to keep. That is a
  stronger argument than code tidiness and it is what makes this worth an owner minute rather than a
  backlog entry.
- *Closes when:* Move the rask-only side effects behind the management API, re-express each remaining refusal with the spec's own code, ~~decide the protection code (3 vs `InvalidTableStateError` 19) for all four protected object kinds~~ **(done 2026-09-16 — table 19, containers 3, on the spec's own definition)**, and honour `branch` on the eight refusing ops via the plumbing at `dataplane.py:1085`.

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
- **RE-MEASURED 2026-09-15 AND THE BLOCKER MOVED — this is one experiment, not an owner ruling.**
  Driven on the pinned stack (lance-namespace 0.11.x + pylance 11.0.0) through
  `DirectoryNamespace.merge_insert_into_table`: a single-string `on` is ACCEPTED and merges; a list is
  refused by **pydantic** inside the request model (`ValidationError: Input should be a valid string`),
  before pylance or the Rust side ever sees it.
  *The spec is not ambiguous about what should be possible.* `lance_docs/ns_catalog/spec.yaml:3063`
  defines `on` as a composite match key — "Multiple fields form a composite match key", carried as a
  query parameter repeated once per field path. So the estate's own vendored spec already answers the
  "should we support this" question; nothing here needs an owner to decide it.
  *And the pin's own stated mechanism was falsified and is now rewritten.* `pyproject.toml` cited a
  `TypeError` from `lance/namespace.py:580`; on 11.0.0 that method passes `request.model_dump()` straight
  to the Rust binding with no Python-side handling of `on`, and :580 is the `model_dump()` call.
  *What remains unknown is exactly one thing:* whether pylance 11.0.0's Rust side accepts a list once
  0.12's model stops refusing one. 0.12 rejects a bare string, so the bump is all-or-nothing and cannot
  be probed from inside this resolution — it needs a scratch environment, not a decision.
- *Closes when:* Establish how the pylance 11 Rust binding types `merge_insert_into_table(on=...)`, lift the `<0.12` ceiling on the nine pins with whatever pylance version accepts a list, then re-apply the `on: list[str]` door in `data.py`'s merge handler plus the per-column index coverage and re-run the catalog + integration suites.

**LH-023 · ~~A client-supplied `delimiter` is refused 400 rather than honoured on all 153 catalog ops~~ — STRUCK 2026-09-16 (PREMISE FALSIFIED: the refusal is a RECORDED decision, and honouring it is the hazard that decision names)**
`catalog` · med

- *Why open:* Only the silent half closed: a router-level guard now refuses an unsupported delimiter with code 13 instead of reporting a real table as 404. Honouring it means threading the delimiter through both `parse_identifier` AND `fga.canonical_object_id`, and authorizing against a differently-spelled object was judged worse than the bug being fixed — so the design was deferred, not done.
- *What is true now:* the row says "the design was deferred, not done". It was decided.
  `docs/DECISIONS.md:294` row 6 records it as **consciously skipped**, with the reason this row's own
  closes-when would walk into: "honoring it per-request would have to thread through the router-level
  FGA gate too (endpoint-only support would let the gate authorize a differently-parsed object — an
  authz-drift hazard)".
- *And the hazard is now PRICED, not just named* ([[LH-150]], measured 2026-09-16): the delimiter is
  deploy-fixed at `$`, and of 1000 stored OpenFGA objects **715 carry `$` and 0 carry anything else**.
  A request that spelled an identifier differently would canonicalise to an object none of those 715
  grants covers — denying fail-closed, per request, with nothing naming the cause.
- *One scope nuance, stated rather than papered over:* row 6's SUBJECT is list ops, while its REASON is
  general — the FGA gate parses every op's identifier, not only a listing's. The ruling should be
  widened to match its own reasoning rather than re-argued per door.
- *What would reopen it:* an `fga.canonical_object_id` that no longer derives the object id from the
  request's delimiter (making identifier spelling and authorization independent), or a ruling that
  supersedes DECISIONS.md row 6. Either restores the row; neither is true at HEAD.

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
- *THE CLOSURE RE-VERIFIED AGAINST THE BACKEND 2026-09-11, because this row's argument is the one most
  worth being sure of — it closed a security row by reasoning that the guard belongs elsewhere.* Driven
  on a real `dir` namespace: `register_table` refuses an absolute location ("Absolute paths are not
  allowed for register_table") and refuses traversal ("Path traversal is not allowed"). The closure
  stands. Worth recording beside it: the backend does NOT apply the same containment to
  `create_table_version`'s `manifest_path`, which is a separate defect fixed in `803171d8` — so "the
  backend validates paths" is true of this door and false of that one, and the two must not be reasoned
  about together.
- *What remains is a test rather than a guard* (`tests/integration/test_register_refuses_reserved_platform_storage.py`,
  rewritten in place): the platform's own bucket stays registrable, and locations are root-relative by
  construction.

**LH-028 · Three container-tier deletion paths were never driven live: warehouse delete, project delete, cascade DETACH + plural undrop, and bucket-purge sole-ownership**
`catalog` · med

- *Why open:* Table-level drop/protect/force/undrop are proven; the CONTAINER tier is not, and that is exactly where `force` and cascade interact.
- *Closes when:* Drive warehouse delete, project delete, cascade DETACH + the plural undrop and `projects_claiming_bucket` bucket-purge against the deployed release (`scripts/e2e_live.sh`) and pin each.
- **RE-MEASURED 2026-09-16: the premise HOLDS.** `test_warehouses_e2e.py` carries isolation,
  deactivate/activate and two auth legs and **no delete at all**, and no e2e anywhere drives project
  delete, the warehouse cascade or `projects_claiming_bucket`.
- **DRIVEN LIVE 2026-09-16 — 7 legs, all passing against the deployed catalog**
  (`tests/e2e-py/test_the_container_tier_deletes_are_driven.py`): a missing warehouse 404s; an EMPTY
  warehouse this caller administers deletes (the positive control, without which every refusal below
  could be a door that refuses everything); one still holding a namespace refuses 409 **naming it**;
  `?cascade=true` drops exactly those; an unprivileged caller is refused **without learning what the
  warehouse holds**, with and without `force`; `DELETE /v1/projects/{id}?cascade=true` does not delete a
  project that holds warehouses — the transitive path from one request to a bucket purge stays
  unreachable; and a malformed project id is refused before anything else.
- *Every container it destroys is one it created*, in a bucket named for the suite, and `purge_bucket`
  is never sent — a customer's bucket is not recoverable and the rule worth pinning is the refusal.
- **THE DISCLOSURE LEG FAILED FIRST, AND THE DOOR WAS RIGHT.** Driven with the tenant-B token it got a
  409 naming the namespace, which reads exactly like the ordering defect `delete_warehouse`'s docstring
  forbids. The code's order is correct; the TEST's outsider was not: `scripts/e2e_live.sh:90-94` already
  records that bob is a member of `team:eng`, bound to `project:acme`, so *"can_administer(project:acme)
  is False for publisher and True for bob"*. Corrected to `LANCE_E2E_NONADMIN_TOKEN`, which the runner
  fills only with a candidate that really is unprivileged and leaves EMPTY otherwise, so the leg skips
  rather than alleging a property the estate does not have — `topology.py`'s own rule, which I should
  have read before writing the leg.
- **THE BUCKET-PURGE REFUSAL AND THE FORCE/PROTECTION RULE ARE DRIVEN TOO — 9 legs now, all green.**
  `POST /v1/warehouses` takes an explicit `bucket`, so the suite builds the exact shape the guard exists
  for: TWO warehouses of ONE project on ONE bucket — a work warehouse plus a `serving="gold"` one is
  that shape, and `create_warehouse`'s cross-claim guard subtracts the caller's own project on purpose,
  so nothing at create time refuses it. `?purge_bucket=true` on the first then refuses **409**, and the
  warehouse is still there afterwards — the refusal costs nothing, which is the half a status code alone
  would not prove. Both warehouses are the suite's own, in a bucket it named, so no real bytes are
  reachable by it.
  *And `force` is pinned to what it actually overrides:* a warehouse created `protected: true` refuses
  409, the same delete with `?force=true` succeeds, and the gate above ran identically both times.
- *Still open on this row:* the plural undrop (`POST /v1/namespace/{id}/undrop`, `namespaces.py:678`),
  which is the one clause left — the cascade DETACH half is covered by the cascade leg above.

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

**LH-031 · ~~Root `ListNamespaces` asks only the default namespace, so a spec client discovers no warehouse-bound data from the root~~ — CLOSED 2026-09-15: the root listing now merges the bindings registry before the FGA filter**
`catalog` · med


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15 and OBSERVED on the deployed catalog: `namespaces.py:359` merges `top_ns` names from the
  bindings registry (helper at :294), gated on `not segments and settings.warehouses_enabled`, with the 8-test suite
  `test_the_root_namespace_listing_sees_every_warehouse.py`. Driven live against `main-3e858a70`:
  `GET /v1/namespace/$/list` answers 200 with the bound top-level namespaces present.

- *Why open:* Upheld by construction: the root id has no top segment to route by, so `dependencies.get_namespace` returns the DEFAULT namespace and `_drain_namespaces` asks that one backend — a root listing sees only the shared default root's `__manifest`. The estate proves the consequence: `maintenance/reconcile._top_level_namespaces_across` exists because each tenant's namespaces live in that tenant's own bucket.
- **LANDED 2026-09-13, and simpler than this row assumed.** The root listing now merges the bindings
  registry's `top_ns` names before the per-item `can_get_metadata` filter. No per-root HANDLE is needed:
  the root's children ARE top-level namespaces and a binding names exactly one, so the registry already
  holds the missing names — draining each warehouse would only add namespaces that have no binding, and
  `get_namespace` routes an id BY its binding, so those are reachable by no id route and naming them
  would advertise a listing the estate cannot serve.
- *Measured on the live graph 2026-09-13:* dataset locations span **123 distinct warehouse buckets**
  against 12 other buckets, so the root listing was answering from a small fraction of the estate.
- *The merge is sorted and deduped* (the route's `page_token` is keyset over that list), a binding with
  no `top_ns` is skipped, and an unreadable registry degrades to the default root's answer with a
  warning — the same per-seed tolerance `GET /v1/table` applies. RED-first, 8 tests in
  `tests/unit/test_the_root_namespace_listing_sees_every_warehouse.py`, three of them negative: a child
  listing is not seeded and pays no registry read, a warehouses-off deployment is untouched, and a
  failed registry read still answers.
- **A DEFECT THIS INTRODUCED AND THE ESTATE'S OWN GUARD CAUGHT** (`4a44fa36`): the helpers first landed
  between `@router.get("/{id}/list")` and `list_namespaces`, so the route was registered to the merge
  helper and the endpoint was gone. The LH-031 suite could not see it — those tests call the function
  directly — and `test_di_aliases_are_only_on_routes` did, by flagging `list_namespaces` as a non-route
  helper still carrying `NamespaceDep`/`SettingsDep`. **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live.

**LH-032 · ~~`handle_validation_error` hardcodes 422 while emitting `ErrorCode.INVALID_INPUT`, which maps to 400 — the vendored spec contains zero 422s~~ — CLOSED 2026-09-15: INVALID_INPUT answers 400 on a spec route**
`catalog, service-kit` · med


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15 and OBSERVED live: `ns_errors.py:225` resolves `status = 400 if is_spec_route(...) else 422`
  off the committed `SPEC_ROUTES` set (`spec_routes.py:26,98`) rather than hardcoding 422. Driven against the deployed
  catalog: an over-limit `POST /v1/table/{id}/index/list` answers **400**.

- *Why open:* Measured 2026-09-09 and recorded so the next reader does not start the cheap fix, which does not exist: `install_problem_handlers` is installed on every app that can import `lance_namespace` (app.py:157-165) and 153 suite assertions expect 422; narrowing by path fails because `router.py:55-75` mounts spec and rask-only routes equally under `/v1`.
- **LANDED 2026-09-14 (`c2b2e454`), and the row's own cost estimate was the thing that made it look
  impractical.** "153 suite assertions expect 422" is wrong: measured, 56 lines in the entire estate use
  422 at all, and applying the narrowing broke FIVE — one test whose NAME encoded the defect
  (`test_request_validation_maps_to_422_problem_json`) and four parametrizations of one idempotency-key
  case. Both sit on spec operations (`UpdateTable`, `CreateTable`) where 400 is the correct answer, and
  the second was already self-inconsistent: the same door's CROSSED-key case asserts 400 three classes
  above it, so one door gave two statuses for two flavours of the same invalid input.
- *The defect is sharper than "hardcodes 422" too:* `ns_errors` maps `INVALID_INPUT` to 400 in BOTH
  directions (`_STATUS`, `_STATUS_CODE_FALLBACK`) and then served that code at 422 — one module, one
  code, two statuses, and a generated Lance client dispatches on the CODE.
- **THE PRESCRIBED FIX WAS NOT IMPLEMENTABLE AS WRITTEN.** "Derive the marker FROM the vendored spec"
  cannot happen at runtime: NO image copies `lance_docs/` — checked across every `.docker/*.dockerfile`
  and confirmed in the running catalog pod, which holds no `spec.yaml` at all. So `SPEC_ROUTES` is
  committed DATA and `test_spec_conformance` re-derives it from the spec on every run, which buys the
  same non-drift the row wanted; re-vendoring reds the suite unless the set moves with it. Path-parameter
  names are collapsed, because the spec writes `{id}` where a route may write `{table_id}` and they are
  one route.
- *And the declaration had to move with the wire,* which the row does not mention. FastAPI declares 422
  on every validating operation and `docs/catalog-openapi.json` carries that to the frontend — measured,
  159 operations declaring 422 and NONE declaring 400. Serving 400 without moving it would relocate the
  disagreement rather than close it. After regeneration: spec routes `422=0/400=54`, rask-only
  `422=105/400=0`. Mutation-proven both ways. **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live.
- *Unrelated drift absorbed in the same commit, stated so it is not read as caused by it:* the committed
  contracts had not been regenerated since 2026-09-10, so `ReconcileState`'s `ungoverned` and a schema's
  `versions_without_lineage` came in too — `make openapi-check` was ALREADY red, verified by
  regenerating with this change removed.
- *Closes when:* the roll observes it.

**LH-033 · ~~Nine catalog operations answer 501: branch-scoped `query`/`explain_plan`/`analyze_plan`, `create_index`/`create_scalar_index`, `stats`, `index/list`, `index/{n}/stats`~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* No catalog operation answers 501 for this: `ErrorCode.UNSUPPORTED` maps to **406** by explicit owner decision (Q3, 2026-09-02), and the ops are SERVED on main — only the branch-scoped form is refused, at ten doors now, not nine.
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/ns_errors.py:39 `ErrorCode.UNSUPPORTED: 406` (with the reasoning at :30-38), pinned by tests/unit/test_ns_errors_contract.py:49-50 (`status == 406`). The refusal is branch-only: services/catalog/src/catalog/services/dataplane.py:1264-1270 `if branch is not None: raise UnsupportedOperationError(…)` — every door calls it AFTER reconciling the id and BEFORE delegating main's request to the backend. ELEVEN call sites as of 2026-09-15 (ten when this was written; `drop_table_index` was the missing one and is now refused too — see [[LH-105]], where it was destroying MAIN's index for a branch-targeted request): data.py:212 `plan_table_compaction`, :245 `commit_table_compaction`, :601 `query_table`, :684 `explain_table_query_plan`, :702 `analyze_table_query_plan`; indices.py:84 `create_table_index`, :117 `create_table_scalar_index`, :159 `list_table_indices`, :176 `describe_table_index_stats`; tables.py:1077 `get_table_stats`. Each main-branch path is real work, e.g. tables.py:1078 `return native.call(ns, "get_table_stats", req)` and indices.py:86-97 (queue-or-build plus a measured lineage emit).
- *What would reopen it:* A response from any of those doors carrying HTTP 501, or a main-branch call to `stats`/`index/list`/`create_index`/`query` that is refused rather than served.

**LH-034 · Compression is never configured anywhere and there is no decision record**
`catalog, medallion` · med

- *Why open:* Listed Medium in §O1 with no note and no work. The setting is schema-resident, so
  retrofitting it later costs a rewrite and gets dearer with corpus size.
- **WHAT THE DEFAULT ACTUALLY IS, from the vendored spec.** `lance_docs/file_format.md:679` gives
  `lance-encoding:compression` a default of **`none`** — "Opt-in to general compression" — and :699
  repeats it ("No general compression applied (default)"). So the estate stores general data
  uncompressed today. It is NOT unencoded: the same table shows `lance-encoding:bss` defaulting to
  `auto`, `rle-threshold` to `0.5` and `dict-values-compression` to `lz4` (:682, :681, :685), and :743
  records that BSS only engages when `compression` is set to something other than `none` — so choosing a
  scheme turns on more than the scheme.
- **AND THE URGENCY PREMISE IS FALSE TODAY, measured 2026-09-16.** The row's cost argument is that the
  retrofit "gets dearer with corpus size". The whole object store is **9.07 GB across 28,983 objects in
  111 buckets**, of which **9.02 GB is `rask-observability`** — telemetry under a 14-day TTL, not
  governed data. The lakehouse corpus is therefore about **50 MB**, and no other bucket exceeds 50 MB.
  A rewrite of that is minutes, so this is a decision to take deliberately rather than urgently, and the
  row should stop implying an accumulating debt that has not started accumulating.
- *(Worth its own attention elsewhere: telemetry outweighs the governed corpus ~180:1. That is the TTL
  doing its job on a small estate, not a lakehouse problem — but it is why "the estate's storage" and
  "the lakehouse's storage" must never be quoted as one number.)*
- *Closes when:* a scheme is chosen on the create path — noting that a non-`none` value also switches on
  BSS — and recorded in `docs/DECISIONS.md` with the measured corpus size, so the next reader knows
  whether the cost argument applied when it was taken.

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
- **MEASURED 2026-09-13, driven rather than classed.** Against a real `dir` namespace, a second
  `create_namespace` on the same id answers `NamespaceAlreadyExistsError` for EVERY mode — default,
  `EXIST_OK`, `exist_ok`, `OVERWRITE`, and a nonsense value alike. The catalog forwards `mode` faithfully
  (`namespaces.py:149` passes `req` straight to `native.call`); the backend ignores it. Same shape as
  [[LH-038]]: the door repeats a claim the layer below does not honour.
- *The contract to implement against is explicit, and it is the generated model's own description
  rather than the vendored prose* (`lance_docs/namespace.md` tabulates `mode` for CreateTable and
  InsertIntoTable, not for CreateNamespace): **CreateNamespace** — `Create` fails 409, `ExistOk`
  succeeds and KEEPS the existing namespace, `Overwrite` DROPS it and creates a new empty one.
  **RegisterTable** — `Create` (default) fails 409, `Overwrite` replaces the registration. Case
  insensitive, both PascalCase and snake_case.
- **AND THE PATTERN IS ALREADY IN THE HOUSE:** `POST /v1/table/{id}/create` implements all three via
  `catalog.core.modes.CreateMode` + `services/table_create.py` (`:139` parses, `:198-200` computes
  `pre_existed` / `overwrote_existing` / `existok_kept_existing`, `:271` returns the kept table). So
  this is mirroring a working door, not designing one.
- *WHY IT IS NOT A SMALL CHANGE, which is the part this row did not say.* `ExistOk` on a governed door
  has an AUTHORIZATION consequence: the namespace already exists and already has an owner, so the
  handler must NOT run `seed_ownership_or_compensate` on that path — otherwise any caller passing
  `mode=exist_ok` acquires ownership of someone else's namespace. That is precisely why
  `table_create.py` carries the `pre_existed` flag. `Overwrite` is worse: dropping a namespace here
  means the #96 cascade trashing a whole SUBTREE, interacting with `require_no_live_trash` and the
  existing tuples, so it is destructive and needs an owner ruling rather than an implementation.
- **THE NAMESPACE DOOR LANDED 2026-09-13.** `create_namespace` honours `ExistOk` — the existing
  namespace is kept and DESCRIBED rather than echoed — and it does not seed ownership on that path, so
  a caller passing `exist_ok` against somebody else's namespace gains no tuple. A namespace the call
  really created still gets its owner, which is why the condition is a `kept_existing` flag rather than
  the mode itself, mirroring `table_create.py`'s `existok_kept_existing`.
  *Race-free by CATCHING the backend's refusal rather than pre-checking existence*, and on this door
  that is not a detail: an `exists?` read followed by a create leaves a window in which another caller
  creates the namespace, and the loser of that race would then seed ownership over their object. The
  backend's own refusal is the only answer that cannot be stale.
  `Overwrite` is refused at the SHAPE rung naming itself, before anything is written. An unrecognised
  mode still folds to `Create` — `modes.py` records that tolerance as deliberate for typos and it
  stays. RED-first, 9 tests in
  `tests/unit/test_a_namespace_create_mode_means_what_the_spec_says.py`, three of them pinning
  unchanged behaviour. **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live.
- **AND THE IGNORED `mode` HAD A SECOND VICTIM, found while fixing this one.** `undrop_namespace`
  promises in its own docstring that "a rerun after a mid-recovery failure finishes the job instead of
  409-ing on what the first attempt already rebuilt". Half of that was earned: the TABLE loop catches
  `TableAlreadyExistsError` and logs `undrop_table_already_registered`. The NAMESPACE loop passed
  `mode="exist_ok"` to the backend that ignores it and caught nothing — so a rerun raised on the first
  namespace the previous attempt had rebuilt. **And the broken half gated the working one:** namespaces
  are rebuilt shallowest-FIRST, so a rerun aborted before it ever reached the tolerant table loop,
  leaving the rest of the subtree in the trash. Both callers now go through one seam,
  `create_or_keep_namespace`, since they want `ExistOk` for different reasons — the door because the
  spec says so, `undrop` because its resumability rests on it.
- *LATENT, not live:* the catalog logged no `NamespaceAlreadyExists` and no undrop activity in the six
  hours before the fix, so nothing was failing this way at the time.
- **THERE IS A THIRD DOOR, and the row's pair was not the population.** Asked the generated models
  directly rather than trusting the list: SIX spec request models carry `mode`, in **four different
  vocabularies** — so reading them all with `CreateMode` would silently fold the odd ones to `Create`.

      CreateNamespaceRequest   Create/ExistOk/Overwrite   FIXED (1d2c93e3)
      RegisterTableRequest     Create/Overwrite           FIXED
      CreateTableRequest       Create/ExistOk/Overwrite   already implemented (table_create.py)
      InsertIntoTableRequest   Append/Overwrite           HONOURED (dataplane.insert_into_table)
      CreateEmptyTableRequest  Create/ExistOk/Overwrite   no catalog door uses this model
      DropNamespaceRequest     Fail/Skip                  ACCEPTED AND IGNORED

  `drop_namespace` reads `behavior` (Cascade/Restrict, via `DropBehavior.parse`) and never reads
  `.mode`. Its contract, from the model itself: *"Fail (default): the server must return 400 indicating
  the namespace to drop does not exist. Skip: the server must return 204 indicating the drop operation
  has succeeded."*
- *And this is the one with a consequence beyond a status code.* `Skip` is how a client makes a drop
  IDEMPOTENT — retry a drop and a namespace already gone counts as success. Ignoring it means the second
  attempt errors, which is exactly what breaks a caller recovering from a partial failure. Note the
  shape: the same defect that made [[LH-141]]'s `undrop` non-resumable is here on the drop side.
- *It cannot reuse `CreateMode`* — parsing Fail/Skip with it folds both to `Create`. It needs its own
  closed vocabulary beside `DropBehavior` in `modes.py`, defaulting to `FAIL` (the spec's default, and
  the direction that errors rather than silently claiming success).
- **ALL THREE DOORS LANDED 2026-09-13** (`1d2c93e3`, `1e5f1c25`, `8de2ec28`). `drop_namespace` now
  honours `Fail`/`Skip` through its own `DropMode` vocabulary, covering BOTH existence sites — the
  cascade enumerates the subtree before the drop, so a `Skip` guarding only the drop would still raise
  for the caller most likely to be retrying — and narrow on purpose: a descendant vanishing mid-destroy
  is a real error, not a skip. A skipped drop returns before the trailer and announces nothing.
  `register_table` no longer answers a 409 that means something else: `Overwrite` is REFUSED at the
  shape rung with a 400 naming why — replacing a registration would detach bytes this catalog does not
  own from the table that currently points at them — placed before `idem.begin`, so a request never
  attempted mints no idempotency record.
  **DEPLOYED AND OBSERVED 2026-09-15** on `main-4c762db2` — all three commits (`1d2c93e3`, `1e5f1c25`,
  `8de2ec28`) are ancestors of the rolled tag, confirmed by `git merge-base --is-ancestor`.
  `register_table` was driven against the live catalog with a real Dex bearer and answers **400** with
  the reason intact: *"mode 'Overwrite' is not supported on this door: replacing a registration would
  detach bytes this catalog does not own from the table that currently points at them."*
- **BUT `Skip` IS UNREACHABLE FOR THE CALLER IT WAS BUILT FOR, measured rather than reasoned.** Driven
  end to end — create a nested namespace (200), drop it (200), then retry the drop — both `mode=Skip`
  and the default answer **403 `can_delete required`**, not the Skip no-op. The gate refuses before the
  mode is ever consulted.
  *Why:* the drop's `revoke_ownership` removes EVERY tuple on the id, the `parent` edge included, which
  severs the cascade from its parent namespace. Read from the live store immediately after:
  `namespace:lakehouse$lh037probe` -> **NONE**, against a live sibling `namespace:lakehouse$silver` ->
  `owner` + `parent`. The revoke is deliberate and correct on its own terms — it is the reused-id
  privilege-bleed guard — but it means the owner who just dropped an object cannot retry that drop,
  which is precisely the "caller recovering from a partial failure" this row added `Skip` for.
- **AND IT IS UNREACHABLE FOR EVERYONE, not just for the owner — asked of the model directly rather
  than inferred.** `ListUsers` for `can_delete` on the dropped id returns **0 principals**; the same
  query on a live sibling returns 2. No project admin, no estate admin, nobody.
- *The argument closes because `Skip` only ever applies to an ABSENT target, and every absent target is
  unreachable for the same reason:*
  * absent because it was DROPPED — `revoke_ownership` cleared its tuples, parent edge included;
  * absent because it NEVER EXISTED — it has no tuples either (driven: `lh037absent` -> 403).
  Every `namespace` relation resolves through a direct tuple or the parent chain, and an absent id has
  neither, while the authorization gate runs BEFORE existence resolution. So the door can never reach
  the branch: `Skip` is dead on this door as built.
- *What that does NOT mean:* the `DropMode` vocabulary and the `drop_namespace` work are not wasted —
  `Fail` is the reachable default and the subtree enumeration fix stands. What is unreachable is the
  one mode added to make a retry idempotent, and it is unreachable for a reason that lives in the GATE
  rather than in the drop. Whoever fixes it must answer the ordering question — an existence-aware
  refusal on a door that deliberately refuses before resolving existence, which the catalog skill
  records as a class rule (no existence oracle on destructive doors) rather than a per-door choice.
- **THE ESTATE HAS MET THIS CLASS BEFORE AND ITS ANSWER DOES NOT TRANSFER, which is the useful part.**
  `tables.py`'s drop carries the scar in its own comment: *"revoking here made undrop unreachable for
  exactly that caller (found by driving the deployed catalog, not by a unit test — the unit tests run
  FGA off)"*. The fix there was to NOT revoke on a RECOVERABLE drop, so the owner keeps the grant they
  need to undrop, and the grants die with the bytes at purge.
- *That answer cannot be reused here, and a measurement says why rather than an argument.* This estate
  runs `trash_grace_days=7`, so drops ARE recoverable in general — 992 trash records exist. The probe's
  drop wrote **none**: checked `_trash/` directly, there is no record for it, and all 992 are `table-*`.
  A RESTRICT drop of an EMPTY namespace is deliberately unrecorded — it only ever removes an empty
  manifest row — so it is non-recoverable, so it revokes, so `Skip` is unreachable on exactly the path
  it exists for.
- *So the remaining question is sharper than "make Skip work":* the recoverable path already keeps its
  grants and needs nothing; the RESTRICT path is deliberately unrecorded and correctly revokes; and
  `Skip` lives only on the second. Either the gate learns to admit an idempotent no-op against an id
  with no tuples — which is the existence-oracle class rule, an owner call — or `Skip` is withdrawn from
  this door as unimplementable and the row says so. Both are decisions; neither is a patch.
- *Closes when:* the roll observes all three, and `Overwrite` on the namespace door is either
  implemented against an owner ruling on the cascade/trash interaction or stays refused.
  Note `modes.py` records a deliberate decision that an UNRECOGNISED mode falls through to `Create`
  rather than raising; that tolerance is for typos and should stay, so the refusal is for the named
  modes this door cannot honour, not for anything it does not recognise.

**LH-038 · ~~`POST /v1/table/{id}/version/list` accepts `page_token` and ignores it~~ — CLOSED 2026-09-15: version listing pages in-layer**
`catalog` · med


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15: `api/pagination.py:28` defines `paginate_versions` and `versions.py:241` wires it into the
  version listing, which pages and rejects a malformed token. Shipped in `main-2a5b8c64`.

- **CODE LANDED `283cada1`** — `paginate_versions` (`services/catalog/src/catalog/api/pagination.py`) pages
  in-layer after an unpaginated backend read, and a malformed token raises `InvalidInputError`. Verified on
  origin. **Not yet observed: rides the pending roll**, so this row stays open until a deployed pod answers.

- **FIXED 2026-09-13, RED-first — and the row UNDERSTATES it.** Driven against a real `dir` namespace
  over a seven-version table rather than read: `limit=3` serves `[1, 2, 3]` and answers
  `page_token: None`, and a token handed back in changes nothing. `None` is what a client STOPS on, so
  the caller is not looping — it is told the listing is complete having seen three of seven. Silent
  truncation, not a loop.
- *The catalog's surface was already spec-correct and the lie was one layer down.*
  `lance_docs/namespace.md` § ListTableVersions puts `page_token` and `limit` on the query string, and
  `ListTableVersionsResponse` carries `page_token`; the handler forwarded both faithfully to a backend
  that honours neither. Forwarding made the door repeat the backend's claim.
- *So it pages HERE, which the backend makes possible:* measured, an unbounded call returns the whole
  list (all seven), so the native call is now made UNPAGINATED — the rule `catalog.api.pagination`
  already states for the name listings ("no upstream cursor can ride through by accident") — and the
  cursor is this layer's. `paginate_versions` is a sibling of the existing keyset rather than a
  generalisation, because a version keys on an integer and its comparison must FLIP with `descending`,
  which a name cursor never has to think about. A `page_token` that is not a version number is refused
  `InvalidInputError` rather than ignored.
- *Seven tests, including the one this row asked for:* paging twice over a real table gets `[1,2,3]`,
  `[4,5,6]`, `[7]` with the last page reporting `page_token: None`; plus descending cursors, the
  unbounded case growing no cursor it does not need, and an assertion that the backend is asked with
  `limit=None` and `page_token=None` — because forwarding either would reintroduce the truncation one
  layer down where the helper can never see it.
- *The siblings are NOT the same shape, driven rather than assumed.* `tags/list` on a five-tag table
  answers all five for `limit=2` and all five again for a `page_token` — it ignores BOTH and returns the
  complete set. So it lies about BOUNDING where `version/list` lied about COMPLETENESS, and only the
  second can hide rows from a caller. Worth fixing for the contract (a `limit` that does not bound is a
  declared ceiling that isn't one, and `_MAX_LIST_LIMIT` exists to make that ceiling real), but it is a
  resource concern rather than a correctness one and belongs in its own row.
  *And `indices/list` PAGES CORRECTLY* — `limit=1` over a two-index table answers one index and a real
  continuation token (`'b_idx'`). So three sibling routes on one backend behave three different ways:
  versions truncated silently, tags ignores `limit` and returns everything, indices is spec-correct.
  There is no "the backend cannot page" generalisation to make, and the fix above must NOT be
  blanket-applied — wrapping `indices/list` in a second cursor would break a door that already works.
  `branches` is still undriven. This row covers `version/list`.

**LH-039 · ~~`POST /produce` accepts a governed-tier claim in `settings` and disregards it~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* `POST /produce` takes no request body at all — there is no `settings` object on that door and no governed-tier claim to disregard; its entire request surface is one required header and two query params, and the tier it seeds is fixed (`bronze$events`).
- *Evidence:* Rendered from the live app factory (`medallion.producer.app.openapi()`): `/produce POST → params: [('project','query'), ('rows','query'), ('Idempotency-Key','header'), ('dapr-api-token','header'), ('authorization','header'), ('dapr-caller-app-id','header')], requestBody: None` (same for `/ingest-media`). The handler signature confirms it: services/medallion/src/medallion/api/produce.py:43-60 — `dapr: DaprClientDep, settings: SettingsDep, originator: Depends(authorize_produce), idempotency_key: Header(alias="Idempotency-Key"), project: ProjectParam = None, rows: Query(ge=1, le=1_000_000) = None`; `settings` there is the injected `MedallionSettings`, not a wire field, and it is forwarded as such at :93 `run_produce(dapr, settings, token=idempotency_key, project=…, originator=…, rows=rows)`. The seeded tier is not caller-supplied: services/medallion/src/medallion/services/produce.py:53-59 registers and seeds `bronze$events` unconditionally. A grep for a wire-level `"settings"` key across services/ and packages/ finds no request model with such a field, and no `tier` parameter on any medallion door.
- *What would reopen it:* An OpenAPI render of `/produce` showing a requestBody, or any medallion request model with a `settings` field carrying a tier name.

**LH-040 · ~~Put-if-not-exists is verified only on RustFS, so Lance's CAS commit model is untested on any other store~~ — CLOSED 2026-09-15: the validate probe proves CAS both ways**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* No CAS probe exists anywhere in the registration or validation path — but a warehouse never names a store, so the true fix is one conditional-put step added to the existing /validate probe, not a per-store refusal at registration.

- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15: `warehouses.py:1077-1084` runs two conditional puts — `CAS_RESERVE` must be accepted and
  `CAS_CHECK` must be refused — and `ProbeReport.commit_safe` is three-valued, `None` when skipped or header-rejected
  (`vend_probe.py:37,42,69,83`). Shipped in `main-2a5b8c64`.

  **Evidence:** services/catalog/src/catalog/api/v1/endpoints/warehouses.py:938-978 (POST /{warehouse_id}/validate: the whole probe is scope-only); warehouses.py:1035-1039 the probe's four IO steps are write_inside / read_inside / SCOPE_CHECK / cleanup — no conditional put; services/catalog/src/catalog/services/vend_probe.py:30 SCOPE_CHECK = 'write_outside_refused' is the only security claim the report makes; warehouses.py:122-200 create_warehouse has no CAS check at all. WHY THE 'PER-STORE' FRAMING OVERSHOOTS: create_warehouse provisions a bucket on the catalog's OWN configured endpoint (warehouses.py:181-183 `root_uri = f"s3://{bucket}"` + `provision_bucket(bucket, settings.storage_options())`) — a warehouse names a bucket, never a store; services/catalog/src/catalog/core/config.py:74-76 states the operator invariant that every multibase data base MUST share the catalog's S3 endpoint + creds; services/catalog/src/catalog/api/v1/endpoints/stores.py:130-133,152 force every ATTACHED store read_only, so no second writable store can be registered. THE PROBE ALREADY EXISTS, unwired: scripts/verify_lance_storage.py:255-286 check_conditional_put does exactly this against the configured endpoint, as a manual script. The only contended proof is opt-in and RustFS-only: tests/e2e-py/test_object_store_cas_e2e.py:38-46,74-77 (skips unless LANCE_E2E_S3_ENDPOINT is set).
  **Reopen if:** A conditional-put ProbeCheck appearing in warehouses.py::_run_scope_probe / vend_probe.py, OR a warehouse record that carries its own endpoint distinct from settings.storage_options() (which would make 'per-store' the right shape after all).
`catalog, storage` · med

- *Why open:* The Lance commit model assumes conditional put; on any store rask might run on other than RustFS (COS/GooseFS need commit locks per the guide) that assumption is untested and nothing refuses such a store at registration.
- **LANDED 2026-09-14, in the shape the 09-10 re-measure prescribed rather than the row's original one.**
  `POST /{warehouse_id}/validate` now runs two conditional-put steps inside the vended prefix — the
  first must be ACCEPTED (`conditional_put`), the second REFUSED (`conditional_put_refused`) — and
  `ProbeReport.commit_safe` carries the verdict.
  *Three behaviours, not two, and the difference is the design:* a store may honour the header, IGNORE
  it (accepting the second put — the silent case, which a probe that only asked "did the write work"
  would score as a PASS), or REJECT the header outright (loud, and proving nothing about a second
  writer). `commit_safe` is three-valued for the reason `enforced` already is: an unexercised control is
  UNKNOWN, never a guarantee. Cited: `lance_docs/file_format.md` § "Commit Protocol" -> "Storage
  Primitives" — the primitives are what "guarantee that exactly one writer succeeds when multiple
  writers attempt to create the same manifest file concurrently".
- **AND IT FOUND A PRE-EXISTING LEAK IN THE PROBE ITSELF.** The out-of-scope object
  (`_validate_scope_probe_should_fail`, at the warehouse ROOT) was written by the scope step and never
  removed on a store that ACCEPTED it — so exactly the over-permissive store this probe exists to find
  accumulated one object per validate call. Cleaned now, and only when it landed: a correctly scoped
  credential is refused that delete too, and reporting the refusal as a cleanup failure would be a false
  alarm about the store that had just passed. RED-first, 5 tests, mutation-proven three ways.
  **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live.
- *Closes when:* the roll observes it. The `scripts/verify_lance_storage.py::check_conditional_put`
  script stays as the manual whole-store sweep; what changed is that a warehouse door now asks the
  question for the bucket it is about to govern.

**LH-041 · Branch/tag writes are unconditional at every layer including pylance's `Tags::update` — a lost update in waiting**
`catalog` · med

- **THE VERIFICATION HALF IS DONE 2026-09-13, AND IT FOUND A WIDER GAP THAN THE TAG.** The CAS e2e
  proved only `If-None-Match: *` (put-if-not-exists) across its three tiers. Nothing drove `If-Match`,
  while `records._replace_json` already writes EVERY control-root record with
  `put_object(..., IfMatch=etag)` and `records.mutate_json` is the seam every registry read-modify-write
  goes through — so a store that ignored the header would last-writer-win on the tenant-isolation
  guards, underneath the module written to stop exactly that. The unit suites cannot reach it: they
  exercise the LOCAL branch, where `flock` plus a sha256 compare arbitrate in-process.
  Tier 2b now drives both halves and **passes live**: a stale-etag replace is refused AND not applied,
  and under 8-way contention on one etag exactly one writer wins with the survivor's bytes intact.
  *Stated precisely:* this cluster's store is **MinIO** (`svc/rask-minio`; `rustfs.enabled` is off), so
  what is measured is that MinIO honours `If-Match`. The RustFS leg of the same claim is still unproven.
- **THE TAG HALF IS NOT A CODE GAP, AND THIS ROW'S PRESCRIPTION WOULD MAKE IT ONE.** `_set_tag`'s create
  race is already arbitrated — `tags.create` refuses an existing tag and the handler converges by moving
  it, the same shape `records.create_json` uses. What has no conditional form is the UPDATE, and that is
  the library's: verified on **pylance 11.0.0**, `Tags.update(tag, reference)` takes no expected-version.
  The row asks for "a conditional put on `_refs/tags/<name>.json`" — that means the catalog writing the
  format's internals by hand (`createdAt`/`updatedAt`/`manifestSize`; `lance_docs/file_format.md` §Tags,
  2796-2820 defines the file's shape and gives tag updates no concurrency contract — only manifest
  commits get put-if-not-exists, 5394). Writing another library's on-disk format from outside it is not
  a fix this estate should ship.
- *Closes when:* the owner rules between (a) accepting a last-writer-wins tag MOVE and recording it in
  `docs/DECISIONS.md` — the create race, which is the one that loses data rather than ordering, is
  already closed — and (b) raising a conditional `Tags::update` upstream in Lance, which is where the
  primitive belongs. Re-verify `If-Match` against RustFS if a deployment ever enables it.

**LH-042 · ~~Blob v2 default thresholds disagree three ways (64 KB/4 MB vs 16 KiB/2 MiB vs rask's measured 64 KiB/4 MiB)~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* The thresholds are pinned at the measured values and a test re-measures them against Lance itself on every run, so a pylance retune reds the suite rather than silently moving payloads — one residual: the medallion's blob columns pin nothing.
- *Evidence:* services/ingest/src/ingest/runtime.py:29-36 pins BLOB_INLINE_SIZE_THRESHOLD = 64*1024 and BLOB_DEDICATED_SIZE_THRESHOLD = 4*1024*1024 with the rationale that the guide stores them in the dataset SCHEMA and rejects a differing append; runtime.py:170-174 applies them to the bronze payload's blob_field. The re-measurement is automated and written against the FORMAT, not the constant: services/ingest/tests/test_blob_placement_thresholds.py:16-18 ('deliberately written against LANCE, not against our constant'), :38-46 _placement writes one row per band and reads back the descriptor kind, :50-60 parametrized across all four boundaries. The three disagreeing sources are confirmed present but resolved in rask's favour: lance_docs/guide.md:574 says 16 KiB / 2 MiB, lancemultibasebranchingblobv2.md:669-671 says 64 KB / 4 MB (which AGREES with the measurement), runtime.py:63-67 records the measurement that settles it. RESIDUAL, stated rather than cut: services/medallion/src/medallion/services/ingest.py:57 and compute.py:523,586 call blob_field(name) with no thresholds, so the medallion plane inherits whatever pylance defaults to.
- *What would reopen it:* A pylance bump landing with test_blob_placement_thresholds.py still green while the real placement boundaries have moved (i.e. the test measures the constant rather than the format) — or evidence that the medallion's unpinned blob_field writes threshold metadata that can conflict with ingest's pinned values.

**LH-043 · Unknown whether MemWAL server-id sharding fits append-only bronze landing (coordinator-free ingest)**
`ingest, medallion` · med · **blocked:** §K

- *Why open:* Blob v2 columns read `None` through the MemWAL scanner today, so the shape cannot be evaluated without a prototype.
- *Closes when:* Prototype MemWAL server-id sharding against bronze landing after §K.

**LH-044 · ~~`tags/create` drops `branch`, `branches/create` drops `from_branch`+`from_version`, `branches/delete` drops `name`~~ — STRUCK 2026-09-13 (PREMISE FALSIFIED, all three clauses)**
`catalog` · was low

- *What is true now:* every one of the three parameters is honoured, and each has a named mapping
  helper whose docstring states the semantics — this was never the "classed cosmetic in the sweep and
  untouched" it records.
- *Evidence, clause by clause:*
  **`tags/create` → `branch`.** `services/catalog/src/catalog/api/v1/endpoints/tags.py` forwards the
  whole body to `dataplane.create_tag`, which calls `_tag_reference(req.branch, req.version)`
  (`services/catalog/src/catalog/services/dataplane.py:1782`). `_tag_reference` (:1773-1776) maps it and
  says WHY it must: "a bare int resolves against the CURRENT branch (main), so a branch-scoped tag must
  pass the `(branch, version)` tuple". `CreateTableTagRequest` does carry `branch` (model fields:
  identity, context, id, tag, version, branch), so there was a parameter to drop and it is not dropped.
  **`branches/create` → `from_branch` + `from_version`.** `_branch_reference` (:1814-1824) maps all four
  combinations explicitly — "fromBranch + fromVersion → `(branch, version)`; fromBranch only →
  `(branch, None)`; fromVersion only → the `version` int (on main); neither → `None`" — and
  `create_branch` (:1867) additionally VALIDATES `from_branch` against the branch list, answering
  `TableBranchNotFoundError` rather than branching from the wrong point.
  **`branches/delete` → `name`.** `delete_branch` (:1887) calls `branches.delete(req.name)`.
- *What would reopen it:* a `create_tag` that passes a bare `req.version` to `tags.create`, a
  `_branch_reference` that stops reading `from_version`, or a `delete_branch` keyed on anything but
  `req.name`.

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
- **LANDED 2026-09-14, and the row understated it: one of those 79 lines was a BREAKING contract
  change.** Upstream `eb1de88e` (2026-09-01) is `feat(spec)!: allow multiple columns for the merge
  insert on key (#363)` — `merge_insert`'s `on` went from a single `string` to a repeatable array. Same
  operation id, still 54 operations, still `info.version: 1.0.0` on both sides, so
  `test_the_vendored_spec_still_matches_UPSTREAM` answered "zero difference either way" on 2026-09-09
  across a change already in upstream. The row's "the error contract is byte-identical" was true and
  was not the whole contract.
- *Re-measured 2026-09-14 rather than inherited:* vendored 6663 lines, upstream 6742, 101 differing —
  the row's numbers still hold, and the op-id sets still match exactly.
- *What landed:* `spec.yaml` re-vendored from the named commit; `lance_docs/PROVENANCE.md` recording all
  six documents' sources and separating the ONE that is machine-checked from the five tool-generated
  bundles that are not (a citation from those is weaker, and the file says so rather than implying
  parity); a shapes-level drift gate; and a gate that fetches the spec AT the pinned sha and compares
  bytes, so re-vendoring without moving the pin and moving the pin without re-vendoring both red.
  *The shapes gate reads PATH-LEVEL parameters, which is the load-bearing detail* — `on` sits on the
  path item rather than the operation, so an extractor reading only `operation.parameters` reports "no
  structural change". Mutation-proven both ways, and the second mutation found a defect in the new test
  itself: a 40-zero sha SKIPPED instead of failing, because a served 404 was caught as "upstream
  unreachable". A 404 on a sha you wrote is a bad pin, not an offline laptop.
- *It makes a known deviation VISIBLE rather than removing it:* `data.py`'s own comment records that
  `on` stays a single key until the lance-namespace SDK bump (A9 blocked on A10), because the installed
  0.11.0 model types it `str`. Vendoring the current spec is what stops that deviation hiding behind a
  stale document.
- **AND THE BUNDLES' LAG IS NOT HYPOTHETICAL — one was measured the same day.** Reading
  `lance-format/lance` `rust/lance-table/src/feature_flags.rs` against the vendored `file_format.md`
  2026-09-14: the doc says "flags with bit values 32 and above are unknown"; upstream defines
  `FLAG_DISABLE_TRANSACTION_FILE` (32), the overlay bit (64), `FLAG_COVERED_INDEX_METADATA` (128) and
  `FLAG_MIXED_DATA_FILE_VERSIONS` (256), with `FLAG_UNKNOWN` at `1 << 8`. So the doc is FOUR bits behind
  the code it describes. `service_kit.lakehouse.features` already named three of them; the fourth was
  refusing as a bare `256 (unknown)`, which is exactly what that module exists to prevent — fixed and
  mutation-proven, naming it without admitting it to `SUPPORTED`.
- *Closes when:* the five bundles get the same treatment — they carry no scrape commit, so nothing can
  verify them, and re-vendoring them needs the tool that produced them rather than a `curl`. The
  feature-flag case shows the cost is real rather than tidiness: a doc four bits behind is a refusal an
  operator cannot act on.

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

**LH-150 · `LANCE_NS_DELIMITER` is documented as an operator knob and is actually a bootstrap-only identity: changing it on a running estate silently denies every authorization check**
`catalog, service-kit, openfga, lineage` · med · found 2026-09-14 while measuring [[LH-023]] · **blocked:** owner decision — refuse a boot whose delimiter disagrees with the tuples already stored, or document the delimiter as bootstrap identity and stop presenting it as an operator knob

- *Why open:* the delimiter spells the OpenFGA object id, not merely the wire identifier.
  `fga.canonical_object_id(segments, delimiter=...)` JOINS with whatever it is given and 55 of its 64
  call sites pass `settings.delimiter`, so the object a grant lands on and the object a check looks up
  both follow the configured value — consistently at any instant, and NOT across a change.
- *Demonstrated rather than argued, 2026-09-14, against the shipped functions:*

      tuple written under '$' : table:acme$bronze$events
      check issued under '.'  : table:acme.bronze.events        # a different object, no grants
      parent under '$' / '.'  : acme$bronze / acme.bronze       # the cascade misses too

  Every check DENIES, every parent cascade misses, and the fail-closed wrapper renders that as an
  authorization outage on an estate whose catalog logs look healthy.
- **THE PROSE INVITED IT, which is why this is a row and not a note.** `naming.py` said the env override
  "exists so an operator who changes it changes it from this single default" — true about keeping the
  planes aligned, and silent about the fact that changing it renames every governed object. An operator
  reading only that would take it for a formatting preference.
- *NOT fixed by spelling FGA ids with a fixed `$`, and that was checked before proposing it:*
  `tests/unit/test_cross_axis_identity.py` deliberately holds the FGA object, the lineage Dataset name
  and the id embedded in Lance metadata BYTE-IDENTICAL under any delimiter. Hardcoding one axis breaks
  that identity instead of the estate's, which is a worse trade — the current design is right, and what
  is missing is that the value is immutable after bootstrap.
- *What landed now:* the consequence is stated at both documented sites (`naming.py`, `catalog/core/config.py`),
  so the change is an informed one rather than an invited one. That is prose, and prose is not a control.
- **MEASURED 2026-09-16, AND IT PRICES THE CHANGE THE ROW WARNS ABOUT.** The live catalog Deployment
  sets `LANCE_NS_DELIMITER=$` explicitly, and the authorization store agrees without exception: of
  **1000 stored objects, 715 carry `$` in the id and 0 carry `.`**. So the knob has never been changed,
  and changing it would orphan 715 existing grants at once — every one of them denying, fail-closed,
  with no message naming the cause.
- **WHICH REMOVES THE ROW'S OWN OBJECTION TO A MECHANISM.** It defers the fix because a bootstrap record
  would be "new estate state for a knob nobody has changed". No new state is needed: the stored tuples
  ARE the record. A boot can read one governed object id out of OpenFGA and refuse to serve if its
  delimiter disagrees with `settings.delimiter` — comparing against state the estate already keeps,
  rather than inventing a second source of truth that could itself drift from the tuples.
- *Two honest limits on that shape:* a FRESH estate has no tuples to compare, so the check must no-op on
  an empty store rather than refuse the first boot; and it needs a governed object (a `table:`/
  `namespace:` id), not a `user:` subject, whose local part is an IdP `sub` and carries no delimiter.
- *Closes when:* an owner rules — but the decision is now between "refuse a boot whose delimiter
  disagrees with the tuples already stored" and "document the delimiter as bootstrap identity and stop
  presenting it as an operator knob", not between a fix and new estate state. Related: [[LH-023]], whose
  per-request delimiter support `docs/DECISIONS.md` row 6 consciously skipped for the neighbouring reason
  (an endpoint-only delimiter would let the router-level FGA gate authorize a differently-parsed object).

**LH-167 · A tenant's silver tier has been WRITTEN eight times and PUBLISHED never, and nothing reports a tier that stopped mid-cascade**
`medallion, catalog` · low · found 2026-09-16 while refuting [[LH-143]]'s worked example

- *Why open:* `advref31-silver$features` has **8 `WROTE`** edges in the lineage graph and the catalog
  answers its `tags/list` **200 with `{'tags': {}}`** — the table exists, is readable, has been written
  repeatedly, and carries no `published` tag. Its `advref31-gold$catalog` has no `Dataset` node at all,
  so the lane stopped at silver. Measured through the readers the code uses, not a neighbouring store:
  `published_reader('silver->gold', 'advref31')` returns `None` while the same call for `bind86`
  returns `3`.
- **NOTHING REPORTS THIS, and that is the row rather than the unpublished tier itself.** The cascade-lag
  detector is CORRECT to stay silent — [[LH-143]] — because a source that never published has nothing
  to fall behind, so `unmeasurable` is the honest answer. But that means a tier which was written eight
  times and then stopped is indistinguishable, from every surface the estate has, from a lane nobody
  ever ran. One of those is fine and the other is a stalled cascade.
- *What is NOT yet known, and should be established before any fix is designed:* whether the publish was
  ATTEMPTED and held (the quality gate's third answer, `promotions`), or never attempted at all. The
  retained log window carries no `advref31` promotion or hold record, which distinguishes neither —
  absence of a log over a window is not absence of the event.
- *Why it is low and not med:* no data is lost and no wrong answer is served; the gap is observability
  of a mid-cascade stop. It becomes med the moment a tenant expects gold and nothing says why it is
  absent.
- **ESTABLISHED 2026-09-16: NEVER ATTEMPTED, not held.** Every `Run` the graph holds for advref31 is
  `create_table`, `register_table`, `reconcile` or `compaction` — there is **no `silver-to-gold` job in
  any state**, failed or otherwise, so nothing was refused, held or retried. The silver tier itself was
  written by `embed_features` four times beside a `create_table` and a `compaction`.
- *The contrast is what makes it a defect rather than a configuration:* five other tenants DO have a
  gold tier in the same graph — `acme-gold$catalog`, `bind86-gold$catalog`, `c6t115034-gold$catalog`,
  `durproof-gold$catalog`, `gateprobe-gold$catalog`. So this is not "gold is not in use here"; it is one
  lane that stopped, beside five that did not.
- *The honest limit on that evidence:* the graph can only prove that no silver->gold run EMITTED
  lineage. For a lane whose every other step emitted — bronze register, bronze ingest, silver create,
  four silver writes, two compactions — that is strong, but it is not the same as proving no process
  ran. Distinguishing "no trigger was published" from "a trigger was published and refused" needs the
  DLQ, and is the same question [[LH-151]]/[[LH-166]] answer.
- **AND THE BUS CANNOT ANSWER IT, which is worth recording so nobody repeats the attempt.** Measured
  2026-09-16: `MEDALLION` holds **178 messages** (seq 1580-1757), a rolling window, so advref31's silver
  writes have long aged out; a 40 KB sample of the 7,271-message `DLQ` contains no `advref31`, which is
  suggestive and is not proof over a partial read. The trigger's fate is simply not recoverable from the
  streams now.
- *What the deployed stage runner DOES say (`rask-silver-to-gold`, read 2026-09-16):*
  `MEDALLION_SUB_TOPIC=medallion.silver`, `MEDALLION_FROM_URI=s3://lance-catalog/medallion/silver`,
  `MEDALLION_TO_URI=s3://lance-catalog/medallion/gold`, and `MEDALLION_PUB_TOPIC=` EMPTY — gold is
  terminal, so publishing nothing downstream is correct there.
- *A tempting wrong conclusion, recorded because it is the obvious one:* those FROM/TO URIs are the
  COMPOSED paths ([[LH-137]]/[[LH-164]]'s double-home) while advref31's silver is a catalog table
  (`advref31-silver$features`), which reads like "the runner never looks at advref31". It is not that
  simple — five tenants DO have catalog-table-shaped gold (`acme-gold$catalog` and four more) produced
  by this same runner, so it resolves the tenant from the trigger payload rather than from the path. Why
  advref31's silver produced no trigger, or produced one that resolved nowhere, needs that payload —
  which the rolling window no longer holds.
- *Closes when:* a surface names a written-but-unpublished tier — the lag detector's own report, the
  promotions door, or the tier board. The publish state no longer needs establishing; one of those
  three has to say it out loud, because from every surface the estate has today this is still
  indistinguishable from a lane nobody ran.

**LH-151 · The DLQ parking plane assumes every park is retry exhaustion, and Dapr parks on at least two other paths — one invisibly, one as a false page**
`medallion, chart, notifications, lineage` · med · found 2026-09-14 while driving [[LH-106]] gap #2 · **blocked:** owner decision — the parking shape; shares its answer with [[LH-166]]'s refusal-ack ruling

- *Why open:* the DLQ plane's whole promise is that parking is VISIBLE — `/dlq-event` ERROR-logs
  `dapr_dead_letter_parked` and bumps `medallion_dlq_parked_total`, which `MedallionCascadeDeadLettering`
  alerts on. That promise holds only for a message the sidecar can deserialize as a CloudEvent.
- *Measured live 2026-09-14 by publishing a raw non-CloudEvent body to `medallion.bronze` on
  `bronze-to-silver` (`metadata.rawPayload=true`, body `["LH-106-poison-probe"]`):*

      18:15:53.034  error  dapr.runtime.processor.subscription  error deserializing cloud event in pubsub
                           lineage-pubsub-bronze-to-silver and topic medallion.bronze
      18:15:53.055  error  dapr.runtime.processor.subscription  error deserializing cloud event in pubsub
                           lineage-pubsub-bronze-to-silver and topic dlq.bronze-to-silver
      18:15:53.055  error  dapr.contrib                         Error processing JetStream message
                           dlq.bronze-to-silver/{181 9952}

  **21 milliseconds, and no app delivery at all.** The failure is in the sidecar's envelope decoding,
  ahead of any retry policy, so the `constant 120s x 4` schedule never engages — the contrast with the
  well-formed poison driven minutes later on the same subscription (5 attempts, then a parked+logged
  dead letter) is what isolates it. The dead letter then lands on the DLQ topic as the SAME unparseable
  bytes, so `/dlq-event` cannot read it either: nothing logs, nothing counts, nothing pages. The
  message is retried per the component's own `maxDeliver: 3` / `backOff: 720s,720s` and then gone.
- *Why it is not merely theoretical:* the bus is the estate's widest trust surface — `transform.py`'s
  own validate-or-DROP comment says so — and an external OpenLineage producer, a hand-run `nats pub`,
  or any publisher that omits the CloudEvent envelope produces exactly this. It is the one class of
  message MOST likely to be malformed, and it is the one class the parking plane cannot see.
- **A SECOND SHAPE, measured 2026-09-14, and this one PAGES.** A handler returning `DROP` is also
  forwarded to the dead-letter topic — `transform.py`'s `_QUALITY_BLOCKED` is `{"status": "DROP"}`, and
  its comment asserted "no DLQ is configured, so the drop is final", which the chart falsified by
  setting `MEDALLION_DLQ_TOPIC` unconditionally (comment rewritten in place). Observed on the live
  estate: `medallion_quality_blocked token='11417bea0a56'` at 19:48:02.070, `promotion_held_for_review
  reasons=['not_null']` at :02.107, `dapr_dead_letter_parked ... token='11417bea0a56'` at :02.112 —
  **42ms, no retries**, and the only park in the whole window. So `medallion_dlq_parked_total` rises and
  `MedallionCascadeDeadLettering` fires saying "a stage delivery gave up ... a dataset has silently
  stopped updating" about a quality gate doing exactly its job and holding the batch for a human. Every
  deterministic DROP in `transform.py` — malformed payload, authz denial, undeclared transform — has
  the same consequence.
  **A THIRD SITE, AND THE UPSTREAM ANSWER — both established 2026-09-16.**
  `service_kit/draining.py:20` justified its RETRY answer with *"DROP is final and these topics carry no
  DLQ"*. Measured on the running estate, that is false for most of its own call sites:
  `bronze_arrival.py:42,100` and `promotions.py:340` pass `dead_letter_topic=settings.dlq_topic`, and the
  medallion producer carries `MEDALLION_DLQ_TOPIC=dlq.medallion-producer`; only
  `maintenance/api/index_work.py:114` resolves to `None`. The RETRY answer stands — what was wrong is the
  alternative it was weighed against, since a DROP there would PARK a good trigger rather than discard
  it. Corrected in place; behaviour unchanged.
  *Confirmed at the DEPLOYED version rather than master:* daprd is `ghcr.io/dapr/daprd:1.18.1`, where the
  routing is `subscription.go:362-372` and `postman/http/http.go:142-146` (this row previously cited
  master's 365-368 / 143-147, which are correct for master and not for what runs here).
  **AND UPSTREAM HAS ALREADY ANSWERED THE OPEN QUESTION.** `dapr/dapr#6282`, maintainer artursouza:
  *"I agree to make DROP move message to deadletter. Creating a new response type when SUCCESS and DROP
  have same behavior would be confusing IMO. So, for customers that don't want a message to be processed
  ever, SUCCESS is still there."* Implemented by `dapr/dapr#7097`, merged 2023-10-27. So **SUCCESS is the
  sanctioned ack for a message the app can never accept** — option (a) is not a workaround, it is the
  upstream-intended pattern. The reason nobody in this estate knew is that #7097 shipped with its
  "Extended the documentation" box unticked: the pubsub API reference still describes DROP as only
  *"Warning is logged and message is dropped"*, and the dead-letter page does not mention DROP at all.
  *The two sites do NOT have the same blast radius, so one decision does not mean one edit:* medallion's
  cascade components are `deliverPolicy=new` + durable and hold ~23 DLQ messages; lineage's is the
  estate's only `deliverPolicy=all` + ephemeral component and holds ~8,274 of the DLQ's ~8,305. The loop
  is lineage's alone.
  **THE SAME SHAPE HAS A SECOND SITE, AND ITS COMMENT MADE THE SAME FALSE CLAIM — `lineage`, corrected
  2026-09-16 (`8c44e7d8`).** `consumer.handle_cloud_event` returns `_DROP` on `PermissionDeniedError`
  and its docstring gave the reason as avoiding exactly this: retrying "only burns the delivery budget
  and then parks a permanent refusal on the dead-letter topic as if it were an outage". Its subscription
  declares a `deadLetterTopic` too, so DROP is what parks. Measured the same way, sidecar and app naming
  one CloudEvent id back to back: daprd *"DROP status returned from app while processing pub/sub event
  a4d65ffd-…"*, then `dapr_dead_letter_parked event_id='a4d65ffd-…'`, then `POST /lineage-dlq` 200.
  The comment is rewritten; the BEHAVIOUR is unchanged and waits on the same decision as `transform.py`.
  *What lineage adds to the cost side:* its consumer is ephemeral with `deliverPolicy: all`, so every
  restart re-presents the retained stream and re-parks the same unrepairable events. Two rolls measured
  it — 49 parks inside two minutes of one pod start, and DLQ 9,949 -> 10,011 across the next, so **one
  deploy appends 49-62 messages about events already in the queue** (the two rolls measured, by log count and by stream delta respectively). See [[LH-166]].
  *Verified at the source, not only measured:* `dapr/dapr` `pkg/runtime/subscription/subscription.go:365-368`
  — `} else if errors.Is(pErr, rtpubsub.ErrMessageDropped) {` / `// send dropped message to dead letter
  queue if configured` / `if route.DeadLetterTopic != "" { derr := s.sendToDeadLetter(...) }`. The HTTP
  postman returns that error for a `DROP` status (`postman/http/http.go:143-147`).
- **RE-MEASURED ON THE LIVE STREAM 2026-09-15, AND OPTION C IS NOT IMPLEMENTABLE AS COSTED.** The
  owner approved "annotate the DROP and route it separately" on 2026-09-15. Reading an actual parked
  message off the `DLQ` JetStream stream (9,216 messages) shows there is nothing to annotate and
  nothing to route on: the NATS headers are `Nats-Msg-Id` ALONE, and the body is the original
  CloudEvent republished verbatim — keys `data, datacontenttype, id, pubsubname, source, specversion,
  time, topic, traceid, traceparent, tracestate, type`. **No delivery count, no failure reason, no
  dead-letter metadata of any kind.**
  *So the DLQ handler cannot tell a deliberate refusal from a real exhaustion by inspection*, which is
  what option C assumed. Dapr republishes the message it was given; it adds nothing.
- **THE DISCRIMINATOR HAS TO BE MADE AT THE REFUSAL, NOT READ AT THE PARK**, which reshapes the option
  rather than killing it. The only party that knows a refusal is deliberate is the handler returning
  it, and it already counts one (`medallion_stage_refused_total{reason=...}`). The implementable form
  of C is therefore: on a DETERMINISTIC refusal the handler publishes the message to its own
  `refused.<lane>` topic and ACKs, so the payload is still retained for replay; `DROP` stays for
  deliveries that are genuinely undeliverable, and the DLQ keeps meaning exhaustion.
  *That is close to option B and must not be confused with it.* B acked refusals and published nothing,
  which is why it retired the `_drop` verb across four producers and 32 assertions and was reverted on
  2026-09-11 after nine failures. Keeping the payload is the difference that makes the alarm honest
  without losing the record.
- *Cost, stated before anyone starts:* a new topic per lane in the chart, a publish on the refusal
  path, and the metric contract (`medallion_dlq_parked_total` stops tracking refusals). The 7,709
  parked messages already accumulated are not reclassified by any of this.
- *Closes when:* an owner decides the shape. The candidates are not equivalent: (a) subscribe
  `/dlq-event` with `rawPayload` handling so a parked envelope-failure is still logged and counted —
  smallest, but changes how every dead letter is parsed; (b) alert on the sidecar's
  `error deserializing cloud event` log line instead, which needs no app change but moves a data-plane
  signal into log-scraping; (c) accept it and say so in `RESILIENCE.md`, since a non-CloudEvent
  publisher is arguably outside the contract. The measurement above is what the decision needs;
  it should not be guessed at.



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
- *Evidence:* /home/gabriel/Desktop/rask/services/catalog/src/catalog/api/fga_deps.py:1034 (`grant_owner=token.iss != SERVICE_DOOR_ISSUER`); /home/gabriel/Desktop/rask/services/catalog/src/catalog/api/security.py:47,163 (the service door mints `iss="rask://service-door"`); /home/gabriel/Desktop/rask/packages/service-kit/src/service_kit/governed/fga.py:1320-1333 (owner tuple omitted, hierarchy edge still written in the same batch); /home/gabriel/Desktop/rask/packages/service-kit/src/service_kit/governed/auth/model.fga.yaml:1273-1305 (case 'a table a MACHINE registered is owned by the project, never by the machine' — service-silver-to-gold asserts can_drop/can_deregister/can_restore/manage_grants all false, alice true); /home/gabriel/Desktop/rask/tests/unit/test_a_machine_created_table_is_owned_by_its_project.py:1-60
- *What would reopen it:* A stage-runner-issued create that still writes a `user:<service> owner table:<id>` tuple — i.e. a service principal whose token carries an `iss` other than SERVICE_DOOR_ISSUER, or a create door that bypasses `seed_ownership` and calls `grant_on_create` with the default `grant_owner=True`.

**LH-053 · ~~Bucket claims are keyed by warehouse ID, so two warehouse IDs can both claim the SAME bucket — and the four control-root JSON stores it must live in are not collapsed~~ — CLOSED 2026-09-15, stale-open**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The bucket claim is real but narrow — a scan-based guard already refuses a sequential double-claim; what is missing is only the ATOMIC bucket-keyed claim, and the store-collapse the row makes it wait on has already landed.
  **Evidence:** /home/gabriel/Desktop/rask/services/catalog/src/catalog/services/warehouses.py:233-236 (`projects_claiming_bucket`) called at /home/gabriel/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/warehouses.py:180-183 — so a double-claimed bucket IS detected, contra the row's 'No code path detects'; the defect is TOCTOU: the listing is read at warehouses.py:152 before authz, the reserved guard, and the network `provision_bucket` at warehouses.py:186, and the record write is keyed by warehouse id (/home/gabriel/Desktop/rask/services/catalog/src/catalog/services/warehouses.py:186-193 `create_warehouse_record` → `_warehouse_key(record["id"])`), so two different ids racing on one bucket both win. NO bucket-keyed claim exists (grep `bucket-claims` across the tree hits only open_backlog_left.md:399). THE BLOCKER IS DEAD: the conditional-create primitive exists and is already in use — /home/gabriel/Desktop/rask/packages/service-kit/src/service_kit/lakehouse/records.py:89 `create_json` (`IfNoneMatch: *` on s3, `open(...,"xb")` locally), used for the warehouse mint (warehouses.py:193) and the write-once namespace binding (warehouses.py:253); and the four hand-rolled control-root stores WERE collapsed — /home/gabriel/Desktop/rask/packages/service-kit/src/service_kit/lakehouse/record_store.py:1-27 ('the shape four registries hand-rolled: protection, maintenance_policies, trash and warehouse_records'), imported by protection.py:31, maintenance_policies.py:43, trash.py:35, warehouse_records.py:19.
  **Reopen if:** A `_warehouses/bucket-claims/<bucket>.json` (or equivalent) written through `records.create_json` on the warehouse-create path, or a reconcile category that reports two warehouse records naming one bucket — neither exists (reconcile.CATEGORIES at services/maintenance/src/maintenance/services/reconcile.py:86-95 has no such row). The smaller true fix: write the bucket-keyed claim via the already-shared `create_json` in `create_warehouse`; no store collapse and no #85 record primitive is needed first.
`catalog` · **HIGH** · **blocked:** owner ruling: pull the bucket claim forward as its own store, or confirm it stays behind the #85 record primitive

- *Why open:* Deferred by diff2's F1 landing note rather than by omission: the fix belongs with #85's collapse of the four control-root JSON stores, not as a fifth ad-hoc store. No code path detects a double-claimed bucket and recovery is manual — Mallory ends up holding `owner` on a warehouse whose `root_uri` is another tenant's bucket, and `set_project_policy` resolves through the same registry so her maintenance policy can destroy their version history.
- **CLOSED — the condition was already met at HEAD and the row had not been re-read.** Found by a
  re-measurement sweep, then verified by hand rather than taken on the agent's word:
  `warehouses.claim_bucket` writes `{_BUCKET_CLAIMS_PREFIX}/{bucket}.json` — **keyed by BUCKET** — through
  `records.create_json`, the same `If-None-Match: *` conditional-create primitive the row asked for, and
  the warehouse-create door calls it (`endpoints/warehouses.py:199`) before the record write. Commit
  `28f98f11` names this row in its subject.
- *The design subtlety the implementation got right, worth keeping:* a claim already held by the
  CALLER'S OWN project passes rather than colliding, because one project legitimately backs several
  warehouses with one bucket (the work+gold pair) — keying by warehouse id would have refused the
  second half of a legitimate pair, which is the mistake this row's title was pointing at.

**LH-054 · ~~The credential-isolation e2e SKIPS against the shipped stack, so cross-tenant credential refusal is proven only by rask's own offline policy evaluator~~ — CLOSED 2026-09-16 (11 legs driven live against the real store, 0 skipped)**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The legs no longer skip against the deployed estate — the live runner provisions tenant B and the chart vends `sts` (→ mode `direct`) — so what is actually left is the CI kind stack, and the row's `web_identity` requirement is the wrong lever.
  **Evidence:** /home/gabriel/Desktop/rask/scripts/e2e_live.sh:234-271 provisions a fixed second tenant and exports `LANCE_E2E_PROJECT_B` + `LANCE_E2E_TENANT_B_TOKEN` ('the credential-isolation legs will RUN'), and e2e_live.sh:319 runs `pytest tests/e2e-py -m e2e`, which includes the suite. /home/gabriel/Desktop/rask/chart/values.yaml:955 `mode: sts` — and /home/gabriel/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/credentials.py:125 sets `mode = "server_mediated" if creds is None else "direct"`, so the suite's own gate (/home/gabriel/Desktop/rask/tests/e2e-py/test_credential_isolation_e2e.py:110, skip unless `direct`) passes under `sts`. `web_identity` would make it WORSE, not better: /home/gabriel/Desktop/rask/services/catalog/src/catalog/core/vending.py:354 returns None without a caller OIDC token. The test file carries live measurements from the unskipped drives (test_credential_isolation_e2e.py:139, :243-249). WHAT IS STILL OPEN: the CI harness /home/gabriel/Desktop/rask/scripts/e2e_stack.sh:277-284,305-306 lists the suites it runs and this file is not among them, and it exports no tenant-B vars; /home/gabriel/Desktop/rask/Makefile:882-889 still says 'NOT in e2e-ci'. The widened-policy SABOTAGE lever does not exist anywhere (no such fixture or chart value).
  **Reopen if:** A `LANCE_E2E_TENANT_B_TOKEN` export or `test_credential_isolation_e2e.py` line inside `scripts/e2e_stack.sh` would close the remaining half; conversely, if `chart/values.yaml` vending.mode were `mode_b` the `direct` gate would skip again. Live pass/fail against the deployed estate is UNVERIFIED from here — I read the harness and the chart, not a run.
`catalog, storage, chart` · **HIGH**

- *Why open:* The code landed (3cacdd91) but every test skips on the shipped stack and a skip reads identically to a pass — RustFS never evaluates the session policy. `scripts/e2e_stack.sh` provisions neither web-identity vending nor a second tenant admin, and the sabotage half needs a deliberately-widened-policy lever that must not be reachable in production.
- *Closes when:* Provision `vending.mode=web_identity` (requires `rustfs.oidc.enabled` + `auth.enabled`) plus a SECOND tenant with its own admin subject in `scripts/e2e_stack.sh`, add the widened-policy sabotage lever to the harness chart values, then run the credential attack e2e unskipped in CI.
- **THE SECOND-TENANT HALF IS WIRED 2026-09-16, AND THE ROW'S FIRST CLAUSE IS WRONG.** `web_identity`
  is not the lever: `chart/values.yaml:955` ships `mode: sts`, and `credentials.py:125` sets
  `mode = "server_mediated" if creds is None else "direct"`, so the suite's own gate
  (`test_credential_isolation_e2e.py:110`, skip unless `direct`) already passes under `sts` —
  while `vending.py:354` returns None without a caller OIDC token, so `web_identity` would make the
  legs skip MORE, not less. Strike that clause rather than implementing it.
- *What landed:* `scripts/e2e_stack.sh` now provisions `project:e2etenantb` through
  `POST /v1/projects` and grants bob `admin` on it, exports `LANCE_E2E_PROJECT_B` +
  `LANCE_E2E_TENANT_B_TOKEN`, and runs `tests/e2e-py/test_credential_isolation_e2e.py` in the guarded
  list. Bob holds nothing on `acme`, so he stays the 403 leg the sibling suites need. The
  no-silent-skips gate was deliberately NOT widened: a tenant-B provisioning failure skips the five
  legs and reds the job, which is the gate working.
- *STILL OPEN, and this is the whole remaining ask:* the harness has **not been run**. `bash -n` is
  clean and both doors it posts to were verified present on the deployed catalog's OpenAPI
  (`POST /v1/projects`, `POST /v1/access/tuples`, 2026-09-16), but a kind run is what proves the five
  legs EXECUTE rather than skip — and that is the one thing this row has always been about. Also still
  open: the widened-policy SABOTAGE lever, which exists nowhere (no fixture, no chart value).
- **DRIVEN AGAINST THE LIVE ESTATE 2026-09-16 AND EVERY LEG EXECUTED — 8 passed, 0 skipped.**
  `RELEASE=rask bash scripts/e2e_live.sh tests/e2e-py/test_credential_isolation_e2e.py` on the deployed
  k3s release (`main-17aae203`), which discovered `catalog=10.43.220.241:2333`, `s3=10.43.44.177:9000`,
  alice + bob, warehouse `acme-bucket`, and provisioned `tenant B: e2etenantb (admin=bob)`:

      tenant_b_credentials_cannot_list_tenant_a_bucket   [read]  PASSED
      tenant_b_credentials_cannot_list_tenant_a_bucket   [write] PASSED
      tenant_b_credentials_cannot_read_tenant_a_objects  [read]  PASSED
      tenant_b_credentials_cannot_read_tenant_a_objects  [write] PASSED
      tenant_b_write_credentials_cannot_put_into_tenant_a_bucket PASSED
      the_credential_still_works_on_its_OWN_table        [read]  PASSED
      the_credential_still_works_on_its_OWN_table        [write] PASSED
      read_tier_credentials_cannot_write_to_their_OWN_table      PASSED

  **The refusal comes from the STORE, not the catalog** — the attack builds a raw boto3 client from the
  credentials the catalog really vended for B and points it at A's bucket, so the catalog is not in the
  request path. The two OWN-table legs are what make the other six mean something: the credential is
  live, so the refusals are the session policy evaluating, not a dead key. This is the claim
  `open_lakehouse_diff` §3 called "the single most important untested claim in the security story", and
  it is no longer untested.
- *WHAT IS LEFT, and neither part is the claim above:*
  1. **The CI harness.** `scripts/e2e_stack.sh` builds a **kind** cluster (`kind create cluster`,
     `kind load docker-image`), which is Docker — so it cannot be driven from this workstation under the
     estate's own toolchain rule. The live drive above is the stronger evidence in every respect except
     "a fresh install also does this", which is the only question the kind harness answers that this
     one does not.
  2. **The widened-policy SABOTAGE lever**, which still exists nowhere — no fixture, no chart value. It
     is what would prove the legs would FAIL if the policy were wrong, as opposed to passing because
     the store refuses everything. Until it exists, eight green legs prove the refusal happens and not
     that the session policy is the thing causing it.
- **THE SABOTAGE LEVER IS NOT NEEDED — the question it asked is answered by a leg that needs no
  production-reachable switch. 2026-09-16: 11 passed, 0 skipped.** Every original assertion attacks
  ANOTHER TENANT'S bucket, so all six would pass just as well if the store refused on bucket ownership
  and never read the session policy — which is precisely why the row wanted a widened policy to sabotage.
  The same question asked differently: **two tables, ONE tenant, ONE bucket.**
  `build_session_policy` scopes object actions to `arn:aws:s3:::{bucket}/{prefix}/*` and gates
  `s3:ListBucket` on `s3:prefix` matching `{prefix}/*` (`vending.py:208-249`), so a credential vended
  for table 1 must be refused on its SIBLING — while a bucket-level refusal would let it straight
  through. Driven live:

      a_credential_is_scoped_to_its_TABLE_not_merely_its_bucket  [read]  PASSED
      a_credential_is_scoped_to_its_TABLE_not_merely_its_bucket  [write] PASSED
      a_write_credential_cannot_plant_an_object_in_a_SIBLING_table       PASSED

  With `the_credential_still_works_on_its_OWN_table` as the positive control — the SAME credential
  lists its own prefix — the refusals are the prefix condition evaluating, not a dead key and not the
  bucket. A lever "that must not be reachable in production" was the wrong shape for the question; it
  needed a second table, not a weaker policy.
- **CLOSED.** The row's claim — that the refusal is proven only by rask's own offline evaluator — is
  false: it is proven by the store, against the deployed release, 11 legs, 0 skipped. The struck first
  clause (`web_identity`) is measured to be the wrong lever and is not implemented.
- *The one residue, named so it is not lost:* `scripts/e2e_stack.sh` is WIRED for these legs (tenant B
  provisioned, the file in its guarded list, `bash -n` clean) but has never been EXECUTED, because it
  builds a **kind** cluster — Docker — which this estate's toolchain rule forbids from a workstation.
  It answers one question the live drive does not: "does a FRESH install also isolate". **Reopen if:**
  the first CI run of `e2e_stack.sh` reports these legs SKIPPED rather than passed.

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

**LH-057 · ~~Per-base credential vending is unimplemented — the vendor refuses any table whose fragments carry a `base_id` instead of vending a union of bases~~ — CLOSED 2026-09-16 (the short-circuit is narrowed to the bases the policy misses; a SECOND door was vending blind)**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The union vend LANDED — the manifest's base_paths are read and granted READ in the session policy — leaving only the `has_external_bases` short-circuit and the per-base write/deny split the code declined on the record.
  **Evidence:** IMPLEMENTED: /home/gabriel/Desktop/rask/services/catalog/src/catalog/core/vending.py:129,180-201 — `build_session_policy(bucket, prefix, tier, bases)` emits a `ListBase<n>` + `BaseObjects<n>` pair per base (own bucket ARN each), with `_reject_iam_metacharacters("base path", base)` applied to the manifest-sourced path at :180. The manifest read is reused, not doubled: /home/gabriel/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/credentials.py:138-163 `_dataset_facts` returns `(version, base_paths)` off one `lance.dataset` open (it replaced `_current_version`, which no longer exists), and :113 passes `bases=declared_bases` into `vendor.vend`; every vendor signature now takes `bases` (vending.py:82,208,279,353). The §H12 maintainer-probe cost is addressed there. STILL REAL, and narrower than the row: /home/gabriel/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/credentials.py:104-106 still returns `mode="server_mediated"` when `settings.multibase_data_base_list` is set and `has_external_bases` (/home/gabriel/Desktop/rask/services/catalog/src/catalog/core/vending.py:424-448) is true — that is the only remaining refusal, and it is latent behind an empty deployed list. DECLINED ON THE RECORD: vending.py:143-146 states every base gets READ ONLY and that write-on-target_bases / deny-on-reference-only 'need evidence a manifest read does not yet distinguish'; vending.py:148-155 states one grant shape covers both layouts deliberately so the policy need NOT consult `BasePath.is_dataset_root`.
  **Reopen if:** If `credentials.py:104-106` were removed (or narrowed to the bases the policy cannot cover) the row would close in its useful sense. The row's own cited lines are stale — `core/vending.py:213` is inside `_expiry_millis`/`StsVendor.__init__` and `:278` is the `StsVendor.vend` signature that now forwards `bases`. Smaller true fix: drop the `has_external_bases` fallback now that the policy covers the bases; the `is_dataset_root` clause should be struck, not built.
`catalog, maintenance, storage` · **HIGH** · **blocked:** owner acknowledgement of R4; the bases-as-storage-profile framing it rides on

- *Why open:* Two of three clauses closed (the `session_token` seam and `expires_at_millis` on both vend paths) and the falsy-zero guard is fixed, but `endpoints/credentials.py:76-130` and `core/vending.py:213,278` still refuse rather than vend. §H12 is the measured cost: 69 datasets a tick refused compaction because the vended session policy cannot reach a base the manifest declares. Latent behind `settings.multibase_data_base_list` (deployed empty), so it fails on the first estate that enables the feature.
- *Closes when:* In `core/vending.py`, vend the union of the manifest's `base_paths` with per-base rights — read on inherited bases, write on `target_bases`, never on reference-only bases — resolving each path by `BasePath.is_dataset_root`, reusing the manifest `credentials.py::_current_version` already reads, and applying `build_session_policy`'s `*`/`?` metacharacter refusal to manifest-sourced paths.
- **CLOSED in the sense this row's own re-measure names: "if `credentials.py:104-106` were removed (or
  narrowed to the bases the policy cannot cover) the row would close in its useful sense."** Narrowed,
  not removed. `vending.unsanctioned_bases(location, declared_bases, sanctioned)` asks the question with
  the SAME predicate the policy uses (`_base_is_sanctioned`), so the door and the policy can no longer
  disagree about a base — proxying what the policy would have granted, or direct-vending what it would
  have dropped. Only a base the policy genuinely cannot grant forces `server_mediated`, and it says which
  one (`vend_server_mediated_unreachable_bases`).
- **A SECOND DOOR WAS VENDING BLIND, found while wiring the first — measured 2026-09-16.**
  `has_external_bases`'s own docstring says the vend door and describe-with-vending "must answer it
  identically"; `tables.py:418` called `vendor.vend(table_location=…, tier="read")` with **no `bases=`
  at all**, so every multi-base table that passed its guard got a credential scoped to less than the
  table is — § H12's shortfall on the door nobody re-checked. Both doors now read the manifest through
  one shared `vending.dataset_facts` and vend with the bases it returns.
- *The fragment scan is DELETED, not left beside its replacement.* `has_external_bases` walked every
  fragment's data files to answer "does any byte live in a base", which was the pre-union premise; with
  both doors converted it had no caller, and a function nothing calls whose tests still pass reads as
  live code. Its one durable lesson — `base_id` 0 is a real base while a root-local file reads `None`,
  so a truthy test called a shallow clone and a branch "not multi-base" — moved into
  `unsanctioned_bases`, which retires the trap rather than restating it: the manifest's declared
  `base_paths` are a SUPERSET of what fragments resolve through (`base_id` INDEXES them), so the scan
  is not needed to be safe. `test_base_id_zero_is_a_real_base.py` went with it; the real-Lance
  multi-base write in `tests/unit/test_multibase.py` was converted onto the live path instead of
  deleted.
- *NEW BRANCH, fail-closed and pinned:* a location `split_s3_location` cannot parse — a local `dir`-
  backend path, a spelling this module does not know — is never vouched for, whatever the allowlist
  says. The policy is written in S3 ARNs, so a base it cannot address is one a direct client could not
  reach, and the honest answer is the fallback rather than a `ValueError` out of the vend door.
- *The declined clause STAYS declined, and it is the only part that ever needed R4.* Every base is
  granted READ ONLY (`vending.py:143-146`); write-on-`target_bases` and deny-on-reference-only "need
  evidence a manifest read does not yet distinguish", and the row's own re-measure says the
  `is_dataset_root` clause "should be struck, not built". Nothing here pre-empts that ruling — the
  change narrows an existing guard with an existing predicate.
- *No feature flag any more.* The old short-circuit was gated on `multibase_data_base_list` so a
  single-bucket estate "never pays the fragment scan". There is no scan: a single-bucket table declares
  no bases, so the check costs one tuple comparison on a manifest read the door already did.
- **BUILT, DEPLOYED AND OBSERVED 2026-09-16** — Dagger `main-9e0608d8`, all ten stem-mates rolled, pins
  refreshed. Live against the deployed release, 21 passed / 1 skipped:
  `test_client_direct_e2e` (zero-byte-ingress commit, concurrent ACID commits, governed commit),
  `test_credential_isolation_e2e` (all 11), `test_multibase_e2e::test_off_allowlist_base_rejected`,
  `test_warehouses_e2e` (physical isolation, quarantine/restore, the two 401 legs). The one skip is
  `multibase_redirects_data_and_reads_fan_out`, which needs `multibase_data_base_list` set and this
  estate ships it empty — the documented gap, not a silent one.
  * **Catalog: 0 errors and 0 `vend_server_mediated_unreachable_bases`** — no table on this estate
    declares a base the policy cannot grant, so every multi-base table is direct-vended rather than
    proxied, which is the whole point of the narrowing.
  * **Maintenance: 220 SCOPED vends, 0 AMBIENT, 0 `not permitted to read the base at …`** — the union
    vend reaching `s3://lance-catalog/models/` holds, and [[LH-141]]'s four crossings still refuse.

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
- *Evidence:* Both bronze doors now register BEFORE writing bytes. /home/gabriel/Desktop/rask/services/medallion/src/medallion/services/produce.py:122-126 ('ask first, write second, so no window exists in which bronze rows sit on disk unregistered'), :153-167 calls `catalog_register.register_written_dataset` → `POST /v1/table/{id}/register` (/home/gabriel/Desktop/rask/services/medallion/src/medallion/services/catalog_register.py:464), and only then :193 `seed_bronze`. Media: /home/gabriel/Desktop/rask/services/medallion/src/medallion/services/media_produce.py:146,177 calls `ensure_stage_output` → `POST /v1/table/{id}/create` (catalog_register.py:331) before :201 `_seed_and_ingest`. At the catalog, `register`, `create` and `declare` are all create-on-parent (/home/gabriel/Desktop/rask/services/catalog/src/catalog/api/fga_deps.py:132-136), which resolves to `can_create_table` on `namespace:<parent>` (fga_deps.py:325-343 `_create_parent_check`) and is enforced by the router-wide `Depends(authorize)` (/home/gabriel/Desktop/rask/services/catalog/src/catalog/api/v1/router.py:47, fga_deps.py:692-695). The subject checked is the medallion's own service principal: /home/gabriel/Desktop/rask/services/catalog/src/catalog/api/security.py:160-168 mints an IDToken whose `sub` is the presented `x-lance-service-identity`. `bronze_dataset` defaults to `bronze$events` (/home/gabriel/Desktop/rask/services/medallion/src/medallion/core/config.py:519), so the parent is exactly `namespace:<project>-bronze` — the object the row asks for.
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
- **THE FIRST CLAUSE IS MISPRESCRIBED — `accessControl` CANNOT SEE A PUB/SUB DELIVERY. Verified against
  the Dapr documentation 2026-09-16, not inferred.** `docs.dapr.io/operations/configuration/invoke-allowlist`
  states the block's scope verbatim: it restricts "what the operations *calling* applications can
  perform, **via service invocation**, on the *called* application", and it names a different mechanism
  for the one other case it covers (`WorkflowAccessPolicy` for cross-app workflow scheduling). Pub/sub
  delivery is not in its scope. So building this clause as written would ship a control that cannot
  fire — the defect class this register keeps finding — and the row's "zero `accessControl` hits in the
  tree" is a true grep supporting a wrong conclusion.
- **AND THE TOPIC IS ALREADY CLOSED TO FIRST-PARTY APP-IDS, which the row's grep could not see because
  it looked for the wrong thing.** Every lineage pub/sub Component carries a top-level `scopes:` naming
  exactly one app-id (measured off the render): `lineage-pubsub` -> `[catalog, maintenance]`, plus
  per-app copies for `lineage`, `medallion-producer`, `bronze-to-silver`, `silver-to-gold`,
  `media-to-silver`, `notifications` and the DLQ. **A Dapr component is not loaded for an app outside
  its `scopes`**, so an arbitrary pod in the mesh has no lineage pub/sub component at all and cannot
  publish through one. *Corrected in this row because I first read `scopes` at the wrong YAML level —
  it is a document-level key, not `spec.scopes` — and reported "no scoping" before re-reading.*
- *THE RESIDUAL IS NARROWER AND REAL:* within those eight scoped app-ids there is no per-TOPIC
  restriction, so a consumer-only identity (`notifications`, or `lineage` itself) can publish a forged
  `lineage.events.v1` event through its own component. The mechanism that closes that is
  **`protectedTopics` + `publishingScopes`**, verified on
  `docs.dapr.io/developing-applications/building-blocks/pubsub/pubsub-scopes`: *"If a topic is marked as
  protected then an application must be explicitly granted publish or subscribe permissions through
  `publishingScopes` or `subscriptionScopes`"*, with the doc's own example spelling out that an
  unlisted `app3` "cannot interact with these topics". **`publishingScopes` ALONE does not deny** — the
  same page says an unspecified field means "all apps can publish to all topics" — so `protectedTopics`
  is the half that makes it deny-by-default and omitting it would be the third control-that-cannot-fire
  in this row's history.
- *NOT IMPLEMENTED TONIGHT, and the reason is this row's own subject.* Writing `publishingScopes`
  requires the exact set of app-ids that genuinely publish `lineage.events.v1`, and naming it wrong
  silently stops a producer's provenance — the failure [[LH-141]] exists for. A grep for publish call
  sites returns twelve modules across control-plane, work-queue and lineage topics together and does
  not separate them. **The one remaining measurement is: which of the eight scoped app-ids publish to
  `lineage.events.v1` specifically** — answerable from the live bus or by reading each publish's topic
  argument, and it must be answered before the scopes are written.
- **THAT MEASUREMENT IS DONE 2026-09-16, and the env scan alone would have got it WRONG.** Reading each
  publish site's topic argument, not just the env:

      PRODUCERS of lineage.events.v1   catalog (LANCE_DAPR_TOPIC default), maintenance,
                                       medallion-producer, bronze-to-silver, silver-to-gold,
                                       media-to-silver, and **lineage**
      CONSUMER-ONLY                    notifications

  **`lineage` is a producer and no environment variable says so.** `api/reconcile_cron.py:470-474`
  publishes to `settings.dapr_pubsub` / `settings.dapr_topic` — the outbox relay's drain, which
  `main.py:114` explains: "The drain re-publishes a recovered event so a subscriber that never saw it
  still acts on it — without this the relay repairs the GRAPH while the cascade it was meant to restart
  stays halted." A scopes file written from the env pairs would have denied that republish and left the
  relay repairing the graph while the cascade stayed stopped — the exact silent-provenance failure this
  row was told to avoid.
- *So the change is smaller and sharper than "name the producers":* every component is ALREADY scoped to
  exactly one app-id, so `publishingScopes` cannot restrict which APPS publish — it restricts which
  TOPICS each app may publish. The one app it would newly deny is `notifications`, and the real value is
  deny-by-default for the NINTH app-id somebody adds later, which today would silently inherit publish
  rights on the provenance bus.
- **THE HAZARD, named before anyone writes it:** `protectedTopics` gates PUBLISH *and* SUBSCRIBE ("an
  application must be explicitly granted publish or subscribe permissions"), and these per-app
  components are the SUBSCRIBER components — `queueGroupName` lives on them. A component that protects
  the topic and omits its app from `subscriptionScopes` silently stops delivering to that app, on the
  bus that carries every provenance record and the whole cascade. So the edit is per-component and needs
  both directions, plus a gate that derives the expected scopes from the same template data the
  subscriber list is built from, plus a live check after the roll (a sweep tick publishes, lineage
  ingests, the notifications inbox receives).
- **SHIPPED 2026-09-16, once the directions were measured off the running sidecars rather than the
  templates.** `/v1.0/metadata` on each app container, which corrected the truth table twice: the three
  stage runners subscribe to `medallion.bronze|silver|media` and NOT to the provenance topic, and
  **`maintenance` registers no subscriptions at all** (its write-event lane is gated on `workTopic`,
  unset here) while the template comment describes one. Rendered result:

      lineage-pubsub                     protect=lineage.events.v1  pub=catalog;maintenance   sub=-
      lineage-pubsub-lineage             protect=lineage.events.v1  pub=lineage               sub=lineage
      lineage-pubsub-medallion-producer  protect=lineage.events.v1  pub=medallion-producer    sub=medallion-producer
      lineage-pubsub-bronze-to-silver    protect=lineage.events.v1  pub=bronze-to-silver      sub=-
      lineage-pubsub-silver-to-gold      protect=lineage.events.v1  pub=silver-to-gold        sub=-
      lineage-pubsub-media-to-silver     protect=lineage.events.v1  pub=media-to-silver       sub=-
      lineage-pubsub-notifications       protect=lineage.events.v1  pub=-                     sub=notifications

- **AND IT WAS REVERTED WITHIN THE HOUR, because rolling it BROKE DELIVERY. The live estate refuted a
  reading of the Dapr documentation, and this is the finding worth keeping:**

      Retry failed for subscription pubsub lineage-pubsub-notifications, topic dlq.notifications:
      subscription to topic 'dlq.notifications' ... is not allowed

  `dlq.notifications` was **never in `protectedTopics`**. **`subscriptionScopes` is NOT ADDITIVE:**
  naming an app in it makes that list the app's COMPLETE allowlist for that component, including topics
  the protection never mentions. The docs' framing — "if a topic is marked as protected then an
  application must be explicitly granted" — reads as though unprotected topics are unaffected. They are
  not. Measured before the revert: **47 denials on medallion-producer, 16 on notifications, 4 on
  lineage**; after reverting the template, re-applying and restarting the eight app-ids, **0 denials in
  4 minutes** and notifications' three subscriptions all back.
- *My gate asserted the wrong invariant, which is why it passed.* It checked that no topic other than
  `lineage.events.v1` appeared in any `protectedTopics` — true, and irrelevant: the harm came through
  the SCOPES list, not the protection list. A blast-radius test has to bound the mechanism that can
  actually do harm. The test is deleted rather than left encoding a rule that is wrong.
- *What a future attempt needs, recorded in the template itself so it is found at the edit site:*
  enumerate EVERY topic each app uses on its component, in BOTH directions — measured off
  `/v1.0/metadata` and the publish call sites — not merely the one topic being protected. The scoping
  the estate already has is the document-level `scopes:`, which closes each component to one app-id and
  is what makes an arbitrary pod unable to publish at all.
- **RE-SHIPPED NARROW AND VERIFIED LIVE 2026-09-16 — ONE component, not eight.** The goal was always
  the one consumer-only identity, so the change is applied to `lineage-pubsub-notifications` alone with
  its FULL measured topic list in both directions:

      protectedTopics    lineage.events.v1
      publishingScopes   notifications=dlq.notifications          <- provenance withheld
      subscriptionScopes notifications=dlq.notifications,lineage.events.v1

  The dead-letter publish grant is deliberate: notifications has no publish call sites of its own, its
  sidecar parks on `dlq.notifications` via `deadLetterTopic`, and whether THAT publish is scope-checked
  is undocumented — withholding it would be the same guess that broke delivery the first time.
  **Verified on a genuinely fresh pod** (created 17:41:37, probed 17:41:48, so it loaded the scoped
  component rather than the cached one — `HotReload: false` means an older pod proves nothing): all
  three subscriptions registered, **zero scope denials**, and the untouched app-ids unchanged.
- *The gate now pins the invariant that BROKE, not the one the first test checked:* every topic the app
  subscribes to must appear in its `subscriptionScopes`, the provenance topic must appear in its
  publishing scope nowhere, and — the real blast-radius bound — **no sibling component may carry these
  keys** until its own full topic list has been measured the same way.
  `tests/unit/test_the_inbox_may_read_the_provenance_bus_but_never_write_it.py`.
- *Still open on this row:* the producer signature over the CloudEvent (`_StampedAuthor`'s own docstring
  — "nothing proves the stamp"), which is transport-independent and survives a Dapr retreat. The
  seven producers keep unrestricted publish on their own components, which is correct for them and is
  what the remaining signature work is for.
- *Closes when (rewritten):* `protectedTopics: lineage.events.v1` plus a `publishingScopes` naming the
  measured producer set on the lineage pub/sub components, a producer signature verified in the bus
  door (still genuinely open — `_StampedAuthor`'s own docstring says "nothing proves the stamp"), and
  the subject stamped through `enforce_output_authz` (DONE, `35fcabb4`). **Do NOT add an `accessControl`
  block: it governs service invocation and would never see this traffic.**

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

**LH-070 · ~~No versioned authz-model migration (`ACTIVE_MODEL_VERSION` + an idempotent `migrate()`) — the 3-axis model shipped without it~~ — CLOSED 2026-09-14 (OBSERVED ON THE ROLL)**
`service-kit, catalog` · med

- **THE IDEMPOTENT HALF LANDED 2026-09-13, and the measurement is the reason it mattered.** `provision`
  wrote the model unconditionally once the narrowing guard passed, and OpenFGA has no update for a model
  — every write mints a new immutable version. `provision` has exactly ONE non-test caller, the
  catalog, and `tests/unit/test_only_one_service_may_publish_the_authorization_model.py` fails the suite
  on a second — so every one of that single service's boots added a version. Paged out of the live store 2026-09-13:
  **1,316 authorization model versions**, for a `model.json` that has changed a handful of times — counted twice by different routes that agree: paging `GET /stores/{id}/authorization-models`, and `SELECT count(DISTINCT authorization_model_id) FROM authorization_model` against the store's own Postgres.
- *Why that is not merely untidy:* the store's "latest" model is whichever pod booted last, which is the
  value [[LH-139]]'s narrowing guard reads to decide whether a boot is a rollback — churn is the
  substrate that defect lived on — and it leaves an operator asking what the estate's authorization
  model says with 1,316 candidates to diff.
- **THE COMPARISON HAD TO BE PROVEN REACHABLE FIRST, because the obvious one cannot fire.** OpenFGA does
  not store the model it is given: it materialises `metadata: null`, `relations: {}`, `module: ""`,
  `condition: ""` and `source_info: null`, so the stored form is 36,482 characters against 24,579
  authored and a direct equality check answers "different" forever. Dropping null/empty values makes
  them byte-identical — 20,310 characters each for the live store's newest model and this repo's
  `model.json`, measured through the SDK deserializer `_canonical_model` is actually handed. **That
  equality did NOT hold as first shipped**, and the next entry is why: the original measurement used the
  REST JSON on both sides, which is not what the function receives. RED-first, 6 tests, two of which pin that the canonical form is neither always-equal
  nor never-equal. **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live.
- **THE SKIP SHIPPED UNABLE TO FIRE, and an adversarial pass caught it (`12a77138`).** `_plain`
  called the SDK's `to_dict()`, and openfga_sdk's generated models render PYTHON attribute names unless
  asked otherwise — `attr = self.attribute_map.get(attr, attr) if serialize else attr` — so `to_dict()`
  yields `computed_userset` while `model.json` and the wire say `computedUserset`. The two strings could
  never be equal; every boot still minted a version.
  *The original verification compared `model.json` against the REST API JSON.* Both are camelCase, so
  both agreed at 20,310 characters — but the code receives the SDK OBJECT, not the REST JSON. It
  verified a different thing than the code does, which is the whole lesson: the 20,310 match was real
  and measured the wrong pair.
  *And the test could not have caught it* — `_StoredModel.to_dict()` returned the authored dict
  verbatim, agreeing by construction. It now builds real SDK objects. Fixed by asking for the wire
  spelling; re-verified against the live store's model through the real deserialization path, equal at
  20,310 where it was unequal.
- **OBSERVED WORKING 2026-09-14 — this row is closed by the roll.** The catalog booted on
  `main-94e88b35` and logged
  `openfga_model_unchanged store_id='01KYPGG8F8MAZTJANME4K077DE' model_id='01M2946MMRYAXF9KMQD93ZA7FH'`,
  keeping the store's existing model instead of minting one. Counted immediately after that boot: the
  store still holds **exactly 1,316** authorization model versions — the same number measured before
  the fix, across a fresh boot that would previously have written the 1,317th. The skip fires through
  the real SDK path, which is precisely what the original (wrong-pair) verification could not show.
- *Deliberately NOT an `ACTIVE_MODEL_VERSION` constant:* a hand-maintained version is one somebody
  forgets to bump, and the model's own canonical form answers the same question without a second source
  of truth. Everything else is unchanged: a widened model still writes, a narrowing one is still refused
  ahead of this check, and a new store still writes without a read.
- *Closed by:* the roll. The catalog logged `openfga_model_unchanged` on its boot and the store held
  exactly 1,316 versions afterwards — the same count as before the fix, across a boot that would
  previously have written the 1,317th. Both halves of the criterion below, met.
- *The criterion was:* the roll observes `openfga_model_unchanged` on a boot and the store's version count
  stops climbing. If the owner still wants a recorded ACTIVE version pinned for production rollout
  (distinct from the churn this fixed), that is the remaining half.

**LH-071 · ~~Tuple helpers were never split into `tuples.py` and there are no golden tuple tests~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* Both of the row's stated reasons are false at HEAD: grant_on_create is NOT one inline grant (tuple construction is already factored into a shared seam with an invariant gate on its callers), and golden tuple tests asserting exact (user, relation, object) triples already exist in two suites.
- *Evidence:* THE SEAM: packages/service-kit/src/service_kit/governed/fga.py:125-153 `hierarchy_edge_tuples(child_object, parent_object, parent_relation)` returns both directions of the link; its caller set is closed and pinned by tests/unit/test_invariants.py:935-946 test_only_the_sanctioned_writers_seed_a_hierarchy_edge. fga.py:1312-1341 grant_on_create composes owner + hierarchy tuples through it and delegates to write_tuples — it constructs no edge itself. GOLDEN TESTS: packages/service-kit/tests/test_fga_edges.py:76-215 asserts the exact tuple keys for grant_on_create (parent edge + inverse in one batch :76; no edges for a root object :98; inverse skipped where the model declares no child :105; non-default parent relation :186; built on the shared pairing :194) and for revoke_object_tuples (:134, :151); tests/unit/test_a_machine_created_table_is_owned_by_its_project.py:52-95 captures the tuples one table create ACTUALLY writes, at write_tuples rather than at the call site, precisely so a threaded-then-ignored flag cannot pass.
- *What would reopen it:* Showing grant_on_create builds its edge tuples inline rather than via hierarchy_edge_tuples, or that test_fga_edges.py asserts call shape instead of tuple content. WHAT IS ACTUALLY LEFT is smaller and different: no module literally named tuples.py exists, and the raw grant/revoke door still composes a ClientTuple inline at services/catalog/src/catalog/api/v1/endpoints/access.py:350 and :521 with no golden test of its own — that narrower item is worth keeping, the row as written is not.

**LH-072 · The vended response still mixes credentials and config in one dict**
`catalog, storage` · med

- *Why open:* Half of the study's #2 landed per-vendor (`expires_at_millis`); the split never shipped, so a client cannot tell which fields are secret and which are configuration.
- *RE-MEASURED 2026-09-13 — the premise HOLDS and the worst reading of it does NOT.* `VendedCredentials`
  is still one mapping (`storage_options: dict[str, str]`, `vending.py:49`) carrying the key, the secret
  and the endpoint/region/allow_http config together. But the serious version of this row would be a
  live secret leak, and it is not there: searched for the vended dict reaching a log call, an
  `extra={...}` or an f-string across `services/` and `packages/` and found NO path that logs
  `storage_options` or a vended credential. So this is a contract-and-redaction improvement, not an
  incident.
- *AND THE SPLIT HAS A COST THE ROW DOES NOT WEIGH.* The single dict is the shape the CONSUMER wants —
  `vending.py:44` records it: "`storage_options` is consumed directly by pylance / lance-ray /
  object_store". Those libraries take one mapping, so every caller would merge the two objects straight
  back before the call, and the merged dict at the call site is exactly where a secret would land in a
  log. A split that only moves the merge into every caller buys the taxonomy and loses the guarantee;
  what would actually pay is a type that KNOWS which keys are secret and renders them redacted, with
  one merge inside it.
- *Closes when:* Split the vended response into `credentials` and `config` objects across the vendors,
  updating the generated clients that consume it — and decide against the above whether the split is
  the shape that helps, or whether a redacting container over one mapping is.

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
- **THE RETENTION HALF IS NOW VERIFIED LIVE, closing this row's own reopen condition.** It said the
  applied TTL was "UNVERIFIED here beyond the in-repo observation note". Queried against the running
  GreptimeDB 2026-09-16: `SHOW CREATE TABLE opentelemetry_logs` carries **`ttl = '14days'`**. The trail
  does not grow unbounded, in fact and not only in the chart.
- **AND THE INDEX HALF NAMES A COLUMN THAT DOES NOT EXIST.** `DESC TABLE opentelemetry_logs` returns 14
  columns — `timestamp, trace_id, span_id, severity_text, severity_number, body, log_attributes,
  trace_flags, scope_name, scope_version, scope_attributes, scope_schema_url, resource_attributes,
  resource_schema_url` — and **none of them is `dataset`**. The audit's dataset is a key INSIDE the
  `log_attributes` JSON column, so "an index on the dataset column" cannot be written as stated.
- *What the numbers say the fix actually is* (measured live): the table holds **105,390,492 rows**, of
  which **6,463,147 are `lance.audit`** — 6.1%. So the cheap, real win is an index on `scope_name`,
  which is the only first-class column separating the audit stream from the other 99 million rows; a
  per-dataset filter costs a JSON extraction over whatever that leaves. Indexing the dataset itself
  first requires PROMOTING it out of `log_attributes` into a column, which is an OTel Collector
  transform, not a DDL hook.
- *Closes when:* an index on `scope_name` lands in the same hook shape as `greptimedb-ttl-job.yaml`,
  and a ruling on whether the audit's dataset is promoted to a real column (Collector transform, then
  index it) or left as a JSON key filtered after the scope narrowing. The retention clause is DONE and
  verified live; do not re-work it. The separate point this row already records — that 14d is short for
  a compliance trail and is estate-wide — remains its own item ([[XC-003]]).

**LH-076 · `can_observe_events` is the estate-admin bar under a name that says 'read the feed', and its comment names one of its four consumers**
`catalog, service-kit` · med · **blocked:** the comment half is unblocked; the rename half needs an owner ruling on repointing live checks + reseeding

- *Why open:* Verified still open at `service_kit/governed/auth/model.fga:197-200`: the comment documents only the `/v1/events` feed, while tenant minting (`endpoints/projects.py`), every raw tuple route (`access_admin.py:199`, router-wide `require_relation(..., "can_observe_events", settings.fga_root_object)`) and store registration all gate on it. No `can_administer_estate` exists.
- *Closes when:* Rewrite the comment above `define can_observe_events: owner` to enumerate all four gated operations with their call sites; then, separately, decide whether to add a distinctly-named `can_administer_estate` that `projects.py`, `access_admin.py` and `POST /v1/stores` alias to.

**LH-077 · `alter_transaction` gates a whole `AlterTransactionRequest` at one committer-tier check while the model claims a per-action distinction**
`catalog, service-kit` · med · **blocked:** owner ruling on per-action vs one-check authorization (the delete path also needs the `fga` CLI, which 403s through the sandbox proxy)

- *Why open:* `model.fga:443,445` still define `can_set_property: editor` and `can_cancel: committer` as removal candidates awaiting this decision, and `fga_deps._authorize_transaction` (`api/fga_deps.py:381`) picks between exactly `can_describe` and `can_set_status` — so anyone who can commit can also edit properties, and the model claims a distinction nothing enforces.
- **BOTH PREMISES RE-MEASURED 2026-09-11; one citation is stale and the tooling obstacle is not what it
  says.** The substance holds: `can_set_property: editor` and `can_cancel: committer` are still defined
  and still referenced by nothing that authorizes per action — but at `model.fga:492,494`, not the
  `443,445` this row cites, which now hold `can_revoke_grant` / `can_read_assignments`. A reader
  checking the cited lines finds unrelated relations and can reasonably conclude the row is stale when
  it is not.
- *The `fga` CLI is present and runs:* `.localbin/fga` answers `v0.6.4`. It is not on PATH, which is
  what "needs the fga CLI" reads as from a shell. The recorded obstacle — "403s through the sandbox
  proxy" — is about network EGRESS, and the store is an in-cluster `ClusterIP` (`rask-openfga`,
  8081/8080/3000), so a port-forward is the path this row has not tried rather than a wall. Worth one
  attempt before the tooling half is treated as blocking anything.
- *Closes when:* Either extend `endpoints/transactions.py`'s `alter` route to authorize per state-action (making `can_set_property`/`can_cancel` real doors), or delete both lines from `model.fga` with `fga model test` green — noting that deleting the `editor` rung is a second decision, since `viewer` inherits from it and it carries its own direct-grant slot.

**LH-078 · ~~8 credential vends per tick still 403, so 8 rewrites sign with the ambient root key, and no counter or alert surfaces it~~ — CLOSED AND OBSERVED 2026-09-11**
`maintenance, catalog` · low

- **CLOSED BY THE DEPLOY, both halves, observed on `main-b641103f`.** Sampling every
  `maintenance.services.credentials` record in a six-minute window post-deploy:

      356 of 356 SCOPED        0 AMBIENT        0 x 401

  Against 300 x 401 / 300 x AMBIENT / 0 SCOPED before. So no rewrite signs with the ambient credential
  any more — the row's first half. A vend that IS refused now stops the dataset through
  `maintenance_vend_denied` (27 in the window) rather than falling through, which is exactly the
  behaviour this row asked for instead of the ambient fallback.
- *And the second half landed separately* (`eb564cd6` + `4fcba209`): the split is a series
  (`compaction_credential_tier_total`, one counter with a `tier` attribute) plus an alert proven to fire
  on an all-ambient series and proven not to on this row's own healthy posture.
- *The cause was never "8 unregistered tables":* it was the dedicated service token being derived from a
  value no values file defined, so every privileged claim was refused ([[LH-142]]). Fixing the derivation
  fixed the population.
- *Why open:* The headline is fixed (207 AMBIENT → 8, 277 SCOPED, via `_ALTERNATIVE_RUNGS` giving the `credentials` action a second rung), but the remaining 8 are a different population: the 92 per-warehouse `maintainer` tuples cover every warehouse in the registry, so these are tables the registry does not account for. The ambient fallback is loud in the pod log and reaches no report field, counter or alert.
- **THE SECOND HALF LANDED 2026-09-11** (`eb564cd6` + `4fcba209`): the AMBIENT-vs-SCOPED split is a
  series (`compaction_credential_tier_total`, one counter with a `tier` attribute so the number is the
  RATIO) and an alert that pages when most rewrites go ambient. Proven to fire on a synthetic all-ambient
  series and proven NOT to on this row's own measured healthy posture (8 ambient against 277 scoped).
  What remains here is the first half — identifying the tables the registry does not account for.
- *And this row's "207 AMBIENT → 8" no longer describes the estate:* measured 2026-09-11 it is EVERY
  vend, on a 401 rather than a 403, which is a different failure and is tracked as [[LH-142]].
- *Closes when:* Identify the 8 tables whose `POST /v1/table/{id}/credentials?tier=write` still 403s despite `can_maintain` and either register them or make the vend failure refuse the rewrite instead of falling back to the ambient key; and expose the AMBIENT-vs-SCOPED split as a counter/alert rather than only a log line.

**LH-079 · Two standing answers on the `x-api-key` principal contradict each other, and its key store and rotation model are undesigned**
`catalog, gateway` · low · **blocked:** owner decision reconciling Q7 with A6

- *Why open:* Q7 was decided 2026-09-02 (support both spec identity headers; keys minted, scoped and revoked by the management API as FGA principals with an expiry) — but A6 later measured that the spec's `security` block is a DISJUNCTION, that bearer alone is conformant (155 of 160 ops declare it; 401 with no credential and 401 with `x-api-key` alone), and struck the work as a second credential plane against the secret-store-only rule. Meanwhile no key store or rotation model exists.
- *Closes when:* Owner rules whether Q7's api-key principal is withdrawn in favour of A6's bearer-only position or the management API mints scoped, expiring keys after all; edit the losing row out rather than leaving both, and if it survives, write the key-store and rotation design into the management API RFC.

**LH-139 · ~~A catalog boot REWRITES the estate's authorization model from its own bundled copy, so an older image silently REMOVES relations and breaks every door that uses them~~ — CLOSED 2026-09-15: a narrowing FGA model is refused rather than written**
`catalog, service-kit, chart` · **HIGH** · found and measured 2026-09-11 while a helm upgrade was blocked by it


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15: `fga.py:548-560` reads the store's current model, computes `_narrowings`, and on any removal
  logs `openfga_model_narrowing_refused` and RETURNS the store's model without writing. :573 also short-circuits an
  unchanged model. `event_stager`/`can_stage_events` are present in the shipped `model.json`.

- **CODE LANDED `45e7a155`** — `provision()` reads the live model under `_guarded` and refuses a boot that
  would REMOVE a relation, logging `openfga_model_narrowing_refused` with the removed set. Verified on
  origin. **Not yet observed: rides the pending roll**, so this row stays open until a deployed boot proves it.
- **CONFIRMED LIVE 2026-09-11 — this row is not a hazard, it is currently blocking a deploy.** Ran
  `make k3s-up`; the upgrade stalled at rev 151 with `rask-bootstrap-admin` crash-looping on
  `Invalid tuple 'warehouse:lance_catalog#event_stager@user:service-ingest'. Reason: relation
  'warehouse#event_stager' not found`. Measured both copies of the model:

      model.fga at HEAD                : event_stager present (2 occurrences)
      deployed catalog's bundled copy  : 0

  So the running catalog (`main-8c229296`) rewrote the store's model WITHOUT `event_stager` at boot, and
  the bootstrap job then cannot write a tuple that needs it. Exactly the mechanism this row describes,
  observed rather than reasoned.
- *AND IT MAKES A CHART-ONLY DEPLOY STRUCTURALLY IMPOSSIBLE, which this row does not yet say.*
  `make k3s-up` reuses the live/pinned image tags, so the stale catalog boots again and reverts the model
  again — the upgrade cannot converge no matter how many times the chart is applied. Recovering needs a
  catalog IMAGE carrying the newer model, which is what the 2026-09-10 recovery did and why it read as a
  one-off rather than as this row firing.
- *Which is the sharpest argument for the row's own fix:* a boot-time model write that can REMOVE
  relations makes image order load-bearing for the whole estate's authorization, and the failure surfaces
  as an unrelated job crash-looping rather than as anything naming the model.

- *Why open:* `fga.provision` (`service_kit/governed/fga.py:319-348`) writes `load_model()` — the IMAGE's
  bundled `model.json` — on every boot where `RASK_FGA_STORE_ID`/`RASK_FGA_MODEL_ID` are unset, and the
  catalog is the one service that calls it with `provision=True` (`catalog/main.py:140`). The write is
  unconditional: nothing compares the model being written against the one the store already has, so a
  catalog on an OLDER image does not fail, it wins, and the store's newest model is the oldest pod's.
- *MEASURED ON THE LIVE ESTATE 2026-09-11, not argued.* The store's three most recent models:
  `01M28RPM2P8T9QXRBS0DRT4GVC` (latest) has neither `warehouse#event_stager` nor `warehouse#can_stage_events`;
  `01M28Q0X3KAQ6Z701486F6DG42` and `01M28PDH89FKX676KYZRQXSFWY` before it have both. The running catalog
  pod's own bundled copy was read in place and reports `event_stager: False`, so the regression is the
  image's, not a write that went wrong. One `Check` call states the cost twice:

      latest model      -> {"code":"validation_error","message":"object relation does not exist"}
      01M28Q0X… (good)  -> {"allowed":true,"resolution":""}

  for `user:service-ingest # can_stage_events @ warehouse:lance_catalog` — the relation gating the
  outbox staging door. It does not DENY, it ERRORS, which the fail-closed wrapper turns into
  "authorization service unavailable" for every caller of that door.
- *How it got there, because the trigger is mundane and will recur:* `chart/values-live-pins.yaml` is a
  snapshot of what the cluster was running ("GENERATED … not what anyone intended. Regenerate after
  every build+roll"), it was not regenerated after the day's builds, and `make k3s-up` layers it OVER
  the live values — so a routine upgrade rolled the catalog from `main-6fc3748c` back to
  `main-8c229296` and the rollback took the authorization model with it. The visible symptom is
  `rask-bootstrap-admin` crash-looping on `Invalid tuple … relation 'warehouse#event_stager' not found`,
  which blocks `helm upgrade --wait-for-jobs` and has now cost two sessions; nothing names the cause.
- *Why the docstring's answer is not sufficient:* it says "for dev / e2e; in production pin
  `RASK_FGA_STORE_ID` + `RASK_FGA_MODEL_ID`", and that is right for production. It leaves dev with a
  mechanism where deploying an older image silently rewrites who may do what — the estate's own
  signature failure, a control that is not wrong so much as pointed at the wrong authority.
- **THE CODE HALF IS DONE 2026-09-13, RED-first.** `provision` now reads the store's current model and
  refuses to write one that removes a type or a relation, keeping the existing model id and logging
  `openfga_model_narrowing_refused` with what would have been lost. Additions still write, because
  `provision` exists so a `model.json` edit takes effect and a guard that blocked those would freeze
  the estate at whatever the first pod shipped.
  *The read is where the guard gets its teeth, and the first version had none.* Answering `None` on a
  failed read would let a flaky OpenFGA wave a narrowing model through — exactly when a boot storm is
  likeliest. It now runs under the module's own posture (`_guarded`: retry, then fail closed), so an
  unverifiable store raises, `auth_lifespan` builds no client, and the governed routes answer 503: an
  estate that cannot verify its own model does not get to overwrite it. A store created moments ago
  skips the read entirely, so a first boot never depends on a model that cannot exist yet.
  Five tests in `test_a_boot_cannot_narrow_the_estates_authorization_model.py`: a removed relation, a
  removed type, an unreadable store — plus the two controls that keep this from becoming a guard that
  refuses everything (an ADDING model still writes; a fresh store still writes).
- *RE-MEASURED 2026-09-13, and the mechanism was unchanged at HEAD:* `provision` still called
  `load_model()` and wrote unconditionally ("(re)written each time" in its own docstring), the catalog
  still passed `provision=True` (`catalog/main.py:140`), and the deployed catalog still has
  `RASK_FGA_STORE_ID` and `RASK_FGA_MODEL_ID` UNSET with `RASK_FGA_ENABLED=true` — so every boot really
  was rewriting the estate's model from that image.
- **THE VALUES HALF WAS NOT SAFE TO TAKE, and that is now fixed rather than argued.** Pinning works by
  skipping `provision` outright (`auth_lifespan.py`: `if not (store_id and model_id)` wraps BOTH the
  provision and resolve branches), so a pinned boot never calls `load_model()` at all. An operator who
  adopted the prescribed posture and later edited `model.fga` would ship an image whose model takes
  effect NOWHERE and says so nowhere — the same door answering "object relation does not exist", the
  same "authorization service unavailable" the fail-closed wrapper renders, from the opposite
  direction. Measured 2026-09-13: `chart/values.yaml:925-926` ship `fgaStoreId: ""`/`fgaModelId: ""`,
  the live catalog has both env UNSET, and NO shipped values file sets either — so the row's own
  prescription was the one configuration nobody could adopt without re-creating the defect.
  `fga.audit_pinned_model` now reads the pinned model through the already-pinned client and compares it
  to the image's bundled copy, logging `openfga_pinned_model_differs_from_image` with three SEPARATE
  fields — `absent_from_pin`, `absent_from_image`, `redefined` — because a rollback and a roll-forward
  need different answers and one merged list cannot tell them apart. It REPORTS and never refuses: a
  pin is a deployment decision, and crash-looping on one helps nobody. It is deliberately the one model
  read in the module NOT under `_guarded` — `_current_model` gates a WRITE, where failing closed stops
  a narrowing model reaching the store, while this gates nothing, and taking a serving estate down
  because a diagnostic could not run is the wrong trade. Gated on `provision=True` so the nine services
  sharing the bundled model do not report the same divergence nine times.
  RED-first, 8 tests, mutation-proven in both halves: removing the `auth_lifespan` call reds the wiring
  test, removing the comparison reds three rule tests. **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live.
- *Closes when:* the above ships AND an owner takes the values decision — which is now a decision
  rather than a trap. **NOT TAKEN HERE, deliberately:** whether to pin, and whether
  `scripts/k3s-pins.sh` should capture the live store/model ids beside the image tags (it is the
  capture tool `make k3s-up` already layers over live values, so adding them there IS the pin), is an
  operator posture, not a code fix. The guard makes a rollback loud; the audit makes a pin loud;
  pinning is what stops the write happening at all.

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

**THE WORKFLOW-ENGINE HALF OF THIS CONDITION MEASURES AS HOLDING (2026-09-11), and it had no row either
way.** Across catalog, lineage, medallion and maintenance: **zero** Ray imports and **zero** `ray` /
`ray-kit` declared dependencies. `dapr-ext-workflow` is declared by `medallion` alone, and both places
that use it start a runtime behind a flag — `producer.py:119` under `quality_review_enabled or
ray_enabled`, `stage_runner.py:91` under `ray_enabled`, whose own comment says "Only started when the
Ray lane is on, because that is the only lane with a job to wait for". Both imports are lazy and inside
a `try/except`, and the cascade's workflow dispatch sits under `transform.py:823`'s `if use_ray`. So
bronze→silver→gold runs in-process with Ray and Dapr Workflow both off: driven BY, not dependent ON.
What stays open in this section is the ENGINE half — [[LH-083]], where `executor_for` has no production
caller — and the latent gap below.

**LH-147 · ~~`HOSTED_ENGINES` is a constant, so the refusal for "an engine this deployment does not host" cannot fire for Ray — a declared Ray task on a Ray-OFF deployment enqueues a workflow nothing will execute~~ — CLOSED 2026-09-15: a declared Ray task is refused where no Ray runtime runs**
`medallion` · med · found 2026-09-11 while measuring condition 3 · **LATENT, not live**


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15: the `HOSTED_ENGINES` constant is gone; `engine_choice.py:63-81` has `hosted_engines(settings)`
  returning in-process only when `ray_enabled` is false, and `engine_for` (:99-116) raises `UnrunnableTaskError`
  distinguishing 'no adapter in this build' from 'this build knows it, MEDALLION_RAY_ENABLED is off'. Three tests pin the
  relation rather than equality. Rolled in `main-2a5b8c64`; it changes no live behaviour today because all four medallion
  workloads run `MEDALLION_RAY_ENABLED=true`, which is why the roll is the whole of the remaining condition.

- *The chain, read out of the code:* `engine_choice.engine_for` honours the chart only when there is no
  declaration — `chosen = RAY_ENGINE if settings.ray_enabled else IN_PROCESS_ENGINE` (:74). With a
  DECLARED spec it returns `registration.engine` (:85), gated solely by
  `HOSTED_ENGINES: Final = frozenset({RAY_ENGINE, IN_PROCESS_ENGINE})` (:52) — a static constant that
  contains Ray whether or not this deployment runs the Ray lane. So a task registered for `ray` passes
  the check, `use_ray` is true at `transform.py:821`, and `_dispatch_stage_workflow` runs.
- *Why that is worse than an error:* the dispatch builds its own `DaprSagaClient`, which only ENQUEUES.
  The runtime that registers the definitions and pulls work is what `stage_runner.py:91` starts, and
  only `if settings.ray_enabled`. The stage is therefore scheduled and never executed — no failure, no
  DLQ, no refusal. `stage_runner.py:84` records this exact asymmetry from ingest's first in-cluster
  deploy: "the engine running in the sidecar and still could not run a workflow because the APP side was
  absent — an asymmetry that looks healthy from every angle except an actual run."
- *The guard at `:78` is written for precisely this case* — "refusing rather than running it on whichever
  engine happens to be configured here" — and cannot fire for Ray, because hosting is asserted by a
  constant rather than read from the deployment. A control that cannot fire, in the file whose job is
  choosing.
- **LATENT HERE, and stated that way rather than as an incident.** Measured on the live estate:
  `RAY_ENABLED=true` on all four medallion workloads (`bronze-to-silver`, `silver-to-gold`,
  `media-to-silver`, `medallion-producer`), so the runtime is started everywhere and nothing is
  currently stranded. It bites a Ray-OFF deployment — which is exactly the configuration this condition
  says must work.
- **LANDED 2026-09-14, and the root is one flag doing two jobs — which this row did not say.**
  `ray_enabled` is BOTH the chart's default engine for an estate that has declared nothing AND whether
  this pod starts the Ray workflow runtime. Only the second may gate a declaration, and conflating them
  is why the constant looked defensible. `HOSTED_ENGINES` is now `KNOWN_ENGINES` — what this BUILD
  carries adapters for, a ceiling rather than an answer — and `hosted_engines(settings)` narrows it to
  what the deployment runs. In-process is always hosted (it needs no runtime; `transform.py` calls it
  directly), so a Ray-OFF estate is not an estate that can run nothing. The refusal names the LEVER
  rather than only the hosted set: an engine no adapter answers to and one this build knows but the
  deployment turned off are both operator errors no redelivery fixes, but the fixes differ. It stays
  `UnrunnableTaskError`, an `UndeclaredTransformError` subclass caught at `transform.py:469` — DROP
  with a trace, never RETRY.
  *`engine_registry.hosted_engines` stays constant and is compared against the BUILD ceiling.* Their
  EQUALITY was the wrong relation and is what made a Ray-OFF deployment unrepresentable; choosable ⊆
  resolvable is the property worth holding.
  *THREE EXISTING TESTS ASSERTED THE DEFECT OR LEANED ON IT*, each corrected rather than deleted: the
  declared-transform test pinned "a declared ray task on `ray_enabled=false` returns RAY" (and its
  docstring described the first assertion while calling it the second), and two chooser tests declared
  ray while leaving `ray_enabled` at its default `False` — exercising this very path under another
  name. RED-first, 7 new tests, mutation-proven: restoring the constant reds three.
  **DEPLOYED 2026-09-16** (rode `main-16dd2da6` / `main-17e41ffb`, helm 160/161). Verified by reading the RUNNING pods rather than inferring it from the tag: the deployed source of catalog, lineage, medallion and maintenance is byte-identical to HEAD (md5 of each module's file inside the container against `git show HEAD:<path>`), so every fix committed before HEAD is live. Still LATENT there: all four medallion workloads
  run `MEDALLION_RAY_ENABLED=true` (re-measured 2026-09-13), so the roll changes no live behaviour —
  it makes the Ray-OFF configuration honest.
- *Closes when:* the roll observes it. There is nothing further to build.

**LH-083 · ~~`engine_registry.executor_for` has ZERO callers — the deployed stage lane still calls `ray_submit` directly at `workflow.py:495`~~ — CLOSED 2026-09-15: one submission path, and it is the one Flyte sanctions**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The bypass is real and unchanged at workflow.py:495, but the row's second closing action is already done — `ray-kit` is no longer a medallion dependency.

- **CLOSED 2026-09-15 (`b3a10799`), deployed on `main-b3a10799` and observed.** The `RayJobExecutor`
  and its RayJob-CR path are deleted, with everything that named them: the `RAY` branch of
  `executor_for`, two test files, `chart/templates/medallion-rayjob-rbac.yaml` and its only gate
  `medallion.rayJobSubmission`. 697 lines. Observed after the roll: all three stage runners `2/2
  Running`, zero `ModuleNotFound`/`ImportError` across their logs. The chart half is provably inert on
  this release — `helm get manifest` never rendered that Role and `helm get values` never set the flag.
- **THE DECISION WAS CHECKED AGAINST FLYTE AFTERWARDS, and the check is why this row can be struck
  rather than merely closed.** Flyte is the reference workflow engine driving Ray, so "what does it do"
  is a source question, not a preference. Read 2026-09-15 from `flyteplugins/go/tasks/plugins/k8s/ray/`,
  `flyteidl2/plugins/ray.proto` and `flyte-sdk/plugins/ray`:
  * *Flyte creates a RayJob CR* — `constructRayJob` returns `&rayv1.RayJob{...}`; there is no `net/http`
    import and 8265 appears only as a container port. Status comes from watching
    `rayJob.Status.JobDeploymentStatus`, never from polling the dashboard.
  * *But the CR is there for CLUSTER LIFECYCLE, not for submission.* It always builds a
    `rayClusterSpec` and defaults `shutdownAfterJobFinishes: true` / `ttlSecondsAfterFinished: 3600`;
    **`ClusterSelector` is never populated.** Flyte provisions a whole Ray cluster per job. rask runs one
    long-lived shared cluster and provisions nothing per job, so the CR's purpose does not apply here.
  * *Flyte's own answer for an EXISTING cluster is neither CR nor Jobs API:* an `address` field feeding
    `ray.init("ray://…:10001")` — the Ray client protocol, which needs `ray` in the task image. rask
    refuses that deliberately (the stage runner is httpx-only, no `ray` package), so that path is closed
    by a choice already made.
  * *And rask's path is a sanctioned Flyte submission mode.* `submissionMode` accepts `HTTPMode`,
    described in Flyte's own config as "submits via HTTP to the head node; no submitter pod to get
    evicted". That is exactly what `ray_submit` does.
- **THREE CLAIMS IN THE FIRST READING WERE WRONG AND ARE CORRECTED HERE, because an adversarial pass
  (8 agents, 2026-09-15) was asked to REFUTE each one rather than confirm it — and killed three of
  five.** The conclusion survived; these premises did not, and they are rewritten rather than left
  standing beside it:
  * *"One of Flyte's TWO sanctioned modes" is wrong three ways.* There are THREE — `K8sJobMode`,
    `HTTPMode`, `SidecarMode`; `HTTPMode` is **Flyte 2 only** (added 2026-08-10, absent at v1.16.0);
    and Flyte itself never posts to the dashboard — it stamps a mode onto a CR it always creates, and
    KubeRay's operator does the POST (`dashboard_httpclient.go:232`).
  * *"Flyte is cluster-per-job" is overstated.* `main` carries `ClusterPlugin` and the v2 SDK has a
    `ReusePolicy` sharing one Flyte-created cluster across tasks. The accurate and narrower claim: Flyte
    cannot attach to an EXTERNALLY-managed cluster by name, which is rask's case.
  * *"Flyte base64-encodes `runtime_env` into the CR" is wrong in the direction that matters.* It lands
    as PLAINTEXT YAML in `spec.runtimeEnvYAML`. The CR is therefore WORSE than the dashboard as a
    credential surface, not merely equal to it — which strengthens the deletion rather than weakening it.
- **THE DELETION LEFT PROSE BEHIND, and that is tracked rather than quietly fixed later.** Six places
  still describe `RayJobExecutor` as live: `engine_names.py:24`, `engine_registry.py:13-16`,
  `dapr_saga.py:4`, `ray_jobs_api.py:11-12`, `ray_submit.py:~183` and `docs/DECISIONS.md:1451-1486`
  (which still says "a port, TWO adapters"). Filed as [[LH-156]].
- *A worry raised while deciding, and retired by the same reading:* the Jobs API echoes `runtime_env`
  on an unauthenticated dashboard. The CR does not fix that — Flyte base64-encodes `runtime_env` into
  the CR, readable by anyone holding namespace RBAC. Both paths expose it, and the real answer is the
  one this estate already built: `credential_ref` NAMES a secret and never carries one. Flyte documents
  no credential handling for Ray at all.

  **Evidence:** BYPASS STANDS: services/medallion/src/medallion/workflow.py:486 imports `submit_stage_job` and :495 calls it directly inside the `submit_stage` activity; `executor_for`'s only callers are tests (services/medallion/tests/test_the_chosen_engine_is_the_engine_that_runs.py:67,69,77,80 — no production caller in `grep -rn executor_for services/ packages/`), and services/medallion/src/medallion/services/transform.py:779 still constructs `InProcessExecutor(settings.storage_options)` by hand while `RayJobExecutor` is constructed only at engine_registry.py:70. ALREADY DONE: services/medallion/pyproject.toml:7-36 lists no `ray-kit` (`grep -n ray` returns nothing), and uv.lock's `[[package]] name = "medallion"` dependency block has no ray-kit entry — the orphaned comment at pyproject.toml:11-13 is what remains of it. Smaller true fix: route workflow.py:495 through `engine_registry.executor_for(...)`; the pyproject edit is a no-op.
  **Reopen if:** A `ray-kit` line reappearing in services/medallion/pyproject.toml, or a production call to `executor_for` in workflow.py.
`medallion, ray-kit` · **HIGH** · **blocked:** Q17-2 (the Ray adapter's fate)

- *Why open:* Corrected with `ast` rather than grep: exactly one in-scope bypass remains, `workflow.py:495` (`submit_stage_job`, the deployed stage lane) — `train.py:280` is the TRAIN lane and `ray_submit.py:318,425` are the adapter's own calls. The load-bearing half is the opposite error: nothing resolves an engine through the port, `transform.py:778` constructs `InProcessExecutor` by hand and `RayJobExecutor` is constructed by nothing. Migrating `transform.py` would be cosmetic; `workflow.py:495` is the site that CHOOSES an engine.
- **RE-MEASURED 2026-09-15, AND THE "NARROW FIX" ON OFFER IS NOT NARROW — it is the cutover this row
  is blocked on, wearing a refactor's clothes.** The bypass is real and unchanged: `workflow.py:487`
  imports `submit_stage_job` and calls it at :496, and `executor_for` still has ZERO production callers.
  But routing :496 through `executor_for(RAY_ENGINE)` does not wrap that call — it returns
  `RayJobExecutor`, which submits a **RayJob CR** against the Kubernetes API
  (`rayjob_executor.py:81-105`), while `submit_stage_job` posts to the **Ray Jobs API** on the
  dashboard. Two different submission mechanisms, not one seam over one mechanism.
  *The live estate runs the Jobs API:* `helm get values` pins `medallion.rayAddress=http://ray-lance-head:8265`
  (the dashboard), and no `RayService`/`RayCluster` CR exists despite the CRDs and the kuberay-operator
  being installed. So the "one-line" change would switch every cascade submission from the Jobs API to
  CRs on a cluster that has never run one.
  *Recorded because the cheap-looking version is the trap:* an audit pass on 2026-09-15 proposed it as
  independent of Q17-2. It is not — it IS Q17-2. The row stays blocked, and the blocker is correctly
  stated.
  *What genuinely is independent and still undone:* the second ask — no `ray` line in
  `services/medallion/pyproject.toml` — is already satisfied.
- *Closes when:* Route `workflow.py:495`'s stage submission through `engine_registry.executor_for(...)` once the Ray adapter's fate is decided, then drop `ray-kit` from `services/medallion/pyproject.toml`.

**LH-084 · ~~`BAKED_JOBS_DIR`/`BAKED_CLUSTER_JOBS` live in the shared library and the catalog enforces them, so a non-Ray lane cannot be declared and the word 'Ray' reaches every API client via the published OpenAPI~~ — CLOSED BY MEASUREMENT 2026-09-11**
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
`catalog, lineage, notifications` · med · **blocked:** owner decision — point BYO consumers at `lineage.events.v1` (costs no event), or accept the control lane's broadcast buffer carrying data-plane frequency

- *Why open:* The door landed and was driven live 2026-09-09 (three defects found and fixed), leaving exactly one residue: a consumer can only poll, never be told.
- **THE PRESCRIBED FIX HAS A COST THE ROW DID NOT PRICE, measured 2026-09-14.** `catalog.control.v1`
  has TWO consumers and they want opposite things: notifications (which correctly files an action naming
  no party as IGNORED — `NAMED_ACTIONS` holds exactly the six grant/task verbs) and the catalog's own
  subscription, where "every replica buffers every event for `GET /v1/events`"
  (`chart/templates/services.yaml:162`). A version advance fires on every insert, every compaction,
  every index build and every stage output, so putting it there sizes a GOVERNANCE broadcast buffer for
  DATA-PLANE frequency. `ControlAction`'s whole vocabulary is shape-of-the-estate — created, dropped,
  renamed, registered, protected, policy, grant — and carries nothing meaning "bytes were written".
- *And the premise is weaker than "must poll":* `catalog/core/lineage_emit.py::emit_write_event` already
  takes `version: int | None` and publishes it on `lineage.events.v1` for EVERY governed write,
  compaction, index op and restore included. So the signal a change-feed consumer needs — "table X is at
  version N now" — is on the bus today; what it then does is call `POST /v1/table/{id}/changes` for the
  rows, which is the door working as designed rather than polling for a trigger.
- *Which makes this a lane question rather than a missing event,* and the estate has already written the
  line it turns on (`.claude/skills/rask-notifications`): lineage answers *what happened to this dataset*,
  the control lane answers *what changed for this person*. A version advance is the first.
- *Closes when:* an owner rules on which — either point BYO consumers at `lineage.events.v1` (and say so
  where the changes door is documented, which costs no event), or accept the control lane's broadcast
  buffer carrying data-plane frequency and add the action with that cost stated. Do NOT add it to
  `NAMED_ACTIONS` either way; it names no party, and `transform_set`/`policy_set` already sit on that
  line for the same reason.

**LH-092 · The ingest-lane slice proves the TRIGGER chain but not the DATA chain — `MEDALLION_FROM_URI`/`TO_URI` are unset there**

- *RE-MEASURED 2026-09-10 — THE ASK IS LARGER THAN THE DEFECT.* The tier URIs the row asks to configure are already rendered for the lane's deploy path — the only true residue is that the lane never asserts a committed silver/gold version with row counts.
  **Evidence:** scripts/ingest-lane.sh:79-97 (`lane_values`) sets `medallion: {enabled: true}` and does NOT override `medallion.compute`; chart/values.yaml:1187 has `compute: true` as the shipped default; chart/templates/medallion.yaml:505-512 renders `MEDALLION_COMPUTE_ENABLED`, `MEDALLION_FROM_URI` and `MEDALLION_TO_URI` under `{{- if $root.Values.medallion.compute }}` for every stage runner. So the guard at services/medallion/src/medallion/services/transform.py:971 (`if settings.compute_enabled and from_uri and to_uri:`) is satisfied in the lane slice, not bypassed. The row's cited locator (medallion.yaml:502-503) has drifted to 511-512. The values.yaml:1176-1181 caveat 'REQUIRES OpenBao off' is itself stale — the medallion now fetches its S3 secret through Dapr (services/medallion/src/medallion/core/config.py:447-449 + packages/service-kit/src/service_kit/governed/secrets.py:147-193), and chart/templates/medallion.yaml:522 withholds the plaintext key only when `secretsViaDapr` is on. What IS absent: scripts/ingest-lane.sh asserts only bronze — `committed_version` (line 506-508) and `units_done` (line 512-515), repeated at 617-619 and 717-721 — and the word silver/gold appears in the script only inside the comment at line 89. Nothing reads a silver or gold version or row count.
  **Reopen if:** A `MEDALLION_FROM_URI` that renders empty in the lane's `helm template` output (run `scripts/ingest-lane.sh` render and grep), or an assertion in ingest-lane.sh that opens the silver/gold dataset and checks its version or row count.
`medallion` · med · **blocked:** the double-home ruling ([[LH-137]]/[[LH-164]]) — the tiers this row asks the lane to assert are composed paths the catalog does not know

- *Why open:* `medallion/services/transform.py:970` guards the whole compute path on `settings.compute_enabled and from_uri and to_uri`; in the lane slice those URIs are unset and no project routing is configured, so the stage runner wakes, emits and writes nothing. Partly overtaken — `chart/templates/medallion.yaml:502-503` now renders both for the chart deploy path — so what remains is the lane proving bronze→silver→gold moves BYTES.
- **AND THE ASSERTION CANNOT BE WRITTEN THROUGH THE CATALOG YET, measured 2026-09-16.** The tiers the
  row asks the lane to check are the ones `chart/templates/medallion.yaml:525-526` renders:
  `MEDALLION_FROM_URI` / `MEDALLION_TO_URI` = `s3://<stageBucket>/medallion/<namespace>` — the COMPOSED
  paths. Those are precisely the datasets [[LH-137]]/[[LH-164]] name as the double-home: their derived
  ids are namespace-shaped and name no table door, which is why condition 5's own measurement finds all
  three `maintenance_vend_denied` refusals there (`lakehouse$gold`, `$silver`, `$silver-media`). A lane
  assertion that asked the catalog for a committed silver version would 404 — not because the cascade
  failed, but because the tier is not a catalog table.
- *So this row is gated on the SAME ruling as the double-home*, and which assertion to write depends on
  the answer: if the catalog-vended path becomes the only legitimate home, the lane asserts through the
  catalog like it does for bronze; if composed paths stay, the lane has to open the S3 path directly and
  the test says nothing about governance. Writing either before the ruling means writing it twice.
- *Re-measured, the rest of the row holds:* `silver` and `gold` appear in `scripts/ingest-lane.sh` exactly
  ONCE, in a comment at line 89. The script asserts `committed_version` (line 506) and `units_done`
  (513, 618, 719) — bronze only. The DATA chain is genuinely unproven; only the means of proving it is
  blocked.
- *Closes when:* the double-home ruling lands, then the lane asserts a committed silver and gold version
  with row counts through whichever door that ruling makes correct — not just a `POST /medallion-event 200`.

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
  **THE RESIDUE IS MEASURED 2026-09-17, AND IT IS NOT THE SHAPE THIS ROW GUESSED.** The row proposes
  deciding "whether `is_protected` should exempt an `is_dataset_root=False` (reference-only) base".
  Read off the live estate over 60 minutes: **1,399 `maintenance_refused_protected_base` lines across
  246 distinct datasets**, splitting

      116  equality  — the dataset IS a referenced root, and the refusal reason is exactly right
      129  `<root>/tree/<name>` BRANCHES — 100% of the "lies under a protected root" class
        1  other

  So the refusals are not about a base's dataset-root-ness at all; they are about CONTAINMENT, and the
  whole "under" class is branches.
  * **A BRANCH IS THE REFERRER, NOT PART OF THE REFERENT**, which is what makes the old message wrong.
    `lance_docs/file_format.md:2744` — *"Each branch dataset is technically a shallow clone of the
    source dataset"* — and the layout at `:2746-2761` gives `tree/{branch}/` its own `_versions/`,
    `_transactions/`, `_deletions/` and `_indices/` and **no `data/`**, so a branch resolves its data
    through the parent. That is precisely what makes the PARENT protected. Telling an operator that a
    branch is a root "another dataset resolves its files through" states the parent's situation about
    the child.
  * **FIXED 2026-09-17 — the diagnosis, deliberately NOT the GC behaviour.** `base_refs.containment_of`
    classifies `is` / `branch` / `under` / `ancestor`, and `optimize.py` renders one sentence per
    relation plus a `relation` field on the log line so the four can be counted apart. Every one of the
    four is still REFUSED; nothing about what may be reclaimed changed.
  * **OBSERVED LIVE 2026-09-17** on `lance-rest-catalog:main-472ed48f`, Dagger-built and rolled to
    `rask-maintenance`. In a 20-minute window the sweep reports `relation='is'` **57** and
    `relation='branch'` **57** — the two classes are countable apart on the estate for the first time,
    and a branch now reads *"this is a BRANCH of `branchaud1-wh/74aba457_branchaud1ns$t5` and therefore
    a shallow clone of it — its own data resolves through the parent"* instead of being named as a root
    other datasets resolve through. Sample of each kind read off the running pod, not inferred.
  * *Why the behaviour was left alone, stated so it is a decision rather than an omission:* whether a
    branch may be compacted at all depends on what pylance scopes `cleanup_old_versions` to, and
    `file_format.md` does not say. The row's own Closes-when already prescribes the right instrument —
    "a RED test pinning what pylance does … before changing any GC behaviour" — and
    `tests/unit/test_base_refs_guard.py`'s subprocess reproduction is the established way to settle it,
    because Lance caches dataset state per process and an in-process assertion reports on its own
    memory. **Until that test exists, a permit here risks a clone's entire reason to exist, and the
    gate's own comment says it fails closed on purpose.**
  **DISCOVERY TRUNCATION IS RESOLVED BY THE DEPLOY, observed 2026-09-11:**
  `maintenance_discovery_truncated` is **0** in a five-minute window on `main-b641103f`, against 47 WARN
  lines / 64 prefixes per tick before. The evidence-based `_may_hide_a_dataset` narrowing was written
  2026-09-10 and simply had not shipped — so the 64 "incomplete units" this row family tracks were
  pre-fix residue rather than a live coverage gap.
  **VOLUME RE-MEASURED 2026-09-11, and it is half the estate's warnings.** This row records
  `maintenance_refused_protected_base` at "286 in one sweep window" and rightly calls the refusal the
  shallow-clone rule working as designed. Measured over one hour on the live estate: **10,461**
  occurrences against **20,740** WARN-or-worse records in total — **~50% of every warning the estate
  emits** is this one correct, structural, permanent refusal. A base stays a base for as long as its
  clone exists, so the condition never clears and the line fires again every tick, forever.
  *Why that is worth a line rather than a shrug:* the number is already carried by
  `compaction_datasets_refused_total` (the `#64` counter covers exactly `base_paths / shallow clone`),
  so the WARNING adds volume rather than information — while the per-dataset REASON, which names the
  dependent blocking the compaction, is the part with diagnostic value. The proportionate change is the
  one this estate already makes elsewhere: keep the per-dataset detail at DEBUG and emit one WARNING per
  sweep carrying the count, so a genuinely new refusal is visible instead of being the 10,461st line.
  Not done here — the row holds a considered position and a log level is an operator-experience call —
  but the 50% is new information the position was taken without.
  **Reopen if:** A live reconcile tick whose `incomplete` is non-zero for a reason other than a genuinely unreadable prefix, or a `maintenance_refused_protected_base` log line naming a dataset whose only referrer resolves through an `is_dataset_root=False` external blob base — that would prove the reference-only field is still load-bearing. (The row's live numbers — 13/65/442 — are estate claims I could not verify from the tree.)

- **RE-MEASURED 2026-09-16 on `main-fd3999f4`, the first reconcile tick after [[LH-141]]'s guard and the
  vend fix deployed. Every number in this row has moved:**

      total       13  ->  946
      incomplete  65  ->    3   (all three `storage:lance-catalog`)
      excluded   442  ->  518
      orphan_files 0  ->  932   across 13 buckets

  **`incomplete` falling 65 -> 3 is this row's headline resolving**, and it resolved by deployment
  rather than by code written here: the narrowing was measured 2026-09-10 and the image carrying it is
  only now on the estate. Three residual notes remain and the row's Reopen-if asks whether they are
  "a genuinely unreadable prefix" — the drift line carries the SOURCE and not the reason, so that
  question is one log field away from being answerable and is not yet answered.
- **`orphan_files` went 0 -> 932 in the same tick, and the 0 was not a clean estate.** The scan is
  gated (`orphan_scan_enabled`) and a skipped scan is recorded as SKIPPED, never as zero — so this is a
  scan that ran both times and saw more.
- *The obvious explanation is measured NOT to hold.* With the naming fix deployed the findings name
  their datasets, and the first ten are all ONE dataset —
  `s3://lance-catalog/m2proof_silver$m2-proof-1788537252` — in `lance-catalog`, a bucket the maintainer
  could always read. So this is **not** the `s3://lance-catalog/models/` vend fix making a base visible,
  which is what the same image's other change would have suggested. Recorded because the correlation
  was tempting and is wrong.
- *What the 932 ARE is still open, and the honest reasons are two.* The `0` was measured six days
  earlier (2026-09-10) over an estate that has run e2e traffic since, so newly-CREATED is live; and the
  same image carries [[LH-100]]'s discovery narrowing that took `incomplete` 65 -> 3, so newly-VISIBLE
  is live too. One tick distinguishes neither. **What would:** a per-dataset orphan count. The scan
  already computes one (`DatasetOrphanScan.orphans`) and logs nothing on the success path — only
  `orphan_scan_skipped` / `unreadable` / `listing_failed` — so 932 findings across an unknown number of
  datasets is as far as any reader can get. That, not the bounded ten-name sample, is the gap.
- **THAT GAP IS CLOSED: the drift summary now carries `orphans_by_dataset`.** Largest holder first,
  because that is the order an operator works in; ten named and the tail as ONE entry carrying its own
  count, so the shown numbers still sum to `counts["orphan_files"]` and a truncation cannot hide how
  much it hid. Same bound and same argument as `_drift_names` — a drifting estate can carry thousands
  of datasets and one WARNING must not become the report.
  *It is diagnostic and not merely tidier*, which is why it belongs to this row rather than to a
  logging cleanup: "44 datasets, the largest holding 300" separates newly-VISIBLE from newly-CREATED
  where a single total cannot, and that is the exact question the 0 -> 932 jump left open. Gated by
  `services/maintenance/tests/test_the_orphan_count_says_which_datasets_hold_them.py`, including that
  the truncated tail's own count is right — a bounded list that does not admit its bound repeats the
  failure one level up.
- **DEPLOYED AND OBSERVED 2026-09-16 (`main-fef0e10d`), and the first tick answers more than it was
  built to.** EIGHT datasets hold all 932, and they sum to exactly that:

      s3://acme-bucket/e41135a5_acme-silver$features            293
      s3://acme-bucket/medallion/bronze                         243
      s3://bind86-wh/medallion/silver                           160
      s3://acme-bucket/3c099c25_acme-gold$catalog               148
      s3://lance-catalog/medallion/bronze                        30
      s3://lakehouse-wh/medallion/bronze-media                   26
      s3://c6t115034-wh/medallion/bronze                         19
      s3://lance-catalog/m2proof_silver$m2-proof-1788537252       13

  So it was never a diffuse estate-wide condition — it is four datasets carrying 90% of it, and the
  register can now say which.
- **AND THE CROSS-REFERENCE IS THE SHARPER FINDING: seven of the eight are swept every tick, refuse
  NOTHING, and reclaim NOTHING.** Every one carries `refused=None old_versions_removed=0` while
  holding unreferenced files — 759 between them. The eighth,
  `s3://bind86-wh/medallion/silver`, produces no outcome line at all, which is [[LH-141]]'s crossing
  guard stopping the unit before `_maintain_one` runs; that one is expected.
- *What that points at, stated as a reading and not a root cause:* version cleanup reclaims files a
  SUPERSEDED VERSION referenced. A file no manifest ever referenced — the shape an aborted or partial
  write leaves, and the `_transactions/*.txn` + `data/*.lance` pairs in the sample look exactly like it
  — is not a superseded version's file, so `cleanup_old_versions` has nothing to reclaim and reports 0
  honestly. That is precisely the population the orphan scan exists to find and the #79 purge exists to
  act on, and the purge is gated on `report_is_clean`, which an `incomplete` list of 3 currently blocks.
  Naming the chain rather than asserting it: a second tick over time is what separates "these stopped
  growing" from "these are still accumulating".
- **ROOT-CAUSED 2026-09-16, AND IT IS THE ESTATE'S OWN BACKUPS.** Making the incomplete list say WHY —
  it logged the SOURCE only, three entries all reading `storage:lance-catalog`, which an operator cannot
  act on — produced the answer immediately:

      storage:lance-catalog: depth limit reached at s3://lance-catalog/_backups/control/20260914T163435Z
      storage:lance-catalog: depth limit reached at s3://lance-catalog/_backups/control/20260914T164046Z
      storage:lance-catalog: depth limit reached at s3://lance-catalog/_backups/control/20260914T164519Z

  `control_root_backup.py` writes `_backups/control/<timestamp>/…`, which nests past
  `discovery_max_depth`. The walk records an `IncompleteScan`; `report_is_clean` refuses to certify an
  estate with anything incomplete; the #79 purge is gated on `report_is_clean`. **So the estate's own
  backup snapshots were blocking the TRASH PURGE.**
- **AND THE CLAIM I FIRST WROTE HERE WAS WRONG — corrected by continuing to measure rather than by
  stopping at the fix.** I said the backups were "blocking reclamation of 932 orphan files". They were
  not, because **no orphan reclaimer exists**: `orphans.py:3` states it outright — *"NOTHING DELETES.
  NOTHING MUTATES. Every path here is read-only"* — and `purge.py:8` records the sequencing as
  deliberate: *"Trash purge first, orphan reclaim later."* The 932 were never going to be reclaimed by
  anything. What `report_is_clean` gates is the trash purge, which is a different and real thing.
- *Deployed and measured 2026-09-16: `incomplete=[]`.* The three backup entries are gone, so ONE of the
  gate's four conditions is now satisfied. **The purge is still blocked**, because `report_is_clean`
  requires no findings at all and the tick reports `total=964` — including the 932 orphan files, which
  is circular only in appearance: the orphan count is a REPORT that no reclaimer consumes, so it will
  gate the trash purge until either a reclaimer exists or the gate stops counting a read-only category.
  That is the next question on this row, and it is a design question rather than a defect.
- *The fix is one entry in a list that already existed.* `_CONTROL_PREFIXES` skips the control-plane
  registries because "no dataset ever lives under them"; `_backups` is exactly that kind of directory
  and was missing. Gated by `tests/unit/test_the_backup_directory_does_not_gate_reclamation.py`, which
  also pins the half that must NOT regress: a governed dataset nested past the bound is a real coverage
  gap and is still reported, because the `truncated` field exists for the day that went silent.
- *What this does NOT settle:* whether the 932 then get reclaimed. The purge has its own report-first
  posture and `MAINTENANCE_TRASH_PURGE_ENABLED` default, so the next measurement is whether
  `report_is_clean` now goes true and what the purge does with it.
- **`maintenance_refused_protected_base` is still half the estate's warnings — re-measured: 738 of
  1,356 WARN-or-worse lines in 25 minutes, 54%.** The row's 2026-09-11 position (10,461 of 20,740)
  holds unchanged on a different image and a different window, so it is structural rather than a spike.
- **FIXED HERE: a named orphan finding said WHAT but never WHERE.** `_drift_names` exists because
  "orphan_buckets: 12" gave an operator no way to learn which twelve; `_finding_identity` takes the
  first present field from a fixed list and reaches `path` before it would ever consider `dataset` —
  so all 932 were named `data/00110000….lance` and `_transactions/0-….txn`. `OrphanFile.dataset`
  exists for precisely this ("so a finding is actionable without re-deriving it") and was the one field
  the namer skipped. Every Lance dataset has a `data/` and a `_transactions/`, so across 13 buckets
  those names were not merely unhelpful, they were not unique. A path is now qualified by its dataset,
  gated by `services/maintenance/tests/test_a_drift_finding_is_named_where_it_actually_lives.py`.
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

- **THE PHASE-1 HALF IS DONE AND GATED — re-measured by AST 2026-09-16, not by grep:**

      services/catalog       threaded=10   BARE=0
      services/lineage       threaded= 8   BARE=0
      services/medallion     threaded=15   BARE=0
      services/maintenance   threaded= 9   BARE=0
      packages/service-kit   threaded= 7   BARE=0

  49 threaded opens, **zero bare**, across the four lakehouse services and the platform library they
  all call — against the 66-site / 7-threaded figure this row's own re-measure recorded six days ago.
  Pinned by `tests/unit/test_a_lakehouse_open_shares_the_process_session.py`, which walks the ASTs (the
  row's original counts were taken by grep and counted PROSE — five "bare" hits were docstrings
  explaining why bare opens are bad). `instrument_lance_if_available` is likewise called by all four,
  medallion from both its entrypoints.
- *WHAT IS LEFT IS NOT PHASE 1:* `services/ingest` 9 bare opens (phase 2) and the parked zones
  `services/viewer` 5 + `services/search` 1. The gate names those exemptions and asserts they are still
  NEEDED, so a silent exemption over a converted area fails rather than rots.
- *And the thread/log clause this row bundled is now its OWN row, because it is a different defect
  with its own measurement:* `LANCE_CPU_THREADS`/`LANCE_IO_THREADS`/`LANCE_LOG` still appear nowhere,
  and measured in the running pods every lakehouse container builds a 64-thread compute pool on a
  one-CPU quota — see [[LH-172]]. Keeping it here would have left a memory multiplier filed under a
  row whose memory clause is already solved.
`viewer, medallion, lineage, catalog, ingest, maintenance, service-kit, chart` · med · *was HIGH; the lakehouse half closed 2026-09-16 (49 threaded opens, zero bare, gated) and what remains is `ingest` (phase 2) and the parked zones*

- **THE CATALOG IS CONVERTED 2026-09-14 (`aca6a485`) — the per-REQUEST half, which is where this costs
  most.** All 11 of its opens now thread `shared_lance_session()`: `namespace.py` (2), `dataplane.py` (5),
  `publication.py`, `vending.py`, `models.py` and the credentials door. Caps are settings
  (`LANCE_METADATA_CACHE_MB`/`LANCE_INDEX_CACHE_MB`, 128/256) clamped to the cgroup by
  `affordable_cache_bytes`, the shape maintenance already runs.
  *The behavioural test is the one that matters:* asserting a `session=` kwarg would pass for a session
  nobody reuses, so it opens repeatedly through the shared session and requires `size_bytes` to GROW,
  then requires the identical bare opens to leave it untouched — this row's own measurement, now a gate.
  A structural test walks the catalog's AST so a twelfth bare open cannot come back.
  *Two of my own test assumptions were wrong and are corrected rather than worked around:* the clamp
  assertion ran on a host with NO cgroup limit, where granting verbatim is correct (it now forces a
  budget and mirrors the real `fraction` signature), and `Session.size_bytes` is a method, not a property.
  **OBSERVED ON THE ROLL 2026-09-14** (helm rev 154, `main-dd7c9b20`), and the clamp is the proof it is
  not decoration: the live catalog logged
  `lance_cache_clamped_to_container requested_bytes=402653184 granted_bytes=214748364 container_budget_bytes=214748364 fraction=0.4`
  — the configured 128+256 MB reduced to 204.8 MB, which is exactly 0.4 x the pod's 512 Mi limit, read
  from its own cgroup rather than from a literal. Zero catalog errors in the window; every pod Ready.
- **THE LAKEHOUSE HALF IS COMPLETE 2026-09-14 (`1535fdf1`)** — lineage 8 sites, medallion 15, and
  maintenance 3. Every open in catalog, lineage, medallion and maintenance now threads a bounded session.
- **OBSERVED ON THE ROLL** (helm rev 155, `main-b777d740`): catalog, maintenance AND lineage each logged
  the identical clamp —
  `lance_cache_clamped_to_container requested_bytes=402653184 granted_bytes=214748364 container_budget_bytes=214748364 fraction=0.4`
  — 384 MB configured reduced to 204.8 MB, which is exactly 0.4 x each pod's own 512 Mi cgroup limit.
  The lineage sweep on the new image is unchanged (`checked=356 storage_loss=0 ungoverned=11
  graph_ahead=31 unreadable=24 stale=317`), so threading the session altered nothing about what it reads
  — which is the other half of the claim and the easier one to forget to check.
- **AND THE ESTATE-WIDE GATE FOUND THE MAINTENANCE THREE, which is the argument for it being
  estate-wide.** Maintenance shipped this pattern FIRST and still had bare opens in
  `compaction_executor.py` and `index_build.py` — the newer lanes, added after its own conversion. A
  per-service test would not have looked there, and the catalog's own structural check could not. The
  rule now lives once, in `tests/unit/test_no_lakehouse_service_opens_lance_unbounded.py`, covering all
  four services; the catalog's copy was removed rather than left beside it.
- *A sibling test moved with the change rather than being relaxed:* `test_the_storage_leg_reads_MAIN`
  asserted on the exact source TEXT `lance.dataset(uri, storage_options=storage_options)`, so adding a
  kwarg that alters nothing about WHICH ref is read still broke it. It now pins the property that leg
  holds — it names no ref: no `branch=`, no `version=` — and that assertion is mutation-proven.
- *What is left, and it is a decision rather than a conversion:* **service-kit's 6 opens**
  (`introspect`, `sources`, `stage_stamp`, `descriptor`, `quality`, `registry`). They sit in code SHARED
  by services whose caps may differ, so a session there is a SIGNATURE question — thread it from each
  caller, or give service-kit a default session and accept two caches per process when a service
  configures different caps. The gate names service-kit out of scope for exactly that reason instead of
  passing while shared code still mints the defaults. ingest (8) is phase 2; viewer (6) is parked.

**RE-MEASURED 2026-09-10, AND THE COUPLING THE ROW ASSERTS DOES NOT APPLY TO THE HALF THAT MATTERS.** The row treats "stop opening per request" and "size the caches" as one change. They are two, and only the first is dangerous: a cached HANDLE pins a version, which is why the viewer's registry is a read-only trade. A bounded `lance.Session` is NOT a handle cache — its keys are `(uri, version, etag)`, so a compaction writes NEW keys and there is no freshness contract to design and no stale-read window; `lance_session.py` records that, and that it is thread-safe under 8x50 concurrent opens. **Maintenance has already shipped exactly this and it is the proof:** `shared_lance_session()` caps it at 128 MB metadata + 256 MB index and threads it through reconcile, purge and the orphan scan. **Measured today:** every lakehouse pod runs a 512 Mi limit (128 Mi request), and ONLY maintenance passes a session — catalog opens 12 bare datasets, medallion 15, lineage 6, with 6 more in shared service-kit code. Each of those mints Lance's 1 GiB metadata + 6 GiB index ceilings and discards them WITH the handle, so the cache never engages at all: ten version-opens against a shared session grow `size_bytes` 168 -> ~75k, the same opens without one leave it flat. So the safe, proven half is a bounded session per service — soft LRU bounds, no version pinning, the pattern already running in maintenance — and it needs ONE seam plus 39 call sites converged onto it, not a redesign. - *Why the rest of the row is open:* A shared handle also pins a version, so the fix needs `checkout_latest` (or a session-scoped open) plus an explicit freshness contract per service. Measured: 53 `lance.dataset()` call sites and 5 pass a session; the catalog opens ~24 bare datasets per request path. The same row carries the rest of the runtime hygiene: no `LANCE_CPU_THREADS`/`LANCE_IO_THREADS`/`LANCE_LOG` in the Ray `runtime_env`, `instrument_lance_metrics` never called by ingest/viewer/search/annotator, no branch/tag name validation at the door, blob thresholds unpinned on some create paths, `allow_http` not derived from the endpoint scheme, missing HTTPX timeouts.
- *A LIVE OOM, 2026-09-10, on the one service that ALREADY has the bounded session.* `rask-maintenance` was OOMKilled (exit 137, `reason: OOMKilled`) against its 512Mi limit and restarted; steady-state is 153Mi. It happened while three manual `/maintenance-reconcile-cron` invocations overlapped the 5-minute scheduled ticks — self-inflicted, and the pod recovered unaided — so this is a data point about HEADROOM, not a standing outage.
  Two things it settles and one it does not. It settles that the 512Mi tier has little margin on the scan path, and that **a bounded `lance.Session` is not by itself sufficient** — maintenance ships `shared_lance_session()` at 128MB metadata + 256MB index and still died. It does NOT establish that Lance caches are the allocator: the reconcile scan's own structures over 93 buckets and ~1,163 Dataset nodes are an equally plausible source, and nothing here separates them. Attribute before sizing — `index_cache_size_bytes` tuned against the wrong consumer buys nothing.
  **CHASED, AND IT IS NOT CONCURRENCY — IT IS ARITHMETIC.** `_reconcile_lock` is correct (`if locked(): return skipped`, `routes.py:149`) and the manual calls were sequential, so nothing overlapped. The caps are the problem: `shared_lance_session()` is built from `lance_metadata_cache_mb=128` + `lance_index_cache_mb=256` (`maintenance/core/config.py:38-39`) = **384 MB**, against a **512Mi** pod limit with a measured process baseline of **153Mi**. 384 + 153 = 537 MB. **A fully-warmed session OOMs this pod by construction**, and warming is exactly what a scan that opens every dataset in 93 buckets does — which is why the kill came after repeated reconcile passes rather than during one.
  The session's own docstring gives the right reason for capping — Lance's 1 GiB + 6 GiB defaults "dwarf the pod's own 512Mi limit" — and then picks caps that exceed what is left after the process itself. So the estate's ONE example of the fix this row prescribes is also an example of getting it wrong: **the caps must be sized against (limit - baseline), not against the limit**. Sizing every other service to this pattern would propagate the error to catalog, medallion, lineage and ingest.
- *MEASURED 2026-09-10 — copying the one worked example would have OOMed the whole lakehouse.* Live per-container memory against the uniform 512Mi limit: maintenance 269Mi, catalog 214Mi, lineage 200Mi, medallion-producer 189Mi, ingest 165Mi, viewer 157Mi. Add maintenance's configured 384 MB of session cache to ANY of them — 653, 598, 584, 573, 549, 541 — and every one exceeds 512Mi. This row's Closes-when says to size each service the same way, and maintenance is the only existing implementation, so the pattern the other five would be copied from is the one that OOMKilled its own pod.
  **OBSERVED ENGAGING ON THE LIVE ESTATE 2026-09-10 17:48:42**, not merely deployed: `lance_cache_clamped_to_container requested_bytes=402653184 granted_bytes=214748364 container_budget_bytes=214748364 fraction=0.4` — 384 MB asked for, 205 MB granted, read from the pod's own cgroup. The fresh pod's baseline is 139Mi (down from the 269Mi that included a partly-warm session under the old caps), so the steady state is ~344/512 against the 537 that killed it.
  **Fixed generically instead of per service** (`1ee04aa0`, `6ca5bec9`): `service_kit.lakehouse.lance_session.affordable_cache_bytes` reads the container's own cgroup and reduces the requested caps PROPORTIONALLY (preserving the caller's ratio) when their sum exceeds an affordable share, default 0.4 of the limit. So the converting services need no per-pod arithmetic — they state a ratio and call it. Verified on the real host: `/sys/fs/cgroup/memory.max` = 536870912 in `rask-maintenance`, v1 path absent. At 0.4 the budget is ~205MB, which every service above clears; maintenance is the tightest at 474/512 and its 269Mi includes a partly-warm session under the OLD caps, so the post-deploy baseline should fall.
- **RE-MEASURED BY AST 2026-09-16, AND EVERY SERVICE THIS ROW NAMES IS DONE.** The counts above are
  stale in a way that misdirects: they were taken by grep, which counts the prose too — five of the
  "bare" hits are docstrings in `config.py` files explaining why a bare open is bad. Walking the ASTs
  instead, `lance.dataset(...)` resolves to **44 calls that pass a session and 21 that do not**, and
  the split by service is:

      catalog     11 / 0 bare        medallion   15 / 0 bare
      lineage      8 / 0 bare        maintenance  9 / 0 bare
      ingest       0 / 9 bare        viewer       0 / 5 bare
      service-kit  1 / 6 bare        search       0 / 1 bare

  ALL FOUR LAKEHOUSE SERVICES CARRY ZERO BARE OPENS. The row's own locators — "medallion 15, lineage 6,
  catalog 12 bare", `compute.py`, `reconcile.py`, `namespace.py` — name work that has already landed.
- *What is left in PHASE 1 is six shared helpers in one package*, not 39 sites across five services:
  `lakehouse/sources.py:197` (`_dataset`, 4 callers), `lakehouse/stage_stamp.py:166`
  (`ensure_declared_dataset_id`, 1), `lakehouse/quality.py:165` (`assert_quality`, 2),
  `lancekit/descriptor.py:223` (`load_declared`, 0), `lancekit/introspect.py:76` (`table_info`, 1) and
  `lancekit/registry.py:156` (`table_dataset`, 11). The rest — ingest 9, viewer 5, search 1 — are phase 2
  and parked-zone work.
- **AND THEY NEED AN INJECTED SESSION, WHICH IS WHY THEY WERE LEFT.** `lance_session()` is process-wide
  and int-keyed, but `shared_lance_session()` is defined FOUR TIMES, once per service, each from its own
  settings' caps (`catalog/core/config.py:550`, `lineage/:289`, `medallion/:648`, `maintenance/:422`).
  A shared helper in service-kit has no service settings to read, so it cannot call one — the session
  has to arrive as a parameter from the caller that has it. That is ~19 call sites plus six signatures.
- *Sized rather than attempted, deliberately:* this is the shared data-access seam of all four lakehouse
  services, and this row's own record is a pod OOMKilled by session caps set above its headroom. Getting
  the injection wrong is cheap; getting the sizing wrong is an outage.
- **THE SESSION HALF IS DONE 2026-09-16.** All six helpers take an injected `session` and the five
  lakehouse call sites that hold one pass it — `catalog/services/publication.py` (2 x `assert_quality`)
  and `medallion/services/compute.py` (3 x `ensure_declared_dataset_id`). `table_dataset`, `table_info`,
  `load_declared` and `LanceFragmentSource` have no caller in the four lakehouse services; theirs are in
  ingest, viewer and search, so the parameter is there for when those convert.
- *`session=None` is pylance's OWN default* (`lance.dataset(..., session: Optional[Session] = None)`), so
  a caller that passes nothing behaves exactly as before — the change is strictly additive.
- *Gated by `tests/unit/test_a_lakehouse_open_shares_the_process_session.py`*, an AST walk over the four
  lakehouse services plus service-kit. It NAMES its exemptions (ingest = phase 2, viewer/search =
  parked) and asserts they are still exempt, so a green run cannot be read as "the estate shares one
  session" and an exemption cannot rot silently once that area converts.
- *One test double had to be corrected rather than worked around:* `test_not_found_classifier.py`'s
  stand-in for `lance.dataset` omitted `session`, so threading it made the test fail with a TypeError
  for a reason unrelated to the classifier it pins. A double that does not mirror the real signature is
  a test that passes for the wrong reason.
- *Closes when:* the residue the row bundles lands — the three `LANCE_*` vars in the Ray `runtime_env`,
  one `instrument_lance_metrics` call per process (ingest, viewer and search still never call it),
  door-side branch/tag validation, pinned blob thresholds, `allow_http` derived from the endpoint
  scheme, HTTPX timeouts. The per-service conversion, the per-pod sizing and the shared-helper
  injection are all DONE and should not be re-attempted.

**LH-097 · Tiers re-materialise managed blob bytes per tier instead of silver being a shallow clone of bronze@N plus `add_columns`**
`medallion, maintenance, catalog` · med · **blocked:** owner acknowledgement of R9 plus the storage-vs-coupling trade, and the recorded clone→source edge

- *Why open:* `compute.py` copies managed blob bytes into every tier — measured ~100% per tier materialised against 0.16% (bronze) / 0.19% (silver) carried as forwarded external descriptors — on the ground that 'the bytes exist nowhere else', which a shallow clone refutes. The prerequisite guard is now measured live (43,604 base-ref observations, 220 refusals in six hours, both on-demand doors passing `protected`), so what is left is the lifecycle trade — a referencing silver means bronze can never be reclaimed independently — and the measurement itself, which has never been taken.
- *Closes when:* Land the clone→source pins; add a `scripts/` measurement of both shapes (bytes and latency) against the medallion's blob path on one corpus; then make silver a shallow clone of bronze at a pinned version plus `add_columns` in `medallion/services/compute.py`.

**LH-098 · Maintenance's reclamation trail carries no attempt count, no duration and no per-object fact a TENANT can query**
`maintenance, lineage` · med

- *Why open:* The first half landed and is deployed — `audit_material_work` records a rewrite on the `lance.audit` stream keyed on `table:<id>`, wired into `_record_dataset_outcome` and gated on `_did_material_work` — but it is operator-global telemetry on a 14-day trace TTL, so 'which compaction rewrote my table last quarter, and did it fail first?' has no tenant answer. It is also not yet OBSERVED (2,200 dataset outcomes with `fragments_removed` and `old_versions_removed` both summing to 0, because the estate is converged), and `maintenance/core/lineage_emit.py` gives FAIL a deterministic run id per dataset so attempts merge onto one node and are structurally uncountable.
- **"FIX THE DETERMINISTIC FAIL RUN ID" WOULD BE A REGRESSION, and the code says why (re-measured
  2026-09-13).** It is not an oversight: `lineage_emit.py:271-279` calls it "the flood guard" and states
  the arithmetic — the cron re-sweeps every ~2 min, so a persistently failing dataset would otherwise
  mint a fresh never-pruned `(:Run)` node per tick (~720 a day, per dataset). One id per dataset makes
  every tick MERGE onto ONE node in AGE, and the `/events` partial-unique `(run_id, event_type)` index
  dedups the redelivered FAIL rows. The consequences are ACCEPTED and enumerated there against a
  2026-07-10 review: after recovery the node lingers until Run retention prunes it, and across distinct
  episodes `/runs` is last-wins while `/events` keeps the first.
  *Its one stale premise is now closed:* that comment tells the reader to "enable it alongside
  lineageEmit", and `LINEAGE_RUN_RETENTION_DAYS=30` is deployed (measured 2026-09-11), so consequence
  (a) has its sweeper.
- *WHICH LEAVES THE ROW'S REAL ASK INTACT AND ITS REMEDY WRONG.* "Attempts are structurally
  uncountable" is TRUE and worth fixing; minting a run id per attempt is not the way, because that is
  precisely the flood the guard exists to prevent. Counting does not need a node per attempt — one node
  carrying an attempt COUNTER and a last-attempt timestamp answers "did it fail first, and how often"
  without multiplying nodes. That is a facet on the existing MERGE target, not a new id.
- **THE DURATION HALF IS SHIPPED AND OBSERVED 2026-09-15.** `DatasetResult` gained
  `duration_seconds`, timed at `_maintain_one`'s single choke point (every lane reaches it) around the
  REWRITE rather than the whole span — the policy skip returns before a dataset is opened, and "how
  long did this pass take" should mean the work. `perf_counter`, so a clock step mid-compaction cannot
  corrupt it; `None` rather than `0.0` where nothing was attempted, because a decision that did no work
  and a pass too fast to measure are different facts. It reaches both the `lance.audit` record and the
  `compaction.compact` span.
  *Measured on the deployed estate after the roll* — GreptimeDB auto-created
  `span_attributes.lance.maintenance.duration_seconds`, which is itself proof the attribute is emitted,
  and the values are real per-dataset work:

      0.046  s3://regaud-wh/89014814_regaudns$t2
      0.022  s3://e2e-wh-life/7cd4c28d_e2elifens$t_before
      0.009  s3://cons9931-wh/5ca63792_cons9931ns$tbl

  *The test that matters is on the shipped path:* the audit-record test runs against a double and can
  only show the field TRAVELS, so the assertion that a pass is actually TIMED lives in
  `test_a_work_item_is_self_contained.py`, on a run that really opens and rewrites a Lance dataset.
  Mutation-proven — deleting the timing line reds that one while the double-based test stays green.
- **THE ATTEMPT HALF IS SHIPPED AND OBSERVED 2026-09-15 — two of the three closing clauses.** `r.attempts` +
  `r.last_attempt_at` live on the single node `MERGE_RUN` already targets — the node the deterministic
  run id exists to make every tick converge onto — exposed on `RunStatus` and both read paths.
  *The guard is the whole correctness, and a bare `coalesce(r.attempts,0)+1` would have been WRONG:*
  `MERGE_RUN` runs on EVERY ingest while `/events` dedups redelivered FAIL rows on its partial-unique
  `(run_id, event_type)` index, so an unguarded counter measures the sidecar's retry schedule rather
  than the dataset. Guarded on `coalesce(r.last_attempt_at,'') < $tm`: a genuine attempt stamps a later
  time, a redelivery replays the same one.
  *Read straight out of AGE after the roll* (`cypher('lineage', MATCH (r:Run) WHERE r.attempts IS NOT
  NULL ...`), driving one failing stage trigger:

      attempts=1 -> 88 runs
      attempts=4 ->  1 run
      attempts=5 ->  2 runs

  The 4s and 5s are the counter climbing across real retries, which proves the increment fires; that
  they STOP at 5 proves the guard holds, because the sidecar's schedule is `constant 120s x 4` = five
  attempts and an unguarded counter would have kept climbing on every broker redelivery.
- *Tested as a drift gate, which is a stated limit rather than a shortcut:* `MERGE_RUN` cannot execute
  without AGE and the estate has no AGE-backed fixture, which is why
  `tests/unit/test_a_run_state_does_not_regress.py` reads the statement's source at all. Both guards
  are mutation-proven there, and the behavioural proof is the measurement above.
- *An integration trap the estate's own gates caught:* the board projection is POSITIONAL and three
  column-count tests failed when `r.attempts` was appended while both readers still declared 17 — the
  shape `_LIST_RUNS_BODY`'s own comment warns about ("the caller declares a column count and a mismatch
  is a 500, not a short row"). Two of those three live in `services/lineage/tests`, which the
  invariant+integration layers do not run; only the full configured suite does.
- **READ THROUGH THE DOOR, not only out of the graph (2026-09-15).** The AGE reading above is `psql`,
  which skips the gate that makes this a TENANT fact. Repeated properly: a Dex-minted bearer for
  `alice@example.com` against `GET /runs` on the deployed lineage service returns runs carrying
  `attempts` — `75daeba9-... attempts=5 state=FAIL` among them. `/runs` shows a run only to a caller
  holding `can_get_metadata` on every dataset it wrote, so "FGA-gated per object so a tenant can query
  its own table's rewrites" is satisfied by the board's existing governance rather than a second gate.
- **THE LAST CLAUSE IS BLOCKED BY CONVERGENCE, and that is measured, not assumed.** It asks to observe
  this "on a dataset that actually has fragments to reclaim". There is none: in the last sweep window
  the deployed maintenance pod produced **2,615 dataset outcomes and ZERO with material work**
  (`fragments_removed=0` on every one). The audit record is emitted only when `_did_material_work`, so
  on a converged estate it correctly never fires — which is why the duration is observable today on the
  `compaction.compact` SPAN and not yet on a `compact_dataset` audit row.
  *`test_maintenance_s3_e2e` does not settle it either,* and that was checked: it builds multi-fragment
  fixtures but runs `run_sweep` IN-PROCESS against live S3, so its audit records go to the test
  process's logger and never through the deployed pod.
- *Closes when:* one reclaimable dataset exists in a registered, swept warehouse long enough for a
  deployed tick to compact it, and the resulting `compact_dataset` audit row is read carrying its
  `duration_seconds`. Manufacturing that is an estate mutation with cleanup, not an observation, which
  is why it is named here rather than improvised. The remaining shape of the original ask — a counter on the SINGLE
  deterministic FAIL node, not by minting one id per attempt — and make it FGA-gated per object so a
  tenant can query its own table's rewrites; then observe it on a dataset that actually has fragments to
  reclaim, measured through the running app or a door. Do NOT remove the deterministic run id without
  replacing the flood guard it is.
- **RE-MEASURED AT HEAD 2026-09-16 — TWO OF THE THREE ASKS ARE ALREADY IN, and the third is not where
  the row implies.** `duration_seconds` IS carried: `sweep.py:897` passes it into `audit_material_work`,
  which writes to the `lance.audit` compliance stream keyed on `resource=table:<id>` — so "which
  compaction rewrote my table, and was it slow" has a per-object, FGA-gated answer already. What is
  genuinely absent is the ATTEMPT COUNT: `maintenance/core/lineage_emit.py` carries no `attempts` facet
  (the `attempts` field in `purge.py:140,531` is the TRASH purge's, and `:522-534` records that it is
  deliberately not written because it would undercount).
- **AND THE COUNTER CANNOT BE COMPUTED WHERE THE ROW PUTS IT.** A count of consecutive failures is
  CROSS-TICK state, and the sweep is a cron that holds none — each tick sees one dataset once. The
  emitter therefore has nothing to count. The only place that already has the history is the MERGE
  TARGET itself: every tick merges onto ONE `(:Run)` node by construction of the flood guard, so the
  increment belongs in the graph write — `ON MATCH SET attempts = coalesce(attempts, 1) + 1` in the
  lineage repository — not in a facet the producer computes.
- *Which makes the remaining work a LINEAGE-service change on the provenance write path*, not a
  maintenance one, and larger than "add a counter": it changes MERGE semantics for every deterministic
  run id, not just this lane. Sized and located here rather than attempted, because a wrong increment
  on that path corrupts the estate's account of what ran.

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

**LH-131 · ~~The INGEST stream leaks one durable consumer per run — 3,090 bound, ~100/day, and the health surface documents the opposite~~ — CLOSED 2026-09-15: the per-run JetStream durable is deleted with the run**
`ingest, service-kit, chart` · **HIGH** · phase 2 (ingest), but the resource it exhausts is the lakehouse's own NATS


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15: `ingest/queue.py:445` deletes `ingest-<run_id>` inside `release_run()` after the subject purge,
  matching the durable minted at :404-405, and it is reached on the terminal path via `runtime.py:499`. The terminate
  path was converted to a raised event (`__init__.py:309-312,328`) precisely so that tail still runs, which is what made
  the leak permanent before. `queue_health.py:51-60`'s docstring is now true rather than contradicted.

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

**LH-170 · The lineage replay is idempotent for events the graph ACCEPTS and not for events the gate REFUSES, and no row attributes the DLQ loop to it**
`lineage, chart` · med · found 2026-09-16 by the backlog audit, verified against the live components

- *Why open:* `lineage-pubsub-lineage` is the ONE subscription of eight with no `durableName` and
  `deliverPolicy: all` — every sibling carries `<app>-durable` + `new` (measured live: 8 components,
  1 exception). That is deliberate and the chart says so at `dapr-component.yaml:160-172`: "a restart
  replays the retained stream into the idempotent MERGE ingest — that replay IS the outage-durability
  story", and "a durable cursor would defeat its replay-rebuilds-the-graph recovery story".
- **THE IDEMPOTENCE CLAIM HAS AN EXCEPTION THE COMMENT DOES NOT STATE.** An accepted event MERGEs
  harmlessly on replay — the design working. An event the AUTHORIZATION gate refuses never reaches the
  graph, so it re-parks on every restart and appends a NEW dead letter about an event already in the
  DLQ. That is the feedback loop condition 4's scorecard measured (49 parks within two minutes of a pod
  start, zero before) attributed to its actual mechanism for the first time.
- *This is not a new defect and not a reason to give the subscription a cursor.* It is the missing
  attribution: [[LH-148]] and [[LH-166]] describe the loop's effects and neither names the
  `deliverPolicy: all` half as its cause, so a reader can conclude the transport is unhealthy when the
  transport is doing exactly what it was configured to do.
- *Closes when:* the decision on [[LH-151]]/[[LH-166]]'s ack semantics lands — acking a permanently
  refused event as SUCCESS makes the replay genuinely idempotent and the loop disappears with no change
  to this subscription — and the chart's comment states the exception rather than an unqualified
  "idempotent MERGE ingest".

**LH-171 · Nine governed control-root records are unreadable at HEAD and the estate only WARNs**
`medallion, service-kit` · med · found 2026-09-16 by the backlog audit, re-measured live

- *Why open:* Every listing pass on the deployed medallion producer emits exactly nine
  `transform_spec_malformed` warnings for records under `s3://lance-catalog/_transforms/` — measured
  2026-09-16, 9 in a two-hour window, e.g.
  `path='lance-catalog/_transforms/acme-0904d517…'`. The stored JSON carries fields the current
  `TransformSpec` forbids and lacks fields it now requires, so the model refuses them.
- *Why it matters beyond noise:* these are GOVERNED CONTROL RECORDS — the transform definitions the
  cascade dispatches on. A record the producer cannot parse is a transform that cannot run, and the
  estate's only signal is a WARN on a listing path nobody reads. It is the same shape as the outbox
  lesson: the one path that loses work is the one nothing watches.
- **READ 2026-09-16, AND THEY ARE THREE SCHEMA GENERATIONS, NOT ONE BREAK.** `_transforms/` holds 11
  objects: a directory marker and TEN records. Their key sets:

      current       name, task, cardinality, code_version, from_id, to_id, params, project   x1  (acme-d5e2c64f)
      intermediate  name, ENTRYPOINT,        code_version, from_id, to_id, params, project   x1  (lakehouse-3f59e5b5)
      oldest        LANE, ENTRYPOINT,        code_version, from_id, to_id, params, project   x8

  So nothing is corrupt and nothing was "never valid": two migrations were never run, and one record is
  half-way through — it gained `name` and still carries `entrypoint`.
- *The estate has been running on ONE loadable record*, and the cascade works, so the other nine are
  effectively dead rather than merely unread. The skip is deliberate and documented
  (`transform_specs.py:182-193`: "a record written by an older build whose shape has since tightened
  must not take down the listing").
- *What the migration needs that this measurement cannot supply:* a ruling on the field mapping —
  whether `entrypoint` becomes `task` verbatim, what `lane` becomes (the concept has no successor in
  `TransformSpec`), and what `cardinality` defaults to for a record written before it existed. Guessing
  any of the three writes a wrong governed record, which is worse than an unread one.
- *Closes when:* that mapping is ruled on and the nine are migrated or deleted — the choice is now
  cheap, because the whole set is 10 records and 9 of them share one shape. Then make an unparseable
  control record louder than a WARN, or prove the set empty and keep it so with a gate.

**LH-172 · Every lakehouse pod builds a 64-wide Lance thread pool against a one-CPU quota, and the documented override is ignored**
`catalog, lineage, medallion, maintenance, service-kit` · **HIGH** · filed 2026-09-16 · **RULED 2026-09-16 (owner): accept it for now** — the pools stay host-sized and the memory multiplier is a recorded cost, not a defect to chase

- **THE PREMISE IS MEASURED NOW, not inferred from the docs — and the first two versions of this row
  were wrong in opposite directions, so the measurement is given in full.**
  Varying only CPU affinity and the environment variable, same dataset, same host, threads created by
  one scan:

      visible CPUs   LANCE_CPU_THREADS   threads
           4              unset             11
           8              unset             16
          64              unset             71
           4                32              11     <- the override is ignored
          64                 1              69     <- and ignored in the other direction

  A core-tracking pool exists — 4 -> 11, 8 -> 16, 64 -> 71 is linear in visible CPUs — and
  `LANCE_CPU_THREADS` does not reach it on pylance 11.0.0, in either direction.
- **WHY THE POD SEES 64 AT ALL is the fact that makes this a resilience row.** A cgroup CPU *quota*
  does not reduce visible CPUs. Measured inside the running containers: `/sys/fs/cgroup/cpu.max` reads
  `100000 100000` — one CPU — while `nproc` reads 64, on catalog, lineage, maintenance and
  medallion-producer alike. So every lakehouse process builds a 64-wide pool against a one-CPU budget.
- *And it is a MEMORY bound, not only a contention one:* `lance_docs/guide.md:3288` sizes a write at
  `io_readahead_buffer + num_cpu_threads * batch_size * (raw_vector_size + transformed_vector_size)`,
  so the thread count multiplies the ceiling `affordable_cache_bytes` exists to hold, against the same
  512Mi tier.
- **THE DOCUMENTED FIX WAS BUILT, DEPLOYED, OBSERVED APPLYING, AND THEN REMOVED.**
  `bound_lance_thread_pools()` read the container's quota and `setdefault`-ed `LANCE_CPU_THREADS` at
  all five lakehouse entrypoints; every pod logged
  `lance_compute_pool_bound_to_container cpu_threads=1 quota_cores=1.0`. It still did nothing, because
  the variable is ignored. **A control that provably does nothing is the failure this estate keeps
  finding, not a harmless default**, so the wiring is gone rather than kept with a caveat.
  `cpu_budget_cores` survives — reading the container's own quota is correct and useful — and
  `tests/unit/test_lance_sizes_its_compute_pool_to_the_container_not_the_host.py` asserts the companion
  does NOT come back without the measurement being redone.
- *The citation was spot-checked, which [[LH-047]] requires and I had not done.* `guide.md` is one of
  the five tool-generated bundles `lance_docs/PROVENANCE.md` marks as unverifiable — "a citation from
  these is weaker than one from `spec.yaml` and should be spot-checked against the live docs when it is
  load-bearing". Fetched upstream `docs/src/guide/performance.md` 2026-09-16: it says the same thing
  the bundle says, word for word on both variables. **The bundle is accurate; the software does not
  match its own documentation.**
- *Closes when:* the owner rules on the only lever that demonstrably works. Three options and none is
  free: (1) a **cpuset** on the pods, or `os.sched_setaffinity` at startup — makes every pool that sizes
  by visible CPUs agree with the quota, and pins a latency-sensitive service to one core; (2) a
  **pylance upgrade or upstream report** — the variable is documented and does not work, which is
  theirs to fix; (3) **accept it** and record that the pools are host-sized, which is honest and costs
  the memory multiplier above. Re-measure the affinity table before choosing: this row has been wrong
  twice already.

**LH-129 · The Ray job reads `S3_KEY`/`S3_SECRET` from process env while the work order's `RASK_CREDENTIAL_REF` seam is consumed by nobody**
`medallion, ray-kit, chart, service-kit` · **HIGH** · phase 2 (compute), but it is the standing SECRETS rule · **blocked:** its own ruling — "Phase 2 — do not work ahead of the lakehouse"; counted here, worked after phase 1

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
- **RE-MEASURED IN THE RUNNING POD 2026-09-15, and the violation is wider than the row says.** It is
  not only storage: `ray-lance-head` receives SIX secrets through env and no sidecar —
  `S3_SECRET` plus five `RASK_LINEAGE_TOKEN_SERVICE_*`, every one a `secretKeyRef` into an env var,
  with `S3_KEY=rask-ray-compute` a literal in the manifest. `envFrom` is null and the only mounts are
  `/dev/shm` and the service-account token, so nothing arrives by a sanctioned path today.
- **THE SANCTIONED PATH EXISTS END TO END AND IS UNBUILT, which is what makes this a wiring job rather
  than a design question.** `vend_credentials` takes the caller's RAW BEARER JWT
  (`web_identity_token`) and forwards it to the object store for `AssumeRoleWithWebIdentity`, returning
  a triple scoped to bucket+prefix for 900 s — the STS posture the rule names. The Ray pod already
  holds a credential of exactly that shape, delivered the right way: the projected service-account
  token at `/var/run/secrets/kubernetes.io/serviceaccount/token`, a FILE rather than an env var. And
  the pod can reach the door — measured from inside the container, `http://rask-catalog:2333` answers.
  The blocker the row cited is also gone: `lance_storage_options` now carries `session_token`
  (`objectfs.py:35`), so this lane can hold a vended triple.
- *What is left is the trust wiring, in this order:* the catalog must accept the cluster's own OIDC
  issuer for the Ray identity; that identity needs the FGA tuples a write-tier vend checks
  (`can_write_data` or `can_maintain` on the table); the three job scripts must read
  `RASK_CREDENTIAL_REF` + the token FILE instead of `S3_KEY`/`S3_SECRET`; and the manifest must drop
  both env vars. The five service tokens move to a mounted file in the same pass — a `secretKeyRef`
  into env is the banned path whether the value is a storage key or not.
- *A SCOPED STATIC KEY IS NOT AN ACCEPTABLE INTERIM.* `rask-ray-compute` is already scoped and it is
  still a static key in env; narrowing it further would look like progress and change nothing about
  the rule it breaks.
- *Closes when:* the Ray job vends its storage credential (the catalog's STS door, the same one
  `POST /v1/outbox/credentials` was added to) keyed on `RASK_CREDENTIAL_REF`, and `S3_KEY`/`S3_SECRET`
  leave the pod env; plus a gate over `work_order.to_env` asserting every emitted name has a consumer,
  so a dead field fails a test rather than shipping a contract nobody honours.
- *Not closed by* `cc75585d`, which fixed a DIFFERENT half — the published credential was the RustFS
  ROOT secret; it is now scoped and proven bounded. That made the env-borne credential correct in
  SCOPE while leaving it wrong in DELIVERY.

**LH-101 · ~~The sweep has no per-tick budget and no rotated bucket order, so at estate scale the tail is maintained only if the tick has time left~~ — CLOSED 2026-09-16 (mechanism shipped; the VALUE is an operator setting, not open work)**
`maintenance` · med

- *Why open:* Nothing records which buckets a tick actually reached, so silent starvation of the last buckets is undetectable.
- **RE-MEASURED 2026-09-13 — one third done, one third MIS-FRAMED, one third genuinely open.**
  *Rotation is already there, at a finer grain than this row asks for.* `sweep.py:756` is
  `random.shuffle(uris)`, and :750-755 names this exact starvation: "the discovery listing order is
  deterministic across ticks, so a pass that consistently dies at dataset N never maintained anything
  after N — silently, forever. Shuffling rotates which datasets sit behind a recurring failure point."
  It also argues the axis: per-dataset pacing "lives in the policy stamps, which do not care about
  order", so rotating BUCKETS would be coarser than what already ships.
  *"Nothing records which buckets a tick reached" is too strong.* `_discover_all` logs one line per
  bucket carrying its dataset count and truncation (`sweep.py:191-196`). What it does not record is
  MAINTENANCE coverage — a tick can discover a bucket and then never reach its datasets — so the gap is
  real but narrower than the row states.
- **THE OBVIOUS PLACE TO MEASURE COVERAGE CANNOT MEASURE IT, and that was found by building it and
  checking the branch before shipping (2026-09-14).** Adding per-bucket coverage to `plan_sweep` —
  compare the buckets `_discover_all` found against the buckets the planned units and decisions cover —
  produces a warning that can NEVER fire: `_exclude_trashed` (`sweep.py:314-326`) PARTITIONS the
  discovered list, returning `(kept, excluded)` whose union is the input, and every kept uri becomes a
  `DatasetWorkItem` while every excluded one becomes a `DatasetResult`. So on the queue lane coverage is
  total BY CONSTRUCTION and the gate would be decoration. It was written, gated green, and reverted
  unshipped.
- *WHERE THE GAP ACTUALLY LIVES, therefore:* not in planning but in EXECUTION. On the queue lane the
  units are published and executed by a separate subscription in another process, so "did this tick's
  work actually happen" cannot be answered from the planner at all — it needs the unit outcomes
  correlated back to the tick that planned them. On the serial lane it is `run_sweep`'s loop, which can
  die partway. Either is a real measurement; `plan_sweep` is not the place for it.
  *Genuinely absent:* any per-tick time budget. No deadline, elapsed check or `max_seconds` exists
  anywhere in the sweep or its API.
- *And the budget is not a free addition, which is why it is not done here.* Its VALUE is a decision, not
  a default to infer: too small and a large estate never finishes a pass, too large and it is decorative.
  It also needs to interact with whatever happens when a tick outruns its interval — overlap or skip —
  which is not established. A budget cut mid-dataset must stop BETWEEN work items, never inside a
  compaction.
- **THE BUDGET LANDED 2026-09-16, on the lane the estate actually runs.** `execute_within_budget`
  (`sweep.py`) stops BETWEEN work items — never inside one, because a cut mid-compaction leaves a
  rewrite half-done — and exhausting it logs `sweep_budget_exhausted` with the executed and remaining
  counts, since stopping quietly would be this row's own starvation with a setting attached.
  `MAINTENANCE_SWEEP_BUDGET_SECONDS` / `maintenance.sweepBudgetSeconds` defaults to **0 = unlimited**,
  so an estate that sets nothing behaves exactly as before; the row is right that the VALUE is an
  operator decision and inferring one would be guessing.
- *WHICH LANE it bounds, measured rather than assumed:* `routes.py` has two. With `workTopic` set it
  PLANS and enqueues, and execution is distributed across subscriptions that ack for themselves — a
  per-tick budget is meaningless there. Without one (line 115) it calls `run_sweep`, which executes
  every planned dataset serially. The deployed estate runs the serial lane, because `workTopic` is empty
  — the same fact [[LH-153]] turned on and [[LH-127]]'s `maintenance-durable` residue records.
  The setting is therefore plumbed into the maintenance Deployment only; it was briefly added to the
  worker template and removed, because the worker executes queued units and never runs a tick.
- **THE COVERAGE HALF LANDED 2026-09-16, and it is the budget's other end.** `report_bucket_coverage`
  logs `compaction_bucket_maintained` with `planned` and `maintained` per bucket, beside the
  `compaction_bucket_discovered` line discovery already emits. A bucket with nothing maintained STILL
  reports, at `maintained=0`, because absence reads as "no such bucket" — which is exactly what made
  the starvation invisible.
- *Per BUCKET, and the shuffle is why:* rotation deliberately breaks bucket ordering, so a truncated
  tick starves buckets PARTIALLY. Only a per-bucket count shows that; a tick-level "stopped early"
  cannot say whose datasets went unmaintained.
- **CLOSED, and against the row's own ask.** It asked for "an explicit per-tick time budget — checked
  between work items, with its value a chart setting rather than a literal" and for maintenance coverage
  per bucket. Both shipped: `execute_within_budget` + `maintenance.sweepBudgetSeconds`, and
  `report_bucket_coverage`. Rotation was already done and must not be replaced with a coarser bucket
  rotation.
- *THE TITLE'S COMPLAINT IS ADDRESSED EVEN AT THE DEFAULT, which is the part worth being precise about.*
  `sweepBudgetSeconds` defaults to 0 = unlimited, so a tick is still as long as it needs to be until an
  operator sets a value — but the starvation is no longer SILENT either way: every tick now logs
  `compaction_bucket_maintained` with planned-vs-maintained per bucket, so a starved tail is visible
  before anyone chooses a number. "Undetectable" was this row's actual defect; unbounded was its
  symptom.
- *What is NOT done and is deliberately not a row:* choosing the value. Too small and a large estate
  never completes a pass, too large and the bound is decorative; the right number depends on the dataset
  count and the tick interval, and the coverage line now prices any candidate directly.

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

**LH-105 · ~~No door anywhere in the estate rebuilds an index in place, so a mis-parameterised vector index is unrepairable through the API~~ — CLOSED, DEPLOYED AND OBSERVED 2026-09-15**
`catalog, maintenance` · med


- **CLOSED 2026-09-15** (`22d03e9f`, corrected by `2a5b8c64` and `5296f3dc`), deployed on `main-5296f3dc`
  and observed on the running catalog.
  `POST /v1/table/{id}/maintenance/reindex` reads the live index's parameterisation through BOTH calls
  (`describe_indices()` for name/type/column/details, `index_statistics()` for the `num_partitions` the
  first does not carry), merges `body.params` over it, and rebuilds under `replace=True` — queued onto
  the index lane where a topic is configured, inline where it is not. Owner-gated: `maintenance/reindex`
  maps to `can_drop` explicitly, because an unmapped table suffix falls through to `can_write_data`.
  *TWO DEFECTS IN THE FIRST CUT, BOTH FOUND BY THE PROCESS RATHER THAN BY READING IT.* The new door
  refused no branch — the exact pattern `ff9604be` had fixed on the drop door hours earlier — and
  re-checking found its three `/maintenance/` siblings the same, since not ACCEPTING a parameter is not
  refusing it (FastAPI drops an undeclared query parameter in silence). And a name that was simply not
  there answered **500**: `IndexNotFoundForRebuildError` was a bare `LookupError` and reached the
  catch-all. Both are fixed and gated — the branch gate derives its doors from the mounted ROUTES, so a
  fifth verb inherits it without an edit, and the 404 is pinned through `ns_errors._STATUS` rather than
  a literal.
  *Observed on `main-5296f3dc`:* `{"index_name": "no_such_idx"}` -> **404**
  `IndexNotFoundForRebuildError`; the same call with `?branch=work` -> **406**.

- *Why open:* Nothing composes a rebuild. The capability exists one layer down — `replace=True` on
  pylance's own create call — and no service exposes it, so the repair for a corrupt or
  mis-parameterised index is a hand-run script against the bucket with the operator's own credentials,
  outside authz and emitting no lineage.
- **RE-MEASURED 2026-09-14 — the premise holds, it is SHARPER than stated, and the fix needs no new
  vocabulary.** `index_build.build_index` deliberately does not pass `replace`, and says why
  (`index_build.py:84-87`): the spec's request carries no such field, so no caller can ask for it and
  pylance's defaults apply — **a scalar index replaces, a vector index REFUSES a duplicate name**. So
  half the estate's indices are already repairable by re-issuing the build unit and half are not, which
  is a narrower and more actionable statement than "no reindex operation exists".
  *The drop half already exists and is spec-native, so nothing has to be invented:* `DropTableIndex`
  is `spec.yaml:1850,1867` (`/v1/table/{id}/index/{index_name}/drop`), the catalog serves it at
  `api/v1/endpoints/indices.py:180-195` over the native `drop_table_index` op with a `DROP_INDEX`
  lineage emit, and it is live on the deployed catalog (read off `/openapi.json` on port 2333,
  2026-09-14). A reindex is therefore drop-then-create against doors that both exist.
- **RE-MEASURED 2026-09-15 AGAINST pylance 11.0.0 (the version the deployed catalog runs — local and
  pod agree), AND IT CHANGES THE PRESCRIBED COMPOSITION. A REINDEX MUST NOT DROP FIRST.**
  `LanceDataset.create_index` carries `replace: bool = False` and `create_scalar_index` carries
  `replace: bool = True` — read off the signatures, then driven: a same-name vector rebuild is REFUSED
  (`LanceError(Index): Index name 'vidx' already exists`) at the default and **ACCEPTED under
  `replace=True`**; a scalar rebuild is accepted either way. So the vector case, which this row
  correctly identifies as the one that cannot repair itself, is repaired by a flag pylance already
  has — not by a drop.
  *Drop-then-create is the WORSE composition, which is why this supersedes the ask rather than
  refining it.* It opens a window in which the table has no index at all (a search silently degrades
  to a full scan), and if the rebuild then fails the table is left with nothing — strictly worse than
  the mis-parameterised index the operator was repairing. `replace=True` swaps the index in one
  commit and has no such window.
- **A FAITHFUL REBUILD IS POSSIBLE, AND IT TAKES TWO READS, NOT ONE.** Driven on 11.0.0 against an
  `IVF_PQ(num_partitions=4, num_sub_vectors=2, metric='cosine')` index:
  `describe_indices()` yields `.name`, `.index_type`, `.field_names` (so the column needs no separate
  lookup), `.type_url` and `.details` — for a vector index `{"metric_type": "COSINE", "compression":
  {"type": "pq", "num_bits": 8, "num_sub_vectors": 2}, "runtime_hints": {...}}`; for `Inverted` the
  whole tokenizer record (`base_tokenizer`, `language`, `with_position`, `stem`, ...); for `BTree` and
  `Bitmap` an EMPTY `{}`, which is full fidelity because they carry no build parameter beyond the
  column.
  **`num_partitions` is the one field `describe_indices()` does not carry** — measured absent from
  `.details` and present as `indices[0].num_partitions` in `index_statistics(name)`. So a vector
  rebuild that reads only the non-deprecated call silently re-partitions the index to pylance's
  default. Both calls are required.
  *Two traps checked and cleared:* `create_index(metric="COSINE")` is accepted, so the upper-case
  spelling `.details` reports needs no normalising; and `create_scalar_index(index_type="BTree")` — the
  mixed-case spelling `describe_indices()` reports — is accepted verbatim.
  *And the vector/scalar discriminator is OBSERVABLE rather than guessed:* `.type_url` is
  `/lance.index.pb.VectorIndexDetails` for a vector index and `/lance.table.<Kind>IndexDetails` for
  every scalar one. `IndexWorkItem.kind`'s docstring objects that a worker "guessing between them would
  build a different index than the caller asked for" — that objection does not reach this door, which
  reads what exists instead of inferring from `index_type`.
- **THE DOOR BELONGS ON THE CATALOG, NOT ON `services/maintenance`, and the row's placement is the one
  part that cannot be honoured as written.** `services/maintenance` has NO human-facing HTTP surface:
  `api/routes.py:282-294` mounts only the two cron bindings, each behind `require_dapr_token`, and
  `api/index_work.py` / `api/work.py` / `api/arrival.py` are Dapr subscriptions under the same token.
  Adding an operator door there is a new CLASS of surface for that service, not a new operation on it.
  The catalog already carries exactly this shape: `api/v1/endpoints/maintenance.py` serves
  `POST /v1/table/{id}/maintenance/{preview,run,compact}` — non-spec composite verbs under a
  `/maintenance/` sub-path, owner-gated on `can_drop`, and `compact` already splits queue-or-inline on
  `maintenance_work_topic` so the two services cannot disagree about whether a worker exists. A reindex
  is that pattern with `maintenance_index_topic` and an `IndexWorkItem`. (An earlier note of mine
  called a composite verb on a spec object's route grammar "a design decision with real downside";
  that is materially weaker than stated — the precedent is established, sanctioned and three doors
  wide, and this row needs no owner ruling to follow it.)
- *Closes when:* `POST /v1/table/{id}/maintenance/reindex` on the catalog reads the live index's
  parameterisation through BOTH calls above, refuses a branch like the twelve sibling doors, and
  rebuilds under `replace=True` — queued onto the index topic where one is configured and inline where
  it is not — with a test that covers the vector case and fails if the rebuild loses `num_partitions`.

**LH-106 · Resilience rows exist only inline in `RESILIENCE.md`: the chaos harness was never automated, DLQ sidecar parking was never driven live, and lineage scale-0 restart-replay was never re-verified**
`lineage, chart` · med

- *Why open:* The pull-a-service chaos rows were driven by hand and never encoded (deliberately out of default `make e2e` — they scale shared infra). Gap #2's poison-inject → Dapr `deadLetterTopic` parking has only unit tests (the #83 DLQ drive exercised the OUTBOX surface, not sidecar parking) and the runbook §6.5 it pointed at no longer exists. Honesty-note row 1 (lineage scale-0 → restart-replay under the per-app queue-group components) still awaits its one-shot re-verify on a fresh deploy.
- **TWO OF FOUR LANDED 2026-09-14, and the poison drive was worth more than the row expected.**
  *Gap #2 is DRIVEN AND OBSERVED.* A well-formed trigger naming a nonexistent source, published to
  `medallion.bronze` on `bronze-to-silver`, took five deliveries — each `medallion_stage_failed` →
  RETRY — and was then parked: `dapr_dead_letter_parked app='bronze-to-silver'
  dlq_topic='dlq.bronze-to-silver' source_topic='medallion.bronze' token='lh106-poison-probe'`, with
  `/dlq-event` answering 200. That is the first time the retry → dead-letter → parking chain has been
  seen end to end on this estate; the #83 drive only ever exercised the outbox.
  *And it falsified the schedule.* The whole thing completed in **3.553 seconds** against a chart that
  documents ~7.5 minutes: `policy: exponential` ignores `duration` outright (dapr/kit `NewBackOff`
  reads it only under `PolicyConstant`), so the window was cenkalti/backoff's 500ms default times 1.5
  — 0.5 + 0.75 + 1.125 + 1.6875 ≈ 4s. Every measured gap sat inside its jittered band. Fixed to
  `constant 120s x 4` (480s over 5 attempts, deterministic) and gated by
  `tests/unit/test_the_cascade_retry_window_is_the_one_the_chart_states.py`, which COMPUTES the window
  from the rendered policy and checks both edges — the too-short one and the one where backoff plus
  the handler's own time outruns the broker's 720s `ackWait`.
  *RE-DRIVEN AFTER THE ROLL (helm revision 156) and it matches the design to 1.1 seconds:* attempts at
  18:38:31.896 / 18:40:32.107 / 18:42:32.212 / 18:44:32.341 / 18:46:32.960, parked 18:46:32.992 —
  steps of 120.211 / 120.105 / 120.129 / 120.619s with no jitter, **481.096s end to end against a
  designed 480s**, and 134x the 3.584s it managed before. The roll alone was not enough: a Resiliency
  edit reaches a sidecar only on restart, so all six subscriber app-ids were rollout-restarted and each
  logged `Loading Resiliency configuration` before the re-drive.
  *The dangling pointer is rewritten* to `RUNBOOK-oncall.md#dlq-parking--a-delivery-gave-up`, the
  anchor the alerting rules already cite.
  *A finding the row did not anticipate is split out as [[LH-151]]:* a delivery that is not a valid
  CloudEvent is dead-lettered in 21ms with zero retries, and parks where `/dlq-event` cannot read it.
- **THE TWO REMAINING ASKS ARE ONE PIECE OF WORK, and trying to hand-drive the second is what showed
  it (2026-09-14).** Re-verifying lineage scale-0 → restart-replay needs three events published while
  lineage is down. Hand-driving that against the live estate has no good form: `/lineage-events`
  applies `enforce_bus_authz` (`api/dapr.py:52-55`), so a synthetic author is REFUSED — which still
  proves delivery, the property actually at risk, but bumps the refusal counter a real alert watches —
  while an author that IS authorized injects fabricated provenance into the authoritative AGE graph.
  Neither is acceptable ad hoc, and both are fine inside an isolated fixture. The e2e suites already
  establish that shape: `test_maintenance_s3_e2e.py` creates uuid-suffixed buckets and touches only
  those.
- *Closes when:* one harness covers both — a `tests/e2e-py/test_chaos_e2e.py` driving the pull-a-service
  rows (lineage scale-0 → replay included) against a uuid-suffixed throwaway namespace, behind an
  env-gated `make e2e-chaos` target kept OUT of `e2e-ci`'s list, the way the other eleven suites are
  gated. Scoped 2026-09-14; the scaffold and the gating convention already exist.

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

**LH-109 · ~~Eleven stable live-suite failures are still unclassified after the `/runs?limit=1000` drift repair~~ — STRUCK 2026-09-15 (THE ASK IS MET: every leg carries a verdict)**
`medallion, maintenance, catalog, lineage` · was med

- *Why open:* Driven twice against the deployed estate 2026-09-10: 13 failed / 117 passed / 3 skipped / 1 xfailed. One class was repaired (five call sites still sent `limit=1000` after the board was capped at 200; 422 count 6 → 0). The stable eleven — `governed_union` x4, `maintenance_e2e`, `maintenance_s3` x2, `medallion_e2e`, `media_e2e`, `outbox_e2e` — are all in scope and none carries a verdict; two further legs flip between consecutive runs.
- **RE-DRIVEN 2026-09-14, AND MOST OF WHAT THE ROW COUNTS WAS THE HARNESS.** The first run read
  **37 failed / 77 passed / 17 errors** — far worse than the row's figures, and almost none of it the
  estate. `e2e_live.sh` discovered the object store as `<release>-rustfs-io` while this release runs
  MinIO (`<release>-minio`), so the address came back empty and the script exported the literal string
  `http://`; the credentials were stale the same way. Seventeen legs then died inside botocore with
  `ValueError: Invalid endpoint: http://`, naming neither the service nor the script, and eight more
  failed on the same empty endpoint. Fixed in `c8dcb030`, together with the systematic half — every
  address was exported as `http://$X` unconditionally, so anything undiscovered became the scheme
  alone, which no suite recognises as absent, so its own skip never fired.
- **AFTER THE FIX, the same estate reads `15 failed / 117 passed / 3 skipped / 1 xfailed`** — the
  **117 passed is exactly the row's own baseline**, so the measurement is comparable again. Nine suites
  went to zero: `track_a_acceptance` 11→0, `credential_isolation` 8→0, `object_store_cas` 5→0,
  `registry_cas` 2→0, `client_direct` 2→0, `maintenance_s3` 10→1, plus `duckdb_lance`, `multibase`
  and `warehouses`.
- *TWO VERDICTS GIVEN, both SUITE-DRIFT, both fixed and verified live:* `maintenance_s3`'s branch
  refusal asserted the report's `incomplete` list, and `reconcile.py:860-863` deliberately routes it to
  `excluded_datasets` because `incomplete` gates the purge — restoring the old contract would stall
  purges on every dataset that has a branch. And its `swept_uris` omitted `<dataset>/tree/<branch>`,
  which `optimize.discover_datasets` descends on purpose ("a BRANCH is a full dataset the parent
  contains rather than part of it"). That suite is now 10 passed, from 10 errors.
- **THE RAY-LANE CLUSTER IS [[XC-002]] FIRING, and restarting the head fixed three of them.** Every
  cascade stage job was dying `SignatureDoesNotMatch` against `rask-minio` because the hand-applied Ray
  head still held a pre-rotation credential ([[XC-001]]: nothing makes a pod re-read a rotated Secret).
  A `rollout restart` — image preserved — produced `RAY-STAGE OK stage=gold` and took
  `dummy_lane::TERMINAL_event_READS_BACK`, `governed_union::media_lane_derives_under_governance` and
  `governed_union::train_lineage_lands_attributed_under_governance` green. **VERDICT for those three:
  CONTAMINATION** — a deployed-environment defect the chart already fixes, not a suite or estate one.
- **A FOURTH VERDICT: `governed_union::test_fga_deny_drops_promotion_and_regrant_restores` is
  SUITE-DRIFT, and the "revoke did not take" message is misleading rather than alarming.** Read off the
  live store 2026-09-14: `namespace:acme-silver#can_create_table` computes from `#writer`, whose union
  includes `tupleToUserset: namespace:acme-silver#parent -> warehouse:acme-bucket#writer`; the parent
  IS `warehouse:acme-bucket`, and `user:service-bronze-to-silver` holds `writer` on it (alongside the
  producer, media-to-silver and silver-to-gold). The leg deletes only the namespace-level writer and
  owner tuples, so the rung survives by WAREHOUSE INHERITANCE — the model working as designed, not an
  ungated cascade.
  *NOT fixed here, deliberately.* The obvious repair — also delete `warehouse:acme-bucket#writer` —
  strips a grant the live cascade depends on, and a leg that fails between the revoke and its regrant
  leaves the stage runner unable to write at all. The safe shape is to assert against a namespace whose
  parent grants the subject nothing (a fixture namespace the leg owns), which is a redesign of the leg
  rather than an edit, and it should not be improvised against a live estate.
- **MEASURED, not inferred, after all of the above (third full drive, 2026-09-14):
  `13 failed / 119 passed / 3 skipped / 1 xfailed`.** Diffed against the previous drive: four legs
  fixed (`dummy_lane::TERMINAL_event_READS_BACK`, `governed_union::media_lane_derives`,
  `governed_union::train_lineage_lands_attributed` by the Ray head restart; `maintenance_s3::sweep_names_exactly`
  by the branch-set fix), **eleven failing in BOTH**, and **two that flipped in** —
  `lineage_e2e::test_medallion_column_lineage` and
  `observability_e2e::test_distributed_trace_spans_catalog_to_lineage`, both of which PASSED in the
  previous drive.
  **BOTH FLIPPERS NOW HAVE A CAUSE, and they are not the same one.**
  `lineage_e2e::test_medallion_column_lineage` fails with
  `psycopg.OperationalError: consuming input failed: server closed the connection unexpectedly` —
  AGE backends are being OOM-killed (five `terminated by signal 9` events in 24h, `restartCount: 0`),
  which takes the whole graph into crash recovery and drops every in-flight connection. That is a
  real, live defect, and it is FIXED AND OBSERVED (helm revision 157, `rask-age-0` recreated
  20:34:10Z with limits 2Gi / requests 512Mi): a 3m15s lineage-heavy drive of
  `test_lineage_e2e.py` + `test_medallion_e2e.py` came back **14 passed with ZERO
  `server closed the connection` errors**, and the whole `lineage_e2e` suite — this flapper
  included — is green. Given room the pod settles at **652Mi, above the old 512Mi limit**, so the
  working set never fit and the kills were structural. Fixed rather than filed: AGE asked `lance.resources` for a `comp="age"`
  tier that did not exist and fell through to the stateless-app default (128Mi/512Mi) against a 385Mi
  steady state. `observability_e2e::test_distributed_trace_spans_catalog_to_lineage` is a DIFFERENT
  cause — a 500 from GreptimeDB's `/v1/sql`, whose pod has restarted 3 times — and still needs a
  verdict.
  *So the row's own shape is vindicated and is now current rather than four days old:* a stable core of
  ELEVEN plus TWO that flip between consecutive runs. `119 passed` is two better than the baseline it
  records. The two flippers are the ones the row says to "run repeatedly rather than once", and they
  are now named.
- **A FIFTH VERDICT: `catalog_live::test_catalog_errors_translate_to_domain_errors` is SUITE-DRIFT,
  and it looked like a security defect until the subject was read correctly.** The estate answers 404
  (naming the table) where the leg expects 403, and a 403 on a table that DOES exist sat in the same
  log — which reads as an existence oracle. It is not: owner ruling 2026-09-11
  (`fga_deps.py::_absent_to_a_reader_of_the_parent`) converts a denied READ to the spec's 404 **only
  for a caller holding the parent's read rung**, on the grounds that such a caller can already list the
  parent. The check that settles it must use the real subject — the Dex-encoded
  `user:CiQwOGE4Njg0Yi1kYjg4...`, not `user:alice`, which holds nothing — and that subject holds
  `reader` on `namespace:media` (read off the live store 2026-09-14). So 404 is correct and the leg
  encodes the pre-ruling contract.
  *Fixed, and no coverage was dropped:* the no-oracle property needs two identities and a real object,
  which this leg has neither of; all three conditions are pinned at the integration layer by
  `tests/integration/test_an_absent_object_is_not_found_rather_than_forbidden.py`, including
  `test_an_EXISTING_forbidden_table_is_still_403`. Verified live: the suite is 3 passed.
- **A SIXTH VERDICT: `medallion_e2e::test_produce_cascades_bronze_to_gold` is SUITE-DRIFT, and the
  assertion was UNSATISFIABLE by construction.** It compared a global run COUNT before and after
  (`>= before + 3`), read from `GET /runs` with no limit — and `runs.py:_RUNS_RETURN = 200` is both the
  default and the `le=` maximum, so once the estate passed 200 runs `len()` saturated at 200 and no
  cascade could ever satisfy it. The failure message said so plainly and was read as a broken cascade:
  `runs 200->200, expected >= 203`.
  *The cascade was working the whole time,* which the stage runners confirm for the drive's own token:
  `medallion_stage_moved transition='bronze->silver' token='e2e-6f4e7e2652124b4b'` at 20:40:08, then
  silver→gold at 20:40:40 publishing `acme-gold$catalog` — under a DIFFERENT token, because the
  publication head mints the next trigger's token from the publication event id, so a token does not
  survive a tier boundary by construction.
  *Fixed by counting IDS, not rows:* `/runs` is newest-first, so a run created seconds ago is on the
  page whatever the total — the content does not saturate even though `len()` does. A set difference
  proves what the count was written to prove and keeps proving it as the estate grows. Verified live:
  the suite is 5 passed.
- **FOURTH FULL DRIVE, 2026-09-14 21:04-21:25, after the Ray-head restart and the AGE resize:
  `10 failed / 122 passed / 3 skipped / 1 xfailed`.** 122 passed is FIVE better than the baseline this
  row records. Confirmed fixed and no longer failing: `catalog_live`, `medallion_e2e`,
  `maintenance_s3` (all three by the fixes above) and `lineage_e2e::test_medallion_column_lineage` (by
  the AGE resize).
  *A hypothesis that did NOT hold, recorded so it is not re-tried:* the outbox legs were expected to
  recover with AGE, on the reasoning that the relay drains by ingesting into the graph and a dropped
  connection would make it report 0. Both still fail, so the drain reporting `outbox_drained: 0`
  against an event its own `list_events` confirms is staged has a different cause.
- **A SEVENTH VERDICT, and it was INVISIBLE until a crash was fixed:
  `outbox_e2e::test_reconcile_sweep_drains_a_staged_outbox_event` is SUITE-DRIFT.** The drain was
  throwing `UnboundLocalError` on every tick (fixed in `07076737`), which aborted the whole loop and
  reported `outbox_drained: 0` with no reason. With the branch executing, the estate says exactly what
  it is doing: `lineage_outbox_event_unauthorized outbox_key='36944760-...@COMPLETE' author='e2e'
  reason='can_write_data required to amend run ...: e2e_outbox_ds'`, and the tick totals
  `outbox_drained=0 outbox_stranded=4`. The leg stages an event authored by `e2e`, which holds no
  grant, and the relay's authz gate — added deliberately to close the stage-instead-of-publish bypass
  (`tests/unit/test_the_outbox_relay_refuses_what_the_bus_door_refuses.py`) — refuses it. The leg
  predates that gate.
  *NOT fixed here, for the reason the FGA leg was not:* the repair is to stage as an author the estate
  authorizes for that dataset, and inventing that grant against a live estate is a governance mutation,
  not a test edit.
  *An operational fact worth its own line:* four unauthorized probe events are now stranded in
  `_lineage_outbox` and will stay there, because `e2e` will never be authorized. Every future drive
  adds another — measured going 4 -> 5 on the very next one. The drain reports them correctly now,
  which it could not before.
- **AN EIGHTH VERDICT, the same cause: `outbox_crash_e2e::test_sigkilled_producer_loses_nothing` is
  SUITE-DRIFT.** It stages through a real SIGKILLed producer rather than a literal, so it was worth
  driving separately after the drain fix — but `build_run_event(operation="e2e_crash_probe",
  author="e2e", ...)` stamps the same unauthorized author, and its event joins the stranded pile
  (`outbox_drained=0 outbox_stranded=5` on every tick of that drive). Its failure message —
  "every sweep skipped and the run stayed staged" — is half right and misleading: there WERE 25
  `lineage_reconcile_skipped_locked` in the window, which is the documented single-flight contract
  working, but the run would not have drained on an unlocked tick either. Both outbox legs close the
  same way, together with [[LH-109]]'s FGA leg: an e2e identity the estate actually authorizes.
- **A NINTH AND TENTH VERDICT, both on `maintenance_e2e::test_sweep_compacts_real_datasets_and_meters`,
  because fixing the first exposed the second.**
  *NINTH — SUITE-DRIFT, fixed:* the leg fired ONE blind trigger at the sweep route and asserted
  `status != "skipped"`. The sweep is single-flight; a tick that finds one in progress answers 200
  `skipped` and does no work, which is the documented contract. `test_outbox_e2e` measured the cost of
  ignoring that — "one sweep checks 347 datasets in 158 s against an `@every 300s` cron, so a single
  blind trigger lands on a busy lock about half the time" — and retries. This leg did not, so it
  reported a healthy estate as an overlapping sweep on roughly every other drive. Now retries in the
  same shape as its sibling rather than a second invention.
  *TENTH — CONTAMINATION, not fixed:* with the retry in place the leg reaches its real assertion,
  `summarize(swept)["errors"] == {}`, and fails on
  `s3://acme-bucket/4750a5b9_acme-bronze$events` → `403 AccessDenied` on a `_versions/` list. That is
  not a credential defect: `acme-bucket` exists, maintenance presents `rask-maintenance`, the
  provisioning Job for revision 157 reports "Attached Policies: [rask-maintenance]", and that policy
  grants `s3:ListBucket` on `arn:aws:s3:::*`. The path is STALE REGISTRY RESIDUE — the catalog's
  registered uri for that table is `s3://acme-bucket/medallion/bronze`, which compacted normally in the
  same tick (`compaction_distributed_nothing_to_do`). The lineage reconciler reports the same id
  independently in its `unreadable` set: "'4750a5b9_acme-bronze$events' names no storage location — a
  relative path cannot say whether the data is there", one of **24**. So `errors == {}` is
  unsatisfiable on a long-lived estate, and the sweep reporting them is the sweep working.
- **THE TWO QUALITY LEGS ARE NARROWED TO THE PRODUCER, not yet a verdict — recorded so the next
  reader starts where this stopped.** `governed_union::test_governed_allow_full_cascade_with_quality_verdicts`
  fails `silver["quality_passed"] is True` with the field NULL (and `consumed_from_version` /
  `consumed_to_version` null beside it); `governed_union::test_quality_gate_blocks_bad_batch_and_records_verdict`
  fails waiting for `quality_passed is False`. Same field, both directions.
  *What is established:* the gate RUNS — `medallion_gate_resolved gate_source='declared' review_band=0.42`
  then `medallion_quality_blocked token='5215f78ca636'`, the blocked leg's own token. And lineage is not
  refusing it: the only `ingest_run_mutation_denied` / `ingest_denied` lines in the window name the
  `e2e` outbox probes, never a medallion run.
  *A hypothesis ruled OUT, so it is not re-tried:* `SET_WROTE_QUALITY` is a `MATCH ... SET` on an
  existing WROTE edge, which looks like a silent no-op against a missing edge. It is not — it runs in
  the SAME ingest as the edge it matches (`repository.py:336-356`), guarded by `if assertions:`.
  *ANSWERED — VERDICTS ELEVEN AND TWELVE: CONTAMINATION (configuration), and both legs now SKIP with a
  reason instead of failing.* The measurement is a chart toggle that ships OFF, traced end to end:
  `values.yaml:1366 medallion.quality: false` (no override in the live release) → `medallion.yaml:586-590`
  gates `MEDALLION_QUALITY_ENABLED` on it → neither running stage runner carries that env →
  `config.py:448 quality_enabled` defaults False → `transform.py:1075 if settings.quality_enabled:`
  guards `assert_quality`, whose output is the ONLY producer of the `dataQualityAssertions` facet
  (`schemas/events.py:118-124`) → `repository.py:345-346` writes `quality_passed` only `if assertions:`.
  So NULL is correct and the legs read it as a broken gate. The gate is a different mechanism and fires
  regardless — `transform.py:1065` states it: "THE STAGE RUNNER MEASURES; IT DOES NOT RULE."
  *Why it only bites here:* `test_governed_union_e2e.py` is not in `e2e_stack.sh`'s list, so it runs
  only via `e2e_live` against a deployed release. Fixed the way this suite already handles a missing
  prerequisite — `pytest.skipif` naming the toggle — with `e2e_live.sh` discovering it from the running
  POD rather than the values, per its own "every value is discovered" rule. Driven live: 2 passed,
  2 skipped, and the suite's only remaining failure is the FGA-deny leg above.
- **VERDICTS THIRTEEN AND FOURTEEN, both SUITE-DRIFT, both fixed and verified live — and both are the
  200-run board again.** `media_e2e::ingest_media_derives_artifacts` counted runs with `len()` and
  failed `assert 200 >= (200 + 2)` against a media lane that had flowed; converted to a set difference
  on run ids, now 1 passed. `ray_train::train_to_blessed` asked `/runs?limit=300` against
  `le=_RUNS_RETURN` (200), which is a 422 before the handler runs, so its poll burned the full 120s
  reporting that an attributed `service-trainer` run never appeared; the limit is dropped rather than
  lowered, now 2 passed.
  *That is the SIXTH instance of the defect this row already records as repaired* — the earlier pass
  fixed five call sites still sending `limit=1000` after the board was capped. Swept the rest of
  `tests/e2e-py` afterwards: the only other bounded read is `/events?limit=200` against
  `_EVENTS_RETURN = 500`, which is within bounds.
- **VERDICTS FIFTEEN AND SIXTEEN complete the pass — every leg now carries one.**
  *FIFTEEN — `observability_e2e::test_logs_populated`, SUITE-DRIFT, fixed:* it asked GreptimeDB to
  count logs WITHOUT a trace_id, and that engine cannot answer it. `trace_id` is indexed, so any
  predicate that must MATCH NULLs on it 500s — measured live against `opentelemetry_logs`
  (**101,385,870 rows**): both `NOT (trace_id IS NOT NULL AND trace_id != '')` and its De Morgan twin
  `trace_id IS NULL OR trace_id = ''` answer HTTP 500, while the positive form returns 18,422,464 in
  75 ms. So the leg reported "logs not populated" about a table holding a hundred million rows.
  Computed as total minus the trace-carrying count instead — same number, two supported queries.
  Verified live: it passes.
  *SIXTEEN — `observability_e2e::test_distributed_trace_spans_catalog_to_lineage`, SUITE-DRIFT, NOT
  fixed, and it is the second flapper this row tracks:* it drives NO traffic. It reads the newest 200
  rows of `opentelemetry_traces` and requires a trace joining catalog + lineage that also carries a
  `PublishEvent` span — so it passes or fails on whatever the estate happened to do in the preceding
  moments. Consecutive drives show exactly that: run 1 failed `logs_populated` and passed this; run 2
  passed `logs_populated` and failed this.
  *Its fix is a fixture decision, not an edit:* the span it needs is produced only by a governed WRITE
  through the catalog, so making it deterministic means the observability suite starts mutating the
  estate. That is worth deciding rather than improvising.
- **FINAL MEASUREMENT 2026-09-15: `4 failed / 126 passed / 5 skipped / 1 xfailed` in 13:59** — against
  the `37 failed / 77 passed / 17 errors` this row's re-drive opened with, and `126 passed` is NINE
  above the baseline the row itself records.
- *WHY THIS IS STRUCK RATHER THAN CARRIED:* the row's ask is "give each of the eleven a verdict —
  SUITE-DRIFT / ESTATE-DEFECT / CONTAMINATION / ALREADY-FIXED", plus running the two flaky legs
  repeatedly rather than once. **Sixteen verdicts were given and ten legs fixed**; both flappers were
  driven repeatedly and each has a named cause. A stricter closing condition was briefly written here
  (requiring the residual four to be FIXED) and is removed as scope the row never asked for.
- *NOT ONE LEG WAS AN ESTATE DEFECT IN THE ASSERTION IT MADE.* The estate was right and the suite had
  drifted around it — which is the finding, since these failures had been read as broken product since
  2026-09-10.
- *What it leaves:* four legs still fail, each with a verdict, and the residual work is [[LH-152]].


**LH-152 · Four live e2e legs cannot pass against a governed estate: three stage provenance as an identity that holds no grant, and one asserts zero errors against 24 unreadable registry entries**
`lineage, medallion, maintenance, catalog` · med · found 2026-09-15 completing [[LH-109]]'s verdict pass · **blocked:** owner rulings on how an e2e probe obtains provenance-write authority, and the rest of its (a)/(b) pair

- *Why open:* each carries a verdict and none is fixable as a test edit — all four need a decision
  about the estate, which is why they are here rather than left inside a struck row.
- **THE THREE IDENTITY LEGS share one cause, measured on the live store 2026-09-14.**
  `outbox_e2e::test_reconcile_sweep_drains_a_staged_outbox_event`,
  `outbox_crash_e2e::test_sigkilled_producer_loses_nothing` and
  `governed_union_e2e::test_fga_deny_drops_promotion_and_regrant_restores`. The first two stage a run
  event authored by the literal `e2e`, and the relay authorizes before ingesting — deliberately, to
  close the stage-instead-of-publish bypass (`tests/unit/test_the_outbox_relay_refuses_what_the_bus_door_refuses.py`).
  Live: `lineage_outbox_event_unauthorized ... author='e2e' reason='can_write_data required to amend
  run ...: e2e_outbox_ds'`, `outbox_drained=0 outbox_stranded=5`.
- *Granting the harness identity does NOT fix it, which is what makes this a decision:* the Dex subject
  the suites hold (`user:CiQwOGE4Njg0Yi1kYjg4...`) has `can_write_data` on `table:bronze$events` (True)
  and NOT on `table:e2e_outbox_ds` (False) — that synthetic table has no FGA object at all, so NO
  identity can be authorized for it. Either the probes write to a real governed table (putting test
  provenance in the graph) or a fixture mints and removes the object and its grant (a governance
  mutation against a live estate). Both are owner calls.
- *The FGA leg is the same shape from the other side:* it revokes namespace-level writer + owner and
  asserts the rung is gone, but `namespace:acme-silver#writer` also resolves through
  `tupleToUserset: parent -> warehouse:acme-bucket#writer`, which `service-bronze-to-silver` holds
  alongside the producer and the two other stage runners. Revoking THAT strips a grant the live cascade
  needs, and a leg failing between revoke and regrant leaves the stage runner unable to write.
- **THE FOURTH IS NOT ABOUT IDENTITY.** `maintenance_e2e::test_sweep_compacts_real_datasets_and_meters`
  asserts `summarize(swept)["errors"] == {}` and fails on
  `s3://acme-bucket/4750a5b9_acme-bronze$events` → 403 AccessDenied. Not a credential defect: the
  bucket exists, maintenance presents `rask-maintenance`, revision 157's provisioning Job logs
  "Attached Policies: [rask-maintenance]", and that policy grants `s3:ListBucket` on `arn:aws:s3:::*`.
  It is stale registry residue — the catalog's registered uri for that table is
  `s3://acme-bucket/medallion/bronze`, which compacted normally in the same tick, and the lineage
  reconciler independently lists that id among **24** `unreadable` entries naming relative paths.
- *Closes when:* an owner rules on (a) how an e2e probe obtains provenance-write authority — a fixture
  that mints the FGA object and grant for its own synthetic table and removes both, versus probes
  writing to a real governed table; and (b) whether `errors == {}` is the right assertion for a
  long-lived estate, or whether unreadable registry entries belong in an EXCLUSION set the way the
  reconciler already reports them (`excluded_datasets`, `reconcile.py:860-863`). Measured list, 2026-09-14 21:25:
  `governed_union` x3 (`fga_deny_drops_promotion` — verdict given above, not yet fixed;
  `governed_allow_full_cascade`; `quality_gate_blocks_bad_batch`), `maintenance_e2e`
  (`sweep_compacts_real_datasets_and_meters`), `media_e2e` (`ingest_media_derives_artifacts`),
  `observability_e2e` x2 (`logs_populated`, `distributed_trace_spans_catalog_to_lineage`),
  `outbox_crash` (`sigkilled_producer_loses_nothing`), `outbox_e2e`
  (`reconcile_sweep_drains_a_staged_outbox_event`), `ray_train` (`train_to_blessed`) — SUITE-DRIFT / ESTATE-DEFECT /
  CONTAMINATION / ALREADY-FIXED. Current list after the Ray restart, 2026-09-14: `governed_union` x3
  (`fga_deny_drops_promotion`, `governed_allow_full_cascade`, `quality_gate_blocks_bad_batch`),
  `catalog_live` (`errors_translate_to_domain_errors`), `maintenance_e2e`
  (`sweep_compacts_real_datasets_and_meters`), `medallion_e2e` (`produce_cascades_bronze_to_gold`),
  `media_e2e` (`ingest_media_derives_artifacts`), `observability` (`logs_populated`), `outbox_crash`
  (`sigkilled_producer_loses_nothing`), `outbox_e2e` (`reconcile_sweep_drains_a_staged_outbox_event`),
  `ray_train` (`train_to_blessed`).

**LH-110 · No documented, exercised restore of the control root (projects, warehouses, bindings, trash)**
`catalog, chart` · med

- *Why open:* There is a chart snapshot for RustFS and Postgres and nothing that has ever been restored from it, so the control root's recoverability is untested.
- **LANDED AND EXERCISED 2026-09-14 — and "from the snapshot" turned out to be impossible here.**
  The row assumes the chart's snapshot covers this. Measured: `backups.pgDump` covers the lineage and
  OpenFGA databases, `backups.volumeSnapshot` covers the MinIO PVCs, BOTH default off and neither is
  enabled in the live release — and this cluster has no `volumesnapshotclass` resource type at all, so
  that path could not run even if switched on. `RUNBOOK-restore.md` meanwhile claimed the VolumeSnapshot
  row covered "all medallion + registry data"; on this cluster it covers the registry with nothing. That
  claim is corrected in place.
- *So the restore is LOGICAL, not a second PVC snapshot,* which is also the right granularity: the whole
  record set is 1,454 objects of a few hundred bytes, and "somebody deleted the warehouse bindings" is
  not answered by restoring a whole store. `scripts/control_root_backup.py` backs up, verifies and
  restores `_projects/`, `_warehouses/` (registry + bindings), `_trash/`, `_protection/`, `_policies/`,
  `_gates/`, `_transforms/`, `_tasks/`.
- **EXERCISED AGAINST THE LIVE ESTATE, which is what the row asked for and what found the bugs:**
  1,454 backed up, `verify` intact (0 missing, 0 changed), restore into a scratch prefix `verified: true`
  with 0 mismatched. Three defects surfaced only because it was run for real —
  *(a)* MinIO keeps zero-byte directory markers (`_projects/`) that moto does not, and the path join
  stripped the trailing slash, so every record collapsed onto one key: 1,464 copies landed as 9 objects;
  *(b)* a live root is written while it is read — three `_tasks/` records changed between the listing and
  the copy — so a manifest built from LISTED etags declared a good backup corrupt; it now records the
  etag of what was COPIED and reports the moved records as `raced`;
  *(c)* a failed restore's residue BLOCKS the retry on MinIO, because a zero-byte object named
  `<prefix>` occupies the name the correct run needs as a directory — `head_object` finds the child and
  `list_objects_v2` does not. All three are in the runbook.
- *The outboxes are deliberately NOT in the set:* `_control_outbox/`/`_lineage_outbox/` are queues, and
  restoring one re-publishes events the estate already acted on. A queue's correct recovery is empty.
- *Residual, stated rather than accepted:* the tool has NO retention — `backups.pgDump` prunes to
  `keep: 7` and this prunes nothing, so `_backups/control/` grows without bound. And the default
  destination is the control root's own bucket, which survives a bad write and not a lost bucket;
  `--dest` takes another bucket and the manifest says which of the two you got.
- **RETENTION LANDED 2026-09-16.** `do_prune` keeps the newest N and deletes the rest; `backup --keep N`
  runs it AFTER the copy, never before — pruning first would drop the oldest backup on the very run that
  then failed to write its replacement. `keep=0` is the default and means unbounded, matching
  `backups.pgDump`'s own `gt 0` gate, because a tool that quietly began deleting backups on upgrade is a
  worse failure than the growth it fixes. Newest-first is the reverse lexical sort of the
  `%Y%m%dT%H%M%SZ` stamps — deliberately the same ordering `backup-pg.yaml` gets from
  `mc ls … | sort -r`, so an operator reading one lane and reasoning about the other meets one answer.
- *STILL OPEN — and the scheduled half is NOT the small job this row implies.* `scripts/` ships in no
  image: `.docker/rest-catalog.dockerfile` copies none of it, and `/app/scripts/control_root_backup.py`
  is absent from the running catalog pod. So a CronJob cannot simply invoke the tool, and the choice —
  bake `scripts/` into an image, mount the script from a ConfigMap, or re-implement the prune in shell
  with `mc` the way `backup-pg.yaml` does — is a real decision rather than a template to copy.

**LH-111 · ~~No in-flight blob-byte admission budget — the catalog counts requests, not bytes~~ — STRUCK 2026-09-10 (PREMISE FALSIFIED)**

- *What is true now:* The blob door already streams in bounded 8 MiB windows and never buffers a payload, so 'a large blob request is admitted on the same terms as a small one' does not describe the memory cost — the exposure is a concurrency COUNT, not bytes.
- *Evidence:* services/catalog/src/catalog/services/blob_serving.py:38-41 sets `_BLOB_CHUNK_BYTES = 8 * 1024 * 1024`; :77-87 `chunks()` yields the window one bounded `read_range` at a time, and the module docstring (:15) states "a multi-GB blob is served through bounded `read_range` windows". The door itself (services/catalog/src/catalog/api/v1/endpoints/data.py:509-571) returns a StreamingResponse over those chunks — data.py:527-531 says the read-side mirrors the write-side OOM guard. So a 4 GB blob and a 4 KB blob cost the same resident bytes. What the row's fix targets does not exist: no byte budget anywhere (grep byte_budget/max_inflight_bytes/blob_budget across services/, packages/, chart/ returns nothing), and services/catalog/src/catalog/api/load_shed.py:33-51 gates only bulk Arrow-IPC POST writes on a request COUNT, not blob GETs.
- *What would reopen it:* If `chunks()` read the whole window in one call, or if `read_blob` materialised the payload before returning, the row's mechanism would be real. The narrower true residual: blob GETs are not counted by load_shed at all, so N concurrent readers cost N x 8 MiB — a concurrency cap on the blob door (mirroring load_shed's 429 + Retry-After), not a byte budget answering 503.

**LH-112 · ~~Unknown whether Lance honours a tag that pins a BRANCH version during main cleanup~~ — CLOSED 2026-09-14 (MEASURED)**
`maintenance, catalog` · med

- *Why open:* Unmeasured, and it becomes load-bearing as soon as the sweep maintains branches.
- **MEASURED AND CLOSED 2026-09-14 — and the answer is sharper than the question.** pylance 11.0.0,
  main at v1..v5, a branch rooted at v2, then `cleanup_old_versions(older_than=0)`, the most aggressive
  form there is:

      main versions after : [2, 5]      (an UNPROTECTED run leaves [5] — asserted as its own control)
      branch              : opens [2, 3], 3 rows
      tag                 : still resolves, {'branch': 'work', 'version': 3}

  **It is the BRANCH REFERENCE that protects main v2, not the tag.** Run with and without the tag, the
  results are identical — so Lance keeps a version a branch stands on structurally, and tagging neither
  adds to that protection nor is required for it. Crediting the tag would have been the exact shape of
  a control that looks like it fired and did not, which is why both arms are pinned rather than the
  reassuring one alone.
- *A third fact the experiment turned up, worth having before reading `tags.list()` anywhere:* tags are
  ROOT-SCOPED. A tag created on a branch is visible from MAIN and carries its branch name, so "the
  dataset's tags" is one namespace spanning every branch rather than a per-branch list — a caller that
  read it as main's own would act on a pin belonging to a branch it is not looking at.
- *Closes when:* nothing further. `tests/unit/test_main_cleanup_does_not_delete_a_version_a_branch_stands_on.py`
  pins all three findings and is mutation-proven; a pylance upgrade that changes any of them reds it.

**LH-113 · One 340-line catalog `Settings` class carries every domain's configuration**
`catalog` · med

- *RE-MEASURED 2026-09-14 — still true, and the number in the title UNDERSTATES it.* The class is
  `services/catalog/src/catalog/core/config.py:31-467` — **437 lines, 63 annotated fields**, in a
  499-line module, deriving from `GovernedAuthSettings` + `BaseSettings`. It has grown since the row
  was written, which is what an unowned catch-all does.
- *Why open:* Listed OPEN in the Q3 carry-over table and, until now, neither re-measured nor struck.
- *Closes when:* Split the catalog `Settings` into per-domain settings blocks, the shape the eight services already share via `GovernedAuthSettings`.

**LH-114 · ~~Multi-base (`base_paths`) is implemented but never exercised, and no test proves cleanup on a shared non-root base spares its sibling~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* All three asks are met: a multi-base fixture is provisioned and driven live, the orphan scan is tested over a real shallow clone, and a REAL sweep tick is asserted to spare the sibling.
- *Evidence:* Multi-base fixture, live: scripts/e2e_stack.sh:27-28 defines `BASE_A=s3://mb-a` / `BASE_B=s3://mb-b`, :109 installs the chart with `catalog.multibase.dataBases=[mb-a, mb-b]`, :189 provisions both buckets, :266 exports them, :281 runs tests/e2e-py/test_multibase_e2e.py — under the no-silent-skips gate at :294-299. Orphan scan over a multi-base dataset: tests/unit/test_orphan_files.py:336-352 `test_a_shallow_clone_is_refused_because_its_data_lives_elsewhere` builds it with the real `ds.shallow_clone(...)` API and asserts `checked is False`, `structural is True`, reason names `base_paths`. Shared-base cleanup sparing the sibling: tests/unit/test_base_refs_guard.py:205-224 `test_a_REAL_SWEEP_TICK_refuses_the_source_of_a_live_clone` drives the real `run_sweep` (via _sweep_results at :177-202, stubbing only the S3 fs and discovery) and asserts the SOURCE comes back refused with "resolves its files through"; :137-176 proves in a COLD subprocess that without the guard the clone breaks; :227-244 pins that an ordinary dataset is still swept, so the guard is not a blanket refusal.
- *What would reopen it:* If test_base_refs_guard.py's sweep test stubbed `protected_roots` itself (it does not — that is the stated point of the file, :208-212), or if the multibase e2e were absent from the e2e_stack pytest list, the row would still stand.

**LH-115 · ~~Manifest flags 16/64 are refused only by the orphan pass, not by the rest of the maintenance surface~~ — STRUCK 2026-09-14 (PREMISE FALSIFIED, and one third of the ask would be a REGRESSION)**

- *What is true now:* the refusal knowledge lives in `service_kit.lakehouse.features` — the shared
  predicate this row asks for — and every operation it names consults it, with the gate that SUITS that
  operation rather than one blanket check.
- *Evidence, measured 2026-09-14:* `maintenance/services/optimize.py:630-634` takes
  `describe_gc_unsupported_flags` and returns `DatasetResult(refused=…)` with a
  `maintenance_refused_unsupported_features` warning, then `describe_compaction_unsupported_flags` for
  the compaction half; `catalog/services/maintenance.py:136` and `:159` raise
  `UnsupportedOperationError` on the same two for the on-demand doors; `maintenance/services/orphans.py:345`
  keeps the read gate it started with; `lineage/core/reconcile.py:91` classifies a failed open through
  `unsupported_features_from_open_error` so the sweep REPORTS a feature refusal instead of a bare error.
- **AND APPLYING THE REFUSAL TO THE SWEEP WOULD BE WRONG, which is why this is struck rather than
  closed.** The row asks for "the 16/64 flag refusal" on "compaction, reclamation, the sweep". That
  refusal is `describe_unsupported_flags`, which ORs both flag fields because it is *"the gate a WRITE
  takes"*. The sweep writes nothing, and `describe_read_unsupported_flags` documents the trap with a
  measurement: `FLAG_TABLE_CONFIG` (8) is reader-NOT-required and writer-required, and on pylance 10.0.0
  `update_config({"k": "v"})` produces `reader=0, writer=8` — so ORing the fields refuses a read over a
  bit only a writer must understand. That is a defect this module was corrected to remove; re-applying
  it to the estate's one read-only pass would reintroduce it.
- *What would reopen it:* a maintenance operation that WRITES and does not consult
  `describe_unsupported_flags` / `describe_compaction_unsupported_flags` / `describe_gc_unsupported_flags`
  before rewriting bytes. Not a read-only pass declining to take the write gate.

**LH-116 · ~~Fragment sizing is left at Lance defaults with no per-table lever~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* Fragment sizing is not at Lance's default and has a lever at three scopes — a per-tier default with the sizing rationale written where it is chosen, a per-table/namespace/project policy record that overrides it, and a per-request override on the compaction door — and the sweep and the off-pod rewrite both carry it.
- *Evidence:* PER-TIER DEFAULTS + THE RATIONALE, where the default is chosen: /home/gabriel/Desktop/rask/services/maintenance/src/maintenance/services/tiers.py:1-37 (the module docstring's two-forces argument and the bronze/silver/gold rows-x-row-size-x-fragment table), :47-58 `BRONZE_TARGET_ROWS=512` / `SILVER_TARGET_ROWS=262_144` / `GOLD_TARGET_ROWS=524_288`, :171-181 `target_rows_for()` returning None when the tier cannot be read. PER-TABLE LEVER: /home/gabriel/Desktop/rask/services/catalog/src/catalog/schemas.py:455-461 `MaintenancePolicy.target_rows_per_fragment` (with its sizing rationale in the comment above it), stored/resolved by /home/gabriel/Desktop/rask/packages/service-kit/src/service_kit/lakehouse/maintenance_policies.py:133-196 where `kind == "table"` is an exact match that wins over namespace and project records. APPLIED BY THE SWEEP: /home/gabriel/Desktop/rask/services/maintenance/src/maintenance/services/sweep.py:396 (tier default into the plan), :420-421 (policy override), :554 (into `compact_one`), and :496-497 for the off-pod distributed rewrite. PER-REQUEST OVERRIDE ON THE DOOR: /home/gabriel/Desktop/rask/services/catalog/src/catalog/schemas.py:403-406 `CompactRequest.target_rows_per_fragment`, wired at /home/gabriel/Desktop/rask/services/catalog/src/catalog/api/v1/endpoints/maintenance.py:133 and :157, and honoured at /home/gabriel/Desktop/rask/services/catalog/src/catalog/services/maintenance.py:311-322. CONSUMED: /home/gabriel/Desktop/rask/services/maintenance/src/maintenance/services/optimize.py:251, :271-272, :525, :670. Driven by tests at /home/gabriel/Desktop/rask/services/catalog/tests/test_the_compact_button_enqueues.py:110-123 and /home/gabriel/Desktop/rask/services/maintenance/tests/test_compaction_runs_off_the_pod.py:96,146.
- *What would reopen it:* Show that a table cannot get its own `target_rows_per_fragment` — e.g. that `resolve_policy` never reaches the `kind == "table"` branch for a real dataset URI, or that `compact_one` drops the value before `compact_files`. Or show that the sizing rationale is absent from every site where a default is chosen (it is at tiers.py:1-37 and schemas.py:455-461).

**LH-117 · ~~The orphan scan has no chart toggle — `MAINTENANCE_ORPHAN_SCAN_ENABLED` is env-only~~ — STRUCK 2026-09-10 (ALREADY FIXED)**

- *What is true now:* `MAINTENANCE_ORPHAN_SCAN_ENABLED` has been a chart value since 2026-08-15 — `maintenance.orphanScan`, defaulted true in values.yaml and rendered into both the maintenance deployment and the maintenance worker.
- *Evidence:* CHART VALUE: /home/gabriel/Desktop/rask/chart/values.yaml:1693 `orphanScan: true`, with the 25-line rationale block at :1668-1692 recording exactly the gap this row names ('the chart rendered no env var for it, so the scan could not be turned on in a deployed estate by any means'). WIRED THROUGH BOTH TEMPLATES: /home/gabriel/Desktop/rask/chart/templates/maintenance.yaml:208 `- { name: MAINTENANCE_ORPHAN_SCAN_ENABLED, value: {{ hasKey .Values.maintenance "orphanScan" | ternary .Values.maintenance.orphanScan false | quote }} }` and /home/gabriel/Desktop/rask/chart/templates/maintenance-worker.yaml:166 (identical). CODE DEFAULT UNCHANGED (still off absent the chart): /home/gabriel/Desktop/rask/services/maintenance/src/maintenance/core/config.py:299 `orphan_scan_enabled: bool = Field(default=False, alias="MAINTENANCE_ORPHAN_SCAN_ENABLED")`. Landed 2026-08-15, commit 81af086f 'fix(maintenance): #128a + #128d + #114 — the reclaimer could certify an estate it never scanned'.
- *What would reopen it:* `helm template` the chart with `maintenance.orphanScan` set and find the env var absent from the rendered maintenance pod spec, or find a third maintenance workload that runs the reconciler and gets no `MAINTENANCE_ORPHAN_SCAN_ENABLED` row.

**LH-118 · ~~An unparseable `lance-catalog/_projects/_probe.json` that nothing writes keeps `registry:projects` in the reconcile report's `incomplete: 61`~~ — CLOSED 2026-09-15: the object it asks to delete does not exist**
`maintenance` · low · **blocked:** owner authorisation to delete an object from the live control root


- **PREMISE FALSIFIED 2026-09-15 — there is nothing to delete, so there is nothing to authorize.**
  Swept every bucket in the live object store with explicit credentials (108 buckets, full paginated
  listing): NO object named `_probe.json` exists anywhere. The 110 keys matching "probe" are all e2e
  fixture tables named `stockprobe` under `acme-bucket`, which are ordinary test data and not this
  row's residue.
  *This row was carried as blocked on owner authorisation to delete a live-estate object.* The
  authorisation was given on 2026-09-15 and the delete was NOT performed, because re-measuring first —
  the estate's own rule — showed the target absent. Acting on the ruling without the measurement would
  have meant hunting for, and possibly deleting, some other object that merely looked like it.
  *The code half was already true and stays true:* nothing in `services/`, `packages/`, `scripts/`,
  `chart/` or `tests/` writes `_probe.json` — the repo-wide grep returns zero. Whatever wrote it is
  gone, and so is what it wrote.

- *Why open:* Measured live on pylance 11: char-0 JSONDecodeError, and nothing in the tree writes the file, so it is estate residue. `_list_json_records` reporting what it cannot parse is correct — the file is the defect. `incomplete` blocks `report_is_clean` and therefore the purge, but is not the sole blocker (13 real drift findings remain). Not acted on because deleting an object from the owner's live control root is not a change to make unasked.
- *Closes when:* Owner authorises deleting `s3://lance-catalog/_projects/_probe.json` from the live control root; the other 60 `incomplete` rows are `depth limit reached` on the discovery walk, already recorded at `optimize.py:115`.

**LH-119 · The dropped-parameter sweep's coverage is unassessed — 16 verify calls and the completeness critic failed on the weekly subagent limit**
`catalog, medallion, ingest` · low

- *Why open:* 22 candidates produced 53 verdicts (40 real) but the critic never ran, so the remaining rows are candidates, not a finished list. The stated reset (2026-09-04 06:00) has passed.
- *Closes when:* Re-run the sweep's completeness critic and the 16 failed verify calls, then re-state the remaining rows as measured rather than candidate.

**LH-120 · ~~Both medallion entrypoints read settings at import time — `producer.py:170-171`, `producer.py:201` and `stage_runner.py:46`~~ — STRUCK 2026-09-15 (THE STATED COST DOES NOT REPRODUCE, and the fix does not improve the failure it names)**
`medallion` · was low

- *Why open:* Re-measured at HEAD 2026-09-09: the LOGGING half is false (neither entrypoint configures logging at module level), but the SETTINGS half stands at three sites, which is the half that matters — a config error fails at import rather than in the lifespan.
- *RE-MEASURED 2026-09-11 — still true, and the citation drifted.* The module-level reads are
  `producer.py:170` and `:171` (`docs_enabled`, `audit_enabled` — the row named one of the pair) and
  `producer.py:201` (`mount_lag_cron(app, get_settings().cascade_lag_binding_name)`), not `:202`,
  which is one past the end of a 201-line file. `stage_runner.py:46` is unchanged. So four sites,
  not three.
- **DRIVEN 2026-09-15 rather than reasoned about, and the row's cost is half of what it claims.** The
  four sites are real and still there; what is not is the consequence. Measured:

      uv run python -c "import medallion.producer"            -> imports fine, app built
      MEDALLION_QUALITY_ENABLED=not-a-bool  ... same import    -> ValidationError at import,
                                                                 "1 validation error for MedallionSettings"

  So a MISSING config does not fail at all — every field on `MedallionSettings` carries a default — and
  the crash-loop-on-missing-config this row implies cannot happen. What remains is a MALFORMED value
  raising a pydantic `ValidationError` that names the offending field, at startup, which is the
  behaviour `writing-python`'s configuration reference explicitly asks for ("Fail fast at startup — a
  clear error at startup beats a cryptic None mid-request").
- *AND THE PRESCRIBED FIX CANNOT DELIVER WHAT THE ROW WANTS.* "Move them into the lifespan/factory"
  has no lifespan option: `docs_enabled` and `audit_enabled` SHAPE the app (they decide whether the
  docs routes and the audit middleware exist), so they must be read before `build_lance_service_app`
  returns. A factory (`create_app()`, the shape `ingest` ships at `.docker/ingest.dockerfile:100`)
  moves the read from import to construction — the same startup failure, one frame later. The genuine
  gain would be importability without config, and that already holds.
- *What would REOPEN it:* a required field with no default appearing on `MedallionSettings` (making a
  missing value an import crash), or a test that needs to import an entrypoint while deliberately
  holding invalid config. Neither exists: the estate's medallion tests import freely today.


---

**LH-153 · ~~The "compact now" button's in-process lane commits a Rewrite and emits no lineage event~~ — CLOSED 2026-09-16, observed live**
`catalog` · med · migrated 2026-09-15 from `open_lakehouse_audit_2026-09-11.md` (finding 12) before that file was deleted

- *Why open:* Re-measured 2026-09-15: `services/catalog/src/catalog/api/v1/endpoints/maintenance.py` returns
  `CompactResult(**result)` off the `compact_now` path with no `emit_measured_write` anywhere in the module's
  in-pod branch, while the sweep, this same door's QUEUED lane and the sibling `/compaction_commit` door all
  emit. This is the lane the estate actually runs: `maintenance.workTopic` is `""` on the deployed values.
- *Why it is only medium:* no row changes, the presser is audited at the FGA gate, and the version is in the
  commit log — so the loss is the graph's account of WHO compacted and WHEN, not the fact of it.
- *The fix is already shaped by the sibling door:* `emit_measured_write(..., operation=COMPACT_TABLE, pin_version=...)`.
  No docstring, decision record or commit anywhere states the silence as intended, which is the reason this is
  a defect rather than a choice.
- *Closes when:* the in-pod branch emits COMPACT_TABLE like its three siblings, with a test that fails if the
  emit is removed from the lane that has no work topic configured.
- **CLOSED, and the fix carried a SECOND SITE the row did not name.** `reindex`'s in-pod lane was silent
  too, while the two spec index doors at `indices.py:95` emit `CREATE_INDEX` for the same act — it commits
  a version through `rebuild_index_now` and reported nothing. Both doors now emit (`5f507c3c`); compact
  passes no `pin_version` because `compact_now` reports fragment counts and none, reindex pins the version
  it already holds.
- *The gate is route-derived*, like `test_the_maintenance_doors_refuse_a_branch_they_cannot_honour` beside
  it: every POST under `/{id}/maintenance/` must be classified as minting a version or not, so a fifth verb
  is decided about rather than inheriting the silence reindex inherited. `preview` and `run` are exempt with
  the reason recorded, and whether reclaiming history deserves its own event is left open — no operation in
  `catalog.core.lineage_emit` carries it.
- **OBSERVED LIVE 2026-09-16** on `lance-rest-catalog:main-9e5ff5b3`: a real
  `POST /v1/table/aud1ns$t1/maintenance/compact` with a Dex-minted bearer answered 200, and AGE then held
  `(:Run {operation:'compact_table', author:'CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs'})-[:WROTE {version:7}]->(:Dataset {name:'aud1ns$t1'})`.
  The pre-existing `compaction` run on the same dataset carries an EMPTY author — that is the sweep — so the
  presser's identity is exactly what this row was missing.

**LH-154 · ~~A failed outbox stage skips the publish entirely and is swallowed by both emitters~~ — CLOSED 2026-09-16, observed in the deployed pod**
`service-kit, catalog, maintenance` · med · migrated 2026-09-15 from `open_lakehouse_audit_2026-09-11.md` (finding 14)

- *Why open:* Re-measured 2026-09-15: `packages/service-kit/src/service_kit/lakehouse/outbox.py:361` runs
  `stage_event` OUTSIDE the `try:` at :363 that wraps `publish_event`. A stage failure therefore raises past the
  publish, and both callers catch and continue — `catalog/core/lineage_emit.py:706-708` and
  `maintenance/core/lineage_emit.py:308-309`.
- *Why it is only medium, stated so nobody re-rates it from the title:* it is WARN-logged and counted, zero
  occurrences in 29,401 retained catalog log lines, and only a transient store failure landing between the Lance
  commit and the PutObject reaches it at all.
- *What makes it a defect anyway:* the counter has no alert rule and the outbox's four signals are blind to this
  case by construction, so the one path that loses an event silently is the one nothing watches. Publishing
  anyway on a failed stage strictly dominates — an unstaged event that reaches the bus is delivered, while an
  unstaged event that is never published is gone.
- *Distinct from [[LH-004]]*, which is about the kernel swallowing transport failures, not about staging order.
- *Closes when:* a stage failure still attempts the publish, and a test drives a raising `stage_event` and
  asserts the publish was attempted.
- **CLOSED (`bcf156b2`).** The stage moved inside the try, so a stage failure degrades the call to the
  pre-#4 plain publish instead of raising past it. The blindness the row names is closed with it:
  `outbox.stage.failed` is a new instrument, and because
  `test_every_FIRST_PARTY_INSTRUMENT_is_read_by_some_alert_rule` refuses an instrument no rule reads and
  `test_every_alert_rule_has_a_promtool_case` refuses a rule with no fire-proof, `LineageOutboxStagingFailing`
  landed with both a firing and a staying-quiet promtool case.
- **OBSERVED IN THE DEPLOYED POD 2026-09-16**, by driving the running `rask-catalog` image's own
  `_publish_staged` with a raising `stage_event` and a fake publisher: the publish was attempted, the failure
  was counted, and `outbox_stage_failed` was logged. Driving a real object-store failure was declined — it
  would mean breaking the store under a shared cluster.

**LH-155 · ~~Undrop and trash purge race with no arbitration on either side — LATENT~~ — PURGE HALF CLOSED 2026-09-16**
`catalog, maintenance` · med · migrated 2026-09-15 from `open_lakehouse_audit_2026-09-11.md` (finding 9)

- *Why open:* Re-measured 2026-09-15 and unchanged. `maintenance/services/purge.py:745` snapshots `live_ids`
  once per tick, the estate-wide shallow-clone pre-pass follows at :752, and each record is then checked against
  that stale snapshot at :604 through a pure `check()` that never re-reads `__manifest`. Undrop
  (`catalog/.../tables.py:790-832`) does an unguarded `trash.get` -> `register_table` -> clear with no clock gate
  by design. A grep for `lock|lease|CAS|compare_and|etag|if_match` across both files finds no arbitration.
  Either interleaving yields a registered, ownerless, byte-less table answering 200 `table_undropped`.
- *Why it is LATENT rather than live:* purge is OFF on every shipped values file and in the cluster
  (`MAINTENANCE_TRASH_PURGE_ENABLED=false`), so nothing can race today. Turning purge on is what arms it.
- *A docstring is currently false about this:* `purge.py:31-34` says the record is "re-checked immediately
  before deleting". It is not.
- **THE PURGE SIDE IS CLOSED 2026-09-16, and the race was DEMONSTRATED before it was fixed.** A test
  drives the window deterministically rather than by timing: the estate pre-pass runs after the
  liveness snapshot and before the loop, so re-registering from inside it lands an undrop exactly in
  the gap. Measured against the unfixed code, the purge reclaimed a table that was live at the moment
  of deletion — **867 bytes across 4 files, reported as a success**.
- *The fix is a second read at the last instant before the FIRST mutation*, which is the revoke rather
  than the delete (the revoke runs first, so a lost race costs the grants too).
  `_recovered_since_the_snapshot` re-runs the SAME refusal ladder, so an unreadable manifest refuses
  here exactly as it does above rather than degrading to "purge anyway". The shared per-tick snapshot
  STAYS as the cheap filter over every due record; only records that reach a mutation pay for the
  second read, which is at most `trash_purge_max_per_tick`.
- *The false docstring is rewritten*, and says what the code does: the id is checked TWICE, and the
  second read is the one that closes the undrop window.
- *Mutation-proven both ways:* removing the re-check reds exactly that test and nothing else, and the
  test asserts its OWN precondition — that the mid-tick re-register actually landed — because if it had
  not, the id would not be live and purging it would have been correct.
- *A pre-existing gate caught the change and was satisfied by extraction, not exemption:*
  `test_no_god_functions` refused `_purge_one` at 42 statements, which is why the re-check is its own
  function.
- *WHAT REMAINS OPEN IS THE UNDROP SIDE:* `tables.py:790-832` still does an unguarded
  `trash.get` -> `register_table` -> clear with no clock gate. The purge can no longer delete a table
  undrop has registered, but undrop can still re-register one the purge is mid-way through deleting —
  a narrower window, and it needs the catalog half (a lease, or a re-read of the record after
  registering). Still LATENT: `MAINTENANCE_TRASH_PURGE_ENABLED=false` in the cluster, verified 2026-09-16.
- *Closes when:* the undrop side takes the arbitration too — BEFORE `trashPurgeEnabled` is ever set
  true anywhere.

**LH-157 · ~~One `WorkOrder` carries TWO idempotency-key formulas, so an in-process build bump re-attaches to a stale outcome~~ — CLOSED 2026-09-15, observed live**
`medallion` · **HIGH** · found 2026-09-15 by an adversarial workflow, verified first-hand before filing

- *The defect:* `transform.py:753` builds the order's key as
  `f"{to_namespace}:{token or 'notoken'}:{from_dataset}->{to_dataset}"` — **no `code_version`** — while
  `ray_submit.py:181` derives its submission id as
  `stage_submission_id(stage, token, from_uri, to_uri, code=code_version)`, which includes it.
- *Why the asymmetry bites, confirmed by reading the consumer:* `inprocess_executor.py:98` states that
  "the order's `idempotency_key` IS the handle, which makes a redelivered order re-attach". So on the
  RAY path a code bump mints a new id and the work re-runs; on the IN-PROCESS path the key is unchanged
  and the run **re-attaches to the previous build's outcome**. The same `WorkOrder`, two answers.
- *Why it is HIGH despite the in-process lane being the quieter one:* the failure is silent and it is
  the SUCCESS path — a stage reports COMPLETE against an artifact the current code never produced. No
  counter moves, no log fires, and the lineage graph records a run that did not happen at this version.
- **CLOSED, and found stale-open by re-measuring rather than by new work.** `work_order.derive_idempotency_key`
  is the one derivation; `transform.py` and `ray_submit.py` both call it; and
  `tests/unit/test_one_work_order_has_one_idempotency_key.py` pins every documented axis — dropping
  `code_version` from the derivation fails 2 of its 5, so it is not hollow.
- **OBSERVED IN THE RUNNING `rask-bronze-to-silver` pod** on `main-df4582c4`, not inferred from the diff:
  the same stage/token/from/to with `build-1` and `build-2` mint DIFFERENT keys, and both lanes import
  the shared derivation. The in-process lane can no longer re-attach to a previous build's outcome.

**LH-158 · ~~The `Executor` port is declared, documented and used by neither lane~~ — RULED AND SHIPPED 2026-09-17**
`medallion, service-kit` · med · found 2026-09-15 by an adversarial workflow · **RULED 2026-09-17 (owner): option 3, widen the port**

- **DONE. The owner chose option 3 and it landed.** `Capability.RESULT` joins the port's existing
  `StrEnum`; `result()` joins the `Executor` protocol; `InProcessExecutor` claims the capability and
  `RayJobsApiExecutor` declines it and raises, exactly as `InProcessExecutor` already declines `CANCEL`.
  `transform.py` now calls `executor_for(IN_PROCESS_ENGINE, storage_options=...)` and reads the result
  only when the resolved engine promises one, re-deriving with `measure_stage` otherwise. It no longer
  imports `inprocess_executor` at all — the lane names an ENGINE, never an adapter class.
- *The method is on the protocol and only the CAPABILITY is optional, which is not a style choice:*
  `Executor` is `runtime_checkable`, so a method missing from an adapter makes `isinstance` answer False
  and that adapter unreachable through the registry. Both decliners therefore implement it and RAISE —
  `None` would be indistinguishable from a run that measured nothing, the overloaded-`None` defect this
  very port's `RunState.UNKNOWN` exists to name.
- *The return stays `Any`, and that was measured before settling for it:* the two candidate types are
  NOT interchangeable. The medallion's `WriteResult` carries `previous_row_count` (the promotion band's
  comparison point) and `service_kit.lancekit.openlineage.WriteResult` — deliberately shaped to mirror
  lance-ns's wire model — does not. Narrowing to either drops a field the band reads or pulls the
  medallion into the port's shape, which the port's own header forbids.
- **THE TEST LAYER IS NEW BECAUSE THE EXISTING ONE STRUCTURALLY COULD NOT CATCH THIS.**
  `test_the_chosen_engine_is_the_engine_that_runs` and `test_the_ray_lane_reaches_the_port` both call
  `executor_for` FROM THE TEST and assert what it returns — green against a registry with zero callers,
  which is exactly the state this row described.
  `test_the_inprocess_lane_reaches_the_port.py` asserts the PRODUCTION lane resolves through it, and was
  MUTATION-CHECKED rather than merely observed passing: restoring the hand-built
  `InProcessExecutor(settings.storage_options)` kills 3 of its 4 legs, and the 4th (the two shipped
  adapters disagree about `RESULT`) correctly survives because it tests the adapters, not the lane.
- **OBSERVED ON THE DEPLOYED ESTATE 2026-09-17, on the changed path itself rather than beside it.**
  Built with Dagger as `lance-rest-catalog:main-e3139b71` and rolled to all SEVEN deployments sharing
  that image (catalog, lineage, maintenance, medallion-producer and the three stage runners).
  * *Driving it needed a deliberate step, because the deployed cascade does not exercise this lane at
    all:* all four medallion deployments run `MEDALLION_RAY_ENABLED=true`, so a plain `/produce` proves
    nothing about the in-process engine. `rask-bronze-to-silver` was switched to the in-process lane for
    one drive and RESTORED immediately after; all four are back to `ray=true`, verified by reading them
    back.
  * *What ran:* `POST /produce?rows=17` → `202`, and the stage runner logged
    `medallion_stage_moved transition='bronze->silver' token='lh158b-1789642736' to='silver$features'
    duration_seconds=0.23` — a governed silver write against the estate's real S3-backed Lance datasets,
    through the registry-resolved executor.
  * **The proof of WHICH LANE is a span attribute, not the absence of a Ray log line.** Queried from
    GreptimeDB's `opentelemetry_traces` over the 15-minute window covering both drives:
    `lance.medallion.compute` = `in_process` ×1 and `ray` ×2, and the `in_process` row is
    `service_name='bronze-to-silver'`, `span_name='medallion.transform'`, timestamped 10:58:57 UTC —
    the same second as the log line above. The `ray` ×2 is the first drive, which is also the
    regression check: the Ray lane still dispatched to its workflow and its job reported `SUCCEEDED`.
- *The re-derive arm is not taken by the shipped adapter and is deliberately kept:* `InProcessExecutor`
  claims `RESULT`, so production reads it. The arm is what makes the lane describe the PORT rather than
  the class it used to name, it is covered by a test driving a no-`RESULT` engine, and an in-process
  adapter that streamed to disk without measuring would take it. It is a capability read, not a guard
  against something structurally impossible — the "control that cannot fire" shape does not apply.
- *`docs/DECISIONS.md` no longer states the falsehood:* its 2026-09-15 entry said "WHAT THIS ENTRY DOES
  NOT CLAIM: that both lanes go through the port. They do not." A dated 2026-09-17 entry settles it and
  the old paragraph points forward, per this file's own supersedes convention.
- *NOT in scope and deliberately untouched:* collapsing the `use_ray` branch at `transform.py:828`. That
  is the ORCHESTRATION axis (Dapr Workflow dispatch), not the compute axis, and the Ray lane does not
  call `_run_in_process` at all — making that function engine-generic would create a second path nothing
  takes. [[LH-159]] requirement 2 is what this closes.

- **THE HEADLINE THIS ROW WAS FILED WITH WAS WRONG, AND IS CORRECTED RATHER THAN QUIETLY DROPPED.** It
  claimed a task registered for a THIRD engine would silently run in-process because `transform.py:828`
  treats in-process as the implicit `else`. Re-measured 2026-09-15 before working it, and the guard
  already exists — twice:
  * `engine_choice.engine_for` refuses any registration whose engine is outside
    `hosted_engines(settings)`, with a message that distinguishes "this build has no adapter" from
    "this deployment turned it off" and ends *"Refusing rather than running it on whichever engine
    happens to be configured here."*
  * and a third engine cannot be REGISTERED in the first place: `task_register.py:54` builds the
    declarations as `[(task, RAY_ENGINE) for ...] + [(task, IN_PROCESS_ENGINE) for ...]`, so the
    registry only ever stamps those two.
  *Building the proposed guard would therefore have shipped a control that cannot fire* — the defect
  class this estate keeps finding, arrived at from the opposite direction. Recorded because the finding
  came from an adversarial workflow: agent output needs the same re-measurement as any other verdict,
  and "8 agents agreed" is not evidence.
- *What is actually true, and is the whole of this row:* the `Executor` port is honoured by NEITHER
  lane. `transform.py:788` hand-builds `InProcessExecutor(settings.storage_options)` directly;
  `transform.py:828` branches `use_ray` and dispatches the Ray lane to the workflow; and
  `executor_for` has ZERO production callers. `docs/DECISIONS.md:1451` meanwhile describes "a port,
  TWO adapters". This is requirement 2 of [[LH-159]] — clear abstractions — and it is the reason that
  requirement does not hold.
- *The related shape question, stated so the fix is scoped rather than drifting:* `executor_for` has
  ZERO production callers, `transform.py:786` still hand-builds `InProcessExecutor` directly, and since
  the RayJob adapter was deleted ([[LH-083]]) the registry has ONE implementation. So either dispatch
  routes through the port — wrapping `ray_submit` + `job_status` as a Jobs-API executor — or
  `engine_registry.py` is deleted outright. **Keeping it uncalled is the worst of the three**, because
  the decision record already claims a port with adapters that the code does not use.
- **HALF LANDED 2026-09-15 (`30eef5ec`), deployed on `main-30eef5ec` and observed.** `RayJobsApiExecutor`
  wraps the submission path the cascade actually uses, `executor_for` resolves BOTH engines, and the
  live stage runner reports `['inprocess', 'ray']` with each satisfying the `Executor` protocol.
  `WrongEngineError` moved onto the port, since `validate_task` promises to raise and two adapters
  raising two types would break any caller catching one. The estate-wide Ray-secrets gate refused the
  new seam until it was fed the same adversarial material, and it is clean under mutation.
- **WHY THE IN-PROCESS LANE STILL BYPASSES THE PORT IS A DESIGN TENSION, NOT AN OVERSIGHT — this is the
  finding that changes what the rest of the row costs.** `transform.py:788` hand-builds
  `InProcessExecutor` because it then calls `executor.result(handle)` at :800, and **`result()` is not
  on the port**. Its own docstring says why it is not: *"BEYOND the port, and only ever an optimisation:
  the platform's contract is to re-derive what was written from the dataset, and a caller that ignores
  this is not weaker for it."*
  *But the caller does NOT ignore it — it raises when the value is absent*, so an optimisation is being
  consumed as a requirement. Routing this lane through `executor_for` therefore forces a choice, and
  none of the three is free:
  1. **re-derive** with a second `measure(to_uri)` — rejected in a comment right above the call, which
     records it as "a second stats read plus an upstream open, for numbers identical by construction";
  2. **narrow back** to the concrete `InProcessExecutor` after resolving — which defeats the point of
     resolving, and the estate forbids the `cast` that would hide it;
  3. **widen the port** with an optional result capability — honest, but it is a `service-kit` change
     that every future adapter inherits, and it needs an answer for what "the result" means to an engine
     that writes asynchronously.
  Option 3 is the only one that leaves the abstraction intact; it is also the only one that touches a
  shared package, which is why it is a decision rather than an edit.
- **RE-MEASURED 2026-09-16 — all three facts still hold, and one candidate fix was checked and
  refuted.** `executor_for` still has ZERO production callers (the only match outside tests is a
  docstring naming it); `transform.py:788` still hand-builds `InProcessExecutor(settings.storage_options)`;
  and there is no `def result` anywhere in `service-kit`, so `result()` is still off the port.
  *The refuted candidate, recorded so it is not re-proposed:* `transform.py:793` discards an outcome —
  `handle, _outcome = await executor.submit(...)` — which looks like the port already returning what
  `result()` gives, making a fourth, free option. It does not: `SubmitOutcome` is a SUBMITTED/REATTACHED
  enum, and the measurement is stashed in `InProcessExecutor._results` for `result()` to read. The three
  options stand.
- *The decision record no longer overstates this.* `DECISIONS.md`'s 2026-09-15 entry now says
  explicitly that neither lane goes through the port and names this row as the open decision — a first
  draft of that entry claimed "neither is constructed by hand", which `transform.py:788` falsifies.
- *Closes when:* dispatch goes through the port — one door, both lanes — OR `engine_registry.py` is
  deleted and `DECISIONS.md` rewritten to describe the branch the code actually has. **Keeping an
  uncalled port is the worst of the three**, because the decision record then documents an
  architecture nothing implements. Do NOT add an engine-refusal guard: one already exists at
  `engine_choice.engine_for` and a second would be unreachable.
- **OPTION 3 IS SMALLER THAN THIS ROW MAKES IT SOUND, and the port already answers the objection
  against it — measured 2026-09-16.** The row calls widening the port "a `service-kit` change that every
  future adapter inherits" needing "an answer for what 'the result' means to an engine that writes
  asynchronously". The port ALREADY has that pattern, twice: `Capability` is a `StrEnum` whose own
  docstring says *"Absence is the default, so a new adapter is assumed to promise nothing"*, and
  `CANCEL` / `FAILURE_DETAIL` are exactly optional methods gated by a declared capability.
  * So the change is: one `RESULT` member, one optional `result()` on the protocol, `InProcessExecutor`
    claims it, `RayJobsApiExecutor` does not — the same way it already declines `DURABLE_RECORD`.
  * And the objection answers itself: what "the result" means to an asynchronous engine is **that it
    does not claim the capability**, and the caller re-derives. That is not a new question; it is the
    question `DURABLE_RECORD` already settled on this very port.
  * The asymmetry is already REAL in the code, which is the argument for naming it rather than hiding
    it: the Ray lane has no in-process result and re-derives today, so "re-derive when the engine does
    not promise a result" describes what the estate does, while `transform.py`'s hand-built adapter is
    the one lane taking a shortcut the port cannot express.
  *Still filed as a decision*, because it is a shared-package port and the ruling is the owner's — but
  the decision is now "adopt the pattern the port already uses" rather than "design a capability
  model", which is a materially smaller question than the row posed.

**LH-159 · THE BYO CONTRACT — what "bring your own workflow engine and compute engine" actually requires, and where rask is short of it**
`medallion, service-kit` · **HIGH** · owner ruling 2026-09-15, evidence from an adversarial workflow the same day

- **THE RULING, in the owner's own terms (2026-09-15):** *"it would be good if dapr is well integrated
  to run any compute engine. But Ray is first citizen ofc and dapr aswell. But clear abstractions and
  no coupling between lakehouse and/or BYO stuff like workflow engine and compute engine."*
  Three requirements, and they are not the same requirement:
  1. **No coupling** — the lakehouse (catalog, lineage, medallion, maintenance) must not DEPEND on a
     workflow engine or a compute engine. This is condition 3 of the goal.
  2. **Clear abstractions** — the seam between the lakehouse and a BYO engine is explicit and is the
     ONLY door, rather than a branch that happens to pick one.
  3. **First-class citizens, not the only citizens** — Dapr Workflow and Ray are the shipped choices
     and may be the best-integrated ones; they must not be the only ones the code can express.
- **WHERE THE ESTATE STANDS, measured 2026-09-15 rather than assumed (8-agent adversarial workflow,
  0 errors).** Requirement 1 HOLDS. Requirements 2 and 3 do NOT.
  * *No coupling — verified empirically, and it is genuinely true.* No service in the four lakehouse
    services imports `ray` or `ray_kit`; the only Ray contact is httpx against the Jobs REST API
    (`ray_submit.py`, `ray_jobs_api.py`). Both medallion entrypoints BOOT with `ray`, `ray_kit`,
    `dapr.ext.workflow` and `durabletask` all blocked. The in-process engine is a real second engine,
    not a fallback, and its tests are green.
  * *Clear abstractions — **NOW HOLDS, 2026-09-17.*** This bullet read "NO" on the measurement that
    `executor_for` had ZERO production callers while `transform.py` hand-built `InProcessExecutor`. That
    is no longer the code: [[LH-158]] shipped the owner's option-3 ruling, the in-process lane resolves
    through `executor_for`, and the result read is gated on `Capability.RESULT` so an engine promising
    nothing re-derives instead of being special-cased. **The Ray lane still dispatches via `use_ray` at
    `transform.py:828` and that is correct rather than outstanding** — it is the ORCHESTRATION axis
    (Dapr Workflow), a different seam from the compute port, and `RayJobsApiExecutor` is what sits in
    front of the compute half. Requirement 2 is met for the axis this requirement is about.
  * *Not-the-only-citizens — NO, and this is the sharp end.* Because in-process is the implicit `else`
    at `transform.py:828`, a task registered for a THIRD engine does not fail — it silently runs
    in-process. An estate that cannot REFUSE an engine it does not host cannot honestly claim to
    support bringing your own.
- **RE-MEASURED 2026-09-16 AT HEAD — REQUIREMENT 3 NOW HOLDS, AND ITS SHARPEST CLAIM IS FALSIFIED.**
  The bullet above says *"because in-process is the implicit `else` at `transform.py:828`, a task
  registered for a THIRD engine does not fail — it silently runs in-process"*. That is no longer the
  code. `engine_choice.engine_for` resolves the task's registration and raises `UnrunnableTaskError`
  when `registration.engine not in hosted_engines(settings)` — and the refusal distinguishes the two
  operator errors it covers, because their fixes differ: an engine this BUILD has no adapter for ("the
  declaration is valid and belongs to another executor") versus one the build knows and this DEPLOYMENT
  has turned off ("the Ray lane's workflow runtime starts only when MEDALLION_RAY_ENABLED is true, so
  the stage would be enqueued and never executed"). The implicit `else` at :828 is now safe *because*
  the refusal fires upstream of it: by the time `use_ray` is computed, the engine is known to be hosted.
  `hosted_engines` is a DEPLOYMENT fact derived from `ray_enabled`, narrower than the build's
  `KNOWN_ENGINES` ceiling — so a task registered for `ray` on a Ray-OFF deployment is refused rather
  than scheduled-and-never-executed, which is the silent failure the row was really about.
  Pinned by seven files under `services/medallion/tests/`, `test_the_record_decides_which_engine_runs_it.py`
  and `test_a_declared_ray_task_is_refused_where_no_ray_runtime_runs.py` among them.
- **REQUIREMENT 2 HAS MOVED TOO, and what is left of it is narrower than the bullet above says.** The
  claim was "the port exists and is honoured by NEITHER lane". Both adapters now exist and both lanes
  reach the port's interface: `transform.py:788` drives the in-process lane through
  `executor.submit/status/failure/result`, and `rayjobs_api_executor.RayJobsApiExecutor` wraps
  `submit_or_reattach`/`job_status`/`job_failure` (deliberately NOT claiming `DURABLE_RECORD` — a Jobs-API
  submission lives in the head's GCS and a head restart takes the history with it, observed on this
  estate, and that absence is what licenses the resubmit machinery).
  *What genuinely remains is the SELECTION function, not the adapters:* `engine_registry.executor_for`
  still has **zero production callers**, and `transform.py:828` picks the lane with
  `use_ray = ... == RAY_ENGINE` rather than by asking the registry for the adapter. So engine choice is
  still a branch that happens to pick one, which is exactly requirement 2's wording — and it is
  [[LH-158]]'s remaining scope, not a second row.
- **TWO COUPLINGS THAT ARE REAL AND ARE NOT CONDITION-3 VIOLATIONS**, recorded so they are not
  mistaken for either:
  * `services/medallion/pyproject.toml:30` hard-depends on `dapr-ext-workflow`. INSTALL-time coupling,
    not import-time — the service boots without it. Fixable as an optional extra; until then "BYO
    workflow engine" costs a dependency you may not use.
  * **The Ray lane runs ONLY through Dapr Workflow.** "Ray without a workflow engine" is not a
    supported combination today. That is a genuine limit on requirement 3 and is invisible from the
    code's shape, because each axis reads independent while the product of them is not.
- *Also worth knowing before designing this:* the deployed default is engine-ON (`chart/values.yaml:1299`),
  and the in-process lane is exercised by unit tests but has **never run in-cluster**. So the "second
  engine" that proves the abstraction has no live evidence behind it.
- **WHAT FLYTE DOES, checked because it is the reference BYO engine and the answer is NOT "copy it".**
  Flyte reaches Ray by stamping a `submissionMode` onto a KubeRay `RayJob` CR it always creates, and it
  cannot attach to an externally-managed cluster by name (`ClusterSelector` is never populated) — its
  own existing-cluster path is the client protocol, `ray.init("ray://…")`, which needs `ray` in the task
  image and is therefore closed to rask by an existing deliberate choice. Its `runtime_env` lands as
  PLAINTEXT YAML in `spec.runtimeEnvYAML`, so its credential posture is worse than rask's
  `credential_ref`-names-never-carries.
- **WHAT TO TAKE FROM FLYTE, ITEM BY ITEM (owner 2026-09-15: inspiration, and copy outright where it is
  genuinely good).** "Do not copy Flyte" was too blunt; the test is whether a borrowed thing solves a
  problem rask actually has.
  * **COPY — `submissionMode` as a named config value.** Flyte does not branch on a boolean to decide
    HOW it reaches Ray; the mode (`K8sJobMode` / `HTTPMode` / `SidecarMode`) is declared data on the
    task and the plugin dispatches on it. That is exactly what [[LH-158]] needs in place of
    `use_ray = engine_for_async(...) == RAY_ENGINE`: the record says which door, and the core stops
    deciding. Worth copying in spirit, near-verbatim.
  * **COPY THE SHAPE — `shutdown_after_job_finishes` + `ttl_seconds_after_finished` as first-class
    fields.** rask has no way to express "this work's resources should be reclaimed after N seconds".
    It needs none today against a standing cluster, which is why this is a shape to keep rather than a
    field to add now — but a per-tenant ephemeral cluster would need exactly these two and nothing else.
  * **TAKE AS DISCIPLINE — the plugin boundary.** One declared config message per engine, and a core
    that never branches around it. rask's `Executor` port is the same idea already; the gap is that
    nothing uses it, not that the idea is missing.
  * **DO NOT COPY — `runtime_env` as plaintext YAML in the CR.** Strictly worse than
    `credential_ref`-names-never-carries, which rask already has.
  * **DO NOT COPY — always-create-a-cluster.** Correct for Flyte's per-job model and wrong for a
    standing shared cluster, which is rask's.
- *Closes when:* the port is the only door (a dispatch that REFUSES an unhosted engine — [[LH-158]]);
  the Ray lane reaches it as an adapter rather than bypassing it; `dapr-ext-workflow` is an extra rather
  than a hard dependency; and either the Ray/workflow product is decoupled or the limit is WRITTEN DOWN
  as a supported-combination matrix instead of being inferred from two independent-looking flags.
  A second engine that has actually run in-cluster is the evidence this row is really closed.
- **RE-MEASURED 2026-09-16 AND THREE OF THIS ROW'S FOUR CLOSES-WHEN CLAUSES ARE ALREADY SATISFIED.
  Corrected here because a HIGH row that overstates what is missing is how phase-2 planning starts from
  the wrong place.**
  * *"`dapr-ext-workflow` is an extra rather than a hard dependency" — DONE.*
    `services/medallion/pyproject.toml:56` declares `workflow = ["dapr-ext-workflow>=1.18"]`, and
    `.docker/rest-catalog.dockerfile:32-41` passes `--extra workflow` with the reason on the line:
    the engine moved out of the unconditional dependencies "so the lakehouse may be DRIVEN BY a workflow
    engine without DEPENDING on one (goal condition 3, now expressed in package metadata rather than
    only in the import graph)". Measured there too: `uv export --package medallion` names the engine 0
    times without the flag and 2 times with it. **Condition 3 is now true of the PACKAGE, not just of
    the import graph.**
  * *"a dispatch that REFUSES an unhosted engine" — DONE, and it refutes this row's sharpest claim.*
    The row says "a task registered for a THIRD engine does not fail — it silently runs in-process".
    `engine_choice.engine_for` raises `UnrunnableTaskError` for an engine the deployment does not host,
    and the message distinguishes the two cases an operator must tell apart — a build with no adapter
    ("the declaration is valid and belongs to another executor") from one this deployment turned OFF
    ("the Ray lane's workflow runtime starts only when MEDALLION_RAY_ENABLED is true"). Gated by
    `services/medallion/tests/test_the_record_decides_which_engine_runs_it.py::test_an_engine_NOBODY_here_runs_is_refused_rather_than_silently_defaulted`.
    **This estate CAN refuse an engine it does not host.**
  * *"the Ray lane reaches it as an adapter" — DONE at the adapter, NOT at the caller.*
    `medallion/services/rayjobs_api_executor.py` is a real `Executor` for `RAY_ENGINE` — it wraps
    `submit_or_reattach` / `job_status` / `job_failure` rather than reimplementing them, and
    deliberately does NOT claim `DURABLE_RECORD` because a Jobs-API submission dies with the head's GCS
    and claiming it would make `may_resubmit` refuse a run that really was lost. `engine_registry`
    resolves both engines.
- **WHAT IS ACTUALLY LEFT IS ONE BYPASS AND ONE DOCUMENT.**
  * `engine_registry.executor_for` still has **ZERO production callers** — measured by grep across
    `services/` and `packages/`, only three test call sites. `transform.py:830` still decides with
    `use_ray = await engine_choice.engine_for_async(...) == RAY_ENGINE` and branches at :832 and :877.
    So the port has two adapters and nothing dispatches through it: the port is not yet the only door.
    That is [[LH-158]]'s remaining scope, and this row should not be worked separately for it.
  * ~~The Ray-lane-runs-only-through-Dapr-Workflow product limit is still **not written down**.~~
    **WRITTEN DOWN 2026-09-16, as a matrix that is a TEST rather than prose** —
    `services/medallion/tests/test_the_supported_engine_and_workflow_combinations.py`. A document would
    have described the product; a test makes adding or removing a combination silently impossible,
    which is what the clause is for.

        ray_enabled   declared engine   outcome
          false          (none)         in-process — the chart default
          false          inprocess      in-process
          false          ray            REFUSED, and the message names MEDALLION_RAY_ENABLED
          true           (none)         ray — the chart default
          true           inprocess      in-process  <- the cell that carries the decoupling
          true           ray            ray

    *And it is ONE FLAG DOING THREE JOBS, which is why the product was invisible.*
    `engine_choice.hosted_engines`' own docstring says `ray_enabled` "is read here for the SECOND of
    its two jobs"; measured, there are three — `stage_runner.py:91` starts the Dapr Workflow runtime,
    `producer.py:119` starts it for `quality_review_enabled OR ray_enabled`, and `engine_choice` both
    gates `hosted_engines` and supplies the chart default.
    *The unsupported cell, stated so nobody derives it again:* **Ray WITHOUT a workflow engine is not
    expressible** — no setting yields the Ray compute engine with the runtime off, because one flag
    starts both. The converse IS expressible and is honoured, and that asymmetry is the decoupling
    working in the direction it currently works in. A last assertion reads both gates off the SOURCE,
    so a second flag that ever splits them fails this test and forces a new row in the matrix.
  * ~~The in-process lane has still **never run in-cluster**.~~ **IT HAS NOW — 2026-09-17, and this
    was the row's own stated evidence bar** (*"A second engine that has actually run in-cluster is the
    evidence this row is really closed"*). `rask-bronze-to-silver` was switched to the in-process lane
    for one drive on `main-e3139b71`: `POST /produce?rows=17` → `202`, then
    `medallion_stage_moved transition='bronze->silver' token='lh158b-1789642736' to='silver$features'
    duration_seconds=0.23`, a governed silver write against the estate's real S3-backed Lance datasets.
    Which lane ran is read from `opentelemetry_traces`, not inferred:
    `lance.medallion.compute='in_process'`, `service_name='bronze-to-silver'`,
    `span_name='medallion.transform'`. The toggle was restored; all four medallion deployments read
    back `ray=true`.
- **RE-MEASURED 2026-09-17. ONE CLAUSE IS LEFT, AND THE REASON IT WAS LEFT IS A STALE FACT.**
  * *The bypass this row names is HALF closed.* [[LH-158]] shipped: `transform.py` resolves the
    in-process lane through `executor_for` and no longer imports `inprocess_executor`. **The Ray lane's
    CALLER still bypasses the port** — `medallion/workflow.py` imports `ray_submit.submit_stage_job`,
    `ray_jobs_api.job_status` and `ray_jobs_api.job_failure` directly (lines 487, 527-528, 707-708,
    978-979), while `RayJobsApiExecutor` wraps exactly those three and has zero production callers.
    That direct import IS the Ray coupling goal condition 3 names.
  * **THE STATED REASON NOT TO WIRE IT IS FALSIFIED.** `docs/DECISIONS.md`'s 2026-09-15 entry justifies
    the split with *"`WorkOrder.to_env()` supplies `RASK_SOURCE_URI`/`RASK_DEST_URI`/… and
    `scripts/ray_stage_job.py` reads `FROM_URI`/`TO_URI`/… — 0 of 6 overlap"*. Measured at HEAD, the
    script reads the PLATFORM's vocabulary and says so in its own comment (`:441` *"THE PLATFORM'S OWN
    VOCABULARY — `WorkOrder.to_env()`"*), taking
    `os.environ["RASK_SOURCE_URI"], os.environ["RASK_DEST_URI"], os.environ["RASK_STAGE"]` at `:449`,
    plus `RASK_LINEAGE_DOCUMENT`, `RASK_VERSION_FLOOR`, `RASK_CARDINALITY`, `RASK_DEST_TABLE` and
    `RASK_IDEMPOTENCY_KEY` — all emitted by `to_env()`. The S3 credentials the job also reads come from
    the Ray pod's own environment and were never the order's to supply. The correction is recorded in
    `DECISIONS.md` beside the entry it corrects.
  * *So the real remaining gap is ONE specific thing, not a vocabulary mismatch:* `ray_submit.py:167`
    falls back to `settings.ray_entrypoint` when no task is DECLARED, while
    `RayJobsApiExecutor.submit` takes its entrypoint from `registration.command` and has no undeclared
    path. Wiring the adapter needs an answer for the undeclared case and nothing else.
  * **AND THE ACTUAL BLOCKER IS NEITHER OF THOSE — it is the JOB ID, measured 2026-09-17.** The two
    lanes derive the Ray submission id from the SAME four axes ([[LH-157]] unified the axes) and
    produce DIFFERENT STRINGS. Driven with identical input:

        ray_submit.stage_submission_id    -> ray-silver-tok-1-a71b9b7999ac-9b15d9e0
        work_order.derive_idempotency_key -> 7bd99c7109b5821494f51a169b8cc23b106d6817

    `RayJobsApiExecutor.submit` posts under `order.idempotency_key`, so wiring it as-is renames every
    Ray job: in-flight work is orphaned — the poller watches an id the submitter never used, which is
    the exact defect `stage_submission_id`'s docstring says it was extracted to prevent — and the
    operator-readable `ray-<stage>-<token>-…` form the Ray dashboard is searched by is lost.
  * *And the adapter CANNOT simply derive the deployed shape, which is what makes this structural
    rather than a one-line fix:* `stage_submission_id` keys on `token`, and **`WorkOrder` has no token
    field** — it is consumed into `idempotency_key` at construction and never carried. So the adapter
    is missing an axis it would need.
  * *Which leaves three ways to close it, none free:* carry the token on the `WorkOrder` (a shared
    `service-kit` model change, and the token is a credential-shaped value to think twice about
    putting on a queued order); accept the id change behind a drain of in-flight jobs; or have the
    workflow pass the handle it already owns rather than letting the adapter mint one. The last is
    smallest and fits the port — `RunHandle` already separates `engine` from `handle` precisely because
    a handle is the ENGINE's id, not the platform's.
  * *NOT attempted here on purpose:* this rewires the live cascade's submission path, and the estate is
    mid-investigation on [[LH-137]]. Named rather than half-done — and the naming is now exact, which
    it was not before: the obstacle is the id derivation, not the wire vocabulary and not the
    undeclared entrypoint (that one is solvable with the synthesized-registration pattern
    `transform.py` already uses for the in-process lane).

**LH-163 · ~~The sweep's base probe is permanently denied on `lance-catalog/models/`, and answers with a full traceback every pass~~ — CLOSED 2026-09-15, observed live**
`maintenance, chart` · med · measured 2026-09-15 on the running `rask-maintenance`

- *The mechanism:* `features.gather_compaction_bases` probes every base a manifest declares, and
  `NOTHING HERE RAISES` by design — a failed probe is recorded as the unknown it is, and
  "unknown resolves to refusal" because the cost of a wrong permit is a clone's whole reason to exist.
  That part is correct and must stay.
- *What is not correct is that the denial is PERMANENT and reported as an incident.* The sweep runs as
  `rask-maintenance`, whose scoped credential cannot read `s3://lance-catalog/models/`, so the probe
  can never succeed — yet each attempt logs `compaction_base_probe_failed` with `exc_info=True`.
  **Measured: 240 denied probes and ~101 rendered tracebacks in 15 minutes**, all on the one base.
- *Why it matters beyond noise:* a permanent, expected denial rendered identically to a transient store
  fault is how a real fault stops being visible. It also means any dataset declaring that base can
  never be compacted, and nothing says so in terms an operator can act on.
- **THE SECOND ANSWER WAS TAKEN, and the first is refused with a reason.** Widening
  `minio.maintenanceAccessKey` to read `models/` would let the probe answer truthfully — and that
  answer would be "not a dataset root", which PERMITS compaction on every dataset declaring that base.
  A logging complaint is not a reason to move a gate in the permissive direction, so the credential
  stays scoped and the refusal stays.
- *Landed 2026-09-15:* a denial is reported ONCE per base per process and without a stack. The shape is
  exact rather than merely quieter — the S3 credential is resolved once at boot from the Dapr secret
  store, so it cannot change while the process lives and the answer for a given base is deterministic
  for its lifetime; a rotated credential arrives by rollout, which is a new process and a fresh report.
  A NON-denial keeps both its traceback and its repetition, because an outage or a probe bug is exactly
  where the stack matters and is not deterministic.
- *The guard is untouched and pinned:* `probed=None` and `probe_denied=True` still reach
  `describe_compaction_unsupported_flags`, so "unknown resolves to refusal" holds. A de-duplication
  that also dropped the evidence would silently PERMIT the rewrite these bases exist to refuse, which
  is the failure direction that costs a clone its reason to exist — mutation-tested in both directions.
- **OBSERVED LIVE on `main-4fcfd969`:** rendered tracebacks per pass **134 -> 0**, lines for that base
  **134 -> 1**, and the sweep's own result **unchanged at `datasets=552 skipped=0 refused=320`** — which
  is the half that matters, because it shows the gate did not move while the noise did.

**LH-164 · The sweep asks authz about datasets that are not catalog tables, and advises a grant that cannot be made**
`maintenance` · med · measured 2026-09-15; **headline corrected after the live manifests were read**

- **THIS ROW SAID THE CASCADE'S GOVERNED TIERS HAD LOST THEIR TUPLES. THAT WAS WRONG.** Read from the
  live warehouse manifest: `lakehouse$silver`, `lakehouse$gold` and `lakehouse$silver-media` are
  **namespaces**, not tables. The cascade's actual stage outputs are `lakehouse$silver$features` and
  `lakehouse$silver-media$features`, and they are HEALTHY — each carries its `parent` edge and an
  `owner`, and `service-maintenance can_maintain` checks **True** against the live evaluator. The
  governance chain works end to end for everything the cascade registers.
- *What the sweep is actually refused on:* five UNREGISTERED datasets sitting at the chart's rendered
  `s3://<bucket>/medallion/<ns>` paths — `medallion/lakehouse$silver` (8 rows, v12),
  `medallion/lakehouse$gold` (8 rows, v18), `research-bucket/medallion/bronze` (8 rows),
  `bind86-wh/medallion/bronze` (500 rows). They carry the real tier schema
  (`id, payload, source_rowid, stage, lineage`), so they are medallion data — they were simply never
  registered as catalog tables.
- **THE DEFECT THAT REMAINS IS THE ADVICE, AND IT IS REAL.** `discover_datasets` finds these by BUCKET
  SCAN, derives a table id from the path, and asks `can_maintain` on `table:lakehouse$silver`. The
  answer is correctly False — no such table exists — but the sweep reports
  *"this rewrite is not authorized for 'service-maintenance'. Grant can_maintain on
  table:lakehouse$silver if it should be"*, which is advice nobody can follow: that id names a
  NAMESPACE, and granting a table rung on it is impossible. A refusal that cannot be acted on trains
  the reader to ignore the whole category, next to 315 refusals that are correct.
- *So the fix is a CLASSIFICATION, not a grant:* a discovered dataset with no catalog table behind it
  is UNGOVERNED, which is a different finding from UNAUTHORIZED and needs different words — and the
  estate already has a name for it, since lineage's reconciler classifies exactly that condition.
- *The second question is an owner call, not a code change:* whether that parallel medallion data
  should exist at all. `chart/templates/medallion.yaml` renders `MEDALLION_BRONZE_URI` and the stage
  runners' `MEDALLION_FROM_URI` from `s3://<bucket>/medallion/<ns>`, while `ensure_stage_output` vends
  a DIFFERENT, governed location per tier — so each tier has two homes and only one of them is
  governed. Reaping the wrong one destroys live rows (bind86 holds 500).
- *What this row cost and what it bought:* two fixes landed on the way here that are correct on their
  own merits and were NOT what these five needed — [[LH-165]]'s maintainer grant (which did close 20 of
  the 25 refusals) and the two seam convergences below. Keeping them is right; the headline that
  justified them was not.
- **The seam fixes that landed, kept on their own merits:** `register_table` now converges a table's
  structural edge on an already-exists whose location MATCHES and re-raises the 409 unchanged (the
  spec gives this door no ExistOk, so the status is not negotiable); and `ensure_stage_output` creates
  with `?mode=exist_ok`, because a tuple-less table denies every relation and so `describe` refuses it
  exactly as it refuses an ABSENT one, which the default create mode then turned into a 409 collision.
- **THE MESSAGE HALF LANDED 2026-09-15.** Both refusal sites (`catalog_compaction`'s plan door and
  `credentials`' vend door) now end with one shared `denial_remedy(...)`, which names BOTH causes and
  says why they cannot be told apart — the gate runs before existence resolution, so "no rung" and
  "no such table" are the same 403 on the wire. It no longer instructs a grant on an id that may be a
  namespace; it asks the reader to check the id names a TABLE first. One function rather than a phrase
  at two sites, because the two had already drifted apart once.
- **THE DETECTION HALF LANDED 2026-09-15 as a reconciler category**, which is the owner's third
  convergence point built in the shape the evidence supports rather than the one the option named.
  Converging every table on every catalog boot means enumerating ~96 warehouses' tables at startup,
  and `cascade_backfill`'s own docstring argues against making boot depend on that. What was genuinely
  missing is DETECTION: the reconciler compared registry against storage and against FGA at the
  project and warehouse rungs and was blind at the TABLE rung — which is why the first door ever to
  ask one of these a permission question was the sweep's `can_maintain`.
  `ungoverned_tables` now reads the same manifests the namespace scan already opens (`_tables_across`)
  and set-differences them against `counts_by_type['table']` from the one whole-store tuple scan.
  NON-GATING by the module's own rule rather than by preference — a category gates the #79 purge only
  if it is a STORAGE fact with a door that clears it, and this is an authz fact with neither. It
  degrades WITH OpenFGA, which is load-bearing: without the tuple scan every table reads as ungoverned,
  so an outage would otherwise report the whole estate as drift.
- *Closes when:* the owner rules on whether the chart-path medallion datasets are residue to reap or
  data to register. That is the only thing left here, it is a decision rather than a defect, and it
  carries real risk either way — `bind86-wh/medallion/bronze` holds 500 rows.

**LH-165 · ~~NO code writes a per-warehouse `maintainer` tuple — the 93 that have one were written by hand, and every warehouse created since gets none~~ — CLOSED 2026-09-15: observed live, and the boot backfill repaired the estate**
`catalog` · **HIGH** · measured 2026-09-15 against the live store, the code and the registry timestamps

- **THE COUNT IS INVERTED FROM HOW IT FIRST READ, and that inversion IS the finding.** 93 of 97
  warehouses carry `maintainer@user:service-maintenance` and 4 do not, which invites "backfill the
  four". The four are `e2e-iso-a`, `e2e-iso-b`, `lane-wh`, `trackab1bc9ea2-wh` — and sorted by the
  registry's own `created_at` they are **the four NEWEST** (2026-09-10 ×3, 2026-09-14), while every
  granted warehouse is older. Across all 96 registry records the cutoff is clean with zero exceptions:
  newest granted `2026-09-06T04:42Z`, oldest ungranted `2026-09-10T08:36Z`.
- **The reason is that NO CODE PATH WRITES THAT TUPLE.** Verified two ways: a repo-wide grep for a
  `maintainer` relation being written across `services/`, `packages/` and `scripts/` returns NOTHING;
  and `fga_deps.seed_warehouse` — the create door's only seeding — emits `owner@<caller>` plus
  `cascade_tuples()`, which is the `project` edge and the writer/publisher/validator rungs for
  `LANCE_FGA_CASCADE_WRITERS` (the medallion producer and its stage runners; never maintenance).
  `backfill_cascade_grants` calls the SAME function, so the repair path writes the same empty set.
  The one committed writer is the Helm hook `chart/templates/bootstrap-admin.yaml`, and it writes ONE
  tuple on ONE fixed object: `FGA_ROOT_OBJECT` = `warehouse:lance_catalog`. `warehouse.maintainer` is
  `[...] or owner` with no `from parent`, so a grant on the root warehouse reaches no other warehouse.
- *So the 92 are the artefact and the 4 are the software.* They were written OUT OF BAND into the live
  store on 2026-09-08, enumerated from the registry as it stood that day
  (`tests/integration/test_the_maintainer_rung_opens_the_write_tier_vend.py`: *"MEASURED 2026-09-08:
  `cd4697ab` deployed, 92 `maintainer` tuples written"*). 92 by hand + 1 from the chart hook = 93.
  **Every warehouse created after that date has no grant and every future one will have none**, so
  this is a live defect that grows by one per tenant onboarded, not a residue to sweep up.
- *`cascade_tuples`' own docstring is the tell, and it should be corrected in the same change:* it
  justifies granting at the warehouse with *"the same property the sweep's `maintainer` grant buys"* —
  citing, as precedent, a grant that no code has ever written.
- *Why backfilling first would have been the wrong move:* it repairs four rows, turns the sweep green,
  and leaves the create door writing nothing — so the next warehouse reopens it and the repair reads
  as the fix. This row existed for one revision demanding that distinction be measured before acting;
  it was, and it inverted the answer.
- **OBSERVED LIVE on `main-94e86fe7`, 2026-09-15**, by driving the real door rather than reading a
  render: a Dex bearer minted in-cluster, `POST /v1/projects` then `POST /v1/warehouses` -> 200, and
  the new `warehouse:lh165probe-wh` carries `maintainer@user:service-maintenance` in the live store.
  Probe project and warehouse deleted afterwards.
- **AND THE ESTATE REPAIRED ITSELF, which is why the grant belonged IN `cascade_tuples`.** The
  backfill runs in the catalog's own lifespan, so the rollout alone fixed every existing tenant:
  `cascade_backfill_done warehouses=96 tuples=1344 failures=0`, and all four measured warehouses
  (`e2e-iso-a`, `e2e-iso-b`, `lane-wh`, `trackab1bc9ea2-wh`) now hold the grant. Not one tuple was
  written by hand — the failure mode this row was opened against.
- *Measured at the consumer:* the sweep's refusals fell **333 -> 320**, and the breakdown is exact.
  315 are the shallow-clone protection working, and the "not authorized for `service-maintenance`"
  class fell from 25 to **5** — the five being precisely [[LH-164]]'s zero-tuple tiers
  (`lakehouse$silver`, `lakehouse$gold`, `lakehouse$silver-media`, `research-bronze$events`,
  `bind86-bronze$events`). Two defects, cleanly separated by the fix.
- *Residual, and it is [[LH-164]]'s to carry:* nothing yet REPORTS a warehouse missing the grant. The
  backfill now converges it on every catalog boot, which is a stronger answer than a report for this
  particular drift, but a table with no `parent` edge is still invisible to every detector.


**LH-166 · An event naming an UNGOVERNED output can never be accepted, so it parks forever — and the DLQ has grown past what [[LH-148]] recorded**
`lineage, medallion, maintenance` · med · measured 2026-09-15 on the live estate

- **[[LH-148]] SAYS "not currently bleeding" AND THAT IS NO LONGER TRUE.** It measured 8,612 DLQ
  messages on 2026-09-13 with "zero since 2026-09-11". Measured now: the DLQ stream holds **9,887
  messages / 29 MiB**, of which **`dlq.lineage.events` is 9,856** — roughly **+1,341 since that
  reading** — and the last delivery was **11 minutes** before this row was written. The
  `lineage-dlq-durable` consumer's last delivery timestamp agrees.
- **AND THE CONSUMER ALREADY INTENDS THE OPPOSITE OF WHAT IT GETS — `_DROP` IS WHAT PARKS THE EVENT.**
  `consumer.handle_cloud_event` returns `_DROP` on `PermissionDeniedError`, and its own docstring gives
  the reason: *"Redelivery cannot grant a permission, so retrying a refused event only burns the delivery
  budget and then parks a permanent refusal on the dead-letter topic as if it were an outage."* The
  intent is explicitly to AVOID parking. It is not achieved: the subscription declares a
  `deadLetterTopic` (`api/dapr.py`), and for Dapr a DROP on a subscription with a dead-letter topic
  ROUTES THE MESSAGE THERE. Measured on the live estate rather than read from a doc — the sidecar and
  the app name the same CloudEvent id, back to back:

      daprd  "DROP status returned from app while processing pub/sub event a4d65ffd-c7b5-4493-8ea7-739df9412387"
      app    dapr_dead_letter_parked app='lineage' event_id='a4d65ffd-c7b5-4493-8ea7-739df9412387'
      app    POST /lineage-dlq HTTP/1.1 200 OK

  So the three ack outcomes collapse to two in practice: RETRY redelivers, and **both SUCCESS-less
  outcomes park**. There is currently NO ack that says "refused, permanently, do not keep this" — the
  *"a bus answer that isn't an unrepairable park"* half of this row's open fix direction.
  **THIS IS A SECOND SITE OF A CLASS [[LH-151]] ALREADY RECORDED, NOT A NEW MECHANISM**, and saying so
  matters because it changes who decides: that row measured the same DROP-parks shape in `medallion`'s
  `transform.py` on 2026-09-14 and verified it at the Dapr source — `pkg/runtime/subscription/subscription.go:365-368`
  routes `ErrMessageDropped` to the dead-letter topic, and the HTTP postman returns that error for a
  `DROP` status. What lineage adds is a second unfixed instance carrying the same false comment (rewritten
  2026-09-16, `8c44e7d8`; behaviour unchanged) plus the restart-loop cost quantified above. **Both sites
  wait on ONE decision, not two.**
- *Which makes the fix candidates concrete, and the choice is still the owner's:* (a) answer SUCCESS for
  a structurally permanent refusal, so the event is acked and the refusal is recorded in the app's own
  metric/log instead of the DLQ — cheap, stops the loop, and deliberately discards the event; (b) keep
  parking but make it IDEMPOTENT, so an event already in the DLQ is not appended again — preserves the
  record, needs a dedup key the park route can check; (c) fix it at the producer, so a role literal
  never reaches `author.sub` at all — the only option that stops the events being unrepairable in the
  first place, and the only one that helps the 10 of 49 parks that are NOT role literals.
- **THE MECHANISM, MEASURED 2026-09-16: THE DLQ IS A FEEDBACK LOOP, AND ITS GROWTH IS NOT A LOSS
  RATE.** The "burst" above is real and its CAUSE is now known — it is a pod restart, not a wave of
  production traffic. The ingest consumer is ephemeral with `deliverPolicy: all` (the estate's recovery
  story, and `fga_deps._is_replay` documents it), so **every lineage restart re-presents the whole
  retained stream** to the authorization gate. The gate refuses the same unrepairable events again, and
  **each refusal appends a NEW message to the DLQ about an event already in it.**
  * Driven deliberately: rolling lineage to `main-16dd2da6` produced **49 parks inside two minutes of
    pod start (21:53–21:54 UTC), zero before it**, and one more at 22:03 which was a probe of my own.
  * The same event parks repeatedly across restarts. Run `188ab99f-f557-5e96-b311-4c5dd8f4d119` sits in
    the DLQ at **seq 5000, parked 2026-09-10** — and was parked AGAIN at 21:53:50 on 2026-09-15, five
    days later. Two independent sources agree (the stream body and the app log), and the second park is
    a single refusal + a single park, not a retry storm.
  * **No production event newer than 2026-09-14T19:14 has ever been parked.** Sampled across the whole
    sequence range, every parked message carries an `eventTime` from 2026-09-06..09-14 while its park
    time runs to 09-15 — gaps of one to five days. The newest entry in the stream (seq 11189) is the
    probe.
- *So the row's "+1,341 since that reading" is RE-PARKS OF THE SAME EVENTS, not 1,341 new losses*, and
  [[LH-148]]'s "not currently bleeding" was closer to right than this row credited: the set of
  unrecordable events is roughly FIXED and old, while the DLQ counting them grows with every restart.
  **DLQ depth is therefore restart-count x backlog-size, and cannot be read as a production error
  rate** — which is what made it look like an accelerating bleed.
  *AND IT IS BOUNDED, which the first draft of this row omitted and which changes how alarming it is.*
  Read off the stream 2026-09-16: `retention: limits`, `discard: old`, **`max_age` 604800000000000 ns =
  168 h**, `max_msgs` and `max_bytes` both unlimited. So the DLQ holds a rolling SEVEN-DAY window and
  drops the oldest — measured, it spans 2026-09-09T07:10 to 2026-09-16T06:55 and has gone DOWN across
  today's rolls, 10,011 -> 8,291 -> 8,255, because retention is expiring faster than four deploys added.
  The loop is real and it is a steady-state CHURN, not unbounded accumulation: nothing here will fill a
  disk, and the growth this row measured is bounded by how often the estate restarts within any 7 days.
- *And it names the real hazard in [[LH-148]]'s replay door precisely:* replaying this DLQ re-presents
  events the gate must refuse again, each refusal appending another DLQ message. The flood is not
  hypothetical — it is the loop already running once per restart, driven faster.
- *The author split is the same class the estate already refuses elsewhere:* `data_eng` 29, `ray` 8,
  `analyst` 2 of the 49. `service_kit.lakehouse.subjects._NOT_A_PERSON` already classifies exactly
  `data_eng`/`analyst`/`ray` as ROLE LITERALS — "TRUE statements about who acted and useless as an
  address" — and the notifications plane refuses them for addressing. Lineage's authorization path
  accepts one as a SUBJECT, where it can never hold `can_write_data`, because no tuple is ever written
  for a role literal. The refusals name the governed tiers: `acme-silver$features` 21,
  `silver$features` 14, `bronze$events` 5, `acme-bronze$events` 5.
- **SCALE AND CHARACTER, corrected within minutes of first writing this row — it is a BURST FROM TEST
  IDENTITIES, not the steady production bleed the first draft implied.** The park count is IDENTICAL at
  60, 120 and 180 minutes (53 each), so all 53 fall inside one hour with nothing in the two before it.
  By author over 3 h: `data_eng` 30, `e2e` 14, `ray` 10, `service-stage-runner` 3,
  `service-maintenance` 2, a dex user 2. The refused outputs are fixture-shaped —
  `acme-silver$features`, `silver$features`, `e2e_crash_ds`, `bronze$events`.
- *So the honest split:* the MECHANISM is real and reaches production services (5 of 53 parks are
  `service-stage-runner` + `service-maintenance`), while the VOLUME is dominated by test and demo
  identities. The 9,856 backlog is mostly test traffic; the ongoing production loss is a trickle rather
  than a flood. Both halves matter — a trickle of unrepairable provenance loss is still condition 1
  failing, and a DLQ dominated by test noise is how the real ones stay invisible.
- *The park reason, read off the service rather than guessed:* `lineage_event_unauthorized`. Two
  samples with their authors:

      author='service-maintenance'  reason='can_write_data or can_maintain required on outputs: m2proof_silver$m2-proof-1788537252'
      author='<a dex user>'         reason='can_write_data required on outputs: e2e-ns$t74eff1b3'

- **THE REFUSAL IS CORRECT AND THAT IS WHY IT NEVER CLEARS.** Both outputs carry **ZERO FGA tuples**
  (read from the live store). `table.can_write_data`/`can_maintain` resolve through a direct tuple or
  `... from parent`, so NO principal can hold either — the author cannot acquire the rung, redelivery
  cannot change the answer, and the event parks on every attempt until retention drops it. A poison
  message whose poison is an authorization fact.
- *The two samples are DIFFERENT shapes and both matter:*
  * `m2proof_silver$m2-proof-1788537252` — its NAMESPACE `m2proof_silver` has no tuples either, so the
    whole chain is ungoverned.
  * `e2e-ns$t74eff1b3` — its namespace IS governed (`owner` + `parent: warehouse:acme-bucket`); only the
    TABLE is missing its edge. That is exactly [[LH-164]]'s shape, still arriving.
- **AND IT EXPOSES A BLIND SPOT IN THE `ungoverned_tables` DETECTOR ADDED EARLIER TODAY**, which is
  recorded here rather than quietly fixed: that category reports **0**, correctly by its own
  definition, because it enumerates tables from the catalog's `__manifest` and these ids are not
  catalog tables at all. It answers "does every table the catalog knows carry tuples", and the
  question this defect needs is "does every output a lineage event NAMES carry tuples". Those differ
  by exactly the population that is parking.
- *Why it still matters, stated at its measured size rather than at the size the first draft claimed:*
  a production service emitting provenance that can NEVER be accepted is condition 4 failing and
  condition 1 with it — `dapr.py` records that a dead letter older than the stream's retention has no
  path back, so those runs lose their provenance silently. That is true at 5 events in 3 h exactly as
  it would be at 5,000; what changes is the urgency, not the defect.
- *Closes when:* the estate stops producing lineage events whose outputs cannot be governed — either
  the writer registers its output before emitting, or the bus's authorization answers an ungoverned
  output with something other than an unrepairable park — AND `dlq.lineage.events` stops growing,
  measured over a window rather than at a point.


## PHASE 1 · CROSS-CUTTING — service-kit, storage, chart, build, tests

Shared machinery. The first group is what blocks the lakehouse and should be read as part of phase 1.

### Serves the lakehouse (phase 1)

_These cross-cutting rows sit directly under the catalog, lineage and the medallion cascade — secrets that never reach the pods that hold them, plaintext store hops, the authz model and the audit trail every governed commit depends on; left open, phase-1 work runs on an estate whose credentials silently go stale and whose audit record can be deleted unauthenticated._

**XC-001 · No `checksum/secret` pod-template annotation anywhere, so a rotated Secret is never re-read — six of seven zones polled lineage with a dead token and 46% of its traffic was 401**
`chart, lineage, frontend-zones` · **HIGH**

- *Why open:* Measured live: `rask-web-lakehouse` polled `GET /events` with a `LINEAGE_SERVICE_TOKEN` (hash `1b55ba766c3e962c`) matching no key in `rask-infra-credentials`, and 2,627 requests — 46% of all lineage traffic — were refused 401 while the zone rendered the 401 as an empty feed. A restart fixed that instance; nothing prevents the next rotation, because the Deployment reference and the render were both correct and only the running pod was wrong.
- **RE-MEASURED 2026-09-17 — THE SYMPTOM IS BACK, THE HEADLINE IS FALSE, AND THE PRESCRIBED FIX CANNOT
  WORK. Do not implement this row's Closes-when as written.**
  * *The symptom recurred:* `rask-web-lakehouse` (10.42.0.186) is again refused **401** on
    `GET /events?limit=1&summary=true` while `rask-notifications` (10.42.0.245) succeeds **200** on the
    same door with `limit=500`. So the discriminator is the CALLER's credential, not the door.
  * *"No `checksum/secret` pod-template annotation anywhere" is FALSE.* Twelve `checksum/` annotations
    render today, and `frontends.yaml:111` carries `checksum/infra-credentials` on this very zone. The
    deployment's annotation and the RUNNING POD's annotation are the same value
    (`b189d3f854d8cf95…`), so the pod is current against its own template — and it is still 401ing.
    The row's "a rotation is never re-read because nothing annotates it" theory cannot explain that.
  * *WHAT IS ACTUALLY WRONG, measured:* the pod's `LINEAGE_SERVICE_TOKEN` hashes to `482611fb9ab8c057`
    and the `service-token-service-web` key its own `secretKeyRef` names hashes to `35f668f0c818070d`.
    The pod started 2026-09-11T11:17:21Z; the Secret's managers are **`kubectl` AND
    `externalsecrets.external-secrets.io/rask-infra-credentials`**, and that ExternalSecret reported
    `SecretSynced` **5m52s** before this measurement.
  * **SO THE ANNOTATION IS STRUCTURALLY INCAPABLE OF CATCHING THIS, and adding more of them is the
    estate's signature defect — a control that cannot fire.** `checksum/infra-credentials` hashes the
    RENDERED TEMPLATE (`include (print $root.Template.BasePath "/infra-credentials.yaml") $root |
    sha256sum`). ESO rewrites the Secret OBJECT out of band on its refresh interval, with no helm
    render involved, so the object changes while the hash of the template does not. The annotation
    would only ever catch a rotation that arrived through `helm upgrade` — which is the one path that
    already rolls the pods.
  * *The honest fixes are a different shape:* watch the Secret OBJECT and restart its consumers (a
    reloader), or have the zone read its token per request instead of binding it at boot. Both are
    real; a checksum annotation is not. Note the second also removes the env binding [[XC-004]] wants
    gone, so it is one change rather than two.
- *Closes when:* Add a `checksum/secret` pod-template annotation (sha256sum of the rendered Secret) beside every `secretKeyRef` env in `chart/templates/`, and pin it with a rendered-manifest test that fails when a template adds a `secretKeyRef` without the annotation. **Note the ordering against [[XC-004]]:** the zone's `LINEAGE_SERVICE_TOKEN` arrives by `secretKeyRef` — a k8s Secret through env, which is not one of the three sanctioned paths — so the annotation makes the CURRENT path survive rotation while XC-004 removes the path. Doing the annotation first is still right: it is a one-line-per-template render change with a gate, and it stops the bleeding without a release that rotates live credentials.

**XC-002 · The chart fix moving the Ray head's S3 credential onto the sanctioned ESO path is committed and never deployed**
`chart, medallion, storage` · **HIGH** — the cascade half is CLOSED 2026-09-14; what remains is the ESO switch

- *Why open:* Live, `rask-ray-compute-s3` is a hand-applied Secret on none of the three sanctioned paths, and `rask-infra-credentials`' `ray-compute-access-key`/`ray-compute-secret-key` decode to `rustfsadmin` because `values.yaml` ships `rayComputeAccessKey: ""` and `infra-credentials.yaml:36` falls back to `rustfs.accessKey`. Both halves are in the chart and render green (369 chart/secret invariants), but rolling them out rotates a live credential and either half alone is `SignatureDoesNotMatch` on every cascade call.
- **THE ROLLOUT HALF IS DONE AND VERIFIED 2026-09-14, AND IT NEEDED NO ROTATION — the blocker was a
  STALE POD, not a credential.** `values.yaml:2055` now ships `rayComputeAccessKey: rask-ray-compute`
  and `infra-credentials.yaml:48-49` derives the scoped secret from it, so the `rustfsadmin` fallback
  this row describes is gone from the chart; `helm upgrade` to revision 156 carried it to the cluster.
  What kept the estate broken is [[XC-001]]: no `checksum/secret` annotation exists anywhere, so the
  hand-applied Ray head — running since 2026-09-11 — went on serving the pre-rotation value it read at
  start. Every cascade stage job died
  `SignatureDoesNotMatch ... GET http://rask-minio:9000/lance-catalog?list-type=2&prefix=medallion/bronze/_versions/`,
  which is this row's own predicted symptom, firing.
  *`kubectl rollout restart deploy/ray-lance-head` was sufficient and is strictly safer than the
  re-apply this row prescribes* — it recreates the pod from the existing spec, so the image tag cannot
  revert (verified: `localhost:5000/ray-lance:main-7a2de41a` before and after, the exact regression the
  Closes-when warns about). Cascade tick verified immediately after:
  `RAY-STAGE OK stage=gold lane=delta rows=8 rows_in=8 retracted=5370 version=199`, and three
  [[LH-109]] legs that had failed for this reason went green in the same drive.
- *WHAT IS STILL OPEN IS THE ROW'S TITLE, not its symptom.* The credential now WORKS but still arrives
  by `secretKeyRef` — a k8s Secret through env, which is not one of the three sanctioned paths. ESO is
  built and switched off ([[XC-004]]), so "onto the sanctioned ESO path" is unmet.
- *Closes when:* `externalSecrets.enabled=true` with an ExternalSecret for the Ray head's S3
  credential, so it stops arriving through env — the same switch [[XC-004]] tracks for the other 43
  refs. The rotation and the cascade verification this row was blocked on are done; it is no longer
  blocked on an owner go-ahead.

**XC-003 · `lance.audit` shares `opentelemetry_logs` with all telemetry: 14-day TTL, and an unauthenticated in-cluster `DELETE` on the audit stream is accepted**
`catalog, lineage, medallion` · **HIGH**

- *Why open:* Confirmed against the live store 2026-09-07: audit lands in GreptimeDB `opentelemetry_logs` (477,096 rows) beside every other signal, that table declares `ttl = '14days'`, a `DELETE` on the audit stream is accepted, and both queries reached `:4000/v1/sql` with no credentials from inside the cluster. Sharing one table means the catalog's authn/authz/credential-issuance records can carry neither their own retention nor their own access policy. (The correlation half of this row was refuted — `CorrelationFilter` stamps request_id/trace_id on the root handler.)
- *Closes when:* Split `lance.audit` out of the shared `opentelemetry_logs` pipeline in `chart/templates/otel-collector.yaml` into its own append-only sink with non-14-day retention, and put authentication in front of GreptimeDB's `:4000/v1/sql` so a DELETE is not accepted from any in-cluster pod.

**XC-004 · 43 secret refs still arrive through env (APP_API_TOKEN ×10, the zones' OIDC/session/lineage tokens, `ray-lance-head`) while ESO is built, provisioned and switched off**
`chart, viewer, lineage, catalog` · **HIGH** · **blocked:** owner decision — a `helm upgrade` release with `externalSecrets.enabled=true`; the estate carries seven hand-deployed images a values-mismatched upgrade would revert to chart defaults

- **RE-MEASURED 2026-09-17 — "zero ExternalSecret/SecretStore/ClusterSecretStore objects exist" IS
  FALSE, and this row is further along than it says.** Live: `SecretStore/rask-vault` has been `Valid`
  / `ReadWrite` for **8 days**, and TWO ExternalSecrets are syncing against it —
  `rask-infra-credentials` (`SecretSynced True`, refreshed 5m52s before the measurement) and
  `rask-observability-s3` (21m). So ESO is not "built and switched off"; it is running and is the
  live writer of the estate's main credential Secret. Re-count the 43 refs before planning against
  them — an unknown number are already on the ESO path.
  *And it has a consequence this row should carry, found while re-measuring [[XC-001]]:* because ESO
  rewrites a Secret OBJECT out of band, every consumer that binds a value at boot is exposed to a
  silent rotation, and a helm-rendered `checksum/` annotation cannot see it. That is the live cause of
  the lakehouse zone's 401 against lineage. Migrating a ref to ESO without also fixing how the
  consumer READS it converts a startup failure into a silent stale-credential failure.
- *Why open:* Re-measured 2026-09-08 (SUPERSEDED IN PART — see above): the 43 refs sort into ~34 ESO / ~5 STS / ~4 Dapr-store, the ESO auth half (bao kubernetes backend + bound `lance-infra` role) landed and the operator runs with 3 pods — but `externalSecrets.enabled` is still false, so the migration was never driven from the chart. Separately `MEDIA_S3_ACCESS_KEY_ID` on `rask-viewer` is the RustFS ROOT pair (`rask-app`/`AWS_ACCESS_KEY_ID`) wearing a scoped name, findable only by following the secretKeyRef.
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

**XC-010 · ~~OpenFGA subjects are raw-interpolated (`f"user:{user}"` in `service_kit/governed/fga.py`) — user IDs are never URL-encoded, and OIDC subjects here are emails~~ — STRUCK 2026-09-16 (PREMISE FALSIFIED, AND THE FIX WOULD BREAK AUTHORIZATION)**
`service-kit, catalog` · med

- *Why open:* The Lakekeeper study ruled this mandatory before prod OIDC if subjects can contain `@`/`+`/`:`, and email subjects always do; the interpolation is still present at `fga.py:487`, `:532`, `:617` and `:1312`. No verdict on it appears in `docs/DECISIONS.md` §9, so it is neither fixed nor knowingly accepted.
- *What is true now, measured 2026-09-16 against the running estate:*
  * **THE SUBJECT IS NOT IN A URL.** All four sites (now `fga.py:826`, `:871`, `:956`, `:1730`) hand the
    string to SDK models — `ClientCheckRequest`, `ClientBatchCheckItem`, `ClientTuple` — which serialize
    to a JSON BODY. There is no path segment to escape, so percent-encoding does not harden anything.
  * **AND IT WOULD BREAK WHAT WORKS.** Encoding changes the identifier's bytes:
    `user:alice@example.com` becomes `user:alice%40example.com`, a DIFFERENT subject that matches none
    of the stored tuples. The fix as written silently denies every principal whose id contains a
    special character — the opposite of its intent.
  * *The premise is false for this estate.* Dex issues OPAQUE base64 subjects, not emails — measured on
    a freshly minted token and against the live store (`01KYPGG8F8MAZTJANME4K077DE`): of 26 distinct
    `user:` subjects, the OIDC ones are `CiQ3ZjJhOWM0MS01YjhlLTRkMTYt…` shaped, and exactly ONE carries
    a special character at all — `user:alice@example.com`, a seeded fixture, which authorizes correctly
    today (it is the identity that drove the live `compact_table` probe for [[LH-153]]).
- *What would reopen it:* an identity provider whose `sub` can contain OpenFGA's type separator in a
  position that makes `user:<sub>` parse ambiguously, or evidence that any of the four sites reaches a
  URL path rather than a JSON body. Either would restore the row's mechanism; neither is true at HEAD.

**XC-011 · Estate bootstrap is still check-then-write, and `fga.provision()` rewrites the authorization model on every unpinned boot**
`chart, service-kit, catalog` · med · **blocked:** owner decision (C-Q2) — whether `provision()` gates on a model-content hash or `RASK_FGA_MODEL_ID` is the accepted pin

- *Why open:* Verified 2026-09-10: `chart/templates/bootstrap-admin.yaml` contains no `bootstrap.json` / `records.create_json`, and `packages/service-kit/src/service_kit/governed/fga.py::provision` (line 319) still documents 'The model is (re)written each time'. The latch shape — observe unconditionally, act only when enabled, treat 409 as success — is unimplemented, and the pin it should gate on has never been chosen.
- *Closes when:* In `chart/templates/bootstrap-admin.yaml`, write `_control/bootstrap.json {subject, store_id, model_id, at}` with `records.create_json` (create-iff-absent) after the seed and read it before the seed, treating 409-on-exists as success; then gate `fga.py::provision` (~lines 319-349) on that latch using whichever pin the owner picks.

**XC-012 · ~~The dev cluster runs `auth.enabled=false`, so all seven rendered user doors sit in their dev-open state~~ — CLOSED 2026-09-15: the deployed estate runs auth ON**
`chart, catalog, lineage, medallion, ingest` · med · **blocked:** owner decision — whether to turn auth on in the dev cluster


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured against the live release 2026-09-15: every Deployment carries `RASK_OIDC_ENABLED=true` and
  `RASK_FGA_ENABLED=true`, none renders the auth-off acknowledgement `RASK_INSECURE_ALLOW_UNAUTHENTICATED`,
  `helm get values` sets no `auth.enabled=false`, and a no-bearer `GET /api/lineage/graph` answers **401**.
  *Not done, and deliberately not folded in here:* a per-door 401/403 sweep of all seven services.

- *Why open:* The chart gap is closed — 7 of 7 deployments render a user door keyed on the per-service `governedAuth` flag — but the deployed release never turns auth on, which is why the in-cluster proofs had to arm lineage's door by hand from the chart's own values.
- *Closes when:* Set `auth.enabled=true` on the k3s release and re-verify each of the seven doors answers 401/403 without a bearer.

**XC-013 · pg-dump backups land in the same RustFS bucket the VolumeSnapshot protects, nothing prunes `_backups/` or old snapshots, and `snapshotClassName` is empty**
`chart, lineage` · med · **blocked:** owner decision on the off-cluster backup destination

- *Why open:* `chart/templates/backup-pg.yaml` writes the lineage and openfga dumps to `s3://lance-catalog/_backups/pg/` — the same store a PVC loss would take out — nothing prunes either artefact kind, and `snapshotClassName: ""` fails on clusters with no default class. The restore half of this row is closed (`docs/runbooks/RUNBOOK-restore.md` exists).
- *Closes when:* Point the pg-dump CronJob at an off-cluster bucket in `chart/values-prod.yaml`, add retention pruning for `_backups/pg/` and for old VolumeSnapshots in `chart/templates/backup-snapshot.yaml`, and set a real `snapshotClassName`.

**XC-014 · Bootstrap on a fresh machine is not chart-complete: the Ray head is hand-applied `deploy/ray-lance-demo.yaml` and OpenBao's k8s auth backend/policy/role + KV values are seeded by runbook**
`chart, compute, medallion` · med

- *Why open:* The hand-applied Ray head has diverged from the chart's own RayService, and re-applying an older copy silently reverted the scoped S3 credential to the root key once. Until the head is reconciled and the OpenBao bootstrap is a Job, 'it is all in the chart' is false — and the gap sits exactly where the security posture lives.
- **THE OPENBAO HALF IS DONE, verified 2026-09-16 — this row is now only about the Ray head.**
  `chart/templates/openbao.yaml:305-315` is a chart-owned Job that enables the kubernetes auth mount,
  writes `auth/<path>/config`, writes the policy and writes the role, with every step idempotent and the
  one non-idempotent case (`auth enable` on an existing mount) handled explicitly. The KV seeding is in
  the same Job at :171 (`bao kv put secret/lance …`). "Seeded by runbook" is no longer true of OpenBao.
- *The Ray half IS still open, measured the same day:* `deploy/ray-lance-demo.yaml` still exists (9,845
  bytes), the live `ray-lance-head` Deployment carries **no `meta.helm.sh/release-name` annotation**, and
  the chart's own `chart/templates/rayservice.yaml` renders nothing into the deployed release — the same
  hand-applied shape [[LH-169]] and [[XC-052]] describe elsewhere.
- *And that half is PHASE 2, not phase 1.* It is the Ray lane's ownership question, which
  [[CP-012]]/[[CP-020]]/[[CP-021]] already hold as one owner decision (chart-owned RayCluster vs
  ephemeral per-job clusters vs delete the adapter). This row should not be worked ahead of that ruling
  — reconciling the head against `rayservice.yaml` presumes the first answer.
- *Closes when:* the Ray-head ownership ruling lands and the head is reconciled with whatever it chooses,
  and `deploy/ray-lance-demo.yaml` goes. The OpenBao clause is DONE and must not be re-attempted.

**XC-015 · ~~Residual duplicated storage seams: ingest hand-maps its own 409, a dead line in `storage/client.py`, and three boto3 constructors outside `s3_client`~~ — CLOSED 2026-09-15: the three cited seams are clean**
`storage, ingest, catalog, service-kit` · med


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15: ingest's 409s are its own API semantics or its handling of the catalog's 409, and its only Lance
  commit is an `Append` (`lander.py:175`), which by Lance semantics never conflicts — so there is no commit failure to route
  through `classify_commit_failure`. `storage/client.py:79-144` carries no no-op statement. The only `boto3.client(` outside
  `s3_client` is the sanctioned STS family in `packages/storage/src/storage/sts.py:58`; no boto3 constructor exists in
  `services/` or `runners/`.

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

**XC-021 · ~~The OpenFGA subchart is pinned at 0.3.9 while the running image is v1.18.3, which is 0.3.12's appVersion~~ — CLOSED 2026-09-16**
`openfga, chart` · low

- *Why open:* Every value key the chart sets was verified present in 0.3.12, but `Chart.lock` carries a digest over the dependency set, so hand-editing the version desyncs it and the `helm dependency build` run by `make k3s-install` and both `scripts/*_e2e_stack.sh` fails outright.
- **CLOSED.** `Chart.yaml` moves to 0.3.12 and `Chart.lock` is regenerated, so the chart now DECLARES
  the binary it runs. Verified against the upstream index rather than the row: 0.3.12's appVersion is
  exactly `v1.18.3`, the image on the live Deployment. 0.3.13 and 0.3.14 exist (v1.19.0, v1.20.0) and
  were deliberately not taken — this aligns the declaration with what is deployed, it does not bump the
  binary.
- *The lock diff is the evidence the pins held:* three lines — the version, the digest and the
  timestamp. All nine dependencies are EXACT pins with no ranges, so a regenerate could not quietly
  move anything else, which is the risk that made this a "MUST be done with helm on PATH" follow-up
  rather than a hand edit.
- *And the operation the row warns about was driven:* `helm dependency build` — what `make k3s-install`
  and both `scripts/*_e2e_stack.sh` run, and what a desynced lock breaks outright — completes cleanly,
  as do `helm lint` and `scripts/prod_render_check.sh`.

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

**XC-026 · ~~Every actor method accepts a caller-supplied `actor` field; the only guard is pod-network topology, not the app~~ — CLOSED 2026-09-15: the actor routes are behind the Dapr app token**
`annotator, notifications, service-kit` · med · **blocked:** owner decision — dapr-api-token vs netpol tightening vs a sidecar-only guard


- **RE-MEASURED AND CLOSED 2026-09-15** (phase-1 re-measurement of all 137 rows).
  Re-measured 2026-09-15: `guard_actor_routes` (`dapr_auth.py:334-383`) applies `require_dapr_token` to every path under
  `/actors` and `/dapr/config`, and BOTH actor hosts call it — `annotator/main.py:192`, `notifications/lifespan.py:55`.
  Live, both pods carry `dapr.io/app-token-secret`.
  *What the guard proves is 'arrived via the sidecar', not 'trusted caller'*, and the check is a documented no-op when
  `APP_API_TOKEN` is unset in dev — stated so the posture is not read as stronger than it is.

- *Why open:* `service_kit.governed.dapr_auth.require_dapr_token` now exists and gates e.g. `annotator/api/v1/endpoints/jobs.py:47`, but the `/actors/*` invocation routes themselves are not behind it, so anything that can reach the sidecar port can name an arbitrary actor id — true for every actor method in the plane.
- *Closes when:* Pick one of the three named postures — a `dapr-api-token` on the actor routes, a NetworkPolicy limiting who may reach the sidecar, or a sidecar-only guard like the gateway's lineage rows — and apply it estate-wide across the annotator and notifications actor hosts rather than per service.

**XC-027 · `chart/values-prod.yaml` sets `ingress.enabled/className/host` but no `tls:` block, so OIDC tokens and vended S3 credentials traverse plaintext at the edge**
`chart, gateway` · med · **blocked:** owner decision on the prod hostname / certificate issuer

- *Why open:* Verified 2026-09-10: `chart/templates/ingress.yaml` renders `.Values.ingress.tls` when present, and `chart/values-prod.yaml:201-204` supplies only `enabled`, `className` and an empty `host` — no tls entry, no cert-manager annotation. In-cluster Dapr mTLS covers service invocation only.
- *Closes when:* Add an `ingress.tls` block plus the cert-manager issuer annotation to `chart/values-prod.yaml`, and re-run `bash scripts/prod_render_check.sh` to pin it.

**XC-028 · ~~`security.serviceAccounts`, `security.infraContexts` and `dapr.sidecarRestricted` are all false by default and `values-prod` flips none of them~~ — CLOSED 2026-09-16**
`chart` · med

- *Why open:* Verified 2026-09-10: `chart/values.yaml:696` `serviceAccounts.enabled: false`, `:703` `infraContexts.enabled: false`, `:2449` `sidecarRestricted: false`, and none of the three appears in `chart/values-prod.yaml` — so every app pod runs as SA `default` with token automount on. The assessment records all three as live-proven on 2026-07-13, so the omission reads as an oversight rather than a decision.
- **CLOSED.** All four are set in `chart/values-prod.yaml`, and the effect is measured on the render
  rather than asserted from the values: ServiceAccounts go **18 -> 37** and
  `automountServiceAccountToken: false` **1 -> 20**, i.e. every app pod stops running as SA `default`
  with its token mounted.
- *`scripts/prod_render_check.sh` asserts the RENDER, not the file* — a `security:` block can be present
  and reach no pod if a template stops consuming it, which is the failure a values grep cannot see. It
  COUNTS (>=30 SAs, >=15 automount refusals) because one dedicated ServiceAccount proves the switch is
  readable, not that the fleet uses it; mutation-proven against the switches-off baseline of 18 and 1,
  both below the thresholds.
- *NOT done, and stated rather than quietly dropped:* the row also asks to "verify `dapr mtls -k` still
  passes". There is no prod cluster to run it against, and the dev release does not use this overlay.
  That verification belongs to the first prod install.

**XC-029 · ~~The ingress carries no `nginx.ingress.kubernetes.io/proxy-read-timeout`, so the controller's 60 s cut severs every idle `query.live` SSE feed~~ — CLOSED 2026-09-16 (already landed and gated)**
`chart, gateway` · med

- *Why open:* Confirmed live: the running controller has `proxy_read_timeout 60s`, no override annotation exists, and SvelteKit's SSE transport emits no keepalive (kit 2.70.1 — `runtime/server/remote.js:90` is the only `enqueue`, no timer in `runtime/server`). Each reconnect re-primes the whole 200-event window and writes an audit record, so replicating `query.live` 15× without this makes the estate slower while looking faster.
- **CLOSED — the annotation is there and has a gate.** `chart/values.yaml:2326-2327` sets
  `nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"` AND `proxy-send-timeout: "3600"`, and both
  reach the rendered Ingress (verified by `helm template … -s templates/ingress.yaml` 2026-09-16). It is
  pinned by `tests/unit/test_invariants.py::test_ingress_holds_a_live_stream_open_longer_than_nginx_default`,
  which passes — so a values edit that dropped it would fail rather than silently re-sever the feeds.
- *The send side moves with the read side deliberately*, and the values comment says why: leaving
  `proxy-send-timeout` at 60s reaps the same connection from the other end, so the pair is one setting
  in practice.
- *A trap worth recording for anyone re-checking this:* the Ingress template keys off `ingress.host`,
  not an `ingress.enabled` flag. Rendering with `--set ingress.enabled=true` and no host produces NO
  Ingress at all and greps clean — which reads exactly like the annotation being absent.

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
- **THE FIRST-PARTY HALF LANDED 2026-09-16, and the row's own count is why the scope changed.** "3 of
  56 templates" is true and reads as 53 gaps; measured, only SIX pod-spec templates render an image
  through the chart's own helpers without the block — `bootstrap-admin`, `explorer`,
  `maintenance-worker`, `maintenance`, `medallion`, `services` (8 pod specs between them). The other 17
  pod specs run PINNED third-party images (busybox, minio, dex, openbao, otel-collector, greptimedb,
  postgres) from public registries; giving those a pull secret is the airgap/mirror question, which has
  a different answer — mirror every upstream image — and folding it in here made the row unclosable.
- *Measured both directions:* with `imagePullSecrets` set, 21 pods now carry it; with it unset the
  `with` block renders nothing, so the default posture is byte-unchanged. `$.Values` and not `.Values`,
  deliberately — several of these pod specs sit inside a `range`, where `.` is the loop item.
- *Gated by `tests/unit/test_a_first_party_pod_can_pull_from_a_private_registry.py`*, which derives the
  family from the templates so a seventh first-party pod inherits the rule, and reads the SOURCE
  because a render under one set of values cannot tell a skipped conditional pod spec from a compliant
  one. RED for exactly the six before the fix.
- *Closes when:* the second half — registry-qualified `image.*.repository` values in
  `chart/values-prod.yaml` — lands, and the airgap question above is answered separately if it is ever
  asked.

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

**XC-038 · ~~The infra subcharts (GreptimeDB, NATS, Perses, the Dapr control plane) still get no requests/limits from values-prod~~ — CLOSED 2026-09-16 (premise falsified; the REAL unbounded set found and fixed)**
`chart` · low

- *Why open:* `chart/values-prod.yaml` carries only two `resources:` blocks (one of them OpenFGA's, which half-closed the assessment's gap #4); the remaining infra subcharts pass none through, and they are the shared bus and telemetry the cascade rides. Not re-measured against every subchart's own defaults.
- **THE HEADLINE IS FALSE, and the row's own last sentence admits why it was never checked.** All four
  named subcharts carry requests AND limits, supplied by `chart/values.yaml` —
  greptimedb-standalone:3013, nats:2416 (three containers), perses:3028, dapr:2478 (five components).
  Helm merges the overlay ON TOP of the base, so reading `values-prod.yaml` alone answers a different
  question than "is this container bounded". Measured on the rendered prod overlay 2026-09-16:
  **41 of 42 workload containers bounded**, and the one exception is not a subchart.
- **What the same render DID show: 9 of 54 containers unbounded, every one of them ours.** Union across
  both overlays — `services.yaml`'s and `openfga-migrate.yaml`'s two `wait-age` busybox inits, the
  `minio-buckets` / `minio-scoped-users` / `nats-stream-job` / `openbao-seed` bootstrap Jobs,
  `openfga-migrate`'s own `migrate` container, and the `backup-pg` (pg-dump init + upload) and
  `backup-snapshot` CronJobs.
- **That is worse than the tier-sizing the row asked for, and in the opposite direction.** A namespace
  `ResourceQuota` — the ordinary prod control, and the reason anyone audits this at all — REJECTS a pod
  whose containers declare no requests. The fleet would come up and the release would *never converge*,
  because the Jobs that create the buckets, the JetStream streams and the OpenFGA schema would never be
  admitted. The chart renders no `ResourceQuota`/`LimitRange` of its own, so nothing local would have
  revealed this.
- **FIXED**: all ten containers now carry an inline block sized to their own work, matching the idiom
  the four Job templates that already complied use (`bootstrap-admin`, `dapr-inject-sweep`,
  `greptimedb-ttl-job`, `kueue-queues`). Gated by
  `tests/unit/test_every_pod_the_chart_renders_asks_the_scheduler_for_something.py`, which renders BOTH
  overlays — neither alone sees every Job (the backup CronJobs render only under prod, the OpenBao seed
  only under the defaults) and that asymmetry is asserted rather than left to a comment.
- *Not yet in the k3s release, and that is not this row's blocker.* These are Job/CronJob pod specs —
  they take effect at the next install or `helm upgrade`, and the running release is pinned to the
  older image stem until [[LH-169]] is resolved. The row's own Closes-when was "re-render and confirm
  no container is unbounded", which the gate does on every run.

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

**XC-050 · ~~`chart/values-prod.yaml` never sets `observability.environment`, so every OTel resource attribute labels prod telemetry with the chart default~~ — CLOSED 2026-09-16**
`chart` · low

- *Why open:* Verified: `chart/values.yaml:2779` defaults `environment: rask` with the comment 'override per deploy (dev / staging / prod)', and `grep environment chart/values-prod.yaml` returns nothing — so every trace and metric in prod, the cascade's included, carries the wrong `deployment.environment.name`.
- **CLOSED.** Measured on the prod RENDER before and after, because the attribute is what ships and the
  value is only how it gets there: `deployment.environment.name=rask` -> `=prod`. The dev render still
  says `rask`, so nothing else moved.
- *Gated in `scripts/prod_render_check.sh`*, which greps the rendered attribute rather than the values
  file — and its failure message prints what DID render, since "prod telemetry is mislabelled" is
  useless without the label. Mutation-proven by the before-measurement: `=rask` fails the grep.
- *Why it was worth more than a one-line diff:* prod and dev telemetry share one GreptimeDB, so an
  alert firing on "the estate" could not say which estate — including for the cascade.

**XC-052 · A helm-LABELLED Deployment that the release does not own will fail the next upgrade that touches it**
`chart` · med · found 2026-09-16 by the backlog audit, verified live

- *Why open:* `Deployment/rask-assist` and `Service/rask-assist` carry the release's own labels
  (`app.kubernetes.io/instance: rask`, `managed-by: Helm`) with **zero `ownerReferences`** and **no**
  `meta.helm.sh/release-name` annotation, and they are absent from the deployed release's manifest —
  measured live 2026-09-16. They were `kubectl apply`d on 2026-08-03.
- *What that costs:* they look release-owned to every reader and every label selector — `helm` will not
  reconcile them, and a future chart that legitimately renders a `rask-assist` meets an existing object
  it does not own, which is a failed upgrade rather than an adoption. Meanwhile any component-scoped
  NetworkPolicy or PDB selecting on `instance: rask` silently includes them.
- *Closes when:* either the chart renders them (and helm adopts them with the annotations that make
  ownership real), or they are removed as hand-applied residue — with the choice recorded. Related to
  [[LH-169]]: both are the same class, a hand-applied object the release believes it owns.

**XC-053 · ~~Helm hook Jobs accumulate one per revision forever — 17 today, each holding a completed pod~~ — CLOSED 2026-09-16 (the CLASS is gated; the 17 existing husks await a sweep)**
`chart` · low · found 2026-09-16 by the backlog audit, counted live

- *Why open:* 17 `rask-minio-scoped-users-r<NNN>` Jobs are present in `default`, one per release
  revision (r146..r163 at the time of counting), each retaining its completed pod. They carry no
  `helm.sh/hook-delete-policy`, so nothing collects them, and the release revision count only grows.
- *Why it is low and still real:* it costs a little etcd and a lot of `kubectl get pods` legibility —
  the same noise that made the four orphaned durables in [[LH-127]] hard to see. Its sibling
  `openfga-migrate` already learned this: `chart/templates/openfga-migrate.yaml:20` carries
  `ttlSecondsAfterFinished: 3600` with a comment recording that 14 Completed husks had accumulated
  before it did.
- **CLOSED AS A CLASS, not as an instance.** `minio-scoped-users.yaml` gains the
  `ttlSecondsAfterFinished: 3600` its five siblings already carry — measured: SIX chart Jobs are named
  with `bootstrapRev` and it was the only one without. `hook-delete-policy` is not a substitute and
  that is the whole trap: it matches by NAME, and the name is what changes every revision.
- *Gated by `tests/unit/test_a_revision_named_hook_job_is_reaped.py`*, which derives the family from the
  templates rather than listing it, so a seventh revision-named Job inherits the rule. Read off the
  SOURCE, not a render: a render under one set of values cannot distinguish a conditional Job that was
  skipped from one that is compliant. Proven discriminating — it was RED for exactly the one offender
  and GREEN for the five compliant before the fix.
- *Left undone, deliberately:* the 17 husks already on the cluster (r146..r163). A TTL applies to Jobs
  created after it, so removing the existing ones is a `kubectl delete` against a shared cluster — the
  owner's call, not a template's.

**XC-051 · Undecided whether the estate shares one GreptimeDB or runs one per workload**
`chart` · low · **blocked:** owner decision

- *Why open:* Listed as unresolved open decision #4 in the merge checklist. De facto the estate has one (`rask-greptimedb-standalone`), so the decision is being made by default rather than taken — which matters once a runner's telemetry volume competes with the cascade's RED metrics and lineage-adjacent traces.
- *Closes when:* Record the ruling in `docs/DECISIONS.md` (one shared GreptimeDB, or per-workload) and make `chart/values.yaml`'s observability stanza state it.


---

**LH-168 · The live `lance-secrets` Dapr component has DRIFTED from the chart — two app-ids the chart grants are missing, and helm will never put them back**
`chart, viewer, search` · low · found 2026-09-16 by a zero-trust audit, verified against the cluster

- *Why open:* `helm get manifest rask` renders **13** scopes for the `lance-secrets` secret store; the
  live Component has **11**. `search` and `viewer` are in the chart and not in the cluster.
- *Why helm will not repair it:* helm patches only fields that CHANGED between releases, so out-of-band
  drift on a stable field survives every upgrade — the estate already records this as a standing trap.
  The release has gone through revisions 160-163 today with the drift intact.
- *Why it is LOW and not med:* the two affected app-ids are `viewer` and `search`, both explicitly
  low-priority. **All four lakehouse services are correctly scoped** (catalog, lineage, maintenance,
  medallion-producer) along with the three stage runners, annotator, ingest, flows and notifications, so
  nothing in phase 1 is affected. Measured: no `unavailable from Dapr store` line in either pod in the
  last 30 minutes, so whatever they would fetch they are not currently fetching.
- *The shape is what makes it worth a row rather than a note:* an unscoped Dapr component does not error
  at boot — the app comes up, reports Ready, and the capability is simply absent. That is this estate's
  signature defect (a control that cannot fire) arriving through infrastructure drift rather than code.
- *Also found in the same audit and NOT filed here because they are already tracked:* `APP_API_TOKEN`
  reaching 10 sidecar'd services as a k8s Secret through env is [[LH-160]]; the observability stack's
  root-scoped S3 pair is [[LH-161]].
- *Closes when:* the live Component matches the chart, and something detects the divergence rather than
  a person noticing — a render-vs-live diff on Component scopes, which the estate already does for
  images via `k3s-pins`.

**LH-156 · ~~Six places still describe the deleted `RayJobExecutor` as live~~ — CLOSED 2026-09-16**
`medallion, docs` · was low · filed 2026-09-15 with the deletion that caused it

- *Why open:* [[LH-083]] deleted the RayJob-CR adapter. The prose naming it did not go with it:
  `engine_names.py:24`, `engine_registry.py:13-16`, `dapr_saga.py:4`, `ray_jobs_api.py:11-12`,
  `ray_submit.py:~183` ("the port's Ray adapter renders `to_env()` into the CR's runtime_env") and
  `docs/DECISIONS.md:1451-1486`, which still reads "a port, TWO adapters".
- **RE-MEASURED 2026-09-16: THE COUNT WAS RIGHT AND THREE OF THE SIX NAMED SITES WERE ALREADY CLEAN.**
  `dapr_saga.py`, `ray_jobs_api.py` and `ray_submit.py` carry no reference at all. Three OTHERS the row
  never named did: `engine_registry.py`'s construction-arguments paragraph (wrong class AND wrong facts
  — it said the adapter needs "the deployment facts KubeRay's webhook requires", while
  `RayJobsApiExecutor.__init__` takes one optional `httpx.AsyncClient` and nothing else),
  `tests/unit/test_the_submitter_and_the_job_agree_on_the_wire.py:10`, and
  `services/medallion/tests/test_the_chosen_engine_is_the_engine_that_runs.py:7` — the latter also
  asserting "There is no registry", which `engine_registry` has since falsified.
- *The class is confirmed gone:* no `class RayJobExecutor` and no `rayjob_executor.py` anywhere in the
  tree. The live adapter is `rayjobs_api_executor.RayJobsApiExecutor`, submitting to a STANDING cluster
  through the dashboard Jobs API.
- *`DECISIONS.md` was SUPERSEDED, not rewritten*, because a decision record is where history is supposed
  to live (CLAUDE.md says so) — the dated 2026-09-04 entry stands as what was decided then, and a new
  2026-09-15 entry records the deletion, its two reasons (zero production callers; and the wire gate's
  0-of-6 env overlap, which means the adapter could not have worked against the jobs it would launch)
  and what replaced it.
- *The three references that remain are all dated past-tense provenance recording the deletion* —
  `engine_registry.py:86`, `test_no_service_depends_on_a_compute_engine.py:8` and
  `test_a_declared_ray_task_is_refused_where_no_ray_runtime_runs.py:132`. None describes it as live.
- *Why it is worth a row rather than a sweep-when-convenient:* the estate's rule is that falsified prose
  is REWRITTEN, and a decision record describing an architecture the code no longer has is exactly what
  sends the next reader to re-derive a deleted class. It is low only because nothing branches on it.
- *Closes when:* all six say what the code does, and `DECISIONS.md` records the deletion with its reason
  rather than describing the old shape.

**LH-160 · 39 secrets are still delivered through the environment, and nothing stopped the count growing until now**
`chart, service-kit` · **HIGH** · measured 2026-09-15 against the render AND the live cluster

- *The rule, verbatim (owner):* *"Never secret through envs. Either from ESO, secret store dapr and STS
  for zero trust."* A `secretKeyRef` is a Kubernetes Secret injected as an environment variable — the
  banned path. **ESO does not fix it:** it changes where the value comes FROM (OpenBao, live and healthy)
  and not how it is DELIVERED.
- *Measured on the live cluster, 41 entries; on the render, 39. Split by the path each SHOULD take:*

  | delivery today | count | sanctioned path for those pods |
  |---|---|---|
  | `APP_API_TOKEN` (10) + `MEDIA_S3_ACCESS_KEY_ID` (2) — pods WITH a Dapr sidecar | 12 | the Dapr secret store, already reachable in the pod |
  | `LINEAGE_SERVICE_TOKEN` (8), `OIDC_CLIENT_SECRET` (7), `SESSION_SECRET` (7) — no sidecar | 22 | ESO-managed secret as a MOUNTED FILE |
  | `OPENFGA_DATASTORE_URI`, `DAPRSTATE_PASSWORD`, `S3_SECRET`, 4x `RASK_LINEAGE_TOKEN_SERVICE_*` | 7 | file mount; the S3 one is STS's job |

  **No STS-vended credential exists on any Deployment in the estate.**
- **THE GATE LANDED FIRST, and the reason is why 39 accumulated.** Several tests already pinned SPECIFIC
  `secretKeyRef` entries as CORRECT — the Ray pod's, the app token's — each guarding its own plane.
  Nothing counted them estate-wide, so every local test stayed green while the total grew. That is the
  same split this estate already paid for once, when two planes each pinned half of the Jobs-API secret
  defect and it was fixed twice. `test_secret_env_delivery_only_shrinks.py` now ratchets the count: it
  fails on the FORTIETH, and separately fails on a baseline left stale-HIGH, because a number not
  lowered after a removal is unused budget the rule just won and gave back.
- *Why a ratchet and not a ban:* a test demanding zero would be red the moment it landed and skipped
  within a week. The 12 sidecar-bearing entries are counted separately because they are the cheapest —
  the mechanism is already in the pod, so migrating them needs no chart plumbing at all.
- **THE MECHANISM FOR THE SIDECAR TWELVE ALREADY EXISTS, AND THE BLOCKER IS ONE ACCESSOR — designed
  2026-09-15 so the next pass starts from a measurement rather than a survey.**
  `service_kit.governed.secrets.apply_dapr_secrets` is exactly the right shape and is already live for
  four services: when `secrets_from_dapr` is on **the chart omits the value from env entirely** and the
  service fetches the bundle from the local sidecar (`GET /v1.0/secrets/<store>/<key>`) at boot, as the
  STRICT sole source, failing closed. It already RETURNS the whole bundle so a second field costs no
  second fetch — that is how lineage's AGE password rides the same call.
  *So `APP_API_TOKEN` is a bundle field plus a chart edit… except for one thing.*
- **`DaprDoorSettings()` IS CONSTRUCTED PER CALL, which is what stops the splice reaching the door.**
  `dapr_auth.py:134` and `:310` do `DaprDoorSettings().app_api_token` on EVERY request, reading the
  environment each time; `apply_dapr_secrets` splices onto the object `get_settings()` cached, which
  this never consults. Remove the env var today and the door reads `None` — and `:137`'s guard is
  `if expected and not compare_digest(...)`, so a `None` expected value makes the comparison **skip
  entirely**: every inbound Dapr call would be accepted unauthenticated. `assert_app_token_configured`
  would catch it at boot (`:146`) only for services that call it.
  *That is why this is not a chart edit.* The token must move to a boot-resolved accessor before the
  env var is removed, and the two halves cannot ship in either order safely: removing env first opens
  the door, changing the accessor first is inert but harmless. **Accessor first, then chart, then
  observe** — and the observation must be a real Dapr delivery, not a boot log.
- **THE ACCESSOR LANDED 2026-09-15, with [[LH-162]].** It is not the `apply_dapr_secrets` splice this
  row designed: that writes onto the object `get_settings()` cached, which this door never consults.
  `expected_app_token()` resolves at the door instead, cached per process through the bundle cache the
  module already had — so the per-request cost stays a dict lookup and no service needs a lifespan
  edit. `RASK_APP_TOKEN_FROM_STORE` is the per-deployment switch; on, the store is the STRICT sole
  source and `APP_API_TOKEN` is not read at all, because a fallback would let a store outage promote a
  stale env value back to authoritative.
- *So the sidecar twelve are now a CHART FLIP, not a code change:* seed `dapr-app-token` (already
  seeded into `secret/lance`), add `RASK_APP_TOKEN_FROM_STORE`, drop the `secretKeyRef` row — ten
  `APP_API_TOKEN` entries off the baseline. `annotator` is the first and is the proof: it is on the
  store path with no env row at all. The two `MEDIA_S3_ACCESS_KEY_ID` entries are a separate seam.
- *NOT a bootstrap paradox, which is the obvious objection and is wrong:* the fetch is app->sidecar
  (outbound, guarded by `dapr-api-token`), while `APP_API_TOKEN` guards sidecar->app (inbound). The
  boot fetch completes in the lifespan before the first request is served, so there is no circularity —
  only the accessor problem above.
- *Closes when:* the baseline reaches 0 (excluding [[LH-161]]), with the sidecar-bearing twelve first.

**LH-161 · The GreptimeDB subchart pulls a whole Secret into its environment via `envFrom`**
`chart` · med · found 2026-09-15 by the [[LH-160]] gate

- *The defect:* `release-name-greptimedb-standalone` declares `envFrom: {secretRef: rask-observability-s3}`
  — every key in that Secret enters the process environment, which the rule names explicitly and which
  is strictly worse than a keyed ref: a key added to that Secret for an unrelated consumer silently
  lands here too.
- *Why it is not simply fixed:* the `envFrom` is in a THIRD-PARTY subchart's own template, not in
  `chart/templates/`. The fix is an upstream change or a values-level override, neither of which is an
  edit the estate can make where the defect is.
- *It is the ONLY one, and recorded by NAME rather than exempted by a wildcard*, so `envFrom` stays a
  ban for every template the estate authors — a new one anywhere reds the gate.
- *Closes when:* the subchart takes the credential by file or the estate overrides that template — or
  an owner records that a third-party subchart's own env handling is out of scope, which is a
  defensible answer but must be written rather than assumed.

**LH-162 · ~~`require_dapr_token` SKIPS the comparison when no token is configured, and one live service has none~~ — CLOSED 2026-09-15: an unconfigured door refuses, and the token it checks no longer travels through env**
`service-kit` · **HIGH** · found 2026-09-15 while designing [[LH-160]]; confirmed against the running pod

- *The mechanism:* `dapr_auth.py:137` is `if expected and not secrets.compare_digest(...)`. When
  `expected` is falsy the comparison is **skipped entirely** and the request is accepted. So an absent
  `APP_API_TOKEN` does not close the door — it opens it. The estate's posture everywhere else is
  fail-CLOSED; this one seam fails open.
- *The guard against that is per-service and two services do not call it.* `assert_app_token_configured`
  refuses to boot when Dapr is on and the token is unset, but it is called by `catalog`, `compute`,
  `lineage`, `maintenance`, `medallion` and `notifications` — while `annotator` and `ingest` also USE
  `require_dapr_token` and never assert. A protection each caller must remember is one some caller will
  not.
- **CONFIRMED LIVE ON `rask-annotator`, 2026-09-15**, not inferred: the Deployment renders no
  `APP_API_TOKEN` at all; in the running pod `DaprDoorSettings().app_api_token` is `None`; and
  `GET /dapr/config` — a route `guard_actor_routes` applies `require_dapr_token` to — answers **200 with
  no token**. The annotator is an ACTOR HOST, so its actor plane currently accepts any caller that can
  reach the pod.
- *Scope, stated so it is neither under- nor over-sold:* this is an IN-CLUSTER surface, not an internet
  one — the ingress publishes `/api` through the gateway, not the actor routes. `ingest` does carry the
  token, so it is guarded today by configuration rather than by code. The exposure is lateral movement
  from any pod, which is precisely what the Dapr app token exists to stop.
- **IT ALSO BLOCKS [[LH-160]], which is how it was found.** Migrating `APP_API_TOKEN` out of pod env
  means `expected` becomes absent at exactly this line for every service — turning a secrets-hygiene
  change into an estate-wide authentication bypass. The door must fail closed BEFORE the env var moves.
- **DECIDED (owner, 2026-09-15): the opt-in flag.** Failing closed outright would break any deployment
  running Dapr ingest without a token — the `rask-dapr` skill records the skip as a documented dev
  no-op — so the default is inverted rather than the behaviour removed: `RASK_ALLOW_UNAUTHENTICATED_DAPR`
  must be SET for an open door, which makes an open one chosen and greppable.
- **IT WAS NOT SHIPPABLE ALONE, and that is the whole reason [[LH-160]]'s accessor landed with it.**
  `rask-annotator` carries `dapr.io/app-token-secret`, so daprd DOES stamp a valid header on every
  callback it makes — including `GET /dapr/config`, which it calls to learn the app's actor types.
  Closing the door while the app still had nothing to compare against would have 403'd daprd's own
  callback and taken the annotator's actor plane down on the next roll. The token had to arrive first,
  and it could not arrive as a `secretKeyRef` env row like its eleven siblings without taking the
  banned path.
- *Landed:* the door refuses an unconfigured state; `expected_app_token()` is one resolver for all
  three consumers; `RASK_APP_TOKEN_FROM_STORE` switches it onto the Dapr secret store as the STRICT
  sole source; an unreadable store answers 503 rather than 403 so a sidecar keeps retrying; and a
  bundle answering without the field drops the cache, so the chart's seed Job and the pod may roll in
  either order. `test_every_dapr_door_has_a_token_to_check.py` joins the two halves that were each
  tested separately — it derives which packages call `require_dapr_token` from the source tree and
  which Deployments run them from the render, so a service that starts guarding a door is covered the
  day it does.
- **OBSERVED LIVE on `main-3836adbc`, 2026-09-15**, driven against the running pod rather than read off
  a boot log: no token -> `/dapr/config` **403**; a forged token -> **403**; the token resolved from the
  Dapr secret store -> **200**, and `POST /api/jobs/apply` reaches body validation (422) with it. The
  app container holds **no `APP_API_TOKEN`** — the secret does not travel through the environment at all.
- *And the thing that would have broken did not:* daprd's own callback succeeds, so `actorRuntime` is
  `RUNNING` with `AnnotationTaskActor`, `TenantProjectsActor` and `AnnotationProjectActor` all hosted,
  and the sidecar logged zero errors after the roll. Ten workloads rolled, zero failed rollouts.

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

**CP-009 · ~~CLOSED IN CODE: `runs.py`'s outcome-status promotion now accepts terminal FAILED and TERMINATED~~ — CLOSED IN CODE, re-verified 2026-09-11**
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

**CP-013 · ~~`RayJobExecutor.status()` maps only `status.jobStatus`, so a RayJob whose cluster never came up reports PENDING forever and can never be resubmitted~~ — CLOSED 2026-09-16 (SUBJECT DELETED)**
`medallion, ray-kit` · med

- *Why open:* Verified in `services/medallion/src/medallion/services/rayjob_executor.py`: line 152 reads `status.jobStatus` alone, and `jobDeploymentStatus` is consulted only at line 161 to fill a message once `jobStatus` already says FAILED. KubeRay records a cluster that never came up, an `activeDeadlineSeconds` expiry or a Kueue eviction in `jobDeploymentStatus` and leaves `jobStatus` empty, which `_JOB_STATUS` maps to PENDING — and `DURABLE_RECORD` then forbids resubmitting it. Real today as library code; reaches production only if the adapter is kept.
- *Closes when:* Map `jobDeploymentStatus` in `RayJobExecutor.status()` so a cluster-provision failure, deadline expiry or Kueue eviction reports FAILED, pinned by a test feeding a status with an empty `jobStatus`.
- **CLOSED because the file is gone, and — unlike its neighbour — the DEFECT went with it.**
  `services/medallion/src/medallion/services/rayjob_executor.py` was deleted by `b3a10799`
  ("delete the RayJob CR adapter nothing called"). `jobDeploymentStatus` is a RayJob **CR** field; the
  surviving submission path is the Ray Jobs API, whose status model has no such field, so there is
  nothing left to map. Contrast [[CP-014]], filed against the same deleted file, whose defect DID
  survive onto the Jobs-API path — a deletion closes a row only when the defect was a property of the
  thing deleted.

**CP-014 · `RayJobExecutor` treats any 409 as REATTACHED without reading the CR, and the CR name omits `code_version`**
`medallion, ray-kit` · med

- *Why open:* Verified in the same file: line 136-137 returns `SubmitOutcome.REATTACHED` on any `409` without fetching the object. A 409 may mean a DIFFERENT job holds that name, and omitting `code_version` from the derived name makes that reachable — a same-token re-run after a deploy reattaches to the previous build's job.
- *Closes when:* Read the CR on 409 and compare identity before declaring REATTACHED, and include `code_version` in the RayJob CR name derivation.
- **THE CITATION IS DEAD AND THE ROW IS NOT.** `rayjob_executor.py` was deleted (`b3a10799`), so the
  `line 136-137` evidence above resolves to nothing — but BOTH halves of the ask survive on
  `ray_submit`, the Ray Jobs API path that the same commit message calls "the submission path the
  cascade actually uses" and which is now the ONLY one. Re-cite against that module before working it.
  Adversarially verified 2026-09-16: three independent lenses each refuted the proposal to close this
  as done-by-deletion, two of them catching that the closing grep read a different module.

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
