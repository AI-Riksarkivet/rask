# open_backlog.md — what is outstanding, and what is actually PROVEN

**The rule this file exists to enforce: nothing is finished before it is proven and verified.**
A green unit test is not proof that a change works in the estate. A commit is not proof. A passing
gate is not proof. Only the behaviour, observed where it runs, is proof.

So every row carries a **verification level**, and the level is the honest one, not the hopeful one:

| Level | Means |
| --- | --- |
| **LIVE** | Observed working in the running estate — the failure reproduced first, the fix re-observed after. |
| **SUITE** | Unit/integration tests pass, gates green. **Not deployed. Not observed.** |
| **WRITTEN** | Code exists and reads correctly. No test proves it. |
| **BLOCKED** | Cannot proceed without something named in the row. |
| **OPEN** | Not started. |

A row does not move to LIVE because it looks right. It moves when someone drives it.

---

## 1. Deployed and verified live

| # | What | Proof |
| --- | --- | --- |
| 1.1 | The stage watch tells a LOST job from a slow one (`seen`, `MAX_UNSEEN_POLLS`) | **LIVE.** Failure reproduced by restarting the Ray head mid-job — new head answered `jobs: 0`, watch polled 404 indefinitely. After deploy, the two watches stuck since 06:38 and 06:48 both terminated at 06:55:3x, and fleet-wide 404 polls went to 0. |
| 1.2 | The cascade proves it may WRITE before it submits | **LIVE.** 200 + audit record observed; a refusal stops the run before the job exists. |
| 1.3 | The Ray plane runs on a scoped S3 credential; the tenant root key is off the pod | **LIVE.** Cascade succeeds; the control plane is `AccessDenied` from that credential. |
| 1.4 | No credential rides the Ray submission body | **LIVE.** The Jobs API echoes `runtime_env` to any reader; the key is gone from both submission paths. |
| 1.6 | **ExternalSecrets works end to end** | **LIVE.** Was "manifests correct, server dry-run accepted" and had never been driven. Driving it found TWO defects, and the first masked the second. (a) The `SecretStore` address was a BARE service name — correct for every Dapr sidecar, which sits in this namespace, and wrong for the ESO operator, which runs in `external-secrets`: login failed `lookup rask-openbao ... server misbehaving`, a DNS failure that reads as an OpenBao outage. (b) OpenBao's own ServiceAccount lacked `system:auth-delegator`, so it could not submit the TokenReview that validates a caller — every login answered `403 permission denied`, which reads as a bad role and sends you auditing the Vault side, when OpenBao never got far enough to evaluate the role. Both fixed in the chart. Verified: `SecretStore Ready=True "store validated"`, `ExternalSecret Ready=True SecretSynced`, value arrived from OpenBao. Driven against a THROWAWAY target so a wrong value could not overwrite a working credential — which mattered: the Ray credential lives in its own secret, not in `infra-credentials`, so the first KV write used a placeholder that had to be corrected before anything consumed it. |
| 1.5 | **Bounded stage resubmit after the head loses a job** | **LIVE, end to end.** Took four attempts, and the first three failed for a reason worth recording: the `seen`→vanished branch is effectively UNREACHABLE for these jobs — a stage transform is a column stamp over Lance and completes inside one 30 s poll interval even at 400,000 rows, so the head restart always landed after the job was already terminal. The branch that actually fires here is `never_registered`. Driven at 09:21: job submitted, head restarted before the first poll, four 404 polls at 09:21:44 / 09:22:14 / 09:22:44 / 09:23:14, then `medallion_stage_resubmitting` at 09:23:14, a new submission at 09:23:15, `200 OK` at 09:23:45, terminal success the same second — and **silver→gold woke at 09:24:16 with `medallion_stage_moved`**. The cascade survived a head restart that previously killed it silently for 24 hours. |

---

## 2. Committed but NOT verified in the estate — the honest debt

