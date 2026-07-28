# Open work — the backlog that must survive the merge

This file exists because the open items were only ever recorded as **session task IDs** (`#103`, `#124`, …)
in a task tracker that does not outlive the session, and in a re-pin diff that was applied and deleted by
design. After the merge nobody in rask knows what "#103" means.

So every entry below is **self-describing**: what it is, why it is open, where the code lives, and what
would close it. The old task numbers are kept only as a cross-reference for anyone reading the lance-ns
history. **`docs/architecture/lance-ns-merge.md` P0 copies this file into rask** — it is not left behind,
and P8 reconciles it rather than dropping it.

Status as of 2026-07-27. The twenty UX-goal conditions are met — the goal tracker is retired (git
history); **the durable artifact is [`GOAL-UX-REACTIVE-EVIDENCE.md`](GOAL-UX-REACTIVE-EVIDENCE.md)**.
Everything here is what remains *after* that.

---

## A. The merge forces this one

### A1 · The media corpus must leave its node hostPath *(was #103)*

**What.** `services/{viewer,search,annotator}` read the corpus from a node-local `hostPath`
(`/var/media-corpus`, `chart/templates/media.yaml:126`), staged from the lance-audio box. `MEDIA_DB_ROOT`,
`MEDIA_DB` and `MEDIA_DESCRIPTOR_DIR` all hang off `media.corpusMountPath`; 10+ files across the three
services read it.

**Why it is open.** It was correct for a single-node kind cluster and deliberately deferred — "NO data move:
the corpus stays node-local", per the template's own comment.

**Why the merge forces it.** A hostPath binds a pod to whichever node holds the data. The merge plan's P4
already rules **"no hostPath ships"**.

**What closes it.** Two halves, and they should be decided separately:
- *Portable:* register the corpus as **catalog-governed project tables** (the intended read-plane shape).
  This survives any destination and is the part worth doing first.
- *Destination-specific:* a PVC, or a rustfs-backed corpus bucket on rask's operator Tenant. Decide this in
  P4 against the cluster it will actually live on — deciding it in lance-ns means deciding it twice.

---

## B. Built halfway — the second half is named and small

### B1 · No actor type and no workflow are registered *(was #124, second half)*

**What is done.** `lance-statestore` is live: `state.postgresql` on the AGE Postgres, DSN resolved from
OpenBao through `lance-secrets`, `actorStateStore: "true"`, scoped to `catalog` + `annotator`. Per-subject
user state round-trips through it and is proven across browser contexts.

**What is not.** The flag that gates actors *and* workflow is on and **nothing uses it**. No actor type is
registered; no workflow is registered.

**What closes it.** An actor type hosted by a service in the component's `scopes`, proven by a round trip
through the sidecar. Keep `tests/unit/test_invariants.py`'s scope check — an app missing from `scopes` gets
"component not found" and every user's saved work 503s, logged by the sidecar and noticed by nothing else.

### B2 · The notification inbox has no actor *(was #128)*

**What.** Read/dismissed state for notifications is per-tab. The bell itself is done and estate-wide (all
four zones, shared `@repo/api/runs-feed`), because `GET /runs` already carries the lifecycle — but *read*
and *dismissed* are per-subject state the run feed cannot carry.

**Why it is open.** It needs B1.

**What closes it.** One actor per subject inbox, unread counts that cannot race, expiry via reminders rather
than a sweeper cron.

### B2b · ratch's runner imports become the Ray-native name seam *(new, 2026-07-27)*

**What.** `packages/ratch/cli/{speaker,transcribe}.py` still lazily import `from runners.diarize.diarize
import …` — repo-relative module paths from the lance-audio heritage, working only when the repo root is on
`sys.path`. The runners tree deliberately carries no `__init__.py` glue any more (`a4cf8f6`) and runners are
sealed non-members of the workspace, so these imports are dead code walking.

**What closes it.** When ratch is wired (the pipeline step): ratch passes runner NAMES and each runner's
`pyproject.toml` as the Ray worker `runtime_env`; the actor module imports on the WORKER. The contract is
stated in `runners/README.md`. Do not resolve this by making `runners.` importable again.

### B3 · Annotation projects are designed, not built *(was #122)*

**What.** `docs/DESIGN-annotation-projects.md` — entities, both state machines, the authz doors, what a
publish emits, and a slice plan.

