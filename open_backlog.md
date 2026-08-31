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

## 0. Two tracks, not one

Owner observation, 2026-08-31: *"Compute and workflow / batch processing is one thing, and the
lakehouse should work as intended either way — if we do it all manually with code, move data
ourselves, access, and do batch processing ourselves."*

That is correct, and this file had been ignoring it. Everything below had been living in one list as
though the estate were one product. It is two, and they have different acceptance tests:

| Track | What it is | Its acceptance test |
| --- | --- | --- |
| **A · The lakehouse** | Lance Namespace catalog, the file/table layer, versioning + tagging, governance (**OpenFGA**), lineage (**OpenLineage→AGE**), credential vending, maintenance, the bronze/silver/gold **tiers**, and the **object store** the bytes live in | **If a person does bronze→silver→gold THEMSELVES — their own ETL, their own schedule, no mover and no Ray — do they still get governance, lineage, versioning, provenance and the quality gate?** |
| **B · Compute & workflow — OUR WAY of doing ETL** | The medallion **movers**, the cascade **choreography** (topic chains), the **ingest plane**, Dapr Workflow, the Ray submission path, Kueue, the executor contract, and the gate being invoked **automatically** | Does a unit of work get submitted, watched, verified and published — and could a second engine, or a person with a cron script, do the same? |

**A CORRECTION I OWE THIS FILE.** An earlier verification pass "proved" self-sufficiency by booting the
catalog with no Dapr, NATS, Postgres, OpenFGA or S3. That proves the wrong thing, and the owner said so:
**OpenFGA IS the governance, AGE IS the lineage, the object store IS where the data lives, and
maintenance IS the lakehouse.** Running without them shows the catalog can run DEGRADED — it says
nothing about whether the lakehouse is independent of our COMPUTE. What that test genuinely established
is narrower and still worth having: the catalog has no code dependency on any compute-plane module, and
its cross-plane features are opt-in. The acceptance test is the one in the table above, and it is about
who MOVES THE DATA, not which processes are running.

**Track B is a CONSUMER of Track A, never the other way round.** Any finding where A depends on B is a
defect in A by definition — that is what `BAKED_JOBS_DIR` is (the catalog validating Ray paths), and it
is why it ranks first in the decoupling list rather than being a tidy-up.

A consequence worth stating: **Track A can be judged against other catalogs** (Lakekeeper, Polaris,
Unity, Nessie) and Track B cannot, because Track B is not a product anyone else ships. Mixing them is
what made "are we using the right tooling?" hard to answer — the honest answer differs per track.

---

## 1. Deployed and verified live

| # | What | Proof |
| --- | --- | --- |
| 1.1 | The stage watch tells a LOST job from a slow one (`seen`, `MAX_UNSEEN_POLLS`) | **LIVE.** Failure reproduced by restarting the Ray head mid-job — new head answered `jobs: 0`, watch polled 404 indefinitely. After deploy, the two watches stuck since 06:38 and 06:48 both terminated at 06:55:3x, and fleet-wide 404 polls went to 0. |
| 1.2 | The cascade proves it may WRITE before it submits | **LIVE.** 200 + audit record observed; a refusal stops the run before the job exists. |
| 1.3 | The Ray plane runs on a scoped S3 credential; the tenant root key is off the pod | **LIVE.** Cascade succeeds; the control plane is `AccessDenied` from that credential. |
| 1.4 | No credential rides the Ray submission body | **LIVE.** The Jobs API echoes `runtime_env` to any reader; the key is gone from both submission paths. |
| 1.7 | **Step 1 — a governed tier must carry its provenance** | **SUITE (7,388 tests, no regression).** RED first: the `runners/dummy` shape published with `published: true` and two assertions, neither about provenance. The check lives at PUBLISH because the job-side contract cannot see it — it counts parentless rows only `if SOURCE_ROWID_COLUMN in out.schema.names`, so a table that DROPS the column reports zero and passes the check meant to catch it. Opt-in by CLAIM: a table carrying none of the three columns is untouched; one carrying any must carry all three, correctly typed. `runners/dummy` now conforms AND refuses to invent a parent id. Not yet driven in the estate. |
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

## 5c. TRACK A — the lakehouse, verified 2026-08-31

Four questions, answered by DRIVING rather than reading. The manual probe stood up real OpenFGA
v1.18.3, real Apache AGE, real RustFS and a real OIDC issuer (all via Dagger, no docker) and performed
bronze→silver→gold **by hand** — no mover, no Ray, no cascade.

