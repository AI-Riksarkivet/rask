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

**Where it stands.** Slices `S1`–`S4` (domain core, FGA type, publish schema, catalog `create` pin) need no
store and are the next buildable unit. `S5`–`S10` need B1's actors.

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
| `/lakehouse/data` scaffold, `/lakehouse/admin` orphan | Product decisions, not defects with one right answer |

---

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

## How this survives

1. **P0** of `docs/architecture/lance-ns-merge.md` copies this file to `rask/docs/OPEN-WORK.md`.
2. **P8** reconciles it — items closed *by* the merge get struck with the evidence; the rest carry forward
   into rask's own tracking, renumbered or not, but never silently dropped.
3. `MERGE-REPIN-DELTA.md` was a diff, was applied (the plan is re-pinned, rulings R8–R10 + D7 recorded),
   and was deleted as its own instructions required — git history keeps it. **This file is not deletable**;
   it is reconciled at P8, never dropped.