**Where it stands** *(re-checked 2026-07-28)*. Slices `S1`–`S4` (domain core, FGA type, publish schema,
catalog `create` pin) need no store and are the next buildable unit — none of them exists yet
(`services/annotator/projects/` is absent). **`S5` is DONE**: the design doc said the state store did not
exist, and it does — `lance-statestore`, now with three proven consumers (`workflow-graph`, `saved-views`,
`dock-layout`). The doc has been corrected. So the fence is at **`S6`** — the actors — which is B1's second
half, and `S7`–`S10` follow it.

**The management view people ask for is `S9`** ("Projects landing replaces the `DataSelection.svelte`
gallery; send-to-project from search/atlas; the canvas reads and writes drafts"). It is four slices deep,
which is why entering the annotator still shows a gallery rather than a task list.

**One thing to decide before `S6`**, now recorded in the design doc's §10: `services/annotator` has **no
verified subject** — no `OIDCVerifier`, and `get_author` reads a trusted `X-User` header defaulting to
`"anon"`. Every entity in the design is keyed on who owns or claims it, so the actors must either be hosted
in the catalog (which has `CurrentToken` and is already in the store's `scopes`) or the annotator must grow
a verifier. Building `S6` against `X-User` would be the cross-user leak the user-state routes exist to
prevent.

---

## C. Carrying a stated reason

### C1 · `TableDetail`'s 60-assignment reset effect *(was #119)*

**What.** `TableDetail.svelte` resets ~60 assignments in an `$effect` where `{#key table}` would do it
structurally.

**Why it is still open, with evidence.** The fix re-instantiates a 1000-line component under 215 e2e tests.
This is not caution for its own sake: an edit to that component during this session **dropped 6 of its 10
history versions** (`missing: 9, 8, 7, 5, 4, 3`) with `svelte-check` reporting 0 errors and 0 warnings. It
is a component that punishes casual edits and needs its own pass with a browser drive, not a tidy-up.

### C2 · The product-works pass *(was #97)*

Ten conditions — annotator loop, runners, one-nav, FGA workbench, create-project, preview, lineage facets,
drawers, registry, gates. Orthogonal to the merge. Its premise is the one worth keeping: *drive the product
as a skeptical first user, not the elements.* (The "lineage facets" condition is the same gap as **E1**
below — one item, two names; close it once.)

### C3 · Lineage track remainder *(was #111)*

Spec-fidelity and Marquez-parity reports are done; Dapr-delivery and gold-finding tests landed in `b43b8ff`.
**What remains is the gold whole-history JSONB embed** — and note it is the *same artifact* as the merge
plan's **P7b gold schema contract**. Do it once, there.

### C4 · Prod-readiness residuals *(was #86)*

Residuals from the retired `GOAL-production-readiness` tracker. Re-derive against the merged chart rather
than the lance-ns one — several will have been answered by rask's operators.

**Where the enumeration lives:** `ASSESSMENT-2026-07-15.md` §3 is the only in-tree gap-by-gap roll-up
(kept for exactly this reason — historical banner, live enumeration). Verified still open on 2026-07-27:
gap 1 (Dex demo-IdP prod posture — `values-prod.yaml` does not touch dex), gap 5 (OpenBao auto-unseal via
a secrets operator — ESO / bank-vaults; `RUNBOOK-oncall.md:63` cites "ASSESSMENT gap #5", and
`OPERATORS.md` §5 row 5 says *verify whether rask already operates one* before adopting), gap 6
(registry-qualified image repos + `imagePullSecrets` — zero hits in `chart/`). Also unnamed anywhere else:
audit-log retention rides the observability store's TTL (`observability.retention`, 14d default) — a
compliance deploy must raise it manually (`API.md` records the caveat).

---

## D. Owner-deferred — not work, decisions already made

| Item | Ruling |
| --- | --- |
| **Settings surface** *(was #112)* — break out auth / authz / audit | Owner: *"keep it as is"* |
| **NATS HA / nack operator + GitOps; query engine** *(was #20)* | Owner-parked. The merge plan's PROPOSED decision 5 holds it parked too, noting rask's JetStream is on but streamless and lance-ns's stream-job is its first real consumer |
| **Models registry MLflow parity** *(was #101)* | Deprioritized until after the product pass |
| **Annotator residuals** *(was #100)* — export serializers (COCO / YOLO / CSV / HF) + managed label taxonomy | Owner to schedule. ⚠️ **The export half is the same service as the merge plan's P7c `exporter`** (ALTO 4.4 first, owner-ruled R4: serialization is a separate microservice, never inside the lakehouse or the movers). COCO/YOLO/CSV/HF become additional projections from gold — new functions in that service, not a second export path. Do not build these twice |
| **Storybook** | Struck for now — rask keeps its own (plan P2 step 3); adopt rask's rather than re-deciding |
| `/lakehouse/catalog` scaffold, `/lakehouse/admin` orphan | Product decisions, not defects with one right answer |

---

## D2. P7a follow-ups (compute-plane cutover, 2026-07-27)

### D2a · ~~The core-api husk retires with the R6/R20 media wave~~ **CLOSED 2026-07-28, with evidence**

**Closed by the R6/R20 wave (P7b):** `services/core`, `services/core_api`, `services/search_api`
and `services/volumes_api` are deleted; the gateway's core rows AND its `/api` catch-all are gone
(an unmatched `/api/*` now 404s `no upstream` — pinned by
`services/gateway/tests/test_routing.py::test_no_catch_all_since_the_r6_r20_wave`); the chart's
`core-api`/`search-api`/`volumes-api` fleet entries, configmap URL rows, dockerfiles and Makefile
image-list entries are deleted; `ray-api` took the clean `ray` name everywhere external (R20),
then became `compute` on EVERY surface — uv member and import included — at R22 (`import compute`
shadows nothing, so R20's PyPI-shadow exception died with the rename). The S3 object browser was
ported into the media viewer (`viewer/api/v1/endpoints/objects.py`, public `/api/media/object*`,
tests `tests/unit/test_objects_browser.py`) and the lakehouse storage browser re-pointed to it.
The EAD `/catalog/search` endpoint retired with **zero frontend callers**; its re-land is D2d below.

### D2b · The lines FTS surface is dark until the governed lines table lands *(re-anchored 2026-07-28)*

**What.** P7a deleted the indexer; the R6/R20 wave deleted `search_api` itself, so the old
"existing indexed data keeps serving" clause **ended** — there is no lines FTS surface at all right
now (nothing called it: `searchLines`/`searchStats` had zero zone importers). The frozen
`s3://images-batch-search/lines` table is a corpse.
**What closes it.** The P7b gold wave: a **catalog-governed lines table** (line text/geometry/
confidence are `GOLD_CONTRACT_COLUMNS`) + a `DatasetRegistry` descriptor, served at
`/api/media/search?dataset=lines&mode=fts`. Thumb crops ride as a blob column served by the media
blob route — no raw-S3-key proxy gets re-created.

### D2d · The EAD catalog re-lands as a catalog-governed Lance table *(new, 2026-07-28 — the second half of D2a)*

**What.** `scripts/index_catalog.py` + `make catalog-index` are deleted; `scripts/harvest_ead.py`
survives (EAD download only). The `archive_catalog` Lance table at `s3://images-batch-search` is
frozen and unserved.
**What closes it.** An ingest job that writes the EAD table **through the catalog** (governed), plus
a descriptor, so `/api/media/search?dataset=archive_catalog&mode=fts` serves it (media search's
dynamic filterable params cover `archive_code`/`date_*` natively).

### D2e · Warehouse-bucket generalization of the objects browser *(new, 2026-07-28 — the R8 follow-up)*

**What.** The viewer's objects endpoints keep volumes-api's two-bucket `Literal` allowlist
(`images-batch`, `images-batch-alto`). R8 frames the browser as "a lakehouse view of the
warehouse's own buckets".
**What closes it.** Replace the hardcoded pair with a warehouse-derived bucket set (and per-bucket
authz once FGA fronts the browser). Recorded, deliberately not widened in the R6/R20 pass.

### D2c · P7b executes the sealed-runner re-cut this gate only pinned

**What.** `runners/htr` still carries `prefetch_pipeline`/`PrefetchActor`, the S3-diff resumability, and
the `PageLoaderActor`/`AltoWriterActor` endcaps — flagged-D, runner READ-only this gate. The seam they're
replaced by is pinned: mover `stageJob` values knob (`MEDALLION_RAY_ENTRYPOINT`), the gold contract
(`medallion/schemas/htr.py::GOLD_CONTRACT_COLUMNS` + its unit pin), and the `/ingest-iiif` head.
**What closes it.** The P7b gate: the runner CLI grows a `stage` subcommand; layout/lines + transcribe
run as `medallion.bronze`/`medallion.silver` movers; the HTR-cascade e2e (IIIF → bronze → silver →
gold with lineage populated) goes green. *(R23 re-tiered the head: the IIIF harvest lands bronze
directly — there is no raw tier.)*

### D2f · The `/ingest-s3` head route for the second external-raw source family *(new, 2026-07-28 — R23)*

**What.** R23 names TWO external-raw source families: the IIIF Image API (shipped: `/ingest-iiif`) and
external object storage (the ra-hcp pattern). The **adapter seam is landed**:
`medallion/services/s3_harvest.py` (`S3PrefixSource` over `packages/storage`'s provider-agnostic
`storage.S3Source` + `s3_input()` for the `(s3://<bucket>, <prefix>)` OpenLineage input), unit-tested
against moto incl. the bronze blob-v2 landing (`tests/unit/test_s3_harvest.py`).
**Why it is open.** The producer HEAD ROUTE (`POST /ingest-s3`: config for source bucket/prefix
allowlists, token/admin auth, #84 project routing — symmetric with `/ingest-iiif`) is scaffolding-only:
wiring it properly needs the same auth/ceiling/project design pass the IIIF head got, out of the R23
corrective wave's scope.
**What closes it.** The route + settings (`MEDALLION_S3_SOURCE_*`), emitting input=`s3://…` /
output=bronze through the same `/bronze-arrival` seam, with the double-fire pin extended to it.

### D2g · The bronze ingest head's own FGA write gate *(new, 2026-07-28 — R23 collapse residue)*

**What.** The retired raw→bronze mover carried the FGA `can_create_table` self-check for producing
bronze. With the collapse, the bronze write happens in the producer, whose ingest routes are door-gated
(app-token / admin OIDC) but do not self-check a writer rung before the Lance write.
`scripts/seed_medallion_fga.sh` now grants `writer` to `user:service-lance-ray` (the producer identity),
so the model DESCRIBES the intended rung.
**What closes it.** The ingest heads (`/produce`, `/ingest-iiif`, `/ingest-media`) check
`can_create_table` on `namespace:bronze` as `service-lance-ray` when `MEDALLION_FGA_ENABLED` — the same
enforce-not-describe posture the movers keep.

## E. Latent — surfaced by the pre-copy docs audit (2026-07-27), adversarially verified open

These were living only inside reference docs, several anchored to tracker IDs that no longer exist.
Recorded here so the merge cannot lose them; each was verified against the code, not just the doc.

### E1 · OpenLineage "where/why" facets are not captured *(same gap as C2's "lineage facets" condition)*

**What.** `parent` (job hierarchy), `jobDependencies` (why a run waits on another) and `processingEngine`
(Ray version) are in the spec, surfaced by Marquez, and unimplemented here — `LINEAGE.md`'s captured-facets
table omits all three; zero hits in `services/`. Was "Tracked in todo #10b / #12b / #17" in
`event-driven-pipeline.md` — a tracker that no longer exists (`dataQualityAssertions` from that same list
DID land via the quality gate).

**The seam, so it is not rediscovered:** `parent` is already name-reserved in `_RESERVED_RUN_FACETS`
(`services/catalog/core/lineage_emit.py:244`) — but only a rejection test exists, no consumer, and the
docstring at line 240 overstates this. Also unrecorded anywhere durable: ingest handles **RunEvent only**
(no JobEvent/DatasetEvent) — likely deliberate scope, but the scope decision itself was never written down;
decide and record it when this is picked up.

### E2 · Resilience residuals `RESILIENCE.md` carries inline, recorded nowhere else

- The chaos rows (pull-a-service → recover) were driven by hand and never encoded as an automated
  mutating harness (deliberately out of default `make e2e` — they scale shared infra).
- Gap #2's "live check remaining: poison-inject → Dapr `deadLetterTopic` parking" was never driven live
  (only unit tests; the #83 DLQ drive exercised the *outbox* surface, not sidecar parking) and the
  runbook section it pointed at (§6.5) no longer exists after the symptom-first rewrite.
- Honesty-note row 1 — lineage scale-0 → restart-replay under the per-app queue-group components — still
  awaits its one-shot re-verify on a fresh deploy (row 3's was closed 2026-07-06; row 1's never was).
- The bottom-line item "transactional outbox / Ray durable producer belongs to the rask merge" is the
  merge plan's P5 Ray unification — `RESILIENCE.md` is its only other record.

### E3 · Lakekeeper-study adoption backlog, the unshipped remainder *(SYSTEM-SKETCH.md, study wfb25lg74)*

Verified item-by-item against the code; none appear in DECISIONS §9. In priority order:

- **#12 · URL-encode user IDs when serializing to OpenFGA** — subjects are raw-interpolated
  (`f"user:{user}"`, `services/common/fga.py`); the study ruled this *mandatory before prod OIDC* if
  subjects can contain `@`/`+`/`:`. OIDC subjects here are emails. Smallest and sharpest of the set.
- **#9 · Versioned authz-model migration** (`ACTIVE_MODEL_VERSION` + idempotent `migrate()`) — was ruled
  "mandatory before the 3-axis model"; the 3-axis model shipped without it.
- **#11 · Reconcile-from-catalog** — additive FGA rebuild + opt-in drift deletion with dry-run; absent
  (the only `reconcile.py` is lineage storage-drift, a different thing).
- **#10 · Split tuple helpers (`tuples.py`) + golden tuple tests** — `grant_on_create` is still one
  inline grant; the FGA contract test is not this.
- **#2 · Vended-response `credentials` vs `config` split** — `expires_at_millis` shipped per-vendor; the
  dict split did not.
- **#3 · `request_id` + actor propagation** — zero hits in `services/`; *possibly* superseded by OTel
  tracing + the audit trail, but nobody ever recorded that verdict — record it or build it.
- **#14 · `/refresh-credentials` + `revalidation_window_ms`** — conditional: only if STS/web-identity
  vending is enabled (the default profile is `mode_b`, which never expires); carry the conditionality.

---

## F. The docs sweep — split in two so it cannot collide *(new, 2026-07-28)*

An external classification (39 agents, every proposed delete adversarially verified) found `docs/` is
**not junk-heavy, it is stale-heavy**: 25 of 35 proposed deletions were killed because the files are
referenced from `zensical.toml` nav, from code docstrings, or from tests. Only 3 files survived as safe
deletes; the real work is ~82 docs needing UPDATE.

Every claim below was re-verified against this tree on 2026-07-28 before being recorded here.

**Why it is split.** A second workstream (the information-architecture goal: grouped sidebar, the
`/lakehouse/catalog/*` → `/lakehouse/catalog/*` rename, one shell per zone, the storage registry) rewrites
the very things a third of these docs describe. Fixing those docs first means fixing them twice. F1 is
everything disjoint from that work and can start immediately; **F2 is not optional and not dropped** — it
is the same sweep, deferred until the IA goal closes.

### F1 · The collision-free sweep *(ready now)*

**Delete (verified zero live references after their nav rows go):**
`docs/MERGE-HANDOFF-PROMPT.md` — inbound refs are exactly `zensical.toml:93` and
`docs/lakehouse/index.md:55`; remove both in the same commit. `docs/architecture/phase2-schema.dbml`
(DBML for the deleted relational control plane) and `docs/architecture/viewer-phase3-plan.md` (a plan for
a service dissolved in June) have **zero** inbound references.

**R19 — `packages/common` and `services/common` are both gone.** 27 citations across 13 docs still point
at `services/common/*` or `from common.X`: `DATA-CONTRACT.md`, `ARCHITECTURE.md`, `DECISIONS.md`,
`COVERAGE.md`, `BENCH-2026-07-22.md`, `OPEN-WORK.md` (§E3 above), `DESIGN-annotation-projects.md`,
`FLOW.md`, `MEDALLION.md`, `SYSTEM-SKETCH.md`, `DEPLOY.md`, `ASSESSMENT-2026-07-15.md`,
`RASK-INTEGRATION.md`, `architecture/lance-ns-merge.md`. The real homes are
`packages/service-kit/src/service_kit/{dapr_publish,control_events,lakehouse/outbox}.py` and
`service_kit/governed/`. `dapr_publish.py:19,61` cites `DATA-CONTRACT.md` back — fix the pair together;
it is the one code file in F1's scope.

**Dead paths.** `deploy/cnpg-age-cluster.yaml` does not exist — it shipped as
`chart/templates/age-cluster.yaml` — and is cited **three** times: `CNPG-AGE.md:40`, `CNPG-AGE.md:73`,
`OPERATORS.md:14`. `API.md:4` claims a `make openapi-check` CI guard that is **not in the Makefile**:
either add the target or drop the claim.

**`ASSESSMENT-2026-07-15.md` is not a delete.** §1–§2 are discharged and describe the dead pre-merge tree,
but §3 is the only in-tree gap-by-gap prod-readiness enumeration and **two** things depend on it —
`OPEN-WORK.md:118` (C4) and `RUNBOOK-oncall.md:63` ("ASSESSMENT gap #5"). Cut or hard-banner §1–§2; keep
§3 and both inbound refs intact.

**Folds and layout.** The flat copy created duplicate pairs: `SYSTEM-SKETCH.md` (272L) → `ARCHITECTURE.md`
(359L); `DEPLOY.md` (252L) → `architecture/deployment.md` (206L). And the lance docs sit flat at
`docs/*.md` while rask's site uses subdirs — `RUNBOOK-oncall.md` and `RUNBOOK-restore.md` belong in
`docs/runbooks/` beside the `llm-cluster.md` already there.

**Closes when.** Both gates shown green: every `zensical.toml` nav target resolves (it is green **today**
— 0 missing — so this is a regression guard, and a delete without its nav row turns it red), and
`grep -rn "services/common\|from common\." docs/` returns nothing.

### F2 · The deferred remainder *(blocked on the information-architecture goal)*

Not dropped — deferred because the IA goal rewrites the subject matter. Pick this up the day that goal
closes; each item names why it waits.

- **`AUTHZ.md`'s per-zone disclosure table** — line 54 tabulates `` `lakehouse/data` ``, the exact path the
  IA goal renames to `/lakehouse/catalog`. The table also lists fewer than the 7 real zones, and R15 makes
  a missing zone a defect — **that applies to the doc too**.
- **The frontend doc cluster** — `architecture/frontend-microfrontends.md` (305L),
  `architecture/frontend-conventions.md` (592L), `architecture/layout.md`, `components/frontends.md` (44L,
  folds into frontend-microfrontends), `components/progress.md` (264L, self-declares "historical", is
  referenced from `frontend-microfrontends.md:305`), and `architecture/frontend-monorepo.md` (34L, folds
  into frontend-conventions). All describe the `AppShell`/`ZoneNav` structure the IA goal replaces.
  One fix is independent of that goal and should ride along: `frontend-conventions.md:319,347` ship a
  copy-pasteable `@source '../../../../packages/ui/dist'` with **four** `../`; three is correct
  (`frontend/microfrontends/home/src/app.css:7`). Copy-pasting it renders every `@rask/ui` class unstyled
  with no error.
- **`API.md`'s path counts** — says 75/24, the committed specs hold **100/29**, and the IA goal's storage
  registry adds more. Prefer deleting the hardcoded numbers in favour of the `make openapi-check` guard
  over correcting a number that will go wrong again.
- **The three viewer/relational tombstones** — `architecture/data-model.md` (132L, its thesis is the dead
  relational batches control plane, but it carries an ER diagram someone may still want),
  `architecture/viewer-design.md` (659L for a dissolved monolith, referenced from `architecture/index.md`
  and `microservices.md`), `projects/viewer.md` (71L, a tombstone for a plane that has itself since died).
  Two independent verifiers disagreed on all three, so they need a judgment call rather than a blind `rm`.
  Whatever is deleted, fix the inbound reference in the same commit.

**Closes when.** The F1 gates still pass, `AUTHZ.md` lists all 7 zones with post-rename paths, no doc
references a `ZoneNav`/shell shape the code no longer has, and each of the three tombstones has been
explicitly kept-with-a-banner or deleted-with-its-referrers-fixed.

---

## How this survives

1. **P0** of `docs/architecture/lance-ns-merge.md` copies this file to `rask/docs/OPEN-WORK.md`.
2. **P8** reconciles it — items closed *by* the merge get struck with the evidence; the rest carry forward
   into rask's own tracking, renumbered or not, but never silently dropped.
3. `MERGE-REPIN-DELTA.md` was a diff, was applied (the plan is re-pinned, rulings R8–R10 + D7 recorded),
   and was deleted as its own instructions required — git history keeps it. **This file is not deletable**;
   it is reconciled at P8, never dropped.