### Q1 · Is the lakehouse hard-coupled to compute? **No — VERIFIED. One coupling left, NOT fixed.**

| | |
| --- | --- |
| Verified | The full manual hop worked: governed tables, provenance, gate, published pointer, governance, maintenance, lineage, and project>warehouse>namespace>table on real S3. Both registration seams (ASK and TELL) drove by hand with a plain bearer. |
| The one coupling | `BAKED_JOBS_DIR` — the catalog validating Ray filesystem paths at the transform-declaration door. Decision D2 taken (named tasks from a config list); **not implemented**. |

### Q2 · Bugs and missing features in the catalog / table layer

**FIXED this session, each RED-first and regression-checked:**

| Defect | Why it mattered |
| --- | --- |
| **`branch` ignored on Update/Delete** | A branch-scoped mutation hit MAIN, returned 200, and lineage recorded main's version. Silent wrong-target writes — the worst shape a table format can have. A REGRESSION; the suite that would have caught it quotes the correct pre-refactor line. |
| **`list_namespaces` disclosed siblings** | One deep `reader` grant opened the route and returned every sibling namespace NAME, including ones the caller checks False on. Proven live. `list_tables` next door was already filtered. |
| **`tags/delete` + `version/delete` at writer tier** | A plain writer could destroy or unpin the version `published` names, while `maintenance/run` — which EXEMPTS tagged versions — was owner-gated. The guarded door was expensive, the unguarded one cheap. |
| **A tier could publish with no provenance** | `runners/dummy` shipped fabricated parent ids. Now refused at the publish door, opt-in by claim. |
| **`gate` did not refuse what `publish` refuses** | MY OWN regression from the row above: the promotion review asks `gate` before the tag moves, so an under-reporting gate approves work the act will refuse. |

**FOUND, NOT FIXED — ranked:**

| # | Defect | Evidence |
| --- | --- | --- |
| 1 | **The declared GateSpec is resolved through the LINEAGE EMITTER.** With `LANCE_LINEAGE_EMIT_ENABLED=false` the noop emitter returns no project, so every project's declared gate is **silently not applied** and publish answers 200. Quality-gate policy depends on an observability switch. Live k3s has emit on, so it is latent. | Proven by flipping that one variable. `publication.py:177-195`. Fix: read the warehouse binding directly. |
| 2 | **`register_table` accepts a dataset that can never carry provenance.** MEASURED: a `has_stable_row_ids=False, storage=2.1` dataset was registered 200 and announced into the graph; after compaction its `source_rowid` values went from `0..5` to `4294967296..` — silently naming rows that no longer exist. The flag is create-time-only and unrepairable. Gate A14 exists but lives in `services/ingest`, so it guards the ingest path only. | `tables.py` register door vs `ingest/catalog.py:238`. |
| 3 | **Body-id reconciliation missing on 4 routes** — `describe_namespace`, `namespace_exists`, `table_exists`, `get_table_stats` accept a body `id` and never compare it to the path. Spec says a differing pair MUST 400. | Verified by hand. Not an authz hole (the path is authorized), a conformance gap. |
| 4 | **The `delimiter` param is silently ignored** rather than refused. Already recorded as conscious deviation #6 with a good reason (honoring it per-request would let the FGA gate authorize a differently-parsed object). But **silently ignoring is strictly worse than 400-ing**, and a 400 does not reintroduce that hazard. | `docs/DECISIONS.md:294`. |
| 5 | **Both list routes make ONE unpaginated native call** then apply a local cursor. Correct for a filtered listing — a backend limit truncates before the filter — but against a paginating backend it silently gets page one. `_collect_tables` shows the estate already knows how to loop. **My `list_namespaces` fix inherited this**, so there are now two instances. | `namespaces.py:85` (loops) vs `:218`, `:733`. |
| 6 | **`can_promote` buys nothing on `table`** — `can_update_tag: owner` and `can_promote: validator` with `validator ⊇ owner`, measured on the live model. Either raise the publish door or lower `can_promote`. | Live model check. |
| 7 | Two write sites drop the create-time flags: `runners/kg/adapter.py:453` (then destroys history on the next line, on an unpinned pylance) and `scripts/medallion_demo.py` ×3. | Verified by hand. |

### Q3 · Do governance and lineage work? **Yes — both driven.**