Every row here is committed and suite-green. **None has been observed running.** This is the section
that must shrink before any of it is called done.

| # | What | Level | What "proven" would require |
| --- | --- | --- | --- |
| 2.2 | `VS-07` — a dropped search leg logs or raises; a fusion with every leg rejected no longer answers an empty 200 | **SUITE** | Drive a real search with a leg forced to fail and observe the log/5xx rather than a silent 200. |
| 2.3 | `ANN-03` — the project listing reads its actors concurrently | **SUITE** | Observe the listing latency and the peak concurrent actor reads against a real actor plane. |
| 2.4 | `VS-05` — the plain-binary listing stops reading every blob | **SUITE** | Measure bytes read serving `/api/pages` on a real plain-binary corpus. |
| 2.5 | `DUP-04` — `boto3` confined to `packages/storage`; region + session token added; timeouts everywhere | **SUITE** | Exercise a vended temporary credential end-to-end (session token is the half most likely to be silently wrong) and confirm a non-default region survives. |
| 2.6 | `ingest-flow-03` — the exact-cover search stops rescanning the universe per node | **SUITE** | Reproduce the worker's one-fragment-per-redelivered-unit recovery shape against the real staging path. |
| 2.7 | **Lakehouse `PagePreview` treats unknown payload as "attempt it"** | **SUITE** | **Browser-verified with a screenshot** — the estate's standing rule for UI changes, and this row breaks it. `svelte-check` clean is not the same as a thumbnail rendering. |

---

## 3. In flight

| # | What | Level |
| --- | --- | --- |
| 3.1 | Catalog's `LANCE_LINEAGE_OUTBOX_URI` wired | **SUITE** — render-pinned by a new invariant; a real bus outage not exercised |
| 3.2 | Control-lane relay written + wired on a private `_control_outbox` prefix | **SUITE** — never `_lineage_outbox`, which the lineage relay would treat as poison |
| 3.3 | `/bronze-arrival` carries the vended `from_uri` | **SUITE** — the ingest-first ordering hazard is closed; see 5.10 for the remaining limit |
| 3.4 | Gate parity — a `key_column` naming no column is refused | **SUITE** |
| 3.5 | `MAINTENANCE_LINEAGE_OUTBOX_URI` — the same defect on a second service | **SUITE** — found only because an agent named it in its own `left_undone` |
| 3.6 | `open_compute-decoupling.md` — the audit and the executor contract | **WRITTEN** — 3 seams engine-bound, 2 leaky, 2 neutral |

---

## 3b. Owner decisions — SETTLED 2026-08-31

These were the design-session questions. They are answered; nothing here is open any more.

| # | Decision | Ruling | What it means concretely |
| --- | --- | --- | --- |
| D1 | **Is honest `source_rowid` provenance mandatory?** | **MANDATORY** | A published tier must carry provenance that traces to real parent rows. `runners/dummy` fabricates them today (`list(range(len(ids)))` — the row's POSITION, not its parent), so it is in violation and must conform or be refused. An aggregating transform must declare how it maps parents, or declare "no provenance" explicitly — a silent opt-out is exactly what this ruling ends. The case that decided it: after a corrupt ingest, "which gold rows are contaminated?" must not return a confident wrong answer. |
| D2 | **Is Ray forever, and does the catalog get to know it?** | **Ray forever; catalog ignorant** | The owner rejected both extremes. Not a full task registry (a new moving part for an engine that is not changing), and not the status quo (a governance service shipping `/home/ray/jobs/` in its public OpenAPI). The middle: the catalog validates `task` against a **named list given to it as config**, never a filesystem path. Declaration-time refusal — the property worth keeping, because it catches a typo at declare rather than at 3am as `exit 2` — survives unchanged. |
| D3 | **Is structural integrity waivable?** | **WITHDRAWN by me, owner concurred** | I raised it believing "batch with no gate" was impossible. It is not: an empty delta run exits before publishing (`rows_in == 0` → `delta_empty=1`), deliberately, so a redelivered event cannot fire a publication for data nobody added. Only `not_null` on the key column becomes opt-in — *which* column is "the key" is policy. `row_count_positive` and `blob_resolves` stay mandatory. |

---

## 4. Blocked on the owner

*(4.1 ExternalSecrets is CLOSED — see 1.6.)*

| # | What | Blocked by |
| --- | --- | --- |
| 4.2 | **`dedicatedServiceCredentials: false`** — `LANCE_FGA_CASCADE_WRITERS` seeds `owner` on every warehouse for each mover, while `LANCE_PRIVILEGED_SUBJECTS` is deliberately unrendered | An owner ruling. The chart states the consequence in its own words: any holder of the shared `APP_API_TOKEN` could claim any name on `LANCE_SERVICE_SUBJECTS`, and those names hold `owner` on every warehouse. This is a posture, not a defect, so it is not mine to change. |

---

## 5. Known and not yet scheduled

Named so they do not read as done.

| # | What | Why it matters |
| --- | --- | --- |
| 5.1 | **No Dapr Workflow versioning seam** | `grep is_patched\|get_version\|reuse_id_policy` across `services/` and `packages/` returns nothing. Two replay divergences have already shipped. "Drain before deploying" is the only safe answer today, and it is a manual one. This is the single thing Temporal would buy that Dapr does not. |
| 5.2 | **No cascade reconciler and no re-run verb** | Zero `bindings.cron` components on the medallion; the operator surface is list/inspect/terminate with no re-drive. The cascade is forward-only AND unrepairable. |
| 5.3 | **No list-instances, therefore no sweep** | 1,367 orphan rows measured in `daprstate` with no TTL and no alert. Mitigated by retention policy, not solved. |
| 5.4 | **The workflow status metric is structurally false** | Every dying run reports success, forcing a hand-rolled parallel counter. |
| 5.5 | **Ray GCS has no fault tolerance** | Accepted deliberately (no Redis, owner ruling). A head restart still kills in-flight jobs; the platform now degrades in ~2 min with `reason=job_vanished` instead of hanging 24 h, and resubmits once deployed (2.1). |
| 5.6 | Annotator project listing has no `limit`/`cursor` | The fan-out bounds CONCURRENCY, not COUNT. A thousand projects is still a thousand actor round-trips. Needs a wire-contract change and a frontend change. |
| 5.7 | `attach_captions` logs rather than raising on an estate fault | A deliberate call — the hits are complete when it runs — but if a caption-scan outage should be a hard 503, it is one line at the same site. |
| 5.8 | No tree-wide gate forbidding `boto3` outside `packages/storage` | Per-module AST guards exist. A tree-wide gate needs an allowlist for legitimate test-side use (moto fixtures, runner tests), which is a larger decision. |
| 5.10 | `/bronze-arrival`'s confinement is still project-only | With no project, `read_root` is a dataset URI, so a single-tenant ingest-first estate gets a vended location outside it and the mover DROPS it — visibly, with a counter, which beats the silent wrong-data success it replaces, but is not a fix. Widening to `MEDALLION_CATALOG_ROOT` would newly drop a legitimately zoned lane. |
| 5.11 | The media lane has the same `from_uri` coupling, untouched | `media_produce.py` still composes the medallion path. |
| 5.12 | The mover still sends chart values to the publish gate | So a declaration cannot RELAX a required column for cascade publishes — `required_columns` are UNIONED, so a project declaring a shorter list still gets the chart's columns asserted on top. |
| 5.13 | **The work CONTRACT is unwritten, and the second engine already violates it** — NOW UNBLOCKED by D1 | Twelve output obligations exist only as control flow in `scripts/ray_stage_job.py`; the platform enforces three post-write. `TIER_COLUMNS` names the contract and has exactly ONE real importer, a test — in `ray_stage_job.py` the name appears only inside a COMMENT, never as an import or a check. `runners/dummy` is a declarable, baked, ACCEPTED lane with no `stage` column, no `lineage` column, an `int64` `source_rowid` where the platform mints `uint64`, and fabricated parents. Nothing is red. |
| 5.14 | **Kueue is wired and structurally bypassed** | `chart/templates/kueue-queues.yaml` exists, gated on `kueue.enabled`; every job goes through Ray's Jobs REST API, which Kueue cannot admit. Fixing it means submitting `RayJob` CRs — which is also the first executor adapter, and what makes runs cluster objects. |
| 5.15 | No re-run verb | The delta machinery exists (`BASE_VERSION`, verified live). The operator surface is list/inspect/terminate — there is no `POST /movers/{m}/stages/rerun`, so nobody can say "reprocess silver from version X". |
| 5.9 | Multi-base dataset garbage is unreclaimable | Upstream-blocked in Lance, not a backlog item this estate can close. Recorded so it is not rediscovered. |

---

## 5b. The ordered plan — what to build, in order

From `open_compute-decoupling.md` §7.4, with D1–D3 folded in. Steps 1, 3 and 7 are NOT preferences: 1 and 7
close live defects, 3 is the only thing that makes the already-deployed Kueue reachable.

| # | Step | Depends on | The test that proves it |
| --- | --- | --- | --- |
| 1 | **Write the output contract** and enforce it at publish (D1: provenance mandatory) | nothing | A table missing `stage`/`lineage`/a real `source_rowid` cannot publish. `runners/dummy` either conforms or is refused. |
| 2 | Make the in-process executor conform | 1 | Both executors produce byte-identical governance columns for one input. |
| 3 | **Submit as a `RayJob` CR** instead of the Jobs REST API | 1 | `kubectl get rayjobs` lists a run. |
| 4 | Turn Kueue on — quota + gang scheduling | 3 | A job queues when quota is exhausted, then runs. |
| 5 | `not_null` becomes opt-in per declaration (D3) | 1 | A declaration with no key column publishes; an empty table still cannot. |
| 6 | Named-task validation (D2) — the catalog stops seeing paths | 1 | `grep -r "/home/ray/jobs" services/catalog packages/service-kit` is empty; a bad task name still 422s. |
| 7 | **A reconciler and a re-run verb** | 1 | A deliberately missed hop is detected and re-driven. |
| 8 | `Transform` CRD + one generic runner replacing three movers | 1,3,6 | Adding a hop is a git commit. |
| 9 | Finish the "lane" rename | nothing | The word survives only where it means a code branch. |

---

## 6. Not in scope here

`TODO.md` holds the frontend/IA backlog (zone routes, Projects views, Explorer and annotator work).
It is a separate list with a separate owner conversation, deliberately excluded from this file so the
platform backlog does not absorb it.

---

## 7. The living documents

| Document | Where | What it is |
| --- | --- | --- |
| **The stack explainer** | `open_stack.html` (source, in this repo) → published at **https://claude.ai/code/artifact/0e2d5495-5c90-4704-954a-60cf7465634b** | 20 sections, 14 diagrams: what each component is for, how batch actually runs, where blob bytes physically live, the 1:1 and 1:many worked flows, and the ranked decoupling list. **The URL is stable** — republishing the source updates that same page. Iterate on the file, republish, the link never changes. |
| **The decoupling spec** | `open_compute-decoupling.md` | The executor contract and the migration, in implementable detail. |
| **This backlog** | `open_backlog.md` | What is outstanding and what is actually PROVEN. |

The HTML source is version-controlled deliberately: it was authored in a session scratchpad, which does
not survive the session, and a document we intend to keep iterating on cannot live somewhere that
disappears. `open_`-prefixed, like every other working document here, so it is deleted when the work it
describes has landed rather than drifting into `docs/` as settled architecture.