**Governance:** a hand-made table seeded the same tuples a mover's create does (`user:alice owner`, `namespace:gold parent`), and a second user got **403 on describe, on publish, and on write-authorization**. Audit records fired on every grant, denial and access review.

**Lineage:** the catalog emitted `create_table.*` COMPLETE events; a person emitted their own runs through `lineage_kit.job_run`; `producers`, `upstream`, `downstream` and `/graph` all resolved with the right authors and edges.

### Q4 · Is it a full lakehouse when driven by hand? **Yes — with effort.**

Everything held. The "effort" is real and is documentation, not capability:

- **The provenance recipe is written down NOWHERE.** `grep -rln "stamp_stage" docs/ .claude/` matches nothing. Three things a DIY user must know are oral tradition: that `stamp_stage` is the supported way to stamp a tier; that it needs `with_row_id=True` on the read or it mints nothing **and raises nothing**; and that `lineage=""` DELETES an inherited lineage column rather than carrying it.
- **A person cannot mint their own project** — `POST /v1/projects` needs the estate-admin bar once. Everything below it is self-serve.
- **The warehouse HTTP door is S3-only**, so a `file://`-rooted catalog has no HTTP path to the binding a declared gate needs.
- **Credential vending was never exercised** — the probe read S3 with the endpoint's own credentials. `vending_mode` defaults to a mode that vends nothing.

---

## 6a. Track A finding — a hand-written dataset can be registered with provenance that can never work

Found 2026-08-31 while auditing every Lance create path against `lance_docs/file_format.md`.

`enable_stable_row_ids` is **create-time only** — set it later and it is a silent no-op. Every
`source_rowid` in silver and gold is a reference to a stable `_rowid`, so a dataset created without the
flag can never carry honest provenance, and cannot be fixed short of rewriting it.

Three doors, and only two are guarded:

| Door | Behaviour |
| --- | --- |
| The catalog CREATES a table (`dataplane.py:214`) | Sets the flag. Correct. |
| The INGEST plane registers (`ingest/catalog.py:238`, gate A14) | REFUSES a dataset without it. Correct. |
| **`register_table` — the door for data written elsewhere** | **No check at all.** |

That third door is precisely the manual path: a person writes their own Lance dataset and registers
it. It is accepted, `_rowid` is unstable, and the provenance contract added at the publish door cannot
see the problem — it verifies the COLUMNS exist, not that the dataset underneath can produce honest
values for them.

**A prose correction that came with it:** `ingest/lander.py:68` says "gate A14 makes the CATALOG refuse
a governed dataset created without them." The gate is real, but it lives in `services/ingest`, so it
guards the ingest path only. The catalog refuses nothing. Reading that comment, one would reasonably
conclude the third door is covered.

**Not yet fixed, because the right shape needs a decision:** `register_table` also exists for genuinely
external datasets that may never be a governed tier, so refusing outright may be wrong. The consistent
shape with D1 is opt-in by claim — refuse when the registration is into a governed tier, accept
otherwise — but that requires knowing at registration time which it is.

---

## 6b. Bootstrap on a fresh machine — NOT complete

Asked 2026-08-31; the answer is no, and checking it found a security regression waiting to happen.

| Piece | Chart-owned? |
| --- | --- |
| The fleet, the lakehouse services, the zones, the infra toggles | **Yes** |
| ExternalSecrets wiring | **Yes, and now correct** — the FQDN and `system:auth-delegator` both landed today |
| Kueue queues | Yes, and structurally bypassed — nothing creates a CR for them to admit |
| **The Ray head the cascade actually runs on** | **NO** — hand-applied `deploy/ray-lance-demo.yaml`, diverged from the chart's own RayService |
| OpenBao's Kubernetes auth backend, policy, role | **NO** — configured by hand (commands recorded, but a runbook is not a manifest) |
| The KV secret values | **NO** — seeded by hand |

**The regression, now fixed:** `deploy/ray-lance-demo.yaml` set `S3_KEY: rustfsadmin` — the RustFS ROOT
key — while the live pod runs the scoped `rask-ray-compute`. Re-applying it silently undid the
credential scoping, and the undo is INVISIBLE: the cascade keeps working, because root can do
everything the scoped key can. The only way to notice is to read the pod. The file now matches the
live pod exactly.

**Remaining:** reconcile the hand-applied head with the chart's RayService, and make the OpenBao
bootstrap a job rather than a runbook. Until then "it is all in the chart" is false, and the gap is
precisely where the security posture lives.

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
