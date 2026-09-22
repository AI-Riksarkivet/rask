# open_backlog_left_new — only what is LEFT

Replaces `open_backlog_left.md`, which grew to 9,378 lines because every row accumulated its own
history — "re-measured on DATE", "this landed", "DEPLOYED", "the row's own estimate was wrong". That
history is what made it unusable, so **this file does not carry it.** Each row states the defect, what is
left, and what ends it. Nothing else.

**Every row here was re-measured against HEAD on 2026-09-18** by 16 parallel agents reading the code
rather than the prose. The old file stays in git history; a row's full story is recoverable from
`git log -S'<ID>' -- open_backlog_left.md`.

**HOW TO READ A ROW.** `blocked:` means a person cannot finish it without a decision — those are not
available work, and the count below separates them. `PARTIAL` means some clauses shipped and only the
named remainder is left. `REWRITTEN` means implementing the original ask would be wrong; the row says
what to do instead.

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

**ZERO TRUST, and it is an OUTCOME not a mechanism.** Secrets reach a workload by exactly three paths
(owner, verbatim): *"Never secret through envs. Either from ESO, secret store dapr and STS for zero
trust."* — Dapr secret store (OpenBao) for a pod with a sidecar; ESO for a pod without one (Ray lane,
web zones, runners); STS for STORAGE (`vending.build_session_policy`, bucket+prefix, 900 s). **Never
through env** — not process env, not a k8s Secret via `envFrom`, not a chart value, no fallback chain.
A scoped static key is not a fix. A record NAMES a secret; it never carries one. **A credential is a
PAIR** — half a swap signs nothing (`SignatureDoesNotMatch`, measured twice: the Ray lane, and the
per-base vend 2026-09-21). Read the running pod, and never one spelling of a mount.

**IDIOMATIC TO lance-ns AND THE LANCE FORMAT, and to CLOUD-NATIVE — never Iceberg.** `lance_docs/` is
the authority: `ns_catalog/spec.yaml` over any prose (the prose contradicts its own bundle), plus
`file_format.md`, `namespace.md`, `guide.md` and the branching/blob brief. **Read it and cite it.**
Worked 2026-09-21: the spec defines three TABLE-scoped branch ops and no branch resource, so a
`branch` FGA type would invent one — while the format puts branch isolation in the STORAGE PREFIX
(`tree/<b>/`, "read-only on main and write-only on the branch"). Raise `lance_namespace` typed errors
and let `install_problem_handlers` translate; never a hand-picked status.

**READ THE SKILL REFERENCES, not the index — and they are on disk even when the Skill tool cannot
list them:** `~/.claude/plugins/marketplaces/ra-skills/skills/<name>/references/*.md` (fastapi,
writing-python, dagger, testing-python, …) plus this repo's `.claude/skills/rask-*`. Not optional:
reading `fastapi/exception-handlers.md` + `writing-python/error-handling.md` turned a live 500 into
the correct 400 the same day.

**Never Docker — Dagger builds every image. Never mypy, never `# type: ignore` — narrow or cast. No
backward compat. Comments carry rationale and provenance, never history.**

**A VERDICT IS NOT EVIDENCE IT IS STILL TRUE — re-measure before working a row.** Of 17 rows settled
2026-09-09, 8 were already fixed, 2 asked for less than they said, 1 described the wrong thing. My own
verdicts are the least audited: several blockers and two severities dissolved on re-reading in one day.

**MEASURED — DO NOT RE-DERIVE.** An OOM in a lakehouse service is a SIZING/DESIGN question before it
is an allocator one: `maintenance` OOMKilled because the PLANNER runs the sweep INLINE when
`workTopic` is unset (`api/routes.py`), doing 4Gi-sized compaction in a 512Mi pod. Two allocator
fixes moved it 87m -> 442m -> 460m and neither stopped it. **BYO WORKERS is the shape** — heavy work
belongs on a pod sized for it, reached through a queue, not in the planner's memory.
`MALLOC_ARENA_MAX` is glibc-only while pyarrow allocates through **mimalloc** and duckdb bundles
**jemalloc**, so it governs little; `ARROW_DEFAULT_MEMORY_POOL=system` was tried and FALSIFIED.
The stage job emits no OpenLineage of its own; the stage RUNNER emits durably through the outbox, and
a lane driven around the platform is correctly refused rather than under-served.

### Verification, per commit

`uvx ty check`, `uv run ruff check`, the TESTPATHS the change touches — **and always the invariant +
integration layers**. **BATCH them:** one layer run and one image build per BATCH of rows, not per row
— measured, that was the session's real waste. Anything deployable is **BUILT with Dagger, DEPLOYED to
k3s and OBSERVED working**. **A green test is not evidence the code runs in production:** gate EVERY
hop of a wiring, and prove a feature live by configuring it to a value that MUST fail. **Push every
commit.** Background watchers report themselves — do not narrate them each turn.

<!-- FOCUS:END -->

## RIPE DECISIONS — evidence complete, work starts the moment each is answered (2026-09-17)

124 rows carry a decision marker. This section does NOT rank them — the other 121 were not assessed —
it lists the four that were driven far enough that only the ruling is missing, each naming what
happens either way. They are here because a decision buried in row 7,200 is a decision nobody makes.
Adding a fifth means driving it to the same point first, not promoting it from the list below.

**1 · Does an ESO-written Secret delivered by `secretKeyRef` satisfy the secrets rule?**
*Blocks [[XC-002]]; defines what [[LH-160]]'s "baseline reaches 0" even means.*
The estate currently says both things. `.claude/skills/rask-dapr/SKILL.md:152-154` and
`tests/unit/test_the_ray_credential_has_one_source.py:4-8` (3 tests, green) say an ESO-backed
`secretKeyRef` IS the sanctioned path for a sidecar-less pod. `test_secret_env_delivery_only_shrinks.py`
counts every `secretKeyRef` as the banned path. Both read plausibly from the rule as written — ESO is
named as a path, but the rule's subject is "never secret through envs".
*If NO:* the Ray head's six entries move to a projected file mount (`S3_SECRET` to STS), and the skill
line plus that test are rewritten in the same commit. *If YES:* XC-002 closes and LH-160's baseline
excludes ESO-written refs explicitly, so the number keeps meaning something.

**2 · May the maintenance sweep reclaim a BRANCH?** *Both rows this was framed as unblocking —
[[LH-094]] and [[LH-019]] — have since CLOSED, so the decision no longer buys what it says. What
argues for it now is live pressure, measured below: it is roughly half of everything the sweep
refuses.* The change is one line — skip the refusal when
`containment_of(uri, root) == "branch"` — and the EQUALITY refusals are untouched by it, which is the
half that must keep refusing: an external shallow clone in another dataset is invisible to Lance, so
only the estate-wide pre-pass can see it.
**RE-MEASURED LIVE 2026-09-21, so the decision carries a current number rather than a remembered
one:** one sweep tick over the deployed estate reports `refused=260 datasets=585` with
`by_gate={'protected_base': 120, 'manifest_flags': 129, 'invalid_ref': 11}`. So **44% of the estate is
refused**, and `protected_base` — the half this decision would release — is **120 datasets, 21% of
everything the sweep walks**. The other 129 are `manifest_flags` (shallow clone / multi-base) and stay
refused whatever is decided here; they are the equality half, not the branch half.
Safety is measured three ways and pinned by `tests/unit/test_base_refs_guard.py` (15 pass): Lance's
`cleanup_old_versions` IS branch-aware (3 files -> 4 -> 1 without a branch; 3 -> 4 -> 4 with); a branch
still opens from a COLD interpreter after its parent is compacted AND reclaimed; and in the PRODUCTION
shape — parent + external shallow clone + branch — maintaining the branch leaves the external clone
intact (parent 9 rows, clone 3, branch 10).
*It is an owner call because it makes reclamation DELETE MORE on a live estate*, and the guard's own
comment sets that asymmetry deliberately ("a wrong refusal costs disk … a wrong permit costs a clone its
entire reason to exist"). Live pressure, re-measured 2026-09-17 on the current image over 10 minutes: **105
`relation='branch'` refusals against 94 `relation='is'`** — so roughly half of what the sweep refuses is
the class this decision would release.

**3 · Is the operator-readable Ray job name worth a shared-contract change?** *Blocks the last clause of
[[LH-159]], the BYO contract.* Wiring `RayJobsApiExecutor` into `workflow.py` renames every Ray job,
because the adapter submits under `order.idempotency_key` and the deployed submitter uses
`stage_submission_id`. **The correctness worry does not apply:** `workflow.py:490` returns what the
submitter posted and the poller reads `payload.submission_id`, so submitter and watcher agree by
construction and cannot desynchronise. Measured on the live head 2026-09-17: 176 jobs, 48 readable
(27%), **0 PENDING or RUNNING**.
*Recommendation: take the rename.* It changes no shared `service-kit` contract, it is free today, and a
log field carrying stage+token restores the legibility if it turns out to matter. The alternatives widen
`WorkOrder` or the `Executor.submit` signature to buy back a naming convenience.

**4 · Should the lineage ingest door REFUSE a `dataSource.uri` that names no storage location?**
*Driven to the bar 2026-09-22 while closing [[LH-187]]; the attribution half already shipped.*
`_names_a_storage_location` runs only on the READ side (`reconcile.py`), so the door accepts what
every later sweep refuses — each tick, forever. Driven: a `RunEvent` carrying
`dataSource.uri = 4750a5b9_acme-bronze$events` is accepted, stored verbatim, and answers False to the
sweep's own predicate. The live `unreadable=23` is that shape, dated 2026-09-06 and traced to rask's
own `catalog/core/lineage_emit.py`; the door it came through (`register_table`) is fixed, so the
residue is historical rather than an active leak.
*What already landed, and needs no ruling:* the ingest seam logs
`lineage_unresolvable_dataset_location` with producer, job and run id, so the next one is nameable in
one query rather than by archaeology.
*If YES:* a `lance_namespace` typed error at the door, translated by `install_problem_handlers` —
matching the estate's stated posture that a lane driven around the platform is refused rather than
under-served. *If NO:* the log line is the whole answer and the sweep keeps carrying the residue.
*It is an owner call because `dataSource` is an OPTIONAL OpenLineage facet emitted by EXTERNAL
producers*, so a refusal costs a third party its entire run event over one malformed field — the
platform would be rejecting provenance it could otherwise keep.

## Answered 2026-09-18 — provenance placement, and it is a RULING not a row

**Lakekeeper does not do provenance.** No lineage crate; `openlineage` appears nowhere in its source. It
ships a best-effort CloudEvents fan-out its own docs tell you not to trust ("not as a ledger you can
replay") — bounded channel, 50 ms timeout, drop on overflow, no outbox, dispatched after the commit
returns. **For generic tables — what Lance is to Lakekeeper — there is no commit event at all.**

**The industry puts lineage at the engine or orchestrator**, in-process with execution: Spark, Trino,
Flink, Airflow, Dagster, dbt. Of eight catalogs surveyed, **zero emit**, three receive or are scraped,
five have none. Polaris declined ownership in as many words.

**rask does not decouple lineage further, and that is settled.** It is already decoupled — zero imports
either way between `services/catalog` and `services/lineage`, wire-only coupling, and rask has the
transactional outbox Lakekeeper refuses. The catalog's emit is not redundant with compute-side emission:
it is the only component that knows the VERIFIED PRINCIPAL on a write, so moving emission to compute
loses authorship provenance and breaks condition 1 outright. The remaining coupling is to the CONTROL
BUS, not to the lakehouse, and it is deliberate.

*What this ruling does NOT settle* is the opposite direction — the compute plane is the one the industry
says owns lineage, and it is the plane rask has not wired. That was [[LIN-001]], which CLOSED 2026-09-20
with every clause of its own done; its remainder sits in [[CP-032]], where six of the nine runners still
have no `uv.lock` and so cannot be built to emit anything.


## Counted

**198 open items**, of which **102 are blocked on a decision** and **96 can be picked up today**.
18 rows were dropped as already done — listed at the foot so nothing vanishes silently.

| Section | Open | Workable now | High |
| --- | --- | --- | --- |
| **PHASE 1 · LAKEHOUSE** | 38 | 4 | 10 |
| **PHASE 1 · CROSS-CUTTING** | 42 | 16 | 9 |
| **PHASE 2 · COMPUTE** | 55 | 36 | 16 |
| **PHASE 3 · CONTROLPLANE** | 28 | 11 | 6 |
| **FRONTEND** | 10 | 9 | 0 |
| **LOW PRIORITY** | 25 | 20 | 0 |


## PHASE 1 · LAKEHOUSE

**LH-177 · The catalog vends its own in-cluster address, so an off-cluster client gets a valid credential for a host it cannot resolve**
`catalog` · **MED**
- **blocked:** Owner call on what VENDING MEANS OFF-CLUSTER — does an external client receive an externally-resolvable endpoint, or is vending in-cluster-only by design? Split from the closed [[LH-020]] on the 2026-09-19 ruling, which closed that row's test-coverage half and left this as the product question it always was.
- *What is left:* Measured 2026-09-19: the vended `endpoint` is `http://rask-minio:9000`. The k3s service network IS routable from the host (`10.43.44.177` answers) and `rask-minio` does NOT resolve there, so resolution and the vend both succeed and only the byte read fails — the barrier is DNS, and it belongs to where the process runs rather than to the clients. If vending should serve external clients, the endpoint has to be configurable per deployment and someone must decide what an endpoint may disclose about internal topology. If it should not, the door should SAY so in its answer rather than handing out a credential that cannot be used: a 900 s credential for an unreachable host is indistinguishable from a broken object store until the read fails.
- *Closes when:* The answer is recorded in `docs/DECISIONS.md` and the vending door matches it — either an externally-resolvable endpoint, or an explicit refusal/annotation for a caller it cannot serve.
- *Evidence:* `make e2e-spec-conformance` from the host: the lancedb and lance-ray cases skip with "the vended endpoint 'http://rask-minio:9000' is in-cluster and unreachable from here" · the same suite in-cluster: **17 passed, 0 skipped** (`make e2e-spec-conformance-incluster`) · `getent hosts rask-minio` on the host: no answer; `kubectl get svc rask-minio`: ClusterIP 10.43.44.177, routable

**LIN-002 · The estate emits 8 of ~30 standard OpenLineage facets, and told a standard consumer nothing about what a write DID**
`catalog, lineage, medallion, service-kit` · **MED**
- **blocked:** Owner ruling on `NominalTimeRunFacet` — what LOGICAL WINDOW a cascade run covers. The spec's `nominalStartTime`/`nominalEndTime` are the interval the run is FOR, not when it executed, and a cascade hop has three defensible answers: the upstream tier's version timestamp, the trigger event's time, or the window the ORIGINAL ingest covered. Emitting the wrong one is worse than emitting none — a standard consumer schedules and back-fills on it. This is the row's ONLY remaining facet: everything else is either shipped, or left this row with its reason recorded (`DataQualityMetricsInputDatasetFacet` describes an OUTPUT but is an INPUT facet, `SQLJobFacet` has no subject until a query engine lands).
- **TWO SHIPPED 2026-09-19, and the version audit is DONE and CLEAN.** Every one of the estate's eight pinned facet versions matches upstream exactly, read from `OpenLineage/OpenLineage/spec/facets`: ColumnLineage 1-2-0, DatasetVersion 1-0-1, Datasource 1-0-1, ErrorMessage 1-0-1, JobType 2-0-4, OutputStatistics 1-0-2, ParentRun 1-2-0, Schema 1-2-0, envelope 2-0-2. So a VERSION audit is not needed; a COVERAGE one was. Added: `LifecycleStateChangeDatasetFacet` (rask's 11 DDL operations mapped onto the spec's six-value enum — a DATA operation gets NO facet, because the enum has no member meaning "wrote rows" and `OVERWRITE` is a lie a reader acts on) and `ProcessingEngineRunFacet` (which engine wrote this, `version` being the spec's only required field). The rask `lance.operation` name stays beside both: it is more specific than the enum admits, so collapsing onto the standard field would lose what the estate's own consumers read.
- **SEVERITY SHIPPED 2026-09-19, and the value was already in the estate.** The spec's `Assertion` carries `severity` — `error` when the failure blocks the pipeline, `warn` when it does not — and rask emitted neither, so a standard consumer read a list of `success: false` with no way to tell a broken join from an unusual-but-accepted row count. It is DERIVED from `STRUCTURAL_ASSERTIONS` ("findings NO approval can wave through", already enforced at the medallion's review and the catalog's publish door) rather than passed, so the wire and the gate cannot disagree about which failures are blocking; a structural finding cannot be downgraded even by an explicit argument. Pinned by `tests/unit/test_a_quality_assertion_says_how_bad_it_is.py`, parametrised over the SET so a new structural assertion becomes `error` on the wire by being added there and nowhere else. **OBSERVED on the deployed catalog:** `POST /management/v1/table/{id}/publish` with `gate_only` answers `not_null severity='error'` beside `row_count_positive` and both `column_declared` at `'warn'` — the mapping on the wire an external writer actually reads.
- **A FALSIFIED CLAIM ABOUT FACET DIRECTION, and the code followed the wrong one.** `medallion/schemas/events.py` said "dataQualityAssertions is an OUTPUT facet"; it is an INPUT facet (`openlineage.client.generated.data_quality_assertions_dataset` subclasses `InputDatasetFacet`), which `lineage/models.py:73` already states correctly two files away. The placement itself is deliberate and now says so: the spec has no output-side quality facet, the assertions are about the dataset this run WROTE, and `outputFacets` is typed for `OutputDatasetFacet` subclasses — so it rides the plain `facets` slot, which `Dataset.facet` reads along with the other two.
- **BOTH PLUMBING FACETS SHIPPED 2026-09-19.** `CatalogDatasetFacet` is filled from what the catalog already knows — `framework` the constant `lance` (the format is closed by ruling, so a configurable value would advertise flexibility that does not exist), `type` the Lance Namespace impl (`dir` live), `name` REUSING `lineage_job_namespace` so the facet and the events it rides cannot disagree about who is speaking, `warehouseUri` the catalog root. `metadataUri` is deliberately ABSENT and the omission carries the architecture: the spec's example is a JDBC string because Iceberg-style catalogs hold the commit pointer in a database, while Lance puts the CAS in the object store — there is no metadata endpoint to name. An empty `type` or `name` renders NO facet, because those are the fields a consumer joins on and a blank one merges every unnamed catalog into a single node. `DatasetTypeDatasetFacet` is `TABLE` unconditionally on a catalog write (this builder serves catalog writes, and the catalog writes governed Lance tables and nothing else); `FILE` is available for an external source ref. No `subType`: the spec's examples describe properties of a TABLE this estate does not have. Threaded through the emitter as a class-level default the way `_project_resolver` already is, so a hand-constructed emitter renders no facet rather than claiming an unnamed catalog. **OBSERVED on the deployed catalog**, read back off the live `/events` feed after a create: `catalog = {"name": "lance-catalog", "type": "dir", "framework": "lance", "warehouseUri": "s3://lance-catalog"}` with no `metadataUri`, beside `datasetType = {"datasetType": "TABLE"}` — `type` and `warehouseUri` matching the pod's own `LANCE_REST_IMPL` / `LANCE_REST_ROOT`.
- *What is left:* **one facet and one ruling.** `NominalTimeRunFacet` is the one that genuinely needs a ruling: what logical window a cascade run covers. **`DataQualityMetricsInputDatasetFacet` leaves this row** — the estate computes its numbers (`assert_quality` runs `count_rows()` and a null count, then keeps only the booleans) but it is an INPUT facet and those numbers describe an OUTPUT, so filling it would need the metrics carried to the next stage's input. That is a different change from emitting a facet. `SQLJobFacet` has no subject until a query engine lands. Emitting a facet the estate cannot fill truthfully is worse than omitting it.
- *Closes when:* Each remaining applicable facet is emitted with a real value or recorded as deliberately omitted with its reason.
- *Evidence:* `packages/service-kit/src/service_kit/openlineage.py (lifecycle_facet, processing_engine_facet)` · `tests/unit/test_a_standard_consumer_can_read_what_a_write_did.py` · upstream facet versions read via the GitHub API 2026-09-19 · `gh api repos/OpenLineage/OpenLineage/contents/spec/facets` (~30 published)

**LIN-004 · Every DDL change is emitted as a RunEvent, so half the Job nodes in the graph are jobs that never ran**
`catalog, lineage` · **MED**
- **blocked:** Owner call — the fix changes the graph's node population and the `/jobs` ACCESS surface, which is bigger than it looks. Either DDL moves to `DatasetEvent` (and the phantom Jobs stop being created, leaving the existing ones to migrate or age out), or emitting DDL as a run is recorded as deliberate with its reason.
- *What is left:* The OpenLineage spec defines FOUR event types — `BaseEvent`, `RunEvent`, `DatasetEvent`, `JobEvent` (verified in `OpenLineage/OpenLineage/spec/OpenLineage.json`, 2026-09-19). rask emits only `RunEvent`, so `build_write_event` wraps a DDL change in a synthetic run: `eventType: COMPLETE` with a Run that never executed and a Job that never ran. `DatasetEvent` exists for exactly this — "a dataset change outside any job (e.g. a DDL schema change)". **MEASURED on the live feed over 500 events: 168 DDL events producing 144 Job nodes, against 146 from real runs — half the Job population represents no job.** It scales with the TABLE count rather than with work, because the job name is per-table-per-operation (`lance-catalog/add_columns.lh019before299c33ns$t1`). It is not only tidiness: the `/jobs` governance fold makes a Job's output set its access handle, so each phantom is an access-control object for an operation nobody performed.
- *Closes when:* A DDL change emits `DatasetEvent` (or the RunEvent choice is recorded with its reason), and the phantom Job count stops growing with the table count.
- *Evidence:* `services/catalog/src/catalog/core/lineage_emit.py:305 ("eventType": "COMPLETE" for every operation)` · `gh api repos/OpenLineage/OpenLineage/contents/spec/OpenLineage.json → BaseEvent, DatasetEvent, JobEvent, RunEvent` · live feed 2026-09-19: 168 DDL events, 144 DDL-only Job nodes, 146 run Job nodes · https://opendatalakehouse.com/kb/data-lineage/

**LH-178 · An erasure cannot complete while a branch pins the version holding the subject, and deleting that branch destroys someone's work**
`catalog` · **HIGH**
- **blocked:** Owner call — when a GDPR erasure meets a branch, does the estate DELETE the branch (completing the erasure, destroying a working ref someone may be mid-change on) or leave it and report the erasure INCOMPLETE? There is no third answer: the pin is what keeps the version reclaimable, and the version is what still holds the row.
- *What is left:* Measured while building `catalog.services.erasure` (2026-09-19, pylance 11.0.0): deleting rows ON a branch does NOT remove that branch's pin on the parent's history. The branch then reads clean, `cleanup_old_versions` still reports `old_versions=0`, and the parent's pre-delete version survives holding the subject — so every step of the erasure reports success and the row stays readable at `checkout_version(N)`. Deleting the branch DOES remove the pin: same fixture, `branches.delete('work')` then cleanup, and the version is gone. `erase()` already refuses to claim completion here — it verifies against every retained version and sets `complete=False` with the residual listed — so the estate cannot report a false erasure today; what it cannot do is finish one. Whichever way this is ruled, the other half needs saying too: if branches are deleted, the owner of that branch has to be told (the notifications delete-subject door this row's parent [[LH-073]] names), and if they are not, the incomplete erasure has to reach a human rather than a log line.
- **THE BRANCH HALF IS RULED (2026-09-21) — see [[LH-055]].** lance-ns defines no branch resource (three
  table-scoped ops in `spec.yaml`), and the format brief puts branch isolation at the STORAGE PREFIX:
  "storage ACLs can be read-only on main and write-only on the branch". So no `branch` FGA type; the
  work is a branch-aware vended prefix (`<table>/tree/<b>/*`), which the credentials door cannot express
  today because it takes no branch parameter.
- *Closes when:* The ruling is recorded in `docs/DECISIONS.md` and `erase()` implements it — either deleting pinning branches and announcing it, or reporting the incompleteness to a person.
- *Evidence:* `services/catalog/src/catalog/services/erasure.py (steps 1-5, the verify pass)` · `services/catalog/tests/test_erasure_reaches_every_surface.py::test_a_version_a_BRANCH_PINS_still_answers_and_the_report_says_so` · `::test_reclamation_is_a_NO_OP_when_a_branch_still_pins_the_history`

**LH-016 · `silver-media$features` still occupies a medallion namespace in `lakehouse-wh` under two spellings, and the unbind door refuses a non-empty namespace**
`catalog` · **HIGH**
- **THE PRODUCER IS ALREADY GONE, so this residue is CLOSED-ENDED (verified 2026-09-20).** The nested spelling came from the ingest plane composing `f"{project}${dataset}"` while the medallion used `f"{project}-{name}"` — recorded in `warehouse_registry.project_namespace`'s own docstring, which also states the repair: "a naming convention that two services must agree on cannot live inside one of them", so the helper moved into service-kit. Verified rather than taken on trust: the only matches for the old `$` composition anywhere in `services/`, `packages/` and `scripts/` are the two COMMENTS describing it, and six modules across ingest, lineage and medallion now call the shared helper. **So nothing is still producing these**, and the decision this row waits on is about cleaning a fixed-size set rather than stopping a leak.
- **THREE ROWS, ONE SHAPE — and it is the shape of the phase-1 remainder.** [[LH-102]]'s 869 orphaned trash records came from warehouse deletes (fixed at source 2026-09-20), [[LH-148]]'s 121-of-126 provenance gaps are the same e2e fixtures but NOT the same mechanism — corrected below — and this row's duplicates came from the naming divergence above (fixed earlier). All three producers are closed; all three residues remain; and all three need the same thing to clear — a write-capable reconcile, which is [[LH-061]]. The lakehouse is not accumulating these, it is carrying them.
- **THE DOUBLE-SPELLING IS BROADER THAN THIS ROW NAMES — measured on the live sweep 2026-09-20.** The row names `silver-media$features` under two spellings and both are there (`silver-media$features`, `lakehouse$silver-media$features`). But `lakehouse-wh` also holds **`lakehouse$silver$features` AND `lakehouse-silver$features`** — distinct datasets with distinct hash prefixes (`dd923b95_` against `03505f8f_`), so it is two logical tables each duplicated rather than one. `lakehouse-gold$catalog` carries only the project-prefix form, which is what a consistent estate looks like. **The two conventions are NESTED-NAMESPACE (`lakehouse$silver$features`) against PROJECT-PREFIX (`lakehouse-silver$features`), and the medallion's own `<project>-<tier>` naming is the second** — so the nested copies are the odd ones out. This also surfaced through [[LH-148]]: `lakehouse$bronze$pages` and `lakehouse-bronze$pages` BOTH appear in `unknown_to_graph`, which is the same pair seen from the lineage side. Worth knowing before the drop-or-relocate call, because it doubles what that call covers.
- **blocked:** Owner decision: drop or relocate `silver-media$features` (both spellings, `a76d1ca5_silver-media$features/` and `fa8bff0d_lakehouse$silver-media$features/`) — destructive on a real table, and no service identity holds `project:lakehouse#can_administer` by design.
- *What is left:* Take the drop-or-relocate decision for `silver-media$features` in `lakehouse-wh`. Then a human bearer holding `project:lakehouse#can_administer` calls `DELETE /v1/warehouses/{id}/namespaces/silver-media`, which answers 409 `NamespaceNotEmptyError` until the table is gone. `bronze-media` has no object left in the bucket and needs no decision. The live store is not re-measured this session (no cluster access); the register's 2026-09-16 conditions table still lists `lakehouse$silver-media` among live composed paths.
- *Closes when:* `lakehouse-wh` lists no `silver-media` namespace and the unbind door answers 200 for it.
- *Evidence:* `services/catalog/src/catalog/api/v1/endpoints/warehouses.py:624-685 (unbind door, `NamespaceNotEmptyError` refusal)` · `open_backlog_left.md:222 (2026-09-16 live measurement names `lakehouse$silver-media`)`

**LH-055 · The FGA model has no branch/column/base/estate type, can_set_protection collapses onto can_drop, and project has no security_admin/data_admin/role_creator split or machine identity**
`catalog, service-kit, openfga` · **HIGH**
- **THE PROJECT ROLE SPLIT IS IN (2026-09-20), and it is ADDITIVE by construction.** `type project` now declares `security_admin` (grants and ownership without the power to read or change the data it governs), `data_admin` (warehouse work without the power to hand out access) and `role_creator` (minting a role is not the authority to decide who is IN one, so it sits under security_admin). Each is `[user, role#assignee] or admin`, so `admin` reaches every rung and NOBODY loses one by their arrival — which is what made it safe to land without the rest of the model decision. 51/51 model tests, 361/361 checks, `model.fga`/`.fga.yaml`/`model.json` in sync. The test asserts BOTH halves and the second is the one worth having: `admin` reaches all three, and a security_admin-only subject does NOT reach `data_admin` — mutation-checked by making it imply, which reds 50/51. **OBSERVED on the deployed catalog (`lance-rest-catalog:lh055-split`).**
- *Still owner calls, and each now has a stated reason rather than a blank:* the **branch type** (Lakekeeper has none and cannot be borrowed from — `lance_docs/file_format.md` `branch_tag.md` defines a branch as a shallow clone, so what a branch-scoped grant MEANS follows Lance); the **column vocabulary** ([[LH-058]], reframed — the precedent models no column TYPE, so the question is which classification values exist); the **estate root** (moving `can_observe_events`/`can_browse_storage` off warehouse is a tuple MIGRATION, not an additive edit, and `fga_root_object` must be repointed with it); and **`can_set_protection`** — left alone deliberately: the current collapse onto `can_drop` is a RECORDED design (`fga_deps.py:239-243`, a writer must not disarm what they cannot act on), and splitting it decides who may disarm, which if granted to nobody makes every protected object permanently stuck.
- **THE CLOSEST COMPARABLE HAS ALREADY SOLVED THIS, AND IT RUNS ON OPENFGA TOO — read 2026-09-20 from `lakekeeper/lakekeeper` `authz/openfga/v3.4/components/`.** Every piece this row asks the owner to invent exists there as shipped model: **an estate root** (`type server` with `define can_create_project: admin or operator`, the root this row wants `can_observe_events`/`can_browse_storage` moved onto); **a human/machine split** (`admin` "designed for human users" beside `operator` "designed to be used by machines that provision resources", reaching into a tenant via `project_admin: [user, role#assignee] or operator from server`); **the project role split verbatim** — `project_admin` (with stated lock-out protection, "checked to never be empty"), `security_admin` ("manage all security aspects (grants, ownership) but not modify, create or access objects"), `data_admin` ("manage all warehouse aspects but not grant privileges"), `role_creator` ("can create new roles — cannot add assignees to existing roles"); **the role→project edge [[LH-062]] proposes** (`type role` declares `define project: [project]`); and **NO column type at all** — there is no `column.fga`, because column governance is DATA there: tags keyed on FIELD-ID (stable across a rename, where a name is not), served from `/management/v1/warehouse/{id}/table/{id}/column-tags`, gated by table-level metadata access, and explicitly "columns do not inherit". **LANCE_DOCS OUTRANKS THIS, ALWAYS (owner, 2026-09-20).** Lakekeeper is Iceberg-world and is a reference only where Lance has NO opinion — checked before using it: `role`, `grant` and `tenant` appear ZERO times in `lance_docs/namespace.md` and `lance_docs/file_format.md`, so the grant/tenant model is rask's own and nothing upstream competes. What lance-ns DOES own is the error vocabulary a refusal must speak (13 InvalidInput, 15 PermissionDenied, `namespace.md:1678`), and **anything Lance itself defines — branches, tags, field ids, multi-base — follows `lance_docs` and not this precedent**. That bounds the borrowing: the project role split and the estate/machine identity are pure authz and may be adopted; a BRANCH type may not be, because Lakekeeper has no branches and Lance's own `branch_tag.md` defines what one IS (a shallow clone). Each deviation should name what it buys; each ADOPTION should name that Lance was silent on it.
- **blocked:** Owner decision on the model shape: what a branch-scoped grant means when a branch is a whole parallel dataset; the column classification vocabulary (LH-058); the security_admin/data_admin/role_creator split and a machine identity; an `estate` type versus documenting warehouse-as-root; and whether can_set_protection splits from can_drop (a recorded design at fga_deps.py:209-213: disarming protection clears the drop bar).
- *What is left:* With the ruling in hand, edit model.fga once: add a can_set_protection rung and remap it out of _OWNER_SUFFIX_RELATION (fga_deps.py:158,237); add the project role split plus a machine/operator identity; add the branch type, a column-policy relation, and an estate root carrying can_create_project, moving can_observe_events/can_browse_storage (model.fga:244,271) off warehouse onto it and repointing fga_root_object (catalog/core/config.py:320) with its one seeded tuple. Add .fga.yaml cases and _CHILD_EDGE_PARENT_TYPES entries (service_kit/governed/fga.py) for each new type. Coordinate with the role->project edge so the model changes once.
- **RULED 2026-09-21 (owner: "idiomatic to lance-ns and the lance table format"), AND THE SPEC ANSWERS IT
  DIFFERENTLY FROM BOTH OPTIONS I OFFERED.** Read rather than reasoned, from the three sources the owner
  named:
  * **`lance_docs/ns_catalog/spec.yaml` defines exactly THREE branch operations** — `ListTableBranches`,
    `CreateTableBranch`, `DeleteTableBranch` — all under `/v1/table/{id}/branches/*`. lance-ns has **no
    branch-level resource**: a branch operation is an operation ON A TABLE. So authorizing branch
    create/list/delete against the TABLE is the idiomatic shape, and this estate already does it
    (`branches/create` -> `can_create_branch` = owner, `branches/delete` -> `can_drop`).
    **A `branch` FGA TYPE IS THEREFORE NOT THE ANSWER** — it would invent a resource the namespace spec
    does not have, which is the "never Iceberg" failure in a different costume.
  * **`lancemultibasebranchingblobv2.md` § "Building block 3" states the isolation mechanism verbatim:**
    branch data lives physically under `tree/<branch>/`, giving "**strong governance isolation** (branch
    data physically under `tree/<branch>/`, so storage ACLs can be **read-only on main and write-only on
    the branch**)". The boundary is real, and it is enforced at the STORAGE PREFIX — not in the
    authorization model.
  **SO THE GAP IS IN VENDING, AND IT IS MEASURED (2026-09-21):** `build_session_policy(bucket, prefix,
  tier, bases)` already scopes to one prefix, but the credentials door takes **no `branch` parameter at
  all**, so every vended write credential is scoped to `<table-prefix>/*` — which CONTAINS every
  `tree/<b>/`. A table writer's credential therefore grants write to every branch, which is the exact
  posture the format's design says the layout exists to prevent.
  **WHAT THIS RETIRES:** the "add `type branch`" clause, and with it the branch half of the model-shape
  question. What replaces it is smaller and spec-cited: the vend door accepts a branch, and a
  branch-scoped write vends write on `<table>/tree/<branch>/*` with main granted READ only — literally
  "read-only on main and write-only on the branch".
- **"THE BRANCH QUESTION GATES SEVEN ROWS" WAS MINE AND IT WAS WRONG (corrected 2026-09-21).** I put
  that to the owner when asking for the ruling. Read back, the sibling markers ask different questions
  entirely: [[LH-041]] is tag-MOVE semantics (last-writer-wins vs a conditional `Tags::update`
  upstream), [[LH-056]] is who a tag/branch control event TARGETS, [[LH-099]] is whether a compaction
  should tell anybody, [[LH-178]] is what a GDPR erasure does when it meets a branch, and [[LH-058]] is
  the `column` relation and classified-table vending. Only THIS row carried the branch-authz question,
  and it carries four others besides.
  **THE RULING STILL EARNED ITS KEEP, which is why this is a correction and not a retraction:** it
  turned into shipped code — branch-scoped credential vending, closing a real gap where any table
  writer's credential could write every branch. The error was in the ROW COUNT I used to justify
  asking, not in the answer.
  **AND [[LH-178]] WAS CHECKED AGAINST THE SPEC BEFORE BEING LEFT BLOCKED.** The branching brief
  mentions GDPR twice and neither passage rules on erasing a branch: one argues branches beat scattered
  clones ("no GDPR compliance break from a forgotten clone"), the other is about external blob refs
  breaking row lifecycle. So that row is a genuine owner call, not one the spec could have answered.
- **THE BRANCH-VENDING HALF IS DEPLOYED AND OBSERVED (2026-09-21).** Driven against the live catalog:
  a main write vend and a branch write vend both answer **200 with DISTINCT credentials** (different
  access key ids, so the branch really did produce its own scoped session), and a climbing branch name
  now answers **400 `invalidinputerror` code 13** — "branch '../escape' may not traverse out of the
  table's prefix" — where before the fix it was a bare **500 InternalError** telling the caller nothing.
  Driving it live is what found that: the guard was correct and its answer was not, which no unit test
  was ever going to show.
- *Closes when:* model.fga declares branch, column and estate types and a project role split, every new rung has a .fga.yaml case, and fga_root_object names the estate object.
- *Evidence:* `packages/service-kit/src/service_kit/governed/auth/model.fga:41-530 (ten types: no branch/column/estate)` · `packages/service-kit/src/service_kit/governed/auth/model.fga:55-88 (project: team/admin/member only)` · `services/catalog/src/catalog/api/fga_deps.py:158,237 ('protection': 'can_drop')` · `services/catalog/src/catalog/core/config.py:320 (fga_root_object)`

**LH-056 · Branch-scoped governance is missing: no FGA branch type, canonical_object_id and vending are branch-blind, protection/trash have no per-branch record, and tag/branch creation emits no control event**
`catalog, lineage, notifications` · **HIGH** · PARTIAL
- **blocked:** Who is TARGETED by a tag/branch control event (an event naming nobody is undeliverable) — and the R5 half of this marker was spurious, removed 2026-09-20. R5 is an ACCEPTED owner ruling from 2026-07-24 (`docs/architecture/lance-ns-merge.md:435`, re-affirmed "R1–R11 STAND" in `f1dc8d96`), and it says "whole-plane media namespace — `/api/media/{,search,annotations}`, all three SPAs' fetch bases rewritten", which has no bearing on branch-scoped governance. See `docs/DECISIONS.md` § The `R#` rulings.
- *What is left:* Add `type branch { parent: [table]; reader/writer; can_write_data }` to model.fga with .fga.yaml cases (today the only branch rung is can_create_branch: owner at model.fga:434). Make the FGA object chosen in authorize branch-aware, since canonical_object_id joins path segments only and the object is `table:<ns>$<table>` whatever branch the request names. Scope vended STS prefixes to `tree/<b>/` in catalog/core/vending.py, add per-branch protection and trash records, and emit parent_branch/parent_version as lineage facets (they exist only as list_branches response fields, dataplane.py:1892-1893). Add tag and branch values to ControlAction in service_kit/control_events.py:36 (45 values, none for tag or branch) and regenerate docs/catalog-openapi.json plus the TS client. The stats/index body clause and the branches/delete rung are shipped.
- **THE BRANCH HALF IS RULED (2026-09-21) — see [[LH-055]].** lance-ns defines no branch resource (three
  table-scoped ops in `spec.yaml`), and the format brief puts branch isolation at the STORAGE PREFIX:
  "storage ACLs can be read-only on main and write-only on the branch". So no `branch` FGA type; the
  work is a branch-aware vended prefix (`<table>/tree/<b>/*`), which the credentials door cannot express
  today because it takes no branch parameter.
- *Closes when:* A can_write_data holder on main cannot write another branch, tags/create and branches/create emit a targeted control event, and a vended write credential cannot reach a sibling branch prefix.
- *Evidence:* `packages/service-kit/src/service_kit/governed/auth/model.fga:434 (can_create_branch: owner; no type branch)` · `services/catalog/src/catalog/api/fga_deps.py:164,175 (branches/create -> can_create_branch, branches/delete -> can_drop)` · `services/catalog/src/catalog/api/v1/endpoints/tables.py:1188-1205 and indices.py:142-157 (branch read from body)` · `packages/service-kit/src/service_kit/control_events.py:36 (ControlAction: no tag/branch value)`

**LH-058 · No column-level classification exists: `model.fga` has ten types and no `column` relation, and credential vending bypasses any query-door masking**
`catalog, lineage, openfga` · **HIGH** · **REWRITTEN — the original ask would be wrong**
- **BOUNDED BY THE LAKEKEEPER PRECEDENT (see [[LH-055]], read 2026-09-20):** the closest comparable on the same authz stack models NO `column` type at all — column governance is tags keyed on FIELD-ID under `/management/v1/.../column-tags`, gated at table level, and columns do not inherit. So "how does a `column` relation join the model" may be the wrong question.
- **blocked:** The FGA model-shape decision (whether a `column` relation joins the model and how classified tables constrain credential vending)
- *What is left:* The gap stands: `model.fga` declares user/team/role/project/warehouse/namespace/table/materialized_view/transaction/annotation_project and nothing column-shaped, and no classification field exists. `columns.py` IS gated at table level via the router-wide `authorize` (`api/v1/router.py:47`, writer tier), so the title's 'no FGA check' is only true per column. Do not implement 'masking on query' as written: `credentials` is a data-read action (`fga_deps.py:88`) that vends a whole-prefix S3 session, so a reader gets raw bytes without passing any query door. Once the model shape is ruled, put classification on column metadata, add the relation, and enforce at the credential-vending door (refuse or narrow the session for tables carrying classified columns) rather than at `query`.
- **THE BRANCH HALF IS RULED (2026-09-21) — see [[LH-055]].** lance-ns defines no branch resource (three
  table-scoped ops in `spec.yaml`), and the format brief puts branch isolation at the STORAGE PREFIX:
  "storage ACLs can be read-only on main and write-only on the branch". So no `branch` FGA type; the
  work is a branch-aware vended prefix (`<table>/tree/<b>/*`), which the credentials door cannot express
  today because it takes no branch parameter.
- *Closes when:* A classified column cannot be read raw through `credentials` by a subject lacking the column rung, pinned by a test.
- *Evidence:* `packages/service-kit/src/service_kit/governed/auth/model.fga:41-530 (ten types, no column)` · `services/catalog/src/catalog/api/v1/router.py:47 (router-wide authorize)` · `services/catalog/src/catalog/api/fga_deps.py:88 (credentials in _DATA_READ_ACTIONS)`

**LH-141 · A stale `lineage.dataset_id` stamp or a relative Dataset `source_uri` is repaired only by a write that never comes — the guard refuses the crossing each tick but nothing corrects it**
`medallion, maintenance, lineage, catalog` · **HIGH** · PARTIAL
- **blocked:** Owner ruling: may the catalog re-assert a dataset's location/id through a lineage event no run produced (a synthetic assertion restamping via the existing `SET_DATASET_SRC` path), or must lineage instead gain a catalog client and accept a catalog↔lineage cycle? LH-146 closed by exempting last-writer datasets from retention and never ruled on synthetic assertions.
- **RE-MEASURED 2026-09-22 on demand, because this row said its own counts were not** (lineage
  reconcile triggered directly: `POST /lineage-reconcile-cron`, port-forwarded, `dapr-api-token`):
  `checked=483 unreadable=23 storage_loss=1 provenance_holes=0 unknown_to_graph=0
  contract_violations=0`. **All 23 unreadable are the relative-`source_uri` kind** — every entry
  carries "names no storage location". Against this row's stated 60 relative nodes and 24 reaching the
  sweep as MISSING_ON_STORAGE, the population is **60 -> 23** and the storage-loss tail is **24 -> 1**.
- **THE LIVE `storage_loss=1` IS THIS ROW TOO — it is `bronze$events`**, already named in the restamp
  list below. So it is not a separate defect and needs no separate row: the same blocked ruling covers
  it. Whether its bytes went with a bucket reap or were never at the composed `medallion/<tier>`
  location the graph records, the repair is identical — correct the stamp, and the sweep stops
  reporting it.
- **AND THE SOURCE IS CLOSED, which changes what the blocked ruling is FOR** ([[LH-187]]). The door
  that emitted a relative `source_uri` was `register_table` — the one door taking a CALLER-supplied
  location — and it already resolves before emitting (`tables.py:805`, `absolute_table_location`).
  `declare_table`, `rename_table` and `create_table` pass a catalog-MINTED location and were never
  exposed. Attribution confirms it: the producer of a live offender is
  `catalog/core/lineage_emit.py` at `event_time 2026-09-06`, i.e. before the fix. So the 23 are a
  CLOSED, SHRINKING population of historical rows, not a leak — the ruling decides how to repair 23
  known nodes, not how to stop an ongoing one.
- *What is left:* The guard is shipped and pinned (9 tests pass in `tests/unit/test_the_sweep_vends_for_the_dataset_it_is_holding.py` + `services/maintenance/tests/test_a_vended_credential_must_cover_the_dataset_it_signs.py`). Only the repair remains. (a) Rewrite the relative `source_uri` on the 60 Dataset nodes: 58 are governed and resolvable through the catalog, 2 hold no tuple and are removals, 24 reach the sweep every tick as MISSING_ON_STORAGE. (b) Restamp the composed `medallion/<tier>` datasets the guard now refuses by name (`s3://bind86-wh/medallion/silver`→`bronze$events`, `s3://lance-catalog/medallion/gold`→`bronze$events`, `s3://lance-catalog/medallion/silver`→`silver$features`, and the flat `s3://acme-bucket/4750a5b9_acme-bronze$events`); `ensure_declared_dataset_id` runs only from compute.py write paths (251, 368, 392) and `table_id_from_location` and lineage's durable feed cannot supply the value. Implement option 1 or 3 per the ruling; lineage holds no catalog client today. Pin that a corrected dataset keeps its `_rowid`s (`update_schema_metadata` is metadata-only) and diagnose the 94-count `lakehouse-bronze$events` (names no catalog table) under LH-164, not here. Live counts are from the row, not re-measured this session.
- *Closes when:* No Dataset node carries a relative `source_uri` except the two ungoverned removals, and the sweep's location-mismatch refusal fires zero times across a full tick.
- *Evidence:* `uv run pytest tests/unit/test_the_sweep_vends_for_the_dataset_it_is_holding.py services/maintenance/tests/test_a_vended_credential_must_cover_the_dataset_it_signs.py -q → 9 passed` · `services/medallion/src/medallion/services/compute.py:251,368,392` · `services/lineage/src/lineage/services/cypher.py:212 + repository.py:410` · `rg -n 'describe_table|catalog_url' services/lineage/src → no hits`

**LH-064 · The lineage bus door trusts the producer-stamped `author.sub` with no signature over the CloudEvent**
`lineage, lineage-kit, chart` · **MED** · PARTIAL
- **blocked:** [[ZT-001]]'s deployment-policy call. This row's own *Closes when* requires that "the signing key is not derivable from `dapr.appToken`", and that is precisely what ZT-001 must deliver — it is blocked on the owner choosing between prod values that enable ESO and a render that FAILS without supplied material. Re-measured 2026-09-20 and all three premises hold: no signature or HMAC verification exists in `services/lineage/src` or `packages/lineage-kit/src`, `fga_deps.py:263-264` still states the gap in `_StampedAuthor` ("nothing proves the stamp"), and `test_a_dedicated_service_token_is_not_derivable_from_the_shared_one.py` still passes — which pins the tokens as STILL derivable, since that file is deleted rather than inverted when ZT-001 lands. So the work is not merely weaker before ZT-001, it cannot meet its own closing bar: an HMAC keyed on today's material refuses an unauthenticated forger but not any of the 13 pods holding the shared token, and shipping it would read as non-repudiation without being it.
- *What is left:* Add a transport-independent producer signature over the CloudEvent and verify it in the bus door (`on_lineage_event` / `enforce_bus_authz`), with the signing seam in `packages/lineage-kit` so it survives a Dapr retreat; `_StampedAuthor` states the gap ("nothing proves the stamp"). Do NOT add a Dapr `accessControl` block — it governs service invocation and never sees pub/sub delivery. Do NOT extend `protectedTopics`/`publishingScopes`/`subscriptionScopes` to the seven producer components without first enumerating every topic each app uses in BOTH directions off `/v1.0/metadata`: `subscriptionScopes` is a complete allowlist, not additive, and a partial one stops delivery. Already shipped and not to redo: subject stamped through `enforce_output_authz`; the notifications-only scopes on `lineage-pubsub-notifications`; document-level `scopes:` closing each component to one app-id.
- **ITS STRENGTH IS CAPPED BY [[ZT-001]], and that should be settled first.** A producer signature needs
  a KEY, and every key a producer holds today is derivable from `dapr.appToken`: the shared app token
  itself, and the per-identity `service-token-<identity>` which `lance.dedicatedServiceToken` computes
  as `sha256("<identity>-<dapr.appToken>")[:40]` (measured: 5 of 5 privileged identities). So an HMAC
  keyed on today's material would refuse an UNAUTHENTICATED forger — real value — and would not refuse
  any of the 13 pods holding the shared token, which is the population that can already stamp a
  neighbour's subject. Shipping it before ZT-001 produces something that reads as non-repudiation and
  is not; shipping it after, with independent per-producer material, is the control this row describes.
  *What is NOT capped:* `_StampedAuthor` already bounds the forgery — a forged subject must still hold
  the rung on every output — so the gap is narrower than "anyone can claim anyone".
- *Closes when:* A bus event whose signature does not verify is refused at `/lineage-events`, pinned by a unit test, with no `accessControl` in the tree — and the signing key is not derivable from `dapr.appToken`.
- *Evidence:* `services/lineage/src/lineage/api/fga_deps.py:237-275 (`_StampedAuthor` "nothing proves the stamp"; `enforce_bus_authz` delegates to `enforce_output_authz`)` · `grep -rniE 'signature|hmac' services/lineage/src packages/lineage-kit/src → prose only, no verification code` · `chart/templates/dapr-component.yaml:204-245 (scopes only on notifications; comment records the non-additive breakage)` · `tests/unit/test_the_inbox_may_read_the_provenance_bus_but_never_write_it.py (exists)`

**LH-074 · Storage is accounted per bucket; quota ENFORCEMENT has no limit source yet**
`catalog, maintenance` · **MED** · PARTIAL
- **blocked:** Owner choice of USAGE SOURCE for the refusal — this row's own text already says "Neither is plumbing", and the two shapes have genuinely different costs. (1) Maintenance writes a last-known usage onto the warehouse record: cheap at the write door, and the refusal is exactly as stale as the last reconcile tick, so a tenant can overshoot by a tick's worth of writes. (2) The catalog lists the bucket per write: always current, and an unbounded S3 listing on the hot path — the very cost the periodic sweep exists to avoid. A third the row never names is incremental accounting (add on write, subtract on delete), which is current and cheap and introduces a counter that drifts from the bytes the moment anything writes outside the door. Re-measured 2026-09-20: [[LH-061]]'s write-capability ruling HAS landed, so shape (1) is no longer blocked on that — maintenance already writes registry records (`purge.py` deletes trash ones) — and what remains is the trade-off itself, which is the owner's. The LIMIT half is answered and needs nothing: a warehouse-scoped sub-resource under the management prefix, absent = unlimited.
- **THE LIMIT SOURCE IS ANSWERED AND ENFORCEMENT IS STILL BLOCKED — by a USAGE source, which this row does not name (measured 2026-09-20).** A refusal at a write door needs two numbers, not one: the limit (answered — a warehouse-scoped sub-resource under the management prefix) and the CURRENT BYTES at the moment of the write. The estate has no path to the second. Accounting lives in MAINTENANCE's periodic reconcile; the catalog's `warehouses.py` holds no usage field and no reader; and maintenance never writes to the warehouse registry at all — it is report-only, which is exactly [[LH-061]]'s deferred write-capability question. So the two available shapes are: maintenance writes a last-known usage back onto the warehouse record (needs LH-061's ruling, and makes the refusal as stale as the last tick), or the catalog lists the bucket per write (an unbounded S3 listing on the hot path, which is the cost the sweep exists to avoid). Neither is plumbing. **This row is therefore downstream of [[LH-061]], not merely of a configuration choice.**
- **THE ROW'S APPROACH MEASURED THE WRONG BYTES, corrected 2026-09-20.** It said to roll up `size_bytes` from `orphans.py` — but that field is on `OrphanFile`, so the roll-up would have totalled UNREFERENCED bytes, not storage. The listing one level up (`get_file_info(FileSelector(prefix, recursive=True))`) already reads EVERY file with its size — that is how orphans are found at all — and the loop discards the referenced ones. Summing the same pass costs no extra I/O.
- **ACCOUNTING SHIPPED AND OBSERVED.** `DatasetOrphanScan` carries `total_bytes`/`total_files`, `OrphanReport` carries them per dataset, and the reconcile report rolls them up per bucket. Unreadable and excluded datasets are ABSENT rather than zero, so a partly-seen estate cannot report as a small one. Observed on the deployed estate: `bytes_by_bucket={'lance-catalog': 3706801, 'acme-bucket': 437886, 'bind86-wh': 181173, 'lakehouse-wh': 59274, …}` across 570 datasets, largest-first and capped at 10 keys on the log line.
- **NEITHER COMPARABLE CATALOG DEFINES A QUOTA — measured 2026-09-20, so there is nothing to be idiomatic to.** `quota`, `max_bytes`, `storage limit` and `capacity` appear ZERO times in the vendored lance-namespace spec and docs, and ZERO times in Lakekeeper's 17,677-line management OpenAPI. rask would be INVENTING the concept rather than adopting one, which is worth stating before building it. What IS established, and agrees across both, is the SHAPE of per-warehouse policy: Lakekeeper carries warehouse settings as sub-resources (`/management/v1/warehouse/{id}/delete-profile`, `.../protection`), and rask already has that exact shape for protection on the warehouse, namespace and table tiers — now at `/management/v1` from [[LH-021]]'s route work.
- *What is left:* **enforcement.** Owner answer 2026-09-20: the quota belongs on the WAREHOUSE, as a warehouse-scoped sub-resource under the management prefix, absent by default (absent = unlimited, so adding the mechanism changes nothing until someone sets a value) and never a field in a spec create payload. Accounting is already per BUCKET and a warehouse maps to a bucket, so the limit sits where the measurement already lands. Recorded in `docs/DECISIONS.md`. The `by_root` symbol the row's other claim referenced does not exist anywhere.
- *Closes when:* a byte quota is enforced at the create/write doors with a typed refusal.
- *Evidence:* `services/maintenance/src/maintenance/services/orphans.py (total_bytes, bytes_by_dataset)` · `services/maintenance/src/maintenance/services/reconcile.py (_roll_up, bytes_by_bucket)` · observed on the deployed maintenance pod 2026-09-20
**LH-144 · Three live datasets carry no FGA tuples and no drop run, and a lost drop-event emit pages nobody**
`catalog, lineage` · **MED** · PARTIAL
- **blocked:** two POLICY rulings — and the blocker this marker used to name is stale, which mattered because it pointed at work already delivered. It read "downstream of [[LH-061]]'s standing deferral ('No — not yet' on a write-capable reconcile)", and that deferral was OVERTURNED (owner, 2026-09-19): the additive tuple rebuild shipped that day and the repair pass shipped 2026-09-20, dry-run observed. So the door exists. What still does not is permission to walk through it, in either direction. **(1) Governing an unowned table to a named subject is a privilege-escalation path** — this row's own text says so, `seed_ownership` cannot stand in (it grants the CREATOR `owner` and an orphaned table has no creator), and `repair.py` refuses `ungoverned_tables` by name because a real table with real bytes must never be resolved by deleting it. **(2) Recording a synthetic drop instead** — so `repository.dropped_at` stops naming them — is a catalog-asserted lineage event no run produced, which is exactly [[LH-141]]'s open ruling. Measured 2026-09-19 and unchanged: an ungoverned table answers `403 can_get_metadata required` to EVERY subject including the estate admin, because that check derives from `reader` and holding no tuple is what makes a table ungoverned.
- **THE MECHANISM IS FIXED (2026-09-18) AND THE COUNT WAS WRONG.** A namespace CASCADE destroys its children inside one native call, so they never reached the table door's `drop_table` emit and `namespaces.py` carried no lineage emit at all — `dropped_at` derives from run history, so every cascaded table stayed indistinguishable from a live one and the reconcile named it forever. **Proven with a control on the deployed estate:** one namespace, two tables, `solo` dropped through the table door and `cascaded` taken by a CASCADE, both purged — the next tick named `…$cascaded` and did NOT name `…$solo`. The row said THREE datasets; measured the same day the list held **twenty**, eight of them tables this repo's own suites had cascade-dropped hours earlier, so it grew with every test run. The cascade now records a drop per destroyed table, guarded per table so one failure cannot skip the FGA revoke that follows.
- **RE-MEASURED LIVE 2026-09-20: `lineage_reconcile_ungoverned` is 55, NOT the 21 this row records — and I caused part of the rise.** The armed [[LH-061]] repair revoked the tuples of 1,041 `ghost_tables` (an FGA object no catalog record names). A lineage **Dataset node** for such an object outlives both the catalog record and now the tuples, so revoking flips it from governed to UNGOVERNED in this service's view: **20 of the 55 carry the drained names** (`stockprobe*`, `lh018*`, `lh019*`, `e2eundrop*`, `e2eprobe*`), measured by extracting the reconcile line and matching. Nothing about the estate got worse — the tables did not exist before or after — but the residue MOVED from the authz plane to the lineage plane, which is this row's plane, and the count that closes this row is the one that grew. The remaining 35 are the same shape as the 13 the row already calls suite-fixture (`regaudns`, `csx1ns`, `vaud1ns`, `trackansd*`, `silver$vasa-publish-*`, `media$chunks`).
- **SO THE CLOSING CONDITION NEEDS RESTATING BEFORE IT CAN BE MET.** "The pre-fix residue no longer appears in `lineage_reconcile_ungoverned`" is now unreachable by waiting: the list is dominated by fixtures and by objects a correct repair pass deliberately un-governed. What the row actually wants is that no node names a table that still EXISTS and is reachable — which is a different query from the one the metric answers.
- **RE-MEASURED AGAIN 2026-09-22, WITH THE COMPOSITION THIS TIME — 63, and the growth is benign.**
  21 -> 55 (2026-09-20) -> **63** today, from a reconcile triggered on demand rather than waited for.
  This row already argued the list is "dominated by fixture residue"; the breakdown now says so
  exactly: `e2eundrop<gen>` 12, `models$e2etrain*` 8, `bind86-bronze` 7, `acme-bronze` 5, `e2e-ns` 3,
  plus assorted `<gen>-bronze/silver/gold/raw_events`, `e2epro<gen>` and `lh<gen>ns`. **Every added
  entry since 2026-09-20 is another e2e run's residue, not a governance gap.**
- **THE NON-FIXTURE TAIL IS FIVE NAMES, and four are already somebody else's row.**
  `lakehouse$bronze$events`, `lakehouse$bronze-media$objects`, `lakehouse$gold$catalog` carry a DOUBLE
  `$` (namespace$namespace$table) which is its own shape, and `lakehouse-bronze$events` is the
  94-count name [[LH-141]] hands to [[LH-164]]. `models$churn` is the FIXTURE model name used
  throughout `tests/unit/test_train.py` (`registry_uri_for(..., "churn")`, `model="churn"`), and
  `models$auditprobe` appears nowhere in the tree — an ad-hoc probe. **So the non-fixture tail is
  EMPTY: all 63 are fixture or probe residue, with no governed dataset among them.** That is the
  strongest form of this row's own argument, and it means no governance gap is hiding in the count.
- *What is left:* The drop's lineage emit now rides the staged outbox (`catalog/core/lineage_emit.py:690-710`; chart sets `LANCE_LINEAGE_TRANSPORT=dapr` and `LANCE_LINEAGE_OUTBOX_URI` at `chart/templates/services.yaml:149,162`) and a failed emit increments the `_emit_failed` counter (`lineage_emit.py:61`), so the loss mechanism is closed at the emitter. **THE ALERT CLAUSE IS ALREADY DONE and the evidence for it was wrong** — re-measured 2026-09-19, `CatalogLineageEmitFailing` reads `catalog_lineage_emit_failed_total` at `chart/alerting/rules.yml:117`. The row's own grep was for `lineage_emit`, which is the MODULE name, while the metric is `catalog_lineage_emit_failed_total` — a search that could only ever return 0. **RE-MEASURED 2026-09-19 and the list is now 21, of which 13 are SUITE-FIXTURE shaped by name** (`e2e*`, `lh019*`, `lh144*`, `probe$nonexistent`, `acme-bronze$zzprobe8926`) — so the signal degrades with every CI run and the eight real entries hide behind test residue. The category is NON-GATING by design, so nothing is blocked; what is lost is legibility. **A DESCRIBE PROBE CANNOT SORT THEM, and that is the catalog behaving correctly:** run as a user holding no tuple, 16 of the 21 answer **403** — including `probe$nonexistent`, which by its own name never existed — because the FGA gate refuses BEFORE existence resolves (the gate-before-disclosure rule `e2e-container-deletes` covers). Absent and forbidden are deliberately indistinguishable from outside. The five that answer **404** are the ones where the gate did resolve, and two of them are named in this very row: `bind86-bronze$events` and `research-bronze$events` do NOT exist, so "govern them to their real owner" is not an available action for either — they need a recorded drop, and only `uiproof-gold$catalog` (403) might still be governable. **AN ADMIN IDENTITY DOES NOT HELP, AND THAT IS THE REAL FINDING (2026-09-19).** `GET /datasets/{id}/reconcile` — the lineage service's own per-dataset view — answers **403 `can_get_metadata required on table:<id>`** for all six probed, run as the harness's ADMIN token (`LANCE_E2E_ADMIN_TOKEN`). `can_get_metadata` derives from `reader` (`model.fga:195`), so it is a PER-OBJECT check, and holding no tuple is precisely what makes a table ungoverned. **The estate therefore reports a class of problem that no user-facing door can inspect or repair, by construction:** the reconcile names these tables, and then every read door refuses everyone about them, because there is nobody to grant the read to. "Govern it to its real owner" has no door — it needs a repair path that acts as the service identity rather than as a subject. **THAT DOOR IS [[LH-061]], AND IT IS BLOCKED ON A STANDING OWNER DEFERRAL** ('No — not yet' on a write-capable reconcile): its remaining work is "a repair pass that acts on the report at every tier (ungoverned tables, …) with dry-run default" plus "an additive tuple rebuild driven from the `_projects/`, `_warehouses/` and bindings registries". So this clause is not independent work — it is downstream of that deferral, and no amount of enumerating the 21 advances it. `seed_ownership` (`catalog/api/fga_deps.py:1106`) cannot stand in: it grants the CREATOR `owner` and requires the creating subject's token, and an orphaned table has no creator to seed from. Adopting an unowned table to a named subject is also a privilege-escalation path, so it is a policy ruling before it is a mechanism. What is still open: those entries must each be governed to their real owner or recorded as dropped so `repository.dropped_at` (`lineage/services/repository.py:708`) recognises them; the lineage reconcile names them in `lineage_reconcile_ungoverned` (`reconcile_cron.py:161`) every tick. The drop stays best-effort (`tables.py:563-575`). The 47 dropped ones need nothing. The twelve pre-existing entries (`uiproof-gold$catalog`, `research-bronze$events`, `bind86-bronze$events`, `casc9$inner$t`, `undropns$demo`, …) are residue from before the fix and still need governing or a recorded drop; nothing NEW accumulates now.
- *Closes when:* The pre-fix residue no longer appears in `lineage_reconcile_ungoverned` and a failed catalog lineage emit fires an alert.
- *Evidence:* `services/catalog/src/catalog/core/lineage_emit.py:61,690-712` · `chart/templates/services.yaml:149,162` · `services/lineage/src/lineage/api/reconcile_cron.py:161` · ``grep -n lineage_emit chart/alerting/rules.yml` → 0 hits`

**LH-035 · The query door has no inline-bytes opt-in, and the `/blobs` docstring never names `read_blob_ranges` as the batched path**
`catalog` · **MED**
- **blocked:** WHERE a rask-only query parameter may live — and the gate this marker used to name was spurious. It asked for "owner acknowledgement of 'R8'", a ruling the register defines nowhere (measured 2026-09-20: the string occurs twice in this file and both are inside this row saying it is undefined), so it gated on a name rather than on a question. The real gate is conformance. `blob_handling`/`all_binary` appear **zero times** in `lance_docs/ns_catalog/spec.yaml` and the stock `QueryTableRequest` carries neither (21 fields, none blob-related), so adding the opt-in to the SPEC query door would put a rask-only parameter on a spec route — precisely what [[LH-021]] moved off and what `test_the_spec_surface_carries_only_spec_parameters` refuses. The shape is therefore a choice: a rask-only door under `/management/v1`, or an upstream proposal to lance-namespace. **The DOC half needed no gate at all and is DONE** — `data.py`'s `/blobs` docstring now names `read_blob_ranges` as the batched path, verified against the installed pylance 11.0.0 (`(blob_column, requests, selector=…)` over `(row, offset, length)` tuples, planned as one call).
- *What is left:* The query door delegates the body to `native.call(ns, "query_table", body)`, and neither it nor installed `lance_namespace` 0.11.1's `QueryTableRequest` carries `blob_handling`/`all_binary` (both absent from `model_fields`), so a caller cannot ask for inline bytes. Add the opt-in to the query request model. `read_blob_ranges` is documented in docs/audits/lakehouse-2026-09/lance-conformance-and-build-rules.md:424-426 but `data.py` never names it; add the cross-reference to the `GET /{id}/blobs` docstring (data.py:509).
- *Closes when:* A query request can carry an inline-bytes flag that the door honours, and the `/blobs` docstring points at `read_blob_ranges` for many-rows-one-range clients.
- *Evidence:* `services/catalog/src/catalog/api/v1/endpoints/data.py:509 (`/blobs` door)` · ``grep -n 'blob_handling|all_binary|read_blob_ranges' data.py` → no matches` · ``uv run python -c` → QueryTableRequest.model_fields lacks blob_handling and all_binary` · `open_backlog_left.md:3780 (sole 'R8' occurrence)`

**LH-037 · `mode=Skip` on `drop_namespace` is unreachable (the FGA gate refuses before existence resolves) and `Overwrite` on `create_namespace` is refused pending a ruling**
`catalog` · **MED** · PARTIAL
- **blocked:** (1) Whether the authorization gate may admit an idempotent no-op against an id with no tuples (the no-existence-oracle class rule, docs/DECISIONS.md:1394) or `Skip` is withdrawn from `drop_namespace`; (2) whether `Overwrite` on `create_namespace` is implemented against the cascade/trash interaction or stays refused
- *What is left:* Every other mode is honoured: `create_namespace` keeps an existing namespace on `ExistOk` without seeding ownership and refuses `Overwrite` with a 400 (`namespaces.py:142-145`, seam `create_or_keep_namespace` :261); `register_table` refuses `Overwrite` (`tables.py:735-738`); `drop_namespace` parses `Fail`/`Skip` via `DropMode` (`modes.py:54`). Take ruling (1): either let the gate admit a `Skip` drop of an id with no tuples, or remove `Skip` from this door's accepted set and name that in the 400. Take ruling (2): implement `Overwrite` as cascade-drop-then-create, or leave the refusal. Keep `modes.py`'s fold of unrecognised modes to `Create`.
- *Closes when:* Both rulings are recorded in docs/DECISIONS.md and the doors' behaviour matches them.
- *Evidence:* `services/catalog/src/catalog/core/modes.py:21,54` · `services/catalog/src/catalog/api/v1/endpoints/namespaces.py:142-145,261` · `services/catalog/src/catalog/api/v1/endpoints/tables.py:735-738` · `docs/DECISIONS.md:1394`

**LH-041 · pylance `Tags.update` has no conditional form, so a tag MOVE is last-writer-wins; If-Match is proven on MinIO only**
`catalog` · **MED** · PARTIAL
- **blocked:** (a) Accept a last-writer-wins tag MOVE and record it in docs/DECISIONS.md, or (b) raise a conditional `Tags::update` upstream in Lance
- *What is left:* Take the ruling; do not hand-write `_refs/tags/<name>.json` from the catalog. The tag CREATE race is already arbitrated (`tags.create` refuses an existing tag; `publication.py:298 _set_tag`, `models.py:213-215`). Under (b), file against pylance (11.0.0 in uv.lock) and consume the primitive in `_set_tag` and `models.py:215`. Separately, re-run `tests/e2e-py/test_object_store_cas_e2e.py`'s If-Match tier against RustFS if a deployment ever enables it; the chart runs MinIO (`chart/values.yaml:2022-2023`).
- **THE BRANCH HALF IS RULED (2026-09-21) — see [[LH-055]].** lance-ns defines no branch resource (three
  table-scoped ops in `spec.yaml`), and the format brief puts branch isolation at the STORAGE PREFIX:
  "storage ACLs can be read-only on main and write-only on the branch". So no `branch` FGA type; the
  work is a branch-aware vended prefix (`<table>/tree/<b>/*`), which the credentials door cannot express
  today because it takes no branch parameter.
- *Closes when:* The ruling is recorded (and, under (b), the upstream conditional update is consumed).
- *Evidence:* `services/catalog/src/catalog/services/publication.py:298` · `services/catalog/src/catalog/services/models.py:213-215` · `uv.lock:3276-3277 (pylance 11.0.0)` · `chart/values.yaml:2022-2023 (minio.enabled: true)`

**LH-063 · FGA grants are keyed on the raw IdP `sub`, so changing the Dex connector or IdP re-keys every grant**
`service-kit, catalog` · **MED**
- **blocked:** Owner decision: design a stable internal principal id that FGA keys on (IdP subject as a mapped attribute, plus a tuple re-key migration), or record per-IdP subject keys as the permanent answer.
- *What is left:* `governed/deps.py:181` and `:208` return `token.sub` verbatim as the FGA subject; no principal mapping, no configurable claim and no ruling in `docs/DECISIONS.md` (only the 2026-07-23 team/role WONTFIX at :412). After the ruling, land the principal id, the migration that re-keys existing tuples, and only then any configurable subject claim — it must not ship alone.
- *Closes when:* Either DECISIONS.md records subject keys as permanent, or a principal-id seam plus tuple migration lands with a test that a connector rename keeps grants intact.
- *Evidence:* `packages/service-kit/src/service_kit/governed/deps.py:181,208` · `docs/DECISIONS.md:412 (only related ruling)`

**LH-072 · `VendedCredentials.storage_options` is one mapping that mixes secrets with endpoint/region config**
`catalog, storage` · **MED**
- **blocked:** Decide the shape: split the vended response into `credentials` and `config` objects, or keep one mapping inside a type that knows which keys are secret and renders them redacted (pylance/lance-ray/object_store consume one dict, so a split is merged back at every call site).
- *What is left:* `vending.py:45-54` still declares `storage_options: dict[str, str]` carrying key, secret and config together; no redacting container exists anywhere in service-kit or catalog. No path logs the vended dict, so this is a contract change, not an incident. After the decision, apply it across every vendor and regenerate the clients that consume the response.
- *Closes when:* A client can tell secret fields from configuration by type, and a repr/log of the vended object never shows a secret, pinned by a unit test.
- *Evidence:* `services/catalog/src/catalog/core/vending.py:45-54` · `rg -i redact packages/service-kit/src services/catalog/src → no credential container`

**LH-075 · The read audit stream in GreptimeDB has no index; `dataset` is a JSON key inside `log_attributes`, not a column**
`catalog, chart` · **MED** · PARTIAL
- **blocked:** Owner ruling: promote the audit's `dataset` out of `log_attributes` into a real column (an OTel Collector transform, then index it) or leave it as a JSON key filtered after scope narrowing?
- *What is left:* Add an index on `opentelemetry_logs.scope_name` in a hook Job shaped like `chart/templates/greptimedb-ttl-job.yaml`; that is startable now and gated by no ruling. `scope_name` is the only first-class column separating the `lance.audit` rows (6.46M of 105.4M, 6.1%) from the rest. No index DDL exists anywhere under `chart/`. Do not rework retention: the 14d database TTL hook is in place. Take the promotion ruling separately.
- *Closes when:* A chart hook creates the `scope_name` index on `opentelemetry_logs`, and the dataset-promotion question has a recorded answer.
- *Evidence:* `chart/templates/greptimedb-ttl-job.yaml (only GreptimeDB DDL hook; ALTER DATABASE ttl only)` · `grep -rniE 'CREATE INDEX|scope_name|SKIPPING INDEX|INVERTED INDEX' chart/templates chart/values.yaml → only age-postgres.yaml:41` · `ls chart/templates | grep greptime → greptimedb-ttl-job.yaml only`

**LH-076 · `can_observe_events` is the estate-admin bar under a name that says 'read the feed'**
`catalog, service-kit` · **MED** · PARTIAL
- **blocked:** Owner ruling: add a distinctly named `can_administer_estate` that `projects.py`, `access_admin.py` and `POST /v1/stores` alias to (repointing live checks and reseeding tuples), or keep `can_observe_events` as the estate-admin rung under its current name.
- *What is left:* The comment half is shipped: `model.fga:236-244` now states it IS the admin rung and the consumer list is derived from code by `tests/unit/test_the_estate_rung_documents_everything_it_gates.py`. Only the rename half remains: no `can_administer_estate` exists anywhere, and `stores.py:105,135,197`, lineage `fga_deps.py:144`, tenant minting and the raw-tuple routes all still gate on `can_observe_events` at the root object. Do nothing until the ruling lands; if it says rename, repoint those checks and reseed the tuples in one change with `fga model test` green.
- *Closes when:* Either the owner rules the name stays, or a `can_administer_estate` relation exists and every estate-admin call site checks it.
- *Evidence:* `packages/service-kit/src/service_kit/governed/auth/model.fga:236-244 (rewritten comment, define can_observe_events: owner)` · `tests/unit/test_the_estate_rung_documents_everything_it_gates.py (exists)` · `services/catalog/src/catalog/api/v1/endpoints/stores.py:105,135,197; services/lineage/src/lineage/api/fga_deps.py:144` · `grep -rn can_administer_estate services packages chart → none`

**LH-077 · `alter_transaction` gates a whole `AlterTransactionRequest` at one committer-tier check while the model claims a per-action distinction**
`catalog, service-kit` · **MED**
- **blocked:** Owner ruling: authorize `alter_transaction` per state-action (making `can_set_property`/`can_cancel` real doors) or keep one check and delete both relations from `model.fga`.
- *What is left:* `model.fga:514` `can_set_property: editor` and `:516` `can_cancel: committer` are still referenced by nothing; `fga_deps._authorize_transaction` (`:410`) picks only `can_describe`/`can_set_status` (or `can_get_metadata`/`can_update_properties` on a namespaced txn), and `transactions.py:28-32` forwards the whole body after that one check. On the ruling: either extend the `alter` route to check per action, or delete both lines with `fga model test` green — deleting the `editor` rung is a second decision since `viewer` inherits from it. The `fga` CLI is at `.localbin/fga` (v0.6.4, not on PATH); the store is an in-cluster ClusterIP (`rask-openfga`), so a port-forward is the path for `fga model test`, not the sandbox proxy.
- *Closes when:* Either `POST /v1/transaction/{id}/alter` authorizes each state action against its own relation, or `can_set_property` and `can_cancel` are gone from `model.fga` with `fga model test` green.
- *Evidence:* `packages/service-kit/src/service_kit/governed/auth/model.fga:505-516 (removal-candidate comment; the two relations)` · `services/catalog/src/catalog/api/fga_deps.py:410-425 (_authorize_transaction picks describe/set_status)` · `services/catalog/src/catalog/api/v1/endpoints/transactions.py:28-32 (single-check alter route)`

**LH-091 · No control-lane event announces a table version advance, so a BYO change-feed consumer has no push trigger on `catalog.control.v1`**
`catalog, lineage, notifications` · **MED**
- **blocked:** Owner decision: point BYO change-feed consumers at `lineage.events.v1` (every governed write already publishes `version` there; costs no new event), or accept the control lane's per-replica broadcast buffer (`GET /v1/events`) carrying data-plane frequency and add a version-advance `ControlAction`.
- *What is left:* Take the lane decision. If lineage: document at the `POST /v1/table/{id}/changes` door that the trigger is the `lineage.events.v1` write event's `version` and nothing else changes. If control lane: add the action to the 41-member `ControlAction` literal with the buffer cost stated, across the three-file contract. Either way do NOT add it to notifications' `NAMED_ACTIONS` — it names no party.
- *Closes when:* Either the changes-door docs name `lineage.events.v1` as the trigger, or a version-advance action exists in `ControlAction` with an emitter and the stated cost.
- *Evidence:* `packages/service-kit/src/service_kit/control_events.py:36 (`ControlAction` literal; 41 members, none for a write/version advance)` · `packages/service-kit/src/service_kit/control_events.py:57,105 (`NAMED_ACTIONS` exclusion rationale)`

**LH-092 · The ingest-lane slice proves the TRIGGER chain but not the DATA chain — no silver or gold version or row count is ever asserted**
`medallion` · **MED**
- **blocked:** The double-home ruling ([[LH-137]]/[[LH-164]]): whether the catalog-vended path becomes the only legitimate home for the silver/gold tiers, or the composed `s3://<stageBucket>/medallion/<ns>` paths stay — which decides whether the lane asserts through the catalog or opens S3 directly
- *What is left:* The tier URIs are already rendered for every stage runner (chart/templates/medallion.yaml:523-524 under `medallion.compute`), so configuration is not the gap. scripts/ingest-lane.sh asserts bronze only — `committed_version` at line 506 and `units_done` at 513, 618, 719 — and the words silver/gold appear only in the comment at line 89. Once the ruling lands, add an assertion that reads a committed silver AND gold version with row counts through whichever door the ruling makes correct; do not write it before, because the composed tier ids name no catalog table and a catalog lookup 404s today.
- *Closes when:* scripts/ingest-lane.sh fails when silver or gold has no committed version or its row count does not match the bronze input.
- *Evidence:* `scripts/ingest-lane.sh:89 (only silver/gold mention, a comment); :506 `committed_version`; :513,618,719 `units_done`` · `chart/templates/medallion.yaml:517-524 — MEDALLION_FROM_URI/TO_URI rendered under `$root.Values.medallion.compute`` · `open_backlog_left.md:2620 LH-137 header (reopened, unruled); :7521 LH-164 header`

**LH-097 · Silver re-materialises managed blob bytes copied from bronze instead of being a shallow clone of bronze@N plus `add_columns`**
`medallion, maintenance, catalog` · **MED**
- **blocked:** The storage-vs-coupling trade — the R9 half of this marker was spurious, removed 2026-09-20: R9 is an ACCEPTED 2026-07-27 ruling saying "`studio` survives as its own top-navbar zone", which has no bearing on whether silver is a shallow clone of bronze (`docs/DECISIONS.md` § The `R#` rulings). What remains is the trade itself (a referencing silver means bronze can never be reclaimed independently — the sweep already refuses reclaim on shallow-clone/multi-base datasets) and of the recorded clone→source lineage edge.
- *What is left:* Land the clone→source lineage pins. Add a `scripts/` measurement of both shapes — materialised copy vs shallow clone + `add_columns` — reporting bytes and latency on one corpus against the medallion's blob path; the existing `measure_blob_descriptor_carry_forward.py` / `measure_add_columns_on_blob_table.py` cover descriptors and add_columns, not clone-vs-copy. Then make silver a `shallow_clone` of bronze at the pinned version plus `add_columns` in `medallion/services/compute.py`, replacing the copy branch that carries bytes on the ground that they exist nowhere else.
- *Closes when:* A silver produce commits no managed blob bytes of its own and the measurement script's clone shape is the one `compute.py` runs.
- *Evidence:* `services/medallion/src/medallion/services/compute.py:513 ("MANAGED UPSTREAM: the bytes exist nowhere else, so carrying them IS the only option")` · `grep -rn shallow_clone services/medallion → nothing; only maintenance/optimize.py:645 and service_kit/lakehouse/features.py:238 (the reclaim guard)` · `ls scripts/ | grep measure → measure_add_columns_on_blob_table.py, measure_blob_descriptor_carry_forward.py, measure_external_blob_carry_forward.py`

**LH-099 · The sweep's reclaimed bytes reach the summary and audit line but no metric, and no control event says a table was compacted**
`maintenance, service-kit, notifications` · **MED** · PARTIAL
- **THE ALERT EXISTS AND NOTHING ON THIS ESTATE EVALUATES IT — measured 2026-09-20, and it is true of all 50 rules, not just the new one.** `MaintenanceDriftRising` is written and PROVEN to fire by `promtool test rules` (a rise from the real 989 to 1010 fires naming `category="orphaned_trash"`; a flat 989 over 3.5 h does not; a category draining to zero does not). It cannot be OBSERVED firing here: `observability.alerting.enabled` defaults false (`values.yaml:2997`) and neither vmalert nor Alertmanager is deployed — confirmed on the live estate, where the Collector, GreptimeDB and Perses ARE running. So metrics flow and are queryable, dashboards render, and **no rule in the file is evaluated by anything**. That is a resilience posture worth stating rather than a gap in this row: the proving harness is what stands in for the engine, which is exactly why `make alert-rules-check` runs the rules against synthetic series instead of only checking their syntax.
- **THE DRIFT REPORT NOW REACHES A METRIC TOO (2026-09-20), which this row's sibling gap never named.** `metrics.py` carried ten recorders for the sweep, the purge and credential tiers and NONE for the reconcile's drift report — the very report that gates whether the purge may run and answers whether the estate's storage state is understood. It reached a log line and stopped, so no alert could fire on it and no dashboard could show it. `maintenance.drift.items` is a GAUGE labelled by `category` (drift is a level that rises and falls; `delta()` over a counter would read a repaired estate as no change at all), emitted for every CHECKED category and none other — `counts` omits what it could not check so a 0 never reads as clean, and undoing that on the series an alert fires from would put the lie where it does most damage. **OBSERVED end to end in GreptimeDB (`lance-rest-catalog:lh099-driftmetric`): NINE series** — `orphaned_trash=989`, `unbound_namespaces=4`, `orphaned_annotation_tasks=3`, the rest 0, and `orphan_files` ABSENT rather than zero because that tick did not check it. Two of those findings were invisible before this.
- **blocked:** Owner decision, shared with the branch/tag control-event question: is a compaction an audit record only, or should someone be TOLD (a `table_maintained` action)?
- **THE METRIC HALF IS SHIPPED AND OBSERVED (release 187).** `compaction.bytes.reclaimed` is exported and `record_reclaimed` takes `bytes_removed`, which `sweep.py` now passes. Before: `compaction_bytes_reclaimed_total` returned ZERO series from GreptimeDB while `compaction_runs_total` returned one. After: one series, value `0` — correct, because the estate has nothing to reclaim right now (branches cleared, the residual 32 beyond Lance's listing floor), and the always-emit rule is what makes idle distinguishable from broken and from absent. Kept SEPARATE from `maintenance.trash.bytes_reclaimed`: the two answer what superseded versions cost versus what dropped tables cost, and one series answering both answers neither. Only the EVENT half is still blocked. If the owner rules for an event: add `table_maintained` across the three-file `ControlAction` contract (41 members today, pinned by `tests/unit/test_control_action_three_file_contract.py`) and emit it from the sweep and the catalog maintenance endpoint. `summarize` already carries `bytes_removed`; do not re-add it.
- **THE BRANCH HALF IS RULED (2026-09-21) — see [[LH-055]].** lance-ns defines no branch resource (three
  table-scoped ops in `spec.yaml`), and the format brief puts branch isolation at the STORAGE PREFIX:
  "storage ACLs can be read-only on main and write-only on the branch". So no `branch` FGA type; the
  work is a branch-aware vended prefix (`<table>/tree/<b>/*`), which the credentials door cannot express
  today because it takes no branch parameter.
- *Closes when:* A sweep tick emits a bytes-reclaimed metric series, and the event question has a recorded answer with the emit landed or declined.
- *Evidence:* `services/maintenance/src/maintenance/core/metrics.py:182-188 (`record_reclaimed` takes fragments/versions/indices, no bytes); :91,158,179 (trash bytes only)` · `services/maintenance/src/maintenance/services/sweep.py:699,1010,1159 (`bytes_removed` in per-dataset, audit and summary)` · `grep -rn table_maintained --include=*.py --include=*.ts . → nothing; ControlAction literal has 41 members` · `services/maintenance/src/maintenance/services/purge.py:712 (the only `emit_control` in maintenance)`

**LH-108 · The lineage + OpenFGA store is the hand-rolled `rask-age` StatefulSet; the CNPG cutover is built but off**
`lineage, chart` · **LOW**
- **blocked:** Owner ruling 2026-09-21 — **there is no production estate yet** (*"no not yet so we work with our locally dummies"*), so this and six sibling rows are PARKED at LOW rather than closed: the evidence stands and the row returns at its old priority the day a prod estate exists. The question it was waiting on, unchanged: Owner decision: keep the AGE StatefulSet or cut over to CNPG with the ImageVolume extension — plus a K8s 1.33+ / CNPG >= 1.27 cluster to run it on
- *What is left:* `age.cnpgCluster.enabled` defaults false with `extensionImage: ""` (chart/values.yaml:2762-2766) while the CNPG operator is installed with nothing to reconcile (values.yaml:2834 `enabled: true`); chart/templates/age-cluster.yaml:3 fails the render if both paths are on, so this is one-way. If CNPG: build and publish `.docker/cnpg-age-ext.dockerfile`, set `extensionImage` and flip `age.cnpgCluster.enabled` (with `age.enabled=false`) in chart/values-prod.yaml, and migrate the `lineage` + `openfga` databases. If StatefulSet: record the ruling and drop the idle operator toggle.
- *Closes when:* Exactly one graph-store path is the recorded choice and, if CNPG, the two databases run on the `Cluster` with the extension image.
- *Evidence:* `chart/values.yaml:2762-2766 `cnpgCluster: enabled: false` / `extensionImage: ""`` · `chart/templates/age-cluster.yaml:3 `fail "age.enabled and age.cnpgCluster.enabled are mutually exclusive ..."`` · `.docker/cnpg-age-ext.dockerfile exists (2032 bytes)` · `chart/values.yaml:2834 cnpg operator `enabled: true`; no `cnpgCluster` key in chart/values-prod.yaml`

**LH-148 · Nothing re-ingests the JetStream `dlq.<appId>` stream, so a parked lineage event older than 7d retention is unrecoverable**
`lineage, chart` · **MED** · PARTIAL
- **NAMED AT LAST, AND THE NUMBER IS 1,033 — not 121 (2026-09-20).** `ghost_tables` is now a drift category: FGA table objects holding tuples that no catalog record names, the exact INVERSE of `ungoverned_tables` and reusing both its inputs and `_ghosts`. **OBSERVED on the live drift metric: `ghost_tables=1033`.** The 121 I inferred from `unknown_to_graph` were only the subset the GRAPH also did not know — an orphaned tuple whose table still has a dataset node never reached that metric at all. So the class was an order of magnitude larger than the surface that accidentally exposed it, which is the argument for naming a thing directly rather than reading it off a metric that means something else. FGA-derived and NON-GATING: the purge cannot reclaim a tuple, so gating storage reclamation on one would stop the estate for a reason it can never resolve.
- **THE NEAR-BUG SWEEP IS COMPLETE: five destructive paths, one gap, and it was the one fixed.** After finding the warehouse cascade leaking, every other door that destroys a governed object was checked rather than assumed. **Table drop:** `revoke_ownership` removes every tuple on the object — owner grant, `parent` edge and later reader/writer grants — so a reused id cannot inherit one. **Namespace cascade drop:** enumerates with `_collect_descendants` BEFORE the native call and warns on a truncated walk, because "an incomplete revoke leaves orphan grants". **Project delete:** refuses 409 while the tenant still holds warehouses (`projects.py:293`), so it never cascades and can orphan nothing. **Trash purge:** `_revoke` calls `revoke_object_tuples` per record, and a recoverable cascade trashes the namespace AND each table as separate records (#96), so every child gets its own purge and its own revoke. **Warehouse cascade:** the one that did not, now fixed. So the leak was singular rather than a pattern, which is worth knowing before anyone goes looking for more of them.
- **THE PRODUCER IS FOUND AND FIXED (2026-09-20) — and it was NOT residue, it was live.** `DELETE /v1/warehouses/{id}?cascade=true` drops each bound namespace through the NATIVE `drop_namespace`, which destroys its child tables inside one call, and then revoked `namespace:<id>` and nothing else (`warehouses.py:889`). Every table it destroyed kept its FGA tuples. Both other destructive doors get this right — `revoke_ownership` on the table door removes every tuple, and the namespace door enumerates descendants with `_collect_descendants` BEFORE the cascade precisely so they can be revoked after — so this was a gap, not a design. `_revoke_descendants_of` now runs BEFORE the native drop (afterwards the children cannot be listed at all), reusing the namespace door's depth-capped enumerator rather than adding a second walker for an incomplete revoke to hide in. **OBSERVED on the deployed catalog (`lance-rest-catalog:lh148-cascaderevoke`):** the call is present and precedes `drop_namespace`. **This stops new orphans; the existing ~121 still carry tuples** and clearing them is [[LH-061]].
- **MECHANISM ESTABLISHED (2026-09-20), and it reclassifies the number: these are ORPHANED FGA TUPLES, not lost provenance.** `unknown_to_graph = governed - graph` (`reconcile_cron.py:134`), and `governed_tables` is documented as "the table ids carrying at least one authorization tuple" — it reads FGA, never storage. Combined with the measurement that the `trackans*` datasets are GONE from storage, the deduction is forced: those ids still carry authorization tuples for tables that no longer exist, and the graph rightly has no node for them. **So 121 of 126 are tuples the e2e cleanup left behind, counted on a metric whose name says provenance.** That matters beyond bookkeeping: `LineageProvenanceLostOnWrite` fires on this number, so the alert would page about tuple orphaning under the words "a write lost its provenance". Note the gap it falls through — maintenance reports `ghost_projects` and `ghost_warehouses` (an FGA object no registry record names) but has **no `ghost_tables`**, so nothing in the drift report names this class directly.
- **CORRECTING MY OWN ATTRIBUTION (2026-09-20): the 121 are e2e fixtures, but the mechanism is NOT the warehouse delete.** I wrote in `b268c4cd` that these share [[LH-102]]'s cause. They do not. Measured: the `trackans528a1868` datasets are GONE from storage — the sweep enumerates none of them — and yet all 19 are still counted in `unknown_to_graph`. So this set is about a DROP that the graph never recorded, not about a warehouse whose registry entry was removed. The alert's own text says a table enters this set when its FIRST write's event was lost, which is a third possibility again. **The exact mechanism is NOT established** and saying so is better than the tidy answer: what IS established is that the datasets no longer exist, the tables are fixture-named, and no production table is in the set. Note also that this metric is a different surface from [[LH-144]]'s `lineage_reconcile_ungoverned` (21 items) — two lists, not one, and conflating them would repeat the error.
- **AND THE FIVE ARE NOT PRODUCTION EITHER — the gap is zero real tables (2026-09-20).** Taken one at a time: `acme_gold_catalog` and `acme_silver_features` belong to `project:acme`, the demo tenant the verification scripts drive (`scripts/verify_produce_door.sh` sets `medallion.produceAdminProject=acme`) and the FGA model's own test fixture; `transcripts_v2$chunks` is a fixture id used across the suite (`test_estate_table_walk.py`, `test_catalog_caller_token.py`, `test_publish_saga.py`); and `lakehouse$bronze$pages` / `lakehouse-bronze$pages` are ONE table counted twice under two delimiter spellings, which is [[LH-150]]'s subject rather than two lost writes. **So the estate's `unknown_to_graph=126` contains no production table at all**, and criterion 1 — a write's provenance survives it — holds for every real write this estate has taken. The number is a standing artifact of test and demo traffic, which is why it sat at 127 yesterday and 126 today rather than moving with anything.
- **THE MATCH IS DONE AND IT DE-ESCALATES THE DEADLINE: 121 of the 126 are E2E FIXTURES (2026-09-20).** The `unknown_to_graph` set read off the live reconcile is **114 `trackans<hex>` tables (19 each across six track-a namespaces) plus 7 `models$e2etrain*`** — the same fixture family that produced [[LH-102]]'s 869 orphaned trash records, from the same suite, for the same reason. **Only FIVE are not test residue:** `acme_gold_catalog`, `acme_silver_features`, `lakehouse$bronze$pages`, `lakehouse-bronze$pages`, `transcripts_v2$chunks`. So the estate's standing provenance gap is five tables, not 126, and the DLQ expiry tomorrow risks recovering fixtures rather than losing production provenance. The row keeps its deadline — the option really does close — but it is a LOW-STAKES one, and five is small enough to settle per-table rather than by building re-ingestion for a number that was mostly noise.
- **THERE IS A DEADLINE, AND IT IS 2026-09-21T16:20 UTC — measured 2026-09-20.** This row reads as a standing gap; it is not. The DLQ stream holds **2,525 messages, 2,495 of them `dlq.lineage.events`**, the oldest parked 2026-09-14T16:20:46 against a 7-day `max_age` — so they begin expiring in roughly 31 hours and, per this row, nothing re-ingests them. Nothing new has parked since 2026-09-19T19:51, so this is a fixed set that drains by expiry rather than a growing one.
- **THE PARKED EVENTS ARE WELL-FORMED AND INCLUDE CATALOG WRITES, which is what makes the deadline worth a decision rather than a shrug.** Sampled across the stream: every one an OpenLineage `COMPLETE` with job and outputs intact, spanning `lance-catalog`, `lance-medallion`, `external` and `maintenance` job namespaces — not the undeserializable junk [[LH-151]] found. Meanwhile `lineage_reconcile_provenance_missing` reads **`unknown_to_graph=126`** on the live estate, tables whose first write never reached the graph and which the estate's own alert says cannot be auto-repaired "because with no dataSource URI recorded, a node invented for it would assert a write nobody observed". **NOT ESTABLISHED, and it is the question:** whether re-ingesting these 2,495 would repair any of those 126. A sample of four is not a census, the one oldest-shaped event read was a maintenance COMPACTION (which would repair nothing), and confirming it means matching parked outputs against the unknown set. What IS established is that after the deadline the option is gone.
- **blocked:** Disposition of the ~86% role-literal parked population (`author.sub` = data_eng/ray/analyst, unauthorizable by construction): drain the subject and record the loss, or grant a historical-replay identity. Also whether the graph should record provenance for a dropped table (a `service-maintenance` compaction of a dropped probe table parks permanently).
- *What is left:* The metric half is shipped: `on_dead_letter` asks `repository.run_status` and records `PARKED_ALREADY_RECORDED` for a run the graph holds, `DEAD_LETTERED` otherwise (dapr.py:120), pinned by `tests/unit/test_a_park_the_graph_already_holds_is_not_terminal_loss.py`; its docstring states the retention bound (dapr.py:85-88); eight production sites pass `author_subject=settings.fga_service_identity`. The admin `/dlq/{run_id}/replay` door reads the OUTBOX object store only. Build a replay that re-presents a parked `dlq.<appId>` delivery to the ingest handler idempotent on `run_id` WITHOUT re-publishing — never via `reconcile_cron._drain_outbox`, which re-publishes by design. It serves only the authorizable remainder (~14% of samples); the mechanism (lineage's ingest consumer is ephemeral + `deliverPolicy: all`, so every restart re-parks) still holds in the chart. A real Dex subject that still parks (seq 12002/11975) is undiagnosed.
- *Closes when:* A parked delivery on `dlq.lineage.events` can be re-ingested into the graph without landing back on `lineage.events.v1`, and the role-literal residue has a recorded disposition.
- *Evidence:* `services/lineage/src/lineage/api/dapr.py:85-88, :120` · `services/lineage/src/lineage/api/v1/endpoints/dlq.py:87-137 (outbox-only replay)` · `chart/templates/dapr-component.yaml:172 (lineage subscriber deliverPolicy "all")` · ``grep -rn 'author_subject=settings.fga_service_identity' services/` → 8 sites`

**LH-150 · Nothing refuses a boot whose `LANCE_NS_DELIMITER` disagrees with the OpenFGA object ids already stored, so changing it silently denies every check**
`catalog, service-kit, openfga, lineage` · **MED**
- **blocked:** Owner decision: refuse a boot whose delimiter disagrees with the tuples already stored in OpenFGA, or document the delimiter as bootstrap identity only and stop presenting it as an operator knob?
- *What is left:* Get the ruling. If 'refuse': at catalog boot read one governed (`table:`/`namespace:`) object id from OpenFGA and refuse to serve when its delimiter disagrees with `settings.delimiter`; no-op on an empty store so a fresh estate can boot; never compare a `user:` subject. The stored tuples are the record (715 of 1000 sampled ids carry `$`, 0 carry `.`), so no new estate state is needed. Do not hardcode `$` in FGA ids: `tests/unit/test_cross_axis_identity.py` holds the FGA object, lineage Dataset name and Lance-metadata id byte-identical under any delimiter. The prose half is done (`naming.py`, `config.py` both state the consequence).
- *Closes when:* Either a boot-time delimiter/tuple check exists and is pinned by a test, or the knob is documented as bootstrap-only everywhere it is exposed and the ruling is recorded.
- *Evidence:* `packages/service-kit/src/service_kit/lakehouse/naming.py:16 ('RENAMES every…')` · `services/catalog/src/catalog/core/config.py:84 ('BOOTSTRAP-ONLY. It spells every OpenFGA object id…')` · `grep -rn -i delimiter services/catalog/src/catalog/main.py services/catalog/src/catalog/core/lifespan.py → no boot check`

**LH-152 · Three live e2e legs cannot pass against a governed estate: two stage provenance as an unregistered table, and one asserts zero errors against unreadable registry entries**
`lineage, medallion, maintenance, catalog` · **MED** · PARTIAL
- **blocked:** (b) whether `errors == {}` is the right assertion for a long-lived estate, or whether unreadable registry entries belong in an exclusion set the way the reconciler already reports `excluded_datasets` — gates the maintenance leg only
- *What is left:* Ruling (a) is decided — probes write to a REAL governed table created through the catalog and stamp the creating subject as author — and applied to `test_outbox_e2e` via the `probe_author` fixture (tests/e2e-py/test_outbox_e2e.py:107). Apply the same fixture to tests/e2e-py/test_outbox_crash_e2e.py:189-204, which still stages `author="e2e"` against the unregistered `bronze$e2e_crash_ds`. Rework `test_fga_deny_drops_promotion_and_regrant_restores` (tests/e2e-py/test_governed_union_e2e.py:566) so its revoke does not delete the warehouse-level owner tuple (`_owner_tuples`, :130-136) that the live stage runners share — a failure between revoke and regrant strips a grant the cascade needs. The maintenance leg (tests/e2e-py/test_maintenance_e2e.py:105 `assert body["errors"] == {} ...`) waits on (b); the reconciler already exposes `excluded_datasets` (maintenance reconcile.py:271, 966) to build on.
- *Closes when:* The crash and FGA legs pass against the governed estate without touching shared grants, and the maintenance leg's assertion matches the (b) ruling.
- *Evidence:* `git log 91d183cc `test(e2e,LH-152): the outbox probe's output table is a real governed table, authored by its owner`; tests/e2e-py/test_outbox_e2e.py:107-121 `probe_author` docstring records the 2026-09-15 ruling` · `tests/e2e-py/test_outbox_crash_e2e.py:197 `author="e2e"`, :201 `output_name="e2e_crash_ds"`` · `tests/e2e-py/test_governed_union_e2e.py:130-136 `_owner_tuples` — deletes warehouse, namespace and table owner tuples` · `tests/e2e-py/test_maintenance_e2e.py:105 `assert body["errors"] == {} or body["errors"] == []`; services/maintenance/src/maintenance/services/reconcile.py:271,966 `excluded_datasets``

**LH-164 · Chart-path medallion datasets at `s3://<bucket>/medallion/<ns>` are unregistered and ungoverned, so each tier has two homes and only one is governed**
`maintenance, medallion, chart` · **MED** · PARTIAL
- **RULED 2026-09-21 (owner): REAP ALL FOUR.** The closing bar is "each tier has exactly one home", and
  registering these would bless the second home rather than remove it. Three carry 8 rows each — fixtures
  by size — and the 500-row `bind86-wh/medallion/bronze` goes with them: nothing references it and the
  governed tier is the live one. UNBLOCKED; what remains is the deletion and the sweep reporting zero.
- **THE POPULATION IS NOW ENUMERATED BY A CATEGORY RATHER THAN BY HAND (live, 2026-09-20).** [[LH-176]]'s new `unregistered_datasets` — a dataset on storage that no catalog table record names — reports **8**, and **5 of them are this row's subject**: `s3://lance-catalog/medallion/lakehouse$bronze`, `…$bronze-media`, `…$gold`, `…$silver`, `…$silver-media`. The other three are `s3://vaud1-wh/blobtab{,4}_vaud1ns$vblob{2,4}` and LH-176's own `m2proof_silver$m2-proof-1788537252`.
- **AND IT ANSWERS HALF THE BLOCKER WITH EVIDENCE RATHER THAN A RULING: the catalog does not know them.** The question this row parks on is "residue to reap or data to register?"; a dataset that no table record names is not a governed tier by any definition the catalog uses — it is a path something wrote to. Combined with the standing position that the deployed estate holds test and demo data only, reaping is the answer for these five.
- **THE ROW'S OWN LIST IS STALE AND THE SCOPE IS BOUNDED — read both before acting.** It names four (`medallion/lakehouse$silver`, `$gold`, `research-bucket/medallion/bronze`, `bind86-wh/medallion/bronze`); the live category reports five, under `lance-catalog` only, and neither of the two other buckets appears. That is a bound on the MEASUREMENT, not proof of absence: the category sees only datasets the depth-bounded walk DISCOVERED (`discoveryMaxDepth: 3`) in the buckets `_scannable_buckets` selects, so a `medallion/` prefix in another bucket is unmeasured rather than clean. Grepping the pod log for `s3://*/medallion/*` returns far more prefixes, but those are URIs appearing in triggers and refusals — not evidence that a dataset is there.
- *What is left:* Both code halves are shipped: `denial_remedy` (`compaction_executor.py:107`) replaces the impossible grant advice at both refusal sites, and the reconciler's non-gating `ungoverned_tables` category (`reconcile.py:95,127,261`) detects a catalog table with no tuples. Obtain the ruling above, then either reap or register the five datasets. `chart/templates/medallion.yaml:293` (`MEDALLION_BRONZE_URI`) and `:523` (`MEDALLION_FROM_URI`) still render `s3://<bucket>/medallion/<ns>` while `ensure_stage_output` vends a different governed location per tier — collapse each tier to one home once ruled. Reaping the wrong one destroys live rows. Row counts above are carried from the row, not re-measured this session.
- **THE FOUR ARE REAPED (2026-09-21), and re-measuring first found the row UNDERCOUNTS by ten.** Executed
  against the live object store as the MinIO root identity, after recording each dataset's full object
  manifest: `lance-catalog/medallion/lakehouse$silver` (5.5KiB, 5 objects),
  `lance-catalog/medallion/lakehouse$gold` (6.1KiB, 5), `research-bucket/medallion/bronze` (1.8KiB, 3)
  and `bind86-wh/medallion/bronze` (9.1KiB, 3) — all four single-version Lance datasets dated
  2026-09-11, all verified at zero objects after. Exactly the four the ruling named, and no more.
  **TEN FURTHER CHART-PATH PREFIXES EXIST UNDER `medallion/` and were deliberately left**, because the
  ruling authorised four: `lakehouse$bronze` (1.8KiB/3), `lakehouse$bronze-media` (3.8KiB/3),
  `lakehouse$silver-media` (8.0KiB/5), `bronze` (3.6KiB/6), `silver` (5.4KiB/5), `gold` (5.9KiB/5),
  `bronze-media` (3.8KiB/3), `bronze-pages` (**1.7MiB**/4), `models` (**388KiB/126 objects**) and
  `bind86-wh/medallion/silver` (31KiB/33). **`models/` and `bronze-pages/` are not obviously the same
  class** — one is plausibly the model registry and the other a page corpus, neither of which is a
  medallion TIER — so extending the reap to them needs its own answer rather than the same one.
  **SO THIS ROW CANNOT CLOSE ON THE REAP ALONE.** Its bar is "zero UNGOVERNED medallion datasets", and
  ten remain. The next step is a ruling on those ten, split by what they are rather than by where they
  sit: the eight tier-shaped ones read as the same residue, `models/` and `bronze-pages/` do not.
  **AND THE REAP COULD NOT BE DONE BY THE SERVICE THAT REPORTS THE PROBLEM, which is worth recording.**
  The maintenance pod's `storage_options()` carries `aws_access_key_id='rask-maintenance'` — an IDENTITY
  — and an EMPTY secret, because it vends a scoped session per operation rather than holding a static
  key. An ungoverned prefix has no catalog record, so nothing can be vended for it (the 403 [[LH-176]]
  records). The estate therefore reports a class of residue that its own sweep is structurally unable to
  remove, and clearing it is an operator action with the root identity by construction, not by oversight.
- **THE RESIDUE IS NOT INERT — IT BLOCKS A LEGITIMATE CREATE (measured 2026-09-21).** Driving
  [[LH-137]]'s verification, `POST /v1/table/silver$features/create` against the live catalog answered
  **500** with `OSError: Dataset already exists: s3://bind86-wh/medallion/silver` from
  `catalog/services/dataplane.py:261 _write_blob -> lance.write_dataset`. That path is
  `bind86-wh/medallion/silver` (31KiB, 33 objects) — one of the ten chart-path prefixes left unreaped
  because the ruling authorised four. So this class of residue is not merely reported-and-ignored: it
  occupies a location the catalog composes for a real table id and makes that table impossible to
  create, on an estate where nothing references the residue.
  **AND THE REFUSAL WAS THE WRONG SHAPE — FIXED 2026-09-22, independently of the ruling.** A create
  colliding with bytes already at the composed location is a CONFLICT the caller can act on; it
  surfaced as a raw `OSError` through a 500 `InternalError` with `detail: "Internal Server Error"`, so
  the caller was told nothing and the reason existed only in the pod's traceback. **The spec had
  already said so:** `lance_docs/ns_catalog/spec.yaml:1461` declares `ConflictErrorResponse` on
  `CreateTable`, so a collision is a modelled outcome of the operation rather than an internal
  failure. `_write_blob`'s `except OSError` now raises `TableAlreadyExistsError` naming the location,
  matching lance's specific phrase exactly as the sibling external-blob translation beside it does —
  a handler that swallowed every `OSError` would relabel genuine infra faults as client errors.
  **The detail NAMES THE LOCATION AND DENIES THE TABLE**, because these bytes are ungoverned residue
  at a path the catalog composes, not a table record: "table already exists" would send the caller
  looking for something that does not exist, and the location is the one actionable fact the 500
  withheld. Four legs, each mutation-checked, and **the second hop caught a real trap**: `status_for`
  takes a NUMERIC code, so the obvious `status_for(exc)` answers 500 — the leg now asks exactly as the
  installed handler does (`ns_errors.py:140`, `int(exc.code)`). A fourth leg pins that every
  `write_dataset` in the catalog is inside `_write_blob`, by ENCLOSING FUNCTION rather than by line
  number, so one translation genuinely covers every write door and a second one cannot quietly differ.
- **THE EIGHT TIER-SHAPED PREFIXES ARE REAPED (owner ruling 2026-09-21, extending the first four), AND
  ONE MORE WENT WITH THEM BY MISTAKE.** Authorised and reaped: `lakehouse$bronze`,
  `lakehouse$bronze-media`, `lakehouse$silver-media`, `bronze`, `silver`, `gold`, `bronze-media` and
  `bind86-wh/medallion/silver` — all verified empty afterwards. `models/` was to be KEPT and is intact
  at 388KiB / 126 objects.
  **`bronze-pages/` (1.7MiB, 4 objects) WAS ALSO DELETED AND SHOULD NOT HAVE BEEN.** `mc rm --recursive`
  matches by PREFIX, not by directory, so `mc rm --recursive loc/lance-catalog/medallion/bronze` removed
  everything beginning `medallion/bronze` — which took `bronze-media/` (authorised) and `bronze-pages/`
  (explicitly excluded) with it. The bucket is **un-versioned**, confirmed with `mc version info`, so it
  is not recoverable.
  **THE IMPACT IS BOUNDED AND WAS CHECKED RATHER THAN ASSUMED:** a GOVERNED table lives at
  `s3://<bucket>/<hash>_<ns>$<table>/`, never under `medallion/`, so everything under that prefix is
  chart-path residue with no catalog record — which is this row's entire premise. `bronze-pages/` was in
  the same ungoverned class as the eight, and nothing resolved to it. What was lost is 1.7MiB of
  unreferenced residue; what was violated is the boundary of what had been authorised, which is the part
  worth recording.
  **THE LESSON IS THE TOOL'S, AND IT GENERALISES:** an object store has no directories. Any prefix
  delete must be anchored — `medallion/bronze/` with the trailing delimiter, never `medallion/bronze` —
  and a dry-run listing must be taken with the SAME pattern the delete will use, not a similar one.
- **blocked:** one prefix remains and it is not a tier — `lance-catalog/medallion/models` (388KiB, 126
  objects, every entry an `e2etrain*` Lance dataset). Both reap rulings covered TIER-shaped residue; a
  model registry is a different thing and nothing has established what reads this one. Either it is
  named as residue and reaped with the rest, or it is a real store that must be REGISTERED — and until
  that is answered the closing bar ("zero UNGOVERNED medallion datasets") cannot be met either way.
- *Closes when:* Each tier has exactly one home, and the sweep reports zero UNGOVERNED medallion datasets.
- *Evidence:* `services/maintenance/src/maintenance/services/compaction_executor.py:107` · `services/maintenance/src/maintenance/services/reconcile.py:95,127,261` · `chart/templates/medallion.yaml:293,523` · ``grep -i 'two homes\|bind86' docs/DECISIONS.md` → no ruling recorded`

**LH-171 · Nine governed transform records fail `TransformSpec` validation and the estate only WARNs**
`medallion, service-kit` · **MED**
- **blocked:** Owner ruling on the field mapping for the older record shapes: does `entrypoint` become `task` verbatim, and what does `lane` become (no successor in `TransformSpec`)?
- *What is left:* `_transforms/` holds 10 records in three shapes — one current, one with `name`+`entrypoint`, eight with `lane`+`entrypoint` — and `TransformSpec` (`extra="forbid"`, requires `name`, `task`) rejects nine of them; `_parse` at `transform_specs.py:182-193` logs `transform_spec_malformed` and skips. `cardinality` already defaults to `ONE_TO_ONE` in the model, so only the two mappings above need ruling. Once ruled: migrate or delete the nine (10 records total, 9 sharing one shape), then make an unparseable control record louder than a WARN or gate the set empty — no test asserts on `transform_spec_malformed` today. The live count is not re-measured this session.
- *Closes when:* Every record under `<control_root>/_transforms/` validates against `TransformSpec`, and a record that does not is surfaced by more than a listing-path WARN.
- *Evidence:* `packages/service-kit/src/service_kit/lakehouse/transform_specs.py:62-84 (fields, extra=forbid, cardinality default), :182-193 (_parse warns and skips)` · `grep -rn transform_spec_malformed tests services/*/tests packages/*/tests → none` · `grep -rln 'migrate.*transform' scripts → none`

**LH-048 · Two measured upstream pylance defects are unfiled, and they are not the two this row named**
`catalog` · **LOW**
- **THE INVENTORY IS CORRECTED (2026-09-20), both halves measured.** The `header.`/`headers.` prefix workaround this row's second entry pointed at **does not exist** — no site in `services/` or `packages/`, so there is nothing to file and nothing to track. In its place, today's LH-022 measurement produced a real one.
- **blocked:** filing on `lancedb/lance` is an OUTWARD-FACING action on a third party's repository, so it needs the owner's go rather than being picked up as workable. The measurement is done and recorded here; only the posting is held.
- *What is left:* file two issues against `lancedb/lance` and record each URL beside its workaround:
  1. the bundled REST client sends **GET** for `count_rows` and `tags/list` where the spec says POST at every tag v0.9.0-v0.12.0, forcing the dual mount at `data.py:676-677` and `tags.py:40-41` (re-measured 2026-09-21 against pylance **11.0.0**, inside the affected range; the line numbers this row carried had drifted, and both mounts still pair the spec-correct POST with a `_compat_get` twin);
  2. the Rust `merge_insert_into_table` rejects a **list** for `on` — `TypeError: 'list' object is not an instance of 'str'` — while the Dataset API accepts one and lance-namespace types it `list[str]` from 0.12, which is what pins rask at `<0.12` ([[LH-022]]).
  Filing is an outward-facing action on someone else's tracker and has not been done.
- *Closes when:* both issues are filed and their URLs sit beside the workarounds.
- *Evidence:* `grep -rn 'header\.' services/ packages/ → no workaround site` · `services/catalog/src/catalog/api/v1/endpoints/data.py:613-622` · measured on pylance 11.0.0 + lance-namespace 0.13.0
**LH-050 · No query store for catalog listings, deliberately, until interactive-frequency listing load is measured**
`catalog` · **LOW**
- **blocked:** the tripwire — and it has now been READ rather than merely named. **MEASURED 2026-09-20 against the deployed GreptimeDB**, PromQL over `http_server_duration_milliseconds_count{service_name="catalog"}`: `/v1/namespace/{id}/list` **0.000000 req/s**, `/v1/namespace/{id}/table/list` **0.000000 req/s**, `/v1/warehouses` **0.000627 req/s** — about one request every 27 minutes, and **21 requests across the whole retention window**. Interactive frequency is order ≥1 req/s per active user, so this is three to four orders of magnitude below the trigger and two of the three routes are literally zero. The row stays parked; what changes it is the number, not a ruling. *Bounded honestly:* the counter is per-pod and the catalog rolled several times on the measurement day, so the rate is summed over per-pod series (`sum by (http_target) (rate(…[24h]))`) to survive the restarts; and the `/v1/warehouses` traffic that does exist is largely this session's own drives, so the organic rate is lower still.
- *What is left:* Nothing to build, and now nothing to re-measure until someone has reason to think the load changed. No query store or listing cache exists in `services/catalog/src` or `service_kit` (grep for `query store|query_store|listing cache` is empty), and no dashboard or alert rule reads the listing routes' rate. The instrument already exists: `service_kit.setup_otel` emits `http.server.*` RED metrics per route into GreptimeDB, so the tripwire is the PromQL read recorded above, not new code. **The cheapest next step is not a query store but an ALERT** — a rule over that same series would trip the wire automatically instead of waiting for someone to re-run this by hand, which is how a tripwire nobody re-reads becomes a row that sits forever.
- *Closes when:* A recorded measurement shows listing request rate at interactive frequency, followed by a query-store design.
- *Evidence:* `services/catalog/src/catalog/api/v1/endpoints/{namespaces,tables,warehouses}.py (the list handlers)` · `grep -rn -i 'query store|query_store|listing cache|list_cache' services/catalog/src packages/service-kit/src → empty` · `chart/templates/perses-dashboards.yaml, chart/alerting/rules.yml → no listing-rate panel or rule`

**LH-079 · Two standing answers on the `x-api-key` principal contradict each other, and no key store or rotation model exists**
`catalog, gateway` · **LOW**
- **blocked:** Owner: is the Q7 api-key principal withdrawn in favour of bearer-only (the spec `security` block is a disjunction; 155 of 160 ops declare bearer), or does the management API mint scoped, expiring keys after all?
- *What is left:* `x-api-key` is read nowhere in catalog, gateway or service-kit, while `docs/audits/lakehouse-2026-09/lance-conformance-and-build-rules.md` B6 still prescribes accepting it against a management-API key store. Take the ruling and edit the losing statement out. If bearer-only wins, rewrite B6 (:122-124, :367) as a conformance note. If the key principal survives, write the key-store and rotation design into the management API RFC and read `x-api-key` in `catalog/api/security.py`.
- *Closes when:* One answer stands in the tree and the other is gone; if keys survive, the RFC carries the store and rotation design.
- *Evidence:* `grep -rn 'x-api-key' services/catalog/src services/gateway/src packages/service-kit/src → 0 hits` · `docs/audits/lakehouse-2026-09/lance-conformance-and-build-rules.md:122-124,367` · `grep -n -i 'api.key\|bearer' docs/DECISIONS.md → no ruling`

**ZT-001 · Every privileged service's "dedicated" credential is derived from the shared app token, so holding one yields all of them**
`chart, service-kit` · **HIGH**
- **blocked:** a deployment-policy call — either the prod values enable ESO so an operator's own material reaches OpenBao, or each dedicated token must be supplied and the render FAILS without it. Both make a prod estate undeployable in a way it is not today, which is the owner's to choose; the property itself is now pinned and measured either way.
- *What is left:* `lance.dedicatedServiceToken` computes `sha256("<identity>-<dapr.appToken>")[:40]`, so any holder of the shared `dapr.appToken` — which 13 pods carry — can compute the dedicated credential of every privileged identity. That defeats the control `dapr_auth.py` describes it as being: a subject off the privileged allowlist authenticates with the shared token, and the dedicated pair exists precisely so "any holder of that one token may claim ANY allowlisted service" stops being true. The helper's defence is that the prod path supplies independent material through ESO — but `externalSecrets.enabled` is `false` by default and `chart/values-prod.yaml` carries NO uncommented `externalSecrets:` stanza at all, only comments suggesting it, so a prod render from that file gets derived tokens. Either make the prod values enable ESO (and fail the render when a privileged identity has no independent secret), or stop deriving and require each token to be supplied. **Shares its mechanism with [[XC-004]]** — that row is the ESO default itself; this one is what the default costs. Work them together or the fix lands on one side only.
- *Measured:* **5 privileged identities render a dedicated token and all 5 are derivable** — `service-bronze-to-silver`, `service-media-to-silver`, `service-silver-to-gold`, `service-trainer`, `service-web`. Pinned by `tests/unit/test_a_dedicated_service_token_is_not_derivable_from_the_shared_one.py`, which derives each exactly as the chart does and reds if the helper changes shape. That file is deleted, not inverted, when the fix lands.
- *Closes when:* A privileged identity's credential cannot be computed from `dapr.appToken`, and a prod render refuses rather than silently deriving one.
- *Evidence:* `chart/templates/_helpers.tpl — lance.dedicatedServiceToken: printf "%s-%s" $identity $secret | sha256sum | trunc 40` · `chart/values.yaml:2811 externalSecrets.enabled: false` · `grep -nE '^externalSecrets:' chart/values-prod.yaml → no match` · `packages/service-kit/src/service_kit/governed/dapr_auth.py — service_principal binds a privileged subject to service-token-<identity>`

**LH-183 · The maintenance worker is OOMKilled by NATIVE allocation — the Python heap and the Lance session cache are both measured flat**
`maintenance` · **HIGH**
- **THE DIAGNOSTIC WAS NOT RUNNING ON THE LANE THAT RUNS — found and fixed 2026-09-22, deployed and
  observed.** The two readings this row rests on rode `summarize`, which only `run_sweep` calls: the
  SERIAL lane. Every deployment runs the QUEUE lane, whose handler builds its own dict, and the live
  line carried five counters and no memory at all. So for as long as the split has been deployed, the
  instrument for this row has been dark on the only pod it describes — present in the code, present in
  the tests, absent from every tick the estate emits.
  **THE PLANNER STILL DOES THE ACCUSED WORK, which is why this mattered rather than being tidy-up.**
  Splitting execution onto 4Gi workers moved the COMPACTION, not the discovery pass: `plan_sweep`'s own
  docstring says "Every phase here is a metadata read — registries, a bucket listing, one manifest open
  per dataset", and the live tick plans **568 of them every 120s in a 512Mi pod** — the same
  per-dataset open this row narrowed to.
  **`memory_readings()` is now the one seam both lanes call**, pinned as a SET rather than per key, and
  **RSS joined it on the tick line**: the comparison needs both series at the same instant, and taking
  RSS from `kubectl top` means joining two clocks by timestamp against the ~10Mi of sampling spread
  this row already measured. `VmRSS` and NOT `ru_maxrss`, which is a high-water mark and so cannot tell
  a tick that grew and released from one that retained. **OBSERVED on the deployed planner
  (`main-7b996388`):**
  `maintenance_tick_enqueued ... planned=568 published=568 lance_session_bytes=4384301 lance_session_cap_bytes=214748364 python_blocks=891502 rss_bytes=269156352`.
  **`rss_bytes` IS NOT `kubectl top` AND THE TWO MUST NOT BE MIXED:** VmRSS read 256.7 MiB on the same
  pod `kubectl top` reported at 217Mi, because the metrics API reports a container's WORKING SET.
  Within a series each is consistent; across series the difference is the measure, not the memory.
- **THE WORKER HALF IS MEASURED NOW, and a rewrite RELEASES what it takes.** This row's claim is
  native allocation that is never given back, and until today no unit had ever rewritten a fragment,
  so the claim had never met a real compaction. One was built: a 240 MiB table in 60 fragments through
  the catalog, left for the sweep. `/proc/1/status` inside the worker every 2s:
  `294Mi -> 365 -> 414 -> 370 -> 323 -> 427 -> 729 -> 380 -> 316 -> 431 -> 317Mi`, then flat for 60s.
  **Peak +434 MiB over baseline, settling at +22Mi.** The second worker showed the same shape at
  +128Mi. So the bulk IS released; what the row is really about is that +22Mi.
- **ROUND 1 OF THE TWO-COMPACTION EXPERIMENT LANDED CLEANLY (2026-09-22, fourth attempt).** A 240 MiB
  table in 60 fragments, built through the catalog onto a purged lane so its unit was reached in one
  tick. `/proc/1/status` inside the executing worker, every 2s:

  | window | reading |
  | --- | --- |
  | 10:27:10 - 10:27:46 | 303-307Mi, flat (B0 ~ 304Mi) |
  | **10:27:48** | **644Mi** — the rewrite, +340Mi |
  | 10:27:49 | `compaction_distributed_committed`, `fragments_removed=60` |
  | 10:27:50 - 10:28:28 | 315-317Mi, flat 40s+ (B1 ~ 316Mi) |

  **Peak +340 MiB, settled +12 MiB.** The sample landed one second before the commit and the settle
  is unambiguous — forty seconds of flat after it. This is the same shape as the earlier ad-hoc
  reading (+434 peak, +22 settled) on a different worker generation, so the magnitudes are stable
  across runs.
- **ANSWERED 2026-09-22: A REWRITE RETAINS. This row's central claim holds for the worker path.**
  Two compactions of identical tables (240 MiB, 60 fragments) on the SAME worker, `/proc/1/status`
  every 2s, decided by a rule fixed before the data existed:

  | baseline | value | samples | sd |
  | --- | --- | --- | --- |
  | B0 (before round 1) | 302.3Mi | 46 | 4.3 |
  | B1 (after round 1) | **316.7Mi** | 113 | 0.8 |
  | B2 (after round 2) | **370.9Mi** | 48 | 1.2 |

  `B1-B0 = +14.4Mi` · `B2-B1 = +54.2Mi` · threshold was >=10Mi. **Not warm-up:** the planner's own
  discovery pass shows what warm-up looks like — two independent pods stepping to *exactly* 303.9Mi
  at tick 3 and then holding a 2.1Mi band. This does not plateau; it steps again, and both baselines
  here are flat to under 1.2Mi.
- **THE RETENTION IS NOT A CONSTANT PER UNIT, and two points cannot give the rate.** Round 1
  committed once; round 2 committed FOUR times on the same-shaped table, and retained ~4x as much.
  That is consistent with retention per PASS rather than per unit, which would make the cost a
  function of how fragmented the table is rather than of how many units run. Establishing that needs
  a third and fourth round with the pass count recorded.
- **THE PEAK RELEASES; ONLY THE FLOOR RISES.** Round 2 peaked at 724Mi (a second pass at 560Mi) and
  fell to a flat 370Mi within 15s. So [[LH-188]]'s memory bound is still the right shape for the
  peak — what this row adds is that the BASELINE the peak sits on climbs with every rewrite.
- **A VERDICT WAS NEARLY PUBLISHED ON AN ARTEFACT, and the pre-registered rule is what caught it.**
  The first evaluation ran six seconds after the commit and reported `B2-B1 = +71.0Mi` from 16
  samples at **sd 46.6** — the decay tail of the peak, not a baseline. Having B1 at sd 0.8 beside it
  made the discrepancy obvious. The rule fixed what counts as a POSITIVE result but not what counts
  as a VALID measurement; both halves belong in it. B2 was recomputed only once it held sd 1.2 over
  48 samples.
- **THE DECISION RULE IS FIXED BEFORE ROUND 2'S DATA ARRIVES, so it cannot be rationalised after.**
  Measured from round 1's own samples: **B0 = 302.3Mi**, **B1 = 316.6Mi (71 samples, sd 0.9Mi)** — a
  retained step of **+14.3Mi at ~15 standard deviations**, so the baseline is tight enough to decide
  on. But B1 also drifts upward at ~0.85Mi/min (315.9 -> 317.6 over two minutes), which across round
  2's five-minute window is ~4Mi of drift that is NOT retention. Therefore:
  * `B2 - B1` **>= ~10Mi** -> a rewrite RETAINS per unit; this row's claim holds for the worker path.
  * `B2 - B1` **<= ~5Mi** -> consistent with drift alone; round 1's step was warm-up, the same verdict
    the planner's series produced for the discovery pass.
  * between the two -> the experiment does not decide it, and says so rather than picking.
- **WHETHER THAT +22Mi IS WARM-UP OR RETENTION IS THE ROW'S REMAINING QUESTION, and one compaction
  cannot answer it.** The planner's own series settles the analogous question for the discovery pass —
  two independent pods stepped to *exactly* 303.9Mi at tick 3 and then oscillated in a 2.1Mi band, so
  that step is deterministic warm-up rather than a leak. The worker needs the same treatment: two
  successive compactions on ONE worker, comparing the SETTLED baseline between them. `B2 - B1 ~= B1 -
  B0` means it retains per rewrite and this row holds for that path; `B2 ~= B1` means the first was
  warm-up.
  **THE EXPERIMENT IS WRITTEN AND HAS FAILED THREE TIMES, each for a different reason, and the
  reasons are the useful part:** (1) the sampler started 19s AFTER the compaction had committed —
  local clock read as UTC; (2) the work lane was stalled by [[LH-190]], so no second compaction ever
  ran; (3) the catalog PORT-FORWARD had died with a pod restart, so the driver created no tables at
  all — and the runner's own `grep -E "^TABLE=|rows:"` filter swallowed the error, so it reported
  nothing wrong for ten minutes.
  **WHAT A FOURTH ATTEMPT NEEDS, stated so it is not rediscovered:** a driver connection VERIFIED at
  the start rather than assumed; sampler windows compared against the event in UTC; a lane that is
  delivering; and — the one nobody would predict — a work BACKLOG small enough that the new table's
  unit is actually reached. At 7,224 queued and 5.25 units/sec the estate needs ~23 minutes to drain
  before a freshly-created table is even looked at, so each round is ~25 minutes, not 5.
- **THE OOM DOES NOT REPRODUCE TODAY — 14 samples over 13 minutes, 2026-09-22.** The planner held
  **214 -> 217Mi of 512Mi**, max-minus-min **4Mi**, against this row's pre-split baseline of 319Mi
  climbing to OOMKill. Flat inside the +/-10Mi spread the row documents, so a slight upward drift
  cannot be told from cache warm-up at that resolution — which is the argument for the per-tick
  readings above rather than a verdict that the row is closed. The NATIVE question this row is about
  stays open; what changed is that it can now be measured where it happens.
- **THE CAUSE IS NARROWED TO NATIVE ALLOCATION, and every Python-side fix is eliminated by measurement.** Two readings now ride the tick summary (`lance_session_bytes`, `python_blocks`), and on the deployed estate they answer the question the row was opened on:

  | tick | RSS | python_blocks | session |
  | --- | --- | --- | --- |
  | 1 | 187Mi | 895,442 | 4.4 MB |
  | 2 | 210Mi | 895,661 | 4.4 MB |
  | 3 | 229Mi | 896,308 | 14.6 MB |

  **RSS CARRIES ~10Mi OF SAMPLING NOISE and the block count does not**, which is why the conclusion rests on the latter: two independent samplers read tick 3 as 229Mi and 220Mi seconds apart, and an in-pass spot check read 235Mi. So the RSS rise is *tens of Mi* rather than a precise 42. The allocator count has no such spread — **+866 blocks on ~895,000, 0.1%**, against an RSS rise two orders of magnitude larger in relative terms. The session stepped once (4.4 -> 14.6 MB at tick 3, the same step the previous pod made at its tick 3) and accounts for ~10 MB of that; the Python heap accounts for effectively none. So roughly 32Mi of the 42 is outside both: native buffers behind the 585 dataset opens a tick, in Lance or pyarrow, charged to neither the session's caps nor the Python heap.
- **FIVE CANDIDATE FIXES ARE RULED OUT, none of which would have failed a test or a deploy.** Tick-scoping the session, evicting between ticks and lowering the caps all target a cache measured at **7.1% of its 204.8 MB ceiling** and flat across ticks. Trimming `summarize`'s retained `DatasetResult` set and its four side maps (`refusals`, `trashed_datasets`, `index_findings`, `errors`) targets a Python heap that moves 0.1% while RSS moves 26%. The OTel `BatchLogRecordProcessor` runs on the SDK default `max_queue_size=2048` (no `OTEL_BLRP_*` set in the pod), so a bounded queue drops rather than grows — single-digit MB at 585 records a tick.
- **CANDIDATE REFUTED — a fresh `pafs.S3FileSystem` per tick is NOT the cost.** Measured locally 2026-09-21 against pyarrow's own constructor: the FIRST filesystem costs 6.3 MB (the S3 subsystem's one-time init, 57.8 -> 64.1 MB) and the next **100 cost 1.0 MB between them** (64.1 -> 65.1). At one per tick, ~30/hour, that is ~0.3 MB/hour against an observed 10+ Mi per TICK — three orders of magnitude short. (`ru_maxrss` is a high-water mark so it cannot show release, which makes the figure an upper bound and the refutation stronger, not weaker.) The reasoning below is left standing because it is sound and still wrong, which is the point: `objectfs.s3_filesystem` carries no cache (read 2026-09-21), so `sweep._s3fs` constructs a new one on every discovery pass — one per tick at `@every 120s`, ~30/hour — and each holds an AWS SDK client with its own connection pools on the native side, which is exactly the shape `python_blocks` cannot see. **The estate has already made this call once, the other way:** `lance_session` is `@cache`d on its two cap ints precisely so equal arguments share one object, and its docstring says why. `s3_filesystem`'s arguments here are `settings.storage_options()`, fixed at boot, so the same treatment is available. THIS IS UNMEASURED — the mechanism is plausible and the call count is confirmed, but nothing yet shows those objects retaining, and this row has already been wrong twice from a plausible mechanism.
- **THE OPEN PATH ITSELF IS RULED OUT, on local storage, at both axes.** Reproduced 2026-09-21 against the estate's own `lance_session`:

  | workload | opens | RSS | blocks |
  | --- | --- | --- | --- |
  | one dataset, repeated | 1,500 | 177.7 -> 178.2MB, flat from the 100th | +3 |
  | 150 DISTINCT datasets, 5 passes | 750 | 173.2 -> 173.7MB, flat from pass 1 | +4 |

  Neither repetition nor diversity grows anything, and the session plateaus correctly at 0.80 MB after the first pass over 150 distinct uris (LRU re-reads, does not re-insert). So a Lance dataset open is not the cost.
- **THE S3 BACKEND IS RULED OUT TOO, measured INSIDE the running pod** against the real RustFS endpoint (`s3://lance-catalog/medallion/bronze`, `rask-minio:9000`) so the storage path, credentials and network are production's: **750 opens, RSS 153.3 -> 153.8MB and flat from the 50th, allocator blocks unchanged.** That closes the second of the two axes.
- **THE MAINTAIN VERBS DO NOT LEAK EITHER, and the repro now CONTRADICTS the deployed sweep — that tension is the finding.** Driven inside the pod against real S3 datasets, with `compact_one` doing plan + index-inspect (cleanup off):

  | workload | RSS |
  | --- | --- |
  | 375 maintains, ONE dataset | 174.6MB, flat throughout |
  | 190 maintains, 38 DISTINCT datasets | 163.3 -> **193.3MB on pass 1**, then flat for passes 2-5 |

  So a first pass over 38 distinct datasets costs ~30 MB, and four further passes over the SAME set cost nothing. **The cost does NOT scale with dataset count** — re-measured by stepping the distinct set: 25 datasets cost 30.6 MB and 38 cost the identical 30.6 MB, so it saturates early and is a FIXED one-time cost, not the ~0.8 MB-per-dataset rate that dividing the total by the count suggests. (That division was made here first and was wrong; production's climb cannot be a working set filling proportionally.) That is a WORKING SET reached once, not a leak. **But the deployed sweep touches the same 585 datasets every tick and climbs for at least six ticks** (187 -> 250Mi), which a bounded working set cannot do. The repro and production disagree, and resolving that is the next step rather than picking the reading that suits.
  *Bounded honestly:* `ru_maxrss` is a high-water mark, so "flat" means no NEW peak and cannot show release; and the repro drives 38 of the estate's datasets, not 585.
- *What was:* **One axis, by elimination: the maintain operations beyond opening.** Every repro so far opens a dataset and lists fragments; a tick additionally plans compaction, inspects indices and reclaims versions across 585 datasets, and vends a credential per table. The next measurement drives those verbs rather than the open, and this row has refuted four plausible mechanisms while confirming none without measuring — so it is a measurement, not a fifth guess. The sweep opens 585 datasets a tick at `@every 120s` and the reconcile pass walks the estate at `@every 300s`, so the candidates are pyarrow buffers, the S3 filesystem's own pooling, or Lance handles whose native side outlives the Python object. An allocator-level instrument (jemalloc/malloc stats, or `PYTHONMALLOC` accounting) is the next measurement — `tracemalloc` will NOT see this, for the same reason `python_blocks` does not.
- **DISREGARD TICKS 8-9 OF THE LIVE SERIES (385/409/438Mi) — THEY ARE MEASUREMENT CONTAMINATION, MINE.** A repro driving four full `run_sweep` passes was executed via `kubectl exec` INSIDE the pod under observation, so it shared the container's cgroup and counted against both the 512Mi limit and every RSS reading. It reached 438Mi before being stopped, ~74Mi from OOMing the service it was measuring. Once the processes were killed RSS returned to **269Mi**, consistent with the genuine tick-7 reading of 254Mi, and the pod never restarted. **A memory repro must not run inside the pod whose memory is the measurement** — that is the whole lesson, and the earlier in-pod probes (dataset opens, `compact_one`) were safe only because they were small. Two compounding traps worth naming: stopping the local `kubectl` does NOT kill the remote process, and a `case`/grep cleanup scan whose pattern contains the literal it hunts reports ITSELF as a survivor.
- **ANSWERED 2026-09-21 — THE BOUND IS glibc's ARENA COUNT, AND IT IS SIZED BY THE HOST.** The allocator-level instrument this row asked for was `/proc/1/maps`, pulled out of each pod and parsed OUTSIDE the container. Anonymous reservations of 60-68 MB are glibc secondary arenas (`HEAP_MAX_SIZE` is 64 MB on 64-bit), and every lakehouse pod holds dozens:

  | pod | arenas | thread stacks | anon virtual |
  | --- | --- | --- | --- |
  | catalog | 34 | 103 | 6,835 MB |
  | media-to-silver | 40 | 101 | 6,619 MB |
  | medallion-producer | 41 | 109 | 6,996 MB |
  | lineage | 42 | 110 | 6,782 MB |
  | bronze-to-silver | 44 | 109 | 6,925 MB |
  | silver-to-gold | 44 | 108 | 6,924 MB |
  | maintenance | 59 | 117 | 8,797 MB |

  glibc takes its arena cap from `sysconf(_SC_NPROCESSORS_ONLN)` — the HOST's cores — and a cgroup CPU *quota* does not reduce visible CPUs: `nproc` reads **64** in these containers while `cpu.max` reads `100000 100000`, one CPU. This is the same defect `test_lance_sizes_its_compute_pool_to_the_container_not_the_host` pins from the other end ([[LH-172]]: a 64-wide Lance pool against a one-CPU budget), and arenas are the mechanism by which those threads become RESIDENT BYTES.
- **THIS RESOLVES THE TENSION THE ROW COULD NOT, rather than adding a seventh candidate.** The repro measured a FIXED ~30 MB working set — 25 datasets and 38 both cost 30.6 MB — while production climbed for six ticks, and a bounded working set cannot do that. Arena fragmentation does exactly that: each arena keeps its own free lists, is trimmed independently, and never hands memory to another, so RSS settles at the SUM of per-arena high-water marks. **THE COUNT IS NOT WHAT GROWS, measured 2026-09-21 and it corrects the first telling of this:** a container 17 minutes old already held **60** arenas against **59** on the container that had just OOMKilled at 85 minutes. The arenas are established almost immediately; what accumulates is the resident slack INSIDE a fixed set of 60 independent pools, none of which can lend to another. It is FRAGMENTATION, NOT A LEAK, which is why six candidates were eliminated with nothing to replace them and why `tracemalloc`, `python_blocks` and the session caps were all correctly flat.
- **THE LEVER IS MEASURED, NOT ASSUMED — the standard [[LH-172]] set.** That row removed a `LANCE_CPU_THREADS` control after proving it moved nothing, so `MALLOC_ARENA_MAX` was measured before shipping: 64 threads forcing arena growth, counting 64MB mappings in `/proc/self/maps`. Ubuntu glibc 2.39 (host): unset -> **64 arenas**, `=2` -> **1**, `=1` -> **0**. Debian glibc 2.41, in `lance-rest-catalog:heap-blocks` — THE image these pods run, in a THROWAWAY pod: unset -> **65 arenas**, `=2` -> **1**. 2 rather than 1 because a single arena serialises every allocation in a 100+ thread process on one lock. Not a secret, so the never-through-env rule does not reach it.
- **FIXED IN THE REPO, UNFIXED IN THE CLUSTER, and not claimed to work.** `chart/values.yaml` carries `allocator.arenaMax: 2`; `lance.allocatorEnv` renders it onto all seven lakehouse containers; `tests/unit/test_the_lakehouse_bounds_its_allocator_arenas.py` gates it (mutation-checked twice). The image is built and pushed (`lance-rest-catalog:main-426d1ecc@sha256:4a8de33c`). **It is NOT deployed:** rolling the ten-workload `lance-rest-catalog` stem was denied as a shared-cluster mutation, and `k3s-stem-check` rightly refuses `make k3s-up` while the fleet runs four tags. Verified in render and in a throwaway pod only.
- **THE PRE-FIX BASELINE IS 87 MINUTES, and the trajectory PREDICTED it.** Measured on the live pod 2026-09-21: container started `07:35:46Z`, OOMKilled `09:02:38Z` — **1h 26m 52s** from a cold start against the 512Mi limit, `exit=137`, and it restarted into the same climb. The RSS series taken over that life was 187 -> 254 -> 338 -> **403Mi at 60 minutes**; extrapolating it crosses 512Mi at ~85-90 minutes, and the kill landed at 87. **A prediction that lands is the corroboration**: a sum of 60 independently-ratcheting pools produces a straight run into the ceiling, while a bounded working set plateaus and never arrives. (The pools are all present from the start — see above — so the climb is slack accruing within them, not new ones appearing.) This number is what the closing bar is measured against — the fix has to turn 87 minutes into a full day.
- **WHAT IS PROVEN AND WHAT IS INFERRED, stated because this row has been wrong from a plausible mechanism twice.** PROVEN: the arenas exist and are host-sized (60 live, `nproc` 64 against a one-CPU quota); `MALLOC_ARENA_MAX=2` collapses 65 to 1 in the image these pods run. INFERRED, NOT MEASURED: that collapsing them bounds THIS workload's RSS. That is the standard glibc fragmentation argument and nothing here contradicts it, but the only instrument that settles it is the deployed worker's own RSS over a day — so the fix is a well-founded hypothesis until that runs, not a demonstrated cure.
- **DEPLOYED 2026-09-21 AND THE ARENAS ARE GONE, measured in the running pods.** `MALLOC_ARENA_MAX=2` renders onto all seven lakehouse containers (release `rask` rev 195) and took effect in the processes, not just the spec — `/proc/1/maps` pulled from two independent services and parsed outside them:

  | service | arenas | anon virtual |
  | --- | --- | --- |
  | catalog | 34 -> **0** | 6,835 MB -> 4,046 MB |
  | maintenance | 60 -> **0** | 8,148 MB -> 4,148 MB |

  **THE RSS QUESTION IS STILL OPEN AND THAT IS THE ONE THAT CLOSES THIS ROW.** At 28 minutes the fixed worker reads 246Mi with 0 restarts; the pre-fix pod interpolates to ~294Mi at the same age and died at 87. Lower, outside the ~10Mi sampling noise, and NOT yet a plateau — 238/232/232/246Mi across 20-28 minutes is a series this row has been fooled by before (see the flat-from-two-readings note above). What settles it is surviving past 87 minutes, then a full day.
- **THE DEPLOY WAS BLOCKED BY [[XC-054]], WHICH STOPPED BEING A FILED ROW.** `helm upgrade` failed outright: `Secret "sh.helm.release.v1.rask.v194" is invalid: data: Too long`. Revision 193 decoded to 1,041,774 bytes of the 1,048,576 etcd cap — 99.35% — so no upgrade could succeed by anyone, for any change. Cleared by converting `#` comments in 14 templates to `{{/* */}}` (Helm copies the former into the stored manifest and strips the latter), which freed 38,540 gzipped bytes and cost no prose. Headroom 6,802 -> 43,858 bytes, now gated by `tests/unit/test_the_release_secret_stays_under_its_ceiling.py`.
- **THE KNOWN TRADE-OFF DID NOT MATERIALISE, and it was checked rather than assumed.** Capping arenas at 2 makes 100+ threads contend on one allocator lock, which is the standard cost of this fix and would be a regression nobody would attribute to it later. Measured against the estate's own RED metrics in GreptimeDB (mean `http_server_duration_milliseconds`, 15m window, now versus `offset 3h` across the deploy):

  | service | 3h ago | after | change |
  | --- | --- | --- | --- |
  | catalog | 49.7 ms | 34.3 ms | -30.9% |
  | lineage | 1,242.7 ms | 372.9 ms | -70.0% |
  | maintenance | 47,440.9 ms | 38,782.6 ms | -18.3% |
  | medallion-producer | 1,361.0 ms | 1,066.0 ms | -21.7% |
  | **notifications (CONTROL)** | 32.0 ms | 30.3 ms | **-5.3%** |

  **`notifications` is the control and is why the other rows are readable**: it did NOT receive the bound, and it still moved -5.3%, so roughly that much is ambient — node-level memory pressure easing, or load variation — and not attributable here. The four bounded services moved 18-70%, well clear of that floor. The load-bearing result is the NEGATIVE one: no latency regression, so the contention cost this fix is supposed to pay is not visible at this thread count and request rate.
- **THE MEASUREMENT HAS NOT CLEARED 87 MINUTES YET, AND THE REASON IS MINE.** Every deploy restarts the worker and resets the clock, and 2026-09-21 saw three rolls through this stem for other rows. The post-fix pods reached **~68, ~53 and 8 minutes, all with 0 restarts and none OOMKilled** — but the pre-fix baseline is a death at **86m52s**, so none of them has yet outlived it. Encouraging is not the same as proven, and this row has already been fooled once by a series that looked flat.
  **SO THE NEXT STEP IS TO STOP DEPLOYING, not to measure harder.** The fix is in the chart and applies to every new pod; what it needs is an uninterrupted pod. Batch any further stem changes rather than rolling per row, and read the worker again after it has passed 87 minutes and then a day.
  Supporting evidence that does NOT depend on the clock, and is why the expectation is reasonable: arenas 60 -> 0 and 34 -> 0 on two independent services, anonymous virtual address space +610 MB/68min -> -80 MB/17min, RSS at the same age 403Mi -> 262Mi, no latency regression against a control service, and coverage unchanged at 585 datasets with both the sweep and the reconcile lane firing on schedule.
- **THE BASELINE IS CLEARED — MEASURED 2026-09-21, and this is the discriminating result.** The
  uninterrupted pod (`rask-maintenance-5dffdb754d-zkc6s`) reached **97 minutes at 289Mi with 0
  restarts**, outliving the **86m52s** at which its pre-fix predecessor was OOMKilled. That is the
  falsification point this row was waiting on: the earlier ~68/~53/8-minute runs were consistent with
  the fix and also consistent with luck, and none of them could distinguish the two. A pod past the
  age its predecessor died at can.
  **WHAT IT DOES AND DOES NOT ESTABLISH.** It establishes that the bound changes the outcome at the
  age the old failure occurred, which together with the clock-independent evidence above (arenas 60 ->
  0 and 34 -> 0, address space +610 MB/68min -> -80 MB/17min, RSS 403Mi -> 262Mi at equal age) is the
  case for the mechanism. It does NOT yet establish the FULL-DAY closing bar, which is a different and
  slower claim about a plateau rather than a threshold. The pod is still running and still accruing it;
  the medallion deploy that follows this entry rolls only the four medallion deployments (`kubectl set
  image`) precisely so this clock is not reset again, even though they share the
  `lance-rest-catalog` image with maintenance and nine other deployments.
- **THE CLIMB IS 5.2x SHALLOWER AND IS STILL A CLIMB — AND THAT MAY YET FAIL THIS ROW'S OWN BAR.**
  Eight points on the uninterrupted pod (`kubectl top`, maintenance container, minutes/MiB): 47/255,
  63/261, 77/275, 92/289, 97/289, 109/294, 132/312, 148/324. Least-squares slope **0.69 Mi/min** from a
  221Mi intercept, against a pre-fix **3.60 Mi/min** that reached 403Mi at 60 minutes and died at 87.
  So the arena bound did what the mechanism predicted — it cut the rate by 5.2x — and it has NOT yet
  produced the PLATEAU this row's closing bar actually asks for.
  **THE DISTINCTION IS THE ROW'S OWN, and it cuts against the optimistic reading.** This row already
  states it: "a bounded working set plateaus and never arrives", while a ratcheting sum "produces a
  straight run into the ceiling". Eight monotonically rising points fit the second shape with a gentler
  gradient, not the first. If the trend is linear it crosses 512Mi at **~420 minutes (7.0 h)** — which
  would mean the fix bought roughly 5x the time and did not bound the set.
  **SO THE 97-MINUTE RESULT IS EXACTLY WHAT IT SAID AND NO MORE.** It falsified "the bound changes
  nothing at the age the old failure occurred". It does not establish a plateau, and reading it as
  though it did is the error this row has already made once.
  **THE PREDICTION IS RECORDED BEFORE IT RESOLVES, deliberately** — a watcher is armed to report at 420
  minutes or on restart, whichever comes first, and that is the same 420 minutes the fit projects. Either
  it plateaus short of the ceiling, or it arrives and the remaining growth needs a second, different
  cause found rather than the same lever tightened. `kubectl top` reports a working set that includes
  reclaimable pages, so a single reading is noisy; eight monotone ones are the signal, and the pod's own
  `/proc/1/status` read 387Mi of VmRSS at 63 minutes against top's 261Mi, so the two measures disagree
  in LEVEL and must not be mixed when the ceiling is what is being approached.
- **I CONFOUNDED THIS MEASUREMENT MYSELF, AND IT MUST NOT BE READ AS A PLATEAU (2026-09-21).** Between
  minute 250 and 281 the estate lost **fifteen datasets**: twelve chart-path medallion prefixes
  ([[LH-164]]) and three unregistered ones ([[LH-176]]). The sweep and the reconciler both enumerate
  datasets, so the WORKLOAD shrank at the same time the series was being read — and the reading right
  after it (281m, 429Mi) is the first that is not higher than its predecessor (250m 410, 270m 425,
  274m 427, 281m 429... then 429 again).
  **SO A FLATTENING FROM HERE PROVES NOTHING ABOUT THE ARENA BOUND.** Less work per tick is a sufficient
  explanation on its own, and this row has already been fooled once by a series that looked flat. The
  pre-281-minute points remain valid — they were taken against the unchanged workload and give the
  0.69 Mi/min fit — but the fit must not be extended across the change and then read as confirmation.
  **WHAT IS STILL DECISIVE IS THE RESTART, NOT THE SLOPE.** The armed watcher reports on restart or at
  420 minutes, and a restart at any age past 86m52s remains the falsifiable outcome. If the pod instead
  survives, the honest next step is to re-establish a clean series on a pod whose workload has not moved
  under it, rather than to claim the bound from a series whose denominator changed halfway.
- **THE CONFOUND I FLAGGED DID NOT MATERIALISE — MEASURED ACROSS IT (2026-09-21).** I warned that
  reaping fifteen datasets mid-series shrank the sweep's workload and that any flattening afterwards
  would be unreadable. It can now be checked rather than worried about: the slope BEFORE the reap
  (47-274 min, 8 points) is **0.76 Mi/min** and AFTER it (274-336 min) is **0.77 Mi/min**. Removing
  fifteen datasets moved the trajectory by 0.01 Mi/min — nothing. The series is one straight line
  through the change, so the pre-reap fit stands and the post-reap points may be read with it.
  **AND THE POD HAS NOW OUTLIVED MY OWN PREDICTION.** The fit put 512Mi at 344-379 minutes; measured
  at **336 minutes the pod is at 477Mi with 0 restarts**, still running, 35Mi from the limit — about
  45 further minutes at the observed rate. So the prediction is close to its test rather than past it,
  and the outcome is still the restart, not the slope.
  **WHAT A STRAIGHT LINE THROUGH A WORKLOAD CHANGE ACTUALLY SUGGESTS** is worth stating before the
  answer arrives: growth that is indifferent to how many datasets are swept is not the sweep
  accumulating per-dataset slack. That points away from "the bound is insufficient" and towards a
  second, workload-independent source — which is the hypothesis to test if it does reach the ceiling,
  rather than tightening `MALLOC_ARENA_MAX` again.
- **THE ANSWER IS IN, AND IT IS NO: OOMKilled AGAIN AT 7h22m (2026-09-21).** The uninterrupted pod ran
  `12:32:11Z -> 19:54:46Z` and died `OOMKilled, exit 137, restartCount 1` — **442m35s** against the
  pre-fix **86m52s**. So the arena bound bought **5.1x** the uptime and did NOT bound the working set.
  The row's closing bar is a full day; this is a fifth of one.
  **THE PREDICTION LANDED, WHICH IS WHAT MAKES THE MODEL USABLE.** The fit recorded hours earlier put
  512Mi at ~420 minutes from a 0.69-0.77 Mi/min slope; the kill came at 442. A linear model that
  forecasts the ceiling to within 5% is not noise being over-read — the growth really is a straight
  line, and it really does arrive.
  **SO THE NEXT MOVE IS NOT TO TIGHTEN `MALLOC_ARENA_MAX`.** Two measurements rule that out together:
  the arenas were already driven to 0 by the bound (60 -> 0 on maintenance, 34 -> 0 on catalog), and the
  slope was IDENTICAL either side of removing fifteen datasets (0.76 vs 0.77 Mi/min). Growth that is
  indifferent both to arena count and to how many datasets are swept is neither arena fragmentation nor
  per-dataset sweep slack. It is a third thing, workload-independent, and it is what the next
  investigation has to name — with the clock now known to be ~7h20m, which is a long enough window to
  instrument and a short enough one to reproduce twice in a day.
  **WHAT IS ALREADY EXCLUDED, so the next pass does not re-walk it:** the Python heap (measured flat),
  the Lance session cache (measured flat, pinned at 14.6 MB across seven consecutive ticks), arena
  count (0), and dataset count (slope unchanged across a 15-dataset drop). What has NOT been measured
  is native allocation OUTSIDE glibc's arenas — mmap'd regions above the 128 KB threshold, which bypass
  arenas entirely and would be invisible to every check this row has run.
- **THE CAUSE IS NAMED: `MALLOC_ARENA_MAX` BOUNDS AN ALLOCATOR THIS SERVICE BARELY USES (2026-09-21).**
  Measured on the live process from the HOST (`/proc/<pid>/status` and `task/*/comm`; nothing entered
  the cgroup under study). Two allocators besides glibc are resident:
  * **mimalloc**, via pyarrow — `pa.default_memory_pool().backend_name` is **`mimalloc`** on
    pyarrow 25.0.0, and `arrow::mimalloc_memory_pool` is a symbol in the shipped `libarrow.so.2500`;
  * **jemalloc**, via duckdb — 76 jemalloc references in `_duckdb.cpython-313-x86_64-linux-gnu.so`,
    and a live **`jemalloc_bg_thd`** thread in the running process.
  `MALLOC_ARENA_MAX` is a **glibc** tunable. It cannot govern a byte allocated through either, which is
  precisely why the arena count went to 0 and the trend did not move. The bound was not too loose; it
  was bounding the wrong allocator. That reconciles every measurement this row holds: arenas 60 -> 0,
  RSS still linear, slope indifferent to dataset count, death at 442m.
- **A HYPOTHESIS RAISED AND REJECTED IN THE SAME HOUR, recorded so it is not re-raised.** The thread
  count read 98 then 111 three minutes later, which looked like unbounded thread growth — a clean
  workload-independent driver. Sampling it properly showed **oscillation, not growth**: 98 -> 107 -> 98
  across ninety seconds, with `lance_backgroun` going 12 -> 1 as a work cycle ended. Thread count is a
  work signal here, not a leak.
- *What is left:* **ONE ENV VAR, AND A 7h20m CLOCK TO FALSIFY IT.** `ARROW_DEFAULT_MEMORY_POOL` is read
  by the shipped `libarrow.so.2500` (verified with `strings`, not assumed) and `system` routes Arrow's
  allocations through plain `malloc` — which the EXISTING `MALLOC_ARENA_MAX=2` then does govern. So the
  next experiment does not add a lever, it makes the lever already in the chart reach the allocator
  doing the work. The clock is known and short enough to run twice in a day: a pod that passes ~442
  minutes has falsified the old ceiling, and one that plateaus below 512Mi has closed the row.
  A host-side sampler (`/proc/<pid>/status` every 5 min: threads, VmRSS, RssAnon, RssFile, VmData) is
  the instrument; it never enters the cgroup, which is the mistake that corrupted an earlier series.
- **THE EXPERIMENT IS RUNNING, WITH A PAIRED CONTROL (2026-09-21 20:26Z).** The fix is deployed to the
  live worker (`ARROW_DEFAULT_MEMORY_POOL=system` beside the existing `MALLOC_ARENA_MAX=2`, both read
  back off the running pod) and two host-side series are being collected five minutes apart, neither
  entering the cgroup under study:
  * **CONTROL (mimalloc), 4 samples over 15 min:** RssAnon 213,892 -> 232,096 kB — **+1.21 MB/min**,
    RssFile flat at ~138 MB, threads constant at 98. The growth is entirely ANONYMOUS, which is what
    distinguishes allocator retention from page cache and is the shape this change targets.
  * **TREATMENT (system), from 20:26Z.** First reading already differs where the mechanism predicts:
    `VmData` **2.86 GB against the control's 4.03 GB** — about 1.2 GB less virtual data reserved, which
    is mimalloc's large arena reservations not being made. That is a plausibility signal, not a result;
    the slope decides.
  **THE WIRING IS PROVEN, not assumed** — `ARROW_DEFAULT_MEMORY_POOL=system` flips
  `pa.default_memory_pool().backend_name` from `mimalloc` to `system` on this exact wheel, checked
  outside the cluster so the measurement was not perturbed to prove it.
  **WHAT WOULD FALSIFY IT, stated before the answer arrives:** a pod that dies OOMKilled near 442
  minutes again, or an anonymous slope that stays near 1.21 MB/min. What would close the row is a slope
  that flattens and a pod that clears a full day. Either outcome is decided by the same two series, and
  the previous prediction from this fit landed within 5%, so the model is trusted enough to read early.
- **THE CAUSE IS THE DESIGN, NOT THE ALLOCATOR — AND THE OWNER NAMED IT (2026-09-22).** "It should not
  even run stuff in memory, it should BYO workers for doing stuff since operations can be heavy and take
  time." Measured immediately after, and it is exactly right:
  * `api/routes.py:97-125` has TWO LANES. `if settings.work_topic and dapr is not None:` the planner
    PLANS and enqueues units to Dapr/JetStream and returns; **otherwise it falls through to
    `run_sweep(settings)` and executes the whole sweep INLINE.**
  * The live planner's `MAINTENANCE_WORK_TOPIC` is **EMPTY**, so it is on the serial lane. It emitted
    **2,227 `compaction_*` log lines in 30 minutes** — it is doing the compaction itself.
  * Its limit is **512Mi**. The chart's own `dedicatedWorkers` block sizes the WORK at requests 1Gi /
    limits **4Gi** and says why in its own comment: "Compaction reads whole fragments: on bronze, whose
    rows are ~1.8MB page images, `scanBatchSize: 64` is ~115MB in flight before Lance's own overhead."
  So a pod sized for PLANNING is executing work sized for a 4Gi pod. It does not leak; it is doing a job
  it was never sized for, and the only question was how long that takes.
- **BOTH ALLOCATOR FIXES WERE SYMPTOM-CHASING, and the numbers say so.** `MALLOC_ARENA_MAX=2` moved the
  death 87m -> 442m; `ARROW_DEFAULT_MEMORY_POOL=system` moved it 442m -> **460m50s** (OOMKilled, exit
  137, 93 samples, steady-state anon slope 0.63 MiB/min). **The second hypothesis is FALSIFIED on the
  criterion set before the run** ("a pod that dies OOMKilled near 442 minutes again"). Both reduced
  allocator overhead around the work; neither could stop a 512Mi pod doing 4Gi work.
- **AND MY "WORKLOAD-INDEPENDENT" CLAIM WAS UNSOUND — the test was 2.5%.** I argued the slope was
  identical either side of reaping fifteen datasets and concluded growth ignores dataset count. Fifteen
  of **585** is 2.5%; no slope change was detectable at that size, so the reap excluded nothing. The
  dataset-count hypothesis was never actually tested, and the inline-execution finding above is what it
  should have pointed at.
- *What is left:* **BYO WORKERS — enable the plane the chart already ships.** `maintenance.dedicatedWorkers.enabled`
  plus `workTopic` (the chart requires both: "with no queue there is nothing for a worker to consume"),
  which flips `routes.py` to the queue lane — plan, enqueue, and let subscriptions execute and ack for
  themselves on pods sized 1Gi/4Gi. Then the closing bar is measurable as intended: the PLANNER's memory
  should go flat because it stops holding fragments, and the workers absorb the work on a pod sized for it.
- *What is left:* **The REMEDY, not the diagnosis.** The closing bar's second half — "what bounds it
  is named and measured rather than inferred" — is now satisfied: a rewrite costs a transient peak of
  ~1.7x `maxSourceBytes` and leaves **+14 to +54Mi permanently resident**, both measured on the live
  worker. What is undecided is what to do about an allocation the process never returns:
  * **RECYCLE THE WORKER** — count rewrites and exit after N, letting Kubernetes restart it. Crude,
    but it is the standard answer for a native allocator that does not give memory back, and it is the
    only one wholly inside this estate's control. It interacts with [[LH-190]]: every restart strands
    the sidecar's buffer for a full `ackWait`, so the recycle interval and that remedy must be chosen
    together, and a graceful drain would make recycling cheap.
  * **SIZE FOR IT** — pick the pod limit from `baseline + N x retention` for the N rewrites expected
    between natural restarts. Needs the per-pass figure below to be pinned first.
  * **UPSTREAM** — the retention is in Lance/pyarrow's native allocator, not in this code, so a real
    fix is not this estate's to make. Worth reporting with this measurement attached.
- **MEASURED: RETENTION TRACKS COMMITS (PASSES), ~10-14 MiB EACH, and it is close to linear.** A
  third round varied the fragmentation — same 240 MiB in 15 fragments instead of 60 — and landed on
  the other worker, so it is also a replication on a second process:

  | round | fragments | commits | retained | settled |
  | --- | --- | --- | --- | --- |
  | 3 | 15 | 1 | **+9.6Mi** | sd 0.6, n=23 |
  | 1 | 60 | 1 | **+14.4Mi** | sd 0.8, n=113 |
  | 2 | 60 | 4 | **+54.2Mi** | sd 1.2, n=48 |

  `54.2 / 4 = 13.6` per commit, against 14.4 and 9.6 for single-commit runs. So the unit of cost is
  the PASS, not the dataset and not the work item — fragmentation only matters through how many
  passes it provokes. Peaks were 644Mi, 724Mi and 677Mi, all released.
  **THE ARITHMETIC THE REMEDY NEEDS:** a worker doing K passes retains ~12K MiB, so a 4Gi pod over a
  ~300Mi baseline affords roughly 300 passes before the limit. That is a long time on this estate,
  where nearly every unit is a no-op — and short on one where tables are genuinely fragmented, which
  is exactly when maintenance matters most.
  **WHAT THIS DOES NOT ESTABLISH:** three points, one shape of table, one row size. And the second
  worker's own baseline moved ~13Mi between the two windows for reasons not attributed here, so it is
  NOT a clean control and is not claimed as one.
- **THE INSTRUMENT IS DEPLOYED AND ANSWERED ON ITS FIRST PASS (live, 2026-09-22, `main-b301e0ef`).**
  A committed rewrite now carries `rewrite_passes` and `rss_bytes`, so `passes x ~12 MiB` against the
  reported RSS is a prediction the estate checks continuously instead of a figure established once
  over fixtures. First line off the cluster:
  `compaction_distributed_committed ... fragments_removed=70 rewrite_passes=1 rss_bytes=648585216`
  — 618.5 MiB resident at commit, on a 280 MiB / 70-fragment table bounded at 256 MiB, which is the
  ~1.7x peak this row measured. **The peak released:** the worker was 212Mi before and is 236Mi after,
  so the ceiling came back down and the floor moved **+24 MiB for ONE pass**. That is the same order as
  the ~10-14 MiB/pass measured over the retention rounds and about twice it, on a table 2-3x larger —
  a data point that says the per-pass cost is not independent of table size, which the three-point
  fixture series could not have shown. Not a contradiction of the row's answer; a reason the closing
  bar is a soak rather than another three tables.

- *Closes when:* The worker survives a full day of sweep AND reconcile ticks inside its limit with coverage unchanged, and what bounds it is named and measured rather than inferred.
- *Evidence:* arena counts from `/proc/1/maps` on all seven lakehouse pods (table above), parsed outside the containers · `nproc` 64 vs `cpu.max` `100000 100000` measured in-container · the lever measured in-image, Debian glibc 2.41, 65 arenas -> 1 · live 2026-09-21 — `Reason: OOMKilled, Exit Code: 137, Restart Count: 6`, limit 512Mi · the three-tick table above, under `lance-rest-catalog:heap-blocks@sha256:44f4513a8be6` · a prior nine-tick series on the same estate: RSS 192 -> 267Mi with the session pinned at 14.6 MB for seven consecutive ticks · `config.py::shared_lance_session` ("the caps are LRU SOFT bounds") · `docs/DECISIONS.md` § *`compaction_mode` is not a measure of where bytes moved*


**LH-185 · Heavy, unbounded work runs INLINE in pods sized for coordination — the maintenance OOM is one instance of a pattern across all four lakehouse services**
`catalog, lineage, medallion, maintenance` · **HIGH** · OPEN
- **FOUND BY SWEEPING FOR THE SHAPE [[LH-183]] TURNED OUT TO BE (2026-09-22).** The maintenance planner
  OOMKilled because it executed the sweep in its own 512Mi request rather than enqueueing it. A
  16-agent sweep of the other three lakehouse services for the SAME defect class — a handler doing work
  that scales with DATA size or dataset COUNT, in-process, in a pod sized for coordination — returned
  **12 candidates, of which 10 survived adversarial verification** (each verifier instructed to default
  to refuted and to read the code rather than the claim).
- **THE SHARPEST IS THE CATALOG'S INDEX DOOR, and it is the maintenance defect exactly:**
  `indices.py:85-87` calls `_queue_build(...)` and falls straight through to an in-process
  `native.call(ns, "create_table_index", body)` when `settings.maintenance_index_topic` is empty — and
  **empty is the shipped default** (`chart/values.yaml` `indexTopic: ""`, and `services.yaml` gates the
  whole `LANCE_MAINTENANCE_INDEX_TOPIC` env block on it, so the queued lane is unreachable as shipped).
  An IVF_PQ build trains over the table's whole vector column and an FTS build tokenises every row;
  **nothing bounds it** — no batch size, no thread cap — unlike the sibling compact door, which pins
  `batch_size=64, num_threads=2` and whose own comment names the hazard: "rows are not a unit of
  memory, and the default batch size on a blob tier read ~15 GB/thread — the OOM measured on the
  maintenance pod is just as available to the catalog pod through this button". The chart concedes the
  magnitude too: "a compaction unit is minutes and a vector index over a large table is not".
- **THE FULL CANDIDATE SET, recorded so the next pass does not re-derive it:**
  * **catalog** — `indices.py:87` (high): The index-build doors run the build INLINE in the catalog process on the shipped configuration. `create_index` (indices.py:87) and `create_scalar_index` (indices.py:120) call `_queue_build` first, but…
  * **catalog** — `dataplane.py:1547` (high): The change-feed door materialises an unbounded scan three times over in the request handler. `POST /management/v1/table/{id}/changes` (api/v1/endpoints/data.py:644, calling into dataplane at data.py:6…
  * **catalog** — `erasure.py:168` (medium): The erasure door compacts the whole table AND full-scans every retained version, inline, with no queue path at all. `POST /management/v1/table/{id}/erasure` (api/v1/endpoints/erasure.py:42) hands the …
  * **lineage** — `discovery.py:122` (high): GET /graph materialises the ENTIRE lineage estate — every Dataset node, every DERIVED_FROM edge and every WROTE edge in the graph — into the lineage process before its `limit` is applied, so the endpo…
  * **lineage** — `discovery.py:87` (medium): GET /search pulls the whole dataset list AND the estate's entire column inventory into the process on EVERY request and substring-scans them in Python; `limit` (≤100) is applied only after the full sc…
  * **lineage** — `reconcile_cron.py:457` (medium): The Dapr cron handler `_on_cron` reconciles the WHOLE estate inline, in-process, under a cluster-wide lock: an uncapped per-dataset loop that pays up to three AGE round-trips plus five object-store re…
  * **medallion stage runners (bronze-to-silver / silver-to-gold / media-to-silver)** — `compute.py:520` (high): The stage runner's Dapr subscription handler POST /medallion-event runs the whole stage transform IN ITS OWN PROCESS on the in-process lane, full-materialising the entire upstream Lance table AND ever…
  * **medallion-producer** — `media_produce.py:201` (medium): POST /ingest-media harvests the external source prefix inline in the producer's own process, and the two ceilings that exist to bound it are both checked AFTER the unbounded work has already happened:…
  * **medallion stage runners (media lane, external-base tiers)** — `compute.py:641` (medium): The external-blob carry path — written specifically so a stage does NOT materialise the corpus — falls back to reading EVERY blob payload in the tier into a Python list whenever the first 64 rows happ…
  * **notifications** — `inbox_actor.py:340` (medium): Every notification delivery does a whole-partition read-modify-write of the recipient's ENTIRE inbox, in the notifications pod's own process, and the row cap that is supposed to bound that partition (…
  * **notifications** — `reconciler.py:254` (low): The reconcile cron's per-page bound is on ROW COUNT, not on bytes: each page asks lineage for up to 500 events with `summary=false` (the full OpenLineage payload), and the response is buffered whole, …
  * **notifications** — `control_events.py:206` (low): The control-event lane expands a userset grant to its members and then delivers to them in a SEQUENTIAL, uncapped, unbudgeted loop inside the bus handler — one actor round-trip per member, with no pag…
- **ONE OF THE TEN IS CLOSED (2026-09-22): the catalog index door.** `indexTopic` now ships
  `maintenance.index.v1`, so `_queue_build` publishes and the door answers with the unit id instead of
  training an IVF_PQ in the catalog's request handler. Spec-correct rather than merely convenient —
  `lance_docs/ns_catalog/spec.yaml:1705` states "Index creation is handled asynchronously" and
  `CreateTableIndexResponse` carries an optional `transaction_id` and nothing else, so the queued id is
  the whole contract. **Turning the value on exposed three chart holes that the value being empty had
  been hiding, and all three are fixed here:** no JetStream stream captured `maintenance.index.>`, so
  every publish would have landed nowhere (caught RED by the [[LH-151]] gate, both halves — publisher
  and subscriber); `maintenance-index-durable` was missing from `lance.chartDurables`, so the orphan
  pass would have DELETED the index subscription every run — the 2026-07-13 dead-subscription failure
  produced by the loop built to prevent it; and nothing pinned the index lane OUT of the drift loop,
  whose EXP config its 3600s ackWait can never match. Every hop is now gated and each gate was
  mutation-checked: the planner does not subscribe (`execute_work=False`, pinned in
  `test_an_index_build_leaves_the_request_handler.py`), the component is scoped to `catalog`, and a
  failed publish raises rather than answering 200 with a phantom transaction id.
- **TWO OF THE TEN ARE CLOSED (2026-09-22): the second is the change feed.** `read_changes` and its
  `read_deleted_row_ids` sibling now YIELD an Arrow FILE a batch at a time instead of building the
  whole answer three times over (`to_table()`, then the IPC encoding beside it, then `to_pybytes()`
  onto the Python heap). The endpoint answers with `StreamingResponse`, matching the blob door in the
  same file. **Measured, same projection both sides** (200k rows x 256B, 58.4 MB of Arrow over the
  feed's five columns): peak RSS **147.6 MB -> 60.1 MB**, 2.53x payload -> 1.03x, with the wire output
  **byte-identical** (58,410,370 both), same schema, same rows — so it is a pure memory change and no
  consumer can tell. Two things worth keeping: `tracemalloc` reports **0 MB** for the scan and the
  encode because Arrow allocates off the Python heap, so RSS is the only instrument that sees this;
  and the first batch is pulled INSIDE `_user_sql`, because `to_batches()` is lazy and a generator
  body runs after the response has started — a malformed predicate would otherwise become a truncated
  200 instead of a 400. No bound was added because none exists to add: the version window is the only
  cursor a consumer has, and one version can carry the whole table.
- **THREE OF THE TEN ARE CLOSED (2026-09-22): the third is the erasure door.** It reached
  `dataset.optimize.compact_files()` with NO bound, while `services/maintenance.py`'s `compact_now`
  pins `batch_size=64, num_threads=2` and its own comment names this exact hazard — "the OOM measured
  on the maintenance pod is just as available to the catalog pod through this button". Erasure was a
  second such button on the same pod that nobody had counted, and it runs over exactly the tables most
  likely to carry a blob column. The bound is now `COMPACTION_BOUND`, named once and imported, so a
  third door cannot quietly differ. **Also found while writing the gate: TWO residual checks**
  (`_versions_still_matching` and `_answers`) did `to_table(filter=...).num_rows` — materialising every
  matching row to read a count, once per retained version, worst exactly when the subject has the most
  rows. Both are `count_rows` now. The probe that proves this wraps a REAL dataset and records how it
  was asked; `ty` refused it against the `_Dataset` protocol because `__getattr__` is statically
  invisible, so it is `cast` with the reason stated rather than a second copy of the protocol.
- **THE REQUEST SIDE IS ALREADY BOUNDED — checked 2026-09-22, do not re-derive it.** The catalog's
  write doors take `data: Annotated[bytes, Body(media_type=ARROW_STREAM_MEDIA_TYPE)]`, so FastAPI
  buffers the whole upload, which reads exactly like the response defect above. It is not one:
  `main.py:333` applies `BodySizeLimitMiddleware` (64 MiB, `RASK_MAX_BODY_BYTES`) and a
  `WriteConcurrencyLimitMiddleware` beside it. So the asymmetry was real but one-sided — writes were
  capped and reads were not — and closing the read side is what the two rows above did.
- **THE MEDALLION INGEST FINDING IS FALSE — re-measured 2026-09-22.** It claimed `POST /ingest-media`
  "harvests the external source prefix inline" with "the two ceilings both checked AFTER the unbounded
  work has already happened". The code does the opposite (`services/ingest.py:160-200`):
  `iter(source.iter_objects())` is an ITERATOR, `batches()` is a GENERATOR consumed by
  `lance.write_dataset(batches(), ...)` so the write is incremental, and BOTH ceilings are checked
  INSIDE the per-object loop and raise immediately — `if len(source_uris) > max_objects: raise` and
  `if total_bytes > max_total_bytes: raise`. Nothing unbounded is accumulated: `source_uris` is capped
  by the same ceiling that raises, and `chunk` holds at most `chunk_objects`/`chunk_bytes`. This door
  is already the shape the other rows were fixed INTO.
- **SCORECARD FOR THE SWEEP THAT PRODUCED THESE TEN**, now that each has met the code. Two were real
  and are fixed (change feed, erasure). One was MISLOCATED but led to a real defect one hop out (the
  index door was already queued; `indexTopic: ""` was the bug). One is FALSE (this one). Two are
  phase-2 COMPUTE by the FOCUS block's own split, three are phase-3, and the remaining lineage pair is
  a DOCUMENTED TRADE-OFF whose "fix" would break governance paging. So a 16-agent sweep with
  adversarial verification still yielded findings that did not survive contact with the code — the
  verification stage refutes a CLAIM, and cannot tell that the claim is about the wrong layer, was
  fixed last week, or describes a decision somebody made on purpose and recorded in the docstring
  three lines above the flagged call.
- **TWO OF THE REMAINING SEVEN ARE PHASE 2, NOT PHASE 1 — triaged 2026-09-22, do not work them here.**
  The medallion stage-runner finding (`compute.py:520`) is real and the code already states it:
  "Full-materialises payloads into memory, which is fine for this in-process fake-Ray stand-in over
  the cascade's small overwrite-written datasets; a distributed job streams instead." That is the
  in-process lane standing in for the distributed one, which is the same class as maintenance's Ray
  half — the FOCUS block puts both in phase 2 COMPUTE, where BYO lives. Fixing the stand-in's memory
  profile would harden a lane whose replacement is already scheduled. The lineage `/graph` and
  `/search` findings are phase 1 but a DIFFERENT fix shape from the three closed above: both apply
  `limit` AFTER governance filtering in Python, so pushing the bound into AGE changes what `total`
  can honestly report — that needs a count query beside the bounded fetch, not a streaming rewrite.
- **THE LINEAGE ROWS ARE A DOCUMENTED TRADE-OFF, NOT AN OVERSIGHT — and "fixing" them would break
  governance paging.** `repository.list_datasets` says it outright: "Fetch-all + filter/sort in
  Python... Governance and pagination are applied by the endpoint over this full list, so a page is
  taken from the VISIBLE set rather than truncating before the visibility filter has run." `/graph`
  has the same shape — fetch all, `governed(...)`, then cap — and its `total` REPORTS the visible
  count, which cannot be known without enumerating it.
  Pushing `limit` into AGE would page over rows the caller may not see: short or empty pages whose
  length leaks how many hidden rows exist. So the memory cost is the price of a correctness property
  somebody already reasoned about and wrote down. Whoever revisits this needs a different design (a
  governed count query, or FGA-aware filtering in the query itself), not a `LIMIT`.
- **THE TENTH FINDING HAD NO VERDICT AND NOW DOES — `reconcile_cron.py` `_on_cron` (2026-09-22).**
  The scorecard above accounts for nine of the ten and silently skipped this one, which is the same
  failure the register's own counts test exists to catch: an item that is neither closed nor refused
  nor triaged reads as handled. It belongs with the lineage pair — the cost scales with dataset COUNT,
  not data size — and it carries a safety property neither of them has. **Single-flight:** the cron
  fires on every replica and the sweep runs under a cluster-wide advisory lock, so a tick that finds
  one in progress SKIPS and the next retries. An overrun therefore degrades to a less frequent
  reconcile, never to a pile-up. The drain that runs beside it is separately bounded by
  `outbox_drain_limit`.
  **MEASURED LIVE, and it corrects this row's own prose.** The text above says the sweep is
  "completing every ~5 min against a 300s cron", which reads as a sweep taking its whole interval.
  Eleven consecutive ticks over 54 minutes complete **exactly 300s apart** (08:05:52 → 08:35:53,
  sub-second drift) with **zero `lineage_reconcile_skipped_locked` lines** — so the CRON is the pacer
  and no tick has ever found the lock held. `rask-lineage` sits at 191Mi of 512Mi. This is the least
  urgent of the three lineage findings, not an unreviewed one.
- **THE MEASUREMENT STILL STANDS, and it is why this is not urgent — live 2026-09-22.**
  `rask-lineage` sits at **184Mi of a 512Mi limit, 15h uptime, 0 restarts**, with its own reconcile
  tick reporting `checked=483` and completing every ~5 min against a 300s cron. `/graph` and
  `/search` really do materialise the whole estate before applying `limit`, but at 483 datasets that
  costs nothing an operator would notice. This is the DIFFERENCE from [[LH-183]], which had the same
  512Mi limit and was OOMKilled repeatedly: there the work was sized by the DATA (4Gi compaction),
  here it is sized by the dataset COUNT. Fix them when the count grows or when the fix is cheap,
  not ahead of a row with a measured outage.
- **THE PATTERN NOW HAS A MECHANISM AND TWO GATES (2026-09-22), and the first attempt at it STALLED
  THE LANE — which is the part worth keeping.** Bounding memory looked like one number and is two.
  `maxConcurrentUnits` sizes anyio's thread limiter, which serves BOTH the ~568 no-op units a tick
  (two HTTP calls each, I/O-bound) and the rare real rewrite (memory-bound). Setting it to the
  memory-safe figure throttled everything: measured within minutes on the live estate,
  `Unprocessed Messages` went 2,128 -> 4,285 with 346 outcomes in five minutes — **1.15 units/sec
  against the 4.7 the sweep injects** — and the delivery bound shrank with it, because [[LH-188]]'s
  derivation tied the two together.
  **SO THE BOUND MOVED TO THE ONLY STEP THAT HOLDS BYTES.** `services/rewrite_slot.py` is a
  process-wide `BoundedSemaphore` acquired around the rewrite itself — `_execute_one` on the
  distributed path, `compact_files` in-pod — and a no-op unit never acquires it because it never
  reaches one. `maxConcurrentUnits: 40` is THROUGHPUT; `maxConcurrentCompactions: 4` is MEMORY.
  **BOTH HOPS GATED, as an AST walk over the call sites**, because this estate has shipped the
  half-wiring before (`create_table` reached `_write_blob` at two sites, one wired, every test green).
  `rewrite_slots` is REQUIRED rather than defaulted: a defaulted parameter makes an un-wired chain
  look clean. A leg asserts it is not the throughput setting — the mistake named where it would be
  made again.
- **THE EXISTING GATE COULD NOT HAVE CAUGHT IT, and that is a lesson about gates rather than about
  this bug.** [[LH-188]]'s gate ties the lanes' delivery bounds to the fleet's execution capacity, so
  it kept them CONSISTENT while both shrank — `6 + 2 == 4 x 2` held perfectly. **Consistency is not
  adequacy.** The new gate compares the sweep's own cadence with the lane's capacity, two numbers that
  live in different files: `expectedDatasets / scheduleSeconds x secondsPerUnit` units must be
  admissible. `secondsPerUnit` is measured (6 in flight -> 1.15 units/sec -> ~5.2s), and the gate is a
  FLOOR rather than an equality because the units are claim-check POINTERS — over-provisioning
  delivery costs a pointer per queued unit, under-provisioning grows a backlog without bound.
  Mutation-checked against the exact configuration that stalled it.
- **THE PER-UNIT MEMORY FIGURE, which this row and [[LH-183]] both wanted:** a 240 MiB table in 60
  fragments was built through the catalog and left for the sweep; `/proc/1/status` inside the worker
  every 2s went `294Mi -> ... -> 729Mi -> ... -> 317Mi`. One rewrite bounded at `maxSourceBytes`
  (256 MiB) peaked at **+434 MiB resident, ~1.7x the byte bound**, and released cleanly. That ratio is
  what `test_the_worker_can_hold_every_unit_it_admits.py` multiplies against the declared pod limit, so
  raising the byte bound without lowering concurrency now fails the RENDER rather than the pod. The 2s
  interval makes 434 MiB a LOWER bound; the gate's 0.75 usable fraction carries that.
- **THE GATE THE `Closes when` ASKED FOR NOW EXISTS, and writing it found a fourth unbounded door
  (2026-09-22).** `tests/unit/test_a_compaction_door_is_bounded.py` resolves the keywords every
  `compact_files()` call site in the four lakehouse services actually carries — following ONE hop of
  indirection, so a `**`-unpacked dict built locally, updated from a module constant, or handed in as
  a PARAMETER by a caller in the same module all resolve (the last shape is `optimize._rewrite`, whose
  bound is the most carefully built one in the estate and which a naive walk would fail). All four
  doors must pass `max_source_bytes`, `batch_size` AND `num_threads`. Three mutations, three distinct
  legs red: strip erasure's bound → catalog fails; drop maintenance's byte bound → maintenance fails;
  blind the walk → the leg that counts the doors fails, which is the gate this estate has shipped
  before that could not fail.
- **THE FOURTH DOOR IS `compact_one`'s OWN DEFAULTS, and the asymmetry is the tell.** Its three bound
  parameters defaulted to `None`, and `None` meant the keyword was never added to `size_kw` at all —
  so a caller that simply does not mention them gets Lance's 8192-ROW batch, the HOST's core count and
  no byte ceiling. `rewrite_slots` on the SAME function already defaulted the safe way, with the
  rationale written beside it: "a caller that does not care is bounded rather than unbounded". The
  three knobs that bound the bytes defaulted the other way. **The parameter default alone does not
  close it**, and that is the second half: `DatasetWorkItem`'s bound fields are `int | None = None`
  because the wire model must express "the policy said nothing", and the sweep hands
  `plan.scan_batch_size` straight through — so the value crossing the queue for an UNPOLICIED dataset
  is a literal `None`. The floor is now applied where `size_kw` is built, so omitted and explicit-None
  both mean the floor. Numbers named once in `core.config` and read by both the `Settings` defaults
  and the parameter defaults: two spellings of 64 is how a bound gets raised in one place and kept in
  the other.
- **THE CATALOG'S BOUND DID NOT BOUND THE CATALOG, measured 2026-09-22.** `COMPACTION_BOUND` pinned
  `batch_size=64, num_threads=2` and no byte ceiling — a ceiling in a unit nobody can size in advance,
  because on a blob tier one row IS the blob. It is weakest exactly on the tables the erasure door
  runs over, which is the door it was added for. Sized for THIS pod rather than copied from the
  sweep's: the catalog runs at **248Mi of a 512Mi limit** (live), and a pass bounded at B peaks near
  1.7xB resident, so the sweep's 256 MiB would peak ~435 MiB against ~264 MiB of headroom. 64 MiB
  peaks ~109 MiB. A slower one-shot compaction is recoverable; an OOMKilled catalog is an outage for
  every caller of the lakehouse.
- *What is left:* Triage the remaining rows against the fix [[LH-183]] shipped — for each, either a
  queue + a worker sized for the work, or an explicit bound (`batch_size`/`num_threads`) where the work
  must stay in-process. The pattern, not the instances, is the deliverable: a door whose cost is a
  property of the DATA does not belong in a pod sized for a request.
  **PHASE 1 IS DONE ON THIS ROW and the residue is out of phase, which is why it stays open rather
  than closing:** every phase-1 instance is fixed (index door, change feed, erasure, the compaction
  defaults) or accepted with its rationale recorded (the lineage pair, `reconcile_cron`), and three
  gates now cover the three sub-classes — `test_pagination_bounds_are_declared.py`,
  `test_every_lineage_walk_can_be_bounded.py` and the compaction gate above. What keeps it open is
  the FIVE out-of-phase candidates enumerated in this row: two phase-2 (the medallion stage runner's
  in-process lane, the external-blob carry fallback) and three phase-3 (`inbox_actor`,
  `reconciler`, `control_events`). This row is their only record — it closes when they land as rows
  in their own phases, not before, because deleting it would lose the candidate set.
- *Closes when:* No lakehouse handler performs data-scaled work in-process without either a worker lane
  or a declared bound, and a gate refuses a new one.
- *Evidence:* workflow `wf_46997777-6d5`, 16 agents, 12 findings / 10 confirmed · `services/catalog/src/catalog/api/v1/endpoints/indices.py:85-87,264-266` · `chart/values.yaml indexTopic: ""` · `services/maintenance/src/maintenance/services/maintenance.py:327-328 (the bound the index doors lack)` · [[LH-183]] for the measured instance

**CRITERION 1 (provenance survives a write) — MEASURED ON THE LIVE ESTATE 2026-09-22, and it reads clean**
`lineage` · OBSERVATION, not a row
- One full `lineage_reconcile_sweep` tick, read off the running pod rather than reasoned about:
  `checked=483 backfilled=0 storage_loss=1 ungoverned=63 graph_ahead=36 unreadable=23
  dangling_blobs=0 stale=366 contract_violations=0 provenance_holes=0 unknown_to_graph=0
  outbox_drained=0 outbox_stranded=0 outbox_refused=0`.
- **The two sharpest axes are ZERO.** `provenance_holes=0` and `unknown_to_graph=0` — and the second
  is the one that matters most, because `None` there would mean the question went unasked while `0`
  means it was asked and every governed table has a graph node. `SweepReport`'s own docstring records
  **127 governed tables with no node on 2026-09-19**; that gap is closed. `contract_violations=0` and
  `dangling_blobs=0` alongside, and the outbox loop is quiet (`stranded=0`, `refused=0`).
- **THE 1,297 -> 483 DROP IS REAL, NOT A BLIND SWEEP.** The same docstring measured 1,297 graph
  datasets on 2026-09-19 against 483 checked now. `unknown_to_graph=0` is what rules out the
  frightening reading: a sweep that had lost its sight would report `None`, not `0`. The estate
  genuinely shrank this week (bucket reaps, one of them mine and accidental) and the graph tracked it.
- *What is actually left on this axis:* `storage_loss=1` (the graph holds a dataset whose storage is
  gone) and `unreadable=23` (datasets the sweep cannot open — cause unrecorded per dataset in the log
  line, though `unreadable` is a `dict[str, str | None]` carrying the reason in the response body).
  `ungoverned=63` and `graph_ahead=36` are documented as mostly-benign by design and split into their
  own fields precisely so they do not drown these two.


**[[LH-183]] / [[LH-185]] index lane — DEPLOYED AND OBSERVED WORKING 2026-09-22**
`maintenance` · PROOF, not a row
- `make k3s-converge TAG=main-2ffdd524` rolled all ten `lance-rest-catalog` workloads to one tag in
  one helm transaction, which also resolved a release that had drifted to a FIFTH tag none of them
  was running.
- **The split is live and behaving as designed**, read off the cluster rather than the chart:
  `rask-maintenance` 1 replica / 512Mi / `MAINTENANCE_EXECUTE_WORK=false`, and
  `rask-maintenance-worker` 2 replicas / **4Gi** with the flag unset. Both carry
  `WORK_TOPIC=maintenance.work.v1` and `INDEX=maintenance.index.v1`.
- **The planner plans and does not execute** — `planned=567` with **zero** `compaction_distributed` /
  `execute_unit` / `compact_files` lines in its log — while the workers run the units:
  `POST /maintenance-work 200`, each calling the catalog's `compaction_plan` door and answering
  `compaction_distributed_nothing_to_do` for tables already at target.
- **MEASURED MEMORY, which is the whole point of the row:** planner **159Mi of 512Mi** against the
  pre-fix baseline of 319Mi climbing to OOMKill; workers 167Mi and 166Mi of 4Gi. The heavy half is on
  the pods sized for it and the planner is flat.
- **THE INDEX LANE IS LIVE TOO, and proving it took a second deploy** ([[LH-151]]). The first
  converge left `MAINTENANCE_INDEX` declared and absent: the stream job reported `Complete 1/1` while
  `nats stream add` had died on "cannot ask for confirmation without a terminal", and the worker's
  sidecar retried forever against `nats: no stream matches subject`. After the `--defaults` fix and a
  redeploy (job r200): the stream exists, **0 subscribe failures in 90s** where there had been a
  continuous stream of them, and `maintenance-index-durable` is bound with **ackWait 1h0m0s** — the
  window that justified giving this lane its own component, an order of magnitude past the work
  queue's 720s.
- **THE LANE IS HEALTHY UNDER ITS CURRENT LOAD**, read off the consumer rather than inferred:
  `maintenance-work-durable` reports `Unprocessed 0`, `Redelivered 0`, `Ack Floor 44,682` — nothing
  queued undelivered, and no unit has ever exceeded the 720s ack window and been redelivered.
- *Not yet observed, and the caveat got sharper on inspection:* every table this tick was already at
  target, so the 4Gi headroom has not been exercised under real compaction load. The proven claim is
  "the planner no longer does the work", NOT "a large compaction fits in 4Gi". And the consumer shows
  **`Ack Pending 181`** across two workers with no `maxAckPending` or concurrency bound on the work
  pubsub component — so the split bounds the pod SIZE but not the number of units in flight. 4Gi is
  sized for *a* compaction, not for however many land at once; the real ceiling today is FastAPI's
  threadpool, which is an accident rather than a decision. Harmless while every unit is a no-op at
  167Mi; worth a bound before a tier with real rewrite work arrives.


**LH-190 · A worker restart orphans every in-flight unit, and the lane delivers nothing for a full `ackWait`**
`maintenance` · **HIGH** · OPEN
- **MEASURED LIVE 2026-09-22, and it is the true cause of every "stall" read today.** Two minutes
  after a rolling restart the consumer reports:
  `Last delivery: 1m57s ago` · `Outstanding Acks: 152 out of maximum 152` · `Ack Wait: 12m0s` ·
  `Redelivered: 0` · `Unprocessed Messages: 4,420` · worker pods aged 2m10s and 2m20s.
  Every outstanding unit was delivered to pods that no longer exist. JetStream will not deliver
  another until `ackWait` expires on them, so **a deploy costs this lane ~12 minutes of total stall.**
- **THE STALL IS TOTAL, NOT PARTIAL, AND IT IS MEASURED TO THE UNIT.** Across the outage the
  consumer's `num_pending` reads `5,560 -> 6,130 -> 6,700 -> 7,270` on successive samples: **+570
  each time, exactly one tick's injection, with ZERO units leaving.** `num_ack_pending` sat frozen at
  82 against a bound of 72 for over four minutes while `redelivered` stayed 0. So this is not a lane
  running slowly — it is a lane delivering nothing at all, and the arithmetic says so without needing
  a rate.
- **THE UNITS ARE NOT SLOW — 0.21s, measured from each unit's own trace span** (19 traces, median
  0.12s, p90 0.60s, max 1.17s). A no-op unit is two HTTP calls plus a base-ref pre-pass of ~16 reads
  at ~8ms. The lane's steady-state capacity is therefore enormous next to the 4.73 units/sec the
  sweep injects; throughput was never the constraint.
- **IT POISONS EVERY RATE-BASED MEASUREMENT TAKEN NEAR A DEPLOY, which is how it stayed invisible.**
  Drain rates read inside the window gave "5.2s per unit" and then "22s per unit" for a quantity whose
  real value is 0.21s, and each of those drove a configuration change. A rate measured after a restart
  measures the orphan block.
- **RAISING `maxAckPending` MAKES IT WORSE, which is the counter-intuitive part:** the bound is exactly
  how many units a restart can orphan. The lane that was unbounded (NATS's 1,000) could orphan a
  thousand.
- **THE WHOLE CYCLE WAS WATCHED END TO END, so the mechanism is observed rather than inferred.**
  Last delivery `09:55:57`; the consumer then sat frozen for twelve minutes while `num_pending` grew
  by exactly one tick's injection each sample. Recovery, to the second:
  `10:07:26 ack_pending=80/72 outcomes=0` · `10:07:51 ack_pending=72/72 redelivered=0 -> 18,
  pending 7,270 -> 7,220` · `10:07:57 outcomes_last_60s=51`.
  **Delivery resumed 11m54s after it stopped — `ackWait` is 12m0s.** `redelivered` moving off zero is
  the expiry releasing them; nothing else changed, no pod restarted, no configuration was touched. So
  the recovery time IS `ackWait`, exactly, and that is the number any remedy has to beat.
- **THE DRAIN MACHINERY ALREADY EXISTS AND DOES NOT COVER THIS, which narrows the remedy.** The app
  arms a drain on SIGTERM (`service_kit.draining.arm_drain_on_sigterm`) and `work.py:87-98` answers a
  NEW delivery with `retry_when_draining` — "ask for redelivery rather than start work". The pod has
  `terminationGracePeriodSeconds: 120`, a `preStop` of `sleep 5`, and the sidecar carries
  `dapr.io/block-shutdown-duration: 20s`. At 0.21s a unit, everything actually dispatched finishes in
  about a second, well inside all three windows.
  **SO THE STUCK UNITS WERE NEVER DISPATCHED. `Redelivered: 0` is the proof:** had the app seen them
  while draining it would have asked for redelivery and the counter would move. They were delivered by
  JetStream to the SIDECAR, held in its buffer awaiting dispatch, and died with it — unacked and
  un-NAK'd, so only `ackWait` can release them.
- **THAT PUTS THE SIZE OF THE PROBLEM EXACTLY AT `maxAckPending`,** which is the tension with
  [[LH-188]] and has to be decided together: the bound that keeps the lane fed is also the buffer a
  restart can strand. Observed here: `Outstanding Acks: 152 out of maximum 72` — MORE outstanding than
  the current bound allows, because the 152 were stranded under the previous, larger one, and
  JetStream will deliver nothing until enough of them expire to fall below 72.
- **EVIDENCE AGAINST THE CHEAPEST REMEDY, and it is an INFERENCE rather than a measurement.** The
  sidecar already carries `dapr.io/block-shutdown-duration: 20s`, so the obvious first move is to
  raise it. But `Redelivered: 0` says the app never saw those units — had it, the drain would have
  asked for redelivery and that counter would have moved — so the sidecar did not dispatch its buffer
  during the 20s it already blocks for. At 0.21s a unit, 20s is ample time to dispatch 152 of them,
  which is what makes the reading suggestive. **NOT TESTED DIRECTLY:** confirming it means watching
  `daprd` logs through a termination, which costs another full outage, so it is recorded as the
  inference it is rather than as a result.
- **IT COSTS TIMELINESS, NOT DURABILITY, and that bounds how urgent the remedy is.** The stream is
  `Retention: WorkQueue`, `Maximum Age: 7d`, messages and bytes unlimited, holding **7,350 units in
  6.8 MiB** at the peak of the outage — ~1 KB each, which is the claim-check POINTER shape working as
  designed. Nothing is dropped and nothing is lost: a stalled lane means maintenance runs LATE, and
  the next tick re-plans whatever is still owed anyway. So this is a resilience defect about
  RECOVERY TIME, not about work disappearing, and it should be weighed as one.
- **THE RECOVERY TAIL IS MOSTLY REDUNDANT WORK, and the mechanism to collapse it already exists and
  is INERT.** Every tick publishes ALL datasets (`planned=568 published=568`, every tick) and a
  work-queue unit is removed only on ACK — so a backlog of 7,300 over 568 datasets IS ~13 queued
  copies of each. That is a logical consequence of two measured facts, not an estimate, and it is why
  the tail is hours: the fleet is re-doing the same estate a dozen times.
  **THE STREAM CARRIES `Duplicate Window: 2m0s` — exactly the sweep interval — AND THE PUBLISHER
  DEFEATS IT:** `Nats-Msg-Id` is a fresh UUID per publish (read off a live message:
  `Nats-Msg-Id: ba6719fc-c471-4853-9cf2-fc2c5783a7ae`), so JetStream's de-duplication can never match
  anything. An id STABLE per dataset would make the broker drop a republish while the previous unit
  is still queued.
  **IT IS NOT A FREE WIN, which is why it belongs in the ruling rather than in a commit.** The window
  is TIME-based, not pending-based: a dataset maintained and ACKED at t=0 whose next unit publishes
  at t=120s sits exactly on the 2m boundary, so a stable id risks silently SKIPPING a legitimate
  re-plan rather than collapsing a redundant one. Making the planner skip datasets with a unit
  already pending is the semantically correct version and needs the planner to see the queue, which
  it currently cannot.
- *What is left:* Decide how a shutting-down worker releases what it holds. The candidates are a
  graceful drain on SIGTERM (finish or NAK the outstanding units, so they redeliver at once rather
  than after 720s), a shorter `ackWait` (bounded below by the longest single compaction, so it cannot
  go far), or accepting the window and not measuring inside it. The first is the only one that removes
  the stall rather than shortening it.
- *Closes when:* A rolling restart of `rask-maintenance-worker` is followed by delivery resuming in
  seconds rather than in `ackWait`, observed on the live consumer, and a gate covers whichever
  mechanism is chosen.
- *Evidence:* live `consumer info` 2026-09-22 (above) · unit trace spans (19 traces, p90 0.60s) ·
  `chart/templates/dapr-component.yaml` (`ackWait: 720s` on the work component) · [[LH-188]] for the
  bound this interacts with


**LH-191 · The sweep re-plans the WHOLE estate every 120s because no policy sets a cadence**
`maintenance` · **MEDIUM** · OPEN
- **MEASURED LIVE 2026-09-22.** Every tick reports `planned=570 skipped=7`, and those 7 are the trash
  exclusions the code names — **not one dataset is skipped for cadence.** At `@every 120s` that is
  ~17,100 units an hour, re-planning an estate in which almost every table is already at target and
  answers `compaction_distributed_nothing_to_do`.
- **THE CONTROL EXISTS AND NOTHING USES IT.** `sweep._policy_skip_reason` implements
  `compact_interval_hours` — "skips until the interval has elapsed since the sweep's own per-dataset
  `last_maintained_at` stamp" — with the fail-safe already thought through (an unreadable, absent or
  malformed stamp MAINTAINS, so a lost stamp cannot silence maintenance). The estate registers 27
  policies (`policies=27` on the planner) and the live planner carries
  `MAINTENANCE_POLICY_ROOT = None` against a chart default of `policyRoot: ""`. Whatever those 27
  cover, the zero interval-skips say none of them sets one.
- **THE REGISTRY WAS READ, so this is no longer an inference from a skip count.** Under
  `s3://lance-catalog/_policies/`: **27 `table-*.json` policies — every one of them
  `compact_interval_hours: null` and `compact_enabled: true`** — beside 181 `dataset-*` stamps
  carrying `{"last_planned_version": N}`. The field is PRESENT on every policy record and SET on
  none.
- **AND THE 27 COVER 27 TABLES, not the estate.** Their ids are all of one family
  (`media$annotations_m1_*`), so even with intervals set they would skip 27 of ~570 datasets. The
  lever therefore needs two things, not one: intervals declared, AND coverage for the rest — either
  more policy records or a global default the sweep falls back to.
- **THE MECHANISM IS COHERENT AND WOULD WORK — checked, so it is not blamed for this.** The interval
  check reads `last_maintained_at` from `_policies/state/` via `_state_key(record, uri)`, a key the
  code documents as existing "only for datasets carrying a policy with an interval"; the
  `dataset-*` objects in that prefix are the EVENT lane's, keyed by uri alone, and are a separate
  mechanism. With an interval set, the first tick finds no stamp, maintains (the documented
  fail-safe), writes one, and later ticks skip. Nothing here is broken.
- **THE EMPTY `MAINTENANCE_POLICY_ROOT` IS NOT THE CAUSE EITHER — also checked.**
  `resolved_policy_root` is `self.policy_root or f"s3://{self.s3_bucket}"`, so blank falls back to the
  estate bucket, which is where those 27 were found.
- **THE REDUNDANCY CLAIM WAS TESTED BY PURGING THE QUEUE, 2026-09-22.** If a backlog is genuinely
  ~13 re-plans of each dataset, deleting it costs nothing — the next tick republishes the estate.
  Done on the live lane: `stream purge MAINTENANCE_WORK` took **7,194 messages (6.6 MiB) to 0**. The
  very next tick logged `planned=570 published=570`, both workers and the planner stayed Running with
  zero restarts, and the stream settled at **518 messages** — one tick's worth — instead of 7,194.
  **Nothing was lost, because there was nothing there that the next two minutes would not produce
  again.** That is the row's claim demonstrated rather than argued, and it doubles as the operational
  recipe: a lane buried by an outage can be purged rather than waited out.
- **IT IS THE VOLUME BEHIND TWO OTHER ROWS.** The work lane keeps up by only ~9% ([[LH-188]]'s
  bounds are sized against 4.73 units/sec) and an outage's backlog drains at ~1,600 units an hour, so
  [[LH-190]]'s stalls take hours to clear. Both numbers are consequences of planning 570 datasets
  every two minutes; a cadence that skipped tables maintained an hour ago would cut the steady-state
  volume by most of itself and shorten every recovery in proportion. It attacks the VOLUME where
  those rows attack delivery and recovery.
- **WHY THIS IS NOT JUST A VALUE TO SET:** the right interval is a statement about how often a
  governed table genuinely needs compacting, and it differs per tier — a bronze blob tier taking
  continuous ingest is not a gold table written once a day. The mechanism is per-POLICY for exactly
  that reason. Picking one global number would be the same mistake as the thread-limiter bound: one
  knob answering two questions.
- **THE CLOSING CONDITION WAS UNOBSERVABLE, AND NOW IS NOT (2026-09-22).** This row closes on "a tick
  reports a non-zero cadence skip count" — and the tick could not have reported one. The queue lane's
  summary carried `skipped` as a single integer over everything `plan_sweep` decided without work, so
  a trash exclusion, a `compact_enabled: false` opt-out and a `policy_interval` skip were the same
  number. The moment somebody sets an interval, the only evidence it took effect would be `skipped`
  moving 7 -> 8, which one more dataset reaching the trash does identically. `skipped_by` now carries
  the breakdown beside the unchanged total (an alert reads the total), attributing the reason literal
  each `DatasetResult` already holds rather than classifying anything anew, with an `unattributed`
  bucket so the parts always sum to the whole. **The serial lane already split these** — `summarize`
  has separate `skipped` and `trashed` keys — so the same key meant different things on the two lanes,
  and the lane every deployment runs was the coarse one. Pinned by
  `test_a_skip_says_which_kind_it_was.py`, whose third leg reproduces today's estate exactly:
  `{"trashed": 7}` and no cadence key at all.
- *What is left:* Decide whether the estate declares cadences per policy and what they are, or whether
  planning everything every tick is intended. **The ruling is now a value, not a value plus an
  instrument** — set an interval on a policy and the very next tick says whether it took. If intended, the two rows above are sized correctly and
  nothing further is needed; if not, this is the cheapest lever on both.
- *What the ruling has to cover:* not just "what interval", but WHICH DATASETS — 27 policy records
  exist against ~570 datasets, so a per-policy interval alone moves 5% of the volume.
- *Closes when:* A tick reports a non-zero cadence skip count, or this row records the ruling that
  re-planning the whole estate every 120s is deliberate.
- *Evidence:* live planner 2026-09-22 `planned=570 skipped=7` on every tick · `policies=27` ·
  `MAINTENANCE_POLICY_ROOT = None`, `chart/values.yaml policyRoot: ""` ·
  `services/maintenance/src/maintenance/services/sweep.py:103-118` (`compact_interval_hours` and its
  fail-safe) · [[LH-188]] for the bounds this volume sizes, [[LH-190]] for the recovery it lengthens


## PHASE 1 · CROSS-CUTTING

**XC-001 · Helm-written Secrets carry no content checksum and ESO-written Secrets have no watcher, so a rotation never reaches running pods**
`chart, frontend-zones, lineage` · **HIGH**
- **OVERLAP WITH [[LH-160]] RECORDED, NOT FOLDED (2026-09-20).** The audit called this a duplicate ratchet —
  23 residue entries of which 14 are zone OIDC/session, 1 compute, 8 third-party images. The overlap is
  real and the two must be worked together. They are NOT the same row: LH-160 counts what is delivered
  through env, while this one is about whether a ROTATION reaches a running pod, which is a different
  property with a different fix — the lineage token now arrives as a file and is re-read per request,
  which lowered LH-160's count as a side effect rather than as the point.
- **MECHANISM (1) IS DONE (2026-09-19) and the premise is now measured rather than reasoned.** Every zone carries `checksum/frontend-session`, hashed over the two fields the Secret actually holds (`frontend.oidc.sessionSecret` + `dex.clientSecret`) rather than over the template — `include`-ing `frontends.yaml` from inside itself recurses until helm gives up, since the Secret and its seven consumers live in one file, and hashing the values is the tighter answer anyway (an unrelated template edit no longer rolls seven zones). The row's claim that `checksum/infra-credentials` is inert under ESO is now A TEST, not a reading: `test_the_infra_checksum_IS_a_constant_under_external_secrets` renders with `externalSecrets.enabled=true`, rotates `age.password`, and asserts the annotation is UNCHANGED. Beside it, `test_rotating_the_SESSION_secret_rolls_every_zone_under_external_secrets` rotates the sealing key under the same values and asserts all seven move together — half the fleet on the old key and half on the new is a cookie that verifies on one zone and 401s on the next, indistinguishable from a user who is simply signed out.
- **MECHANISM (2) IS DONE FOR THE LINEAGE TOKEN (2026-09-20), and it needed no watcher.** The row offered two shapes — "install a Secret-object watcher that restarts consumers, or read the token from a projected volume per request" — and the second is strictly better here because the standing secrets rule already requires it: a zone has no Dapr sidecar, so its sanctioned delivery is an ESO-managed Secret taken as a MOUNTED FILE, and what rides in the environment is the PATH. `readSecretFile` (`@rask/api/bff`) re-reads per call, so an ESO rotation lands with no restart and no reloader in the chart. A watcher would have entrenched the banned delivery while treating its symptom. **This also moves [[LH-160]]'s ratchet: `SECRET_ENV_BASELINE` 30 -> 23**, the seven zones' `LINEAGE_SERVICE_TOKEN` leaving the environment together. The mount uses `items` to narrow to ONE key — `infra-credentials` also holds MinIO and Postgres credentials, and mounting it whole would put all of them in a zone's filesystem to deliver one token. **THE FIRST ATTEMPT WAS SILENTLY WRONG AND THAT IS WHY THERE IS A GATE.** Changing `makeLineageProxy` in `@rask/api` and dropping the chart env looked complete; measured, seven zones never call that factory — they read `env.LINEAGE_SERVICE_TOKEN` directly in nine `.remote.ts` files, so the removal left all nine reading `undefined` while the zone still served 200 at its root, because the token is only consulted on a lineage call. All nine now take the file, and `frontend/packages/zone-contract/src/secret-from-file.test.ts` walks every zone and package source to refuse `env.<SECRET>` as a value while allowing the `_FILE` path. Mutation-checked both ways: pointing a zone at another identity's key reds `test_the_web_bff_presents_its_own_credential`, and restoring one `env.LINEAGE_SERVICE_TOKEN` reds the new gate. **OBSERVED on the deployed estate (`web-home:xc001-file2`):** `LINEAGE_SERVICE_TOKEN` absent from the pod's environment, `LINEAGE_SERVICE_TOKEN_FILE=/etc/rask/service-token/token` present, the file readable at 40 bytes, the zone serving 200 with a clean log — and the token read from that file authenticates, `GET /events` answering **200** from inside the pod with `dapr-api-token` + `x-lance-service-identity`. Rotation proven on a scratch mount: `original-value` -> `ROTATED-value` in the SAME pod uid, no restart.
- *What is left:* **The remaining 23, and the two OIDC secrets first.** `SESSION_SECRET` and `OIDC_CLIENT_SECRET` are still `secretKeyRef` env on all seven zones (14 of the 23) and take the same mount — they are held back only because they are the sign-in path, where a mistake locks every user out rather than degrading one feed. The other nine are `rask-minio` (2), `openfga` (1), `otel-collector` (1), `age` (1), three Jobs (3), and `compute` (1, the one WITH a sidecar, whose path is the Dapr secret store and which `_UNREACHABLE_STORE` records as not in `lance-secrets`' scopes).
- *Closes when:* Rotating `frontend.oidc.sessionSecret` rolls every zone (gate RED→GREEN under live values), and an ESO refresh of `rask-infra-credentials` restarts or is re-read by its consumers.
- *Evidence:* `chart/templates/frontends.yaml:111,296-303,397-402` · `tests/unit/test_a_rotated_secret_reaches_the_pods_that_hold_it.py:32-43,104-116` · `chart/values.yaml:2420,2817` · `rg -i reloader chart/ → NATS config reloader only`

**XC-003 · `lance.audit` shares `opentelemetry_logs` with all telemetry under the estate-wide 14d TTL, and `:4000/v1/sql` accepts unauthenticated writes and DELETEs in-cluster**
`catalog, lineage, medallion` · **HIGH**
- **blocked:** HOW the GreptimeDB credential is delivered, because the obvious path is closed and the row's own sentence hides it. The row says "the subchart supports `auth.enabled` with a static `passwd` file" — true, and measured 2026-09-20 it builds that file from `.Values.auth.users[].password`: `greptimedb-standalone/templates/users-auth-secret.yaml` renders `stringData` straight out of values into a FIXED-name Secret (`<fullname>-users-auth`), and the pod mounts that name unconditionally. So taking the supported path means putting a password in a chart value, which the standing secrets rule forbids in as many words ("not a chart value"). The subchart offers `existingSecretName` for OBJECT STORAGE credentials and nothing equivalent for auth, so ESO — which is live on this estate and syncing (3 ExternalSecrets, all `SecretSynced` against `rask-vault`) — has nowhere to write. The choice is therefore: (a) vendor the subchart and add `auth.existingSecretName`, mirroring the object-storage block it already has, (b) take auth away from the subchart entirely and have rask own the volume and `USER_PROVIDER` path with ESO writing the Secret, or (c) upstream the same field and wait. **A NetworkPolicy is NOT an available answer** — the defect is that GreptimeDB is configured to accept unauthenticated SQL, so fencing it at the network is the outer-layer workaround `CLAUDE.md` refuses.
- **THE HOLE IS STILL OPEN, RE-VERIFIED 2026-09-20 on the deployed estate:** against a scratch table, unauthenticated `CREATE` 200, `INSERT` 200, `DELETE … WHERE v='probe'` **200 `{"affectedrows":1}`**, `DROP` 200. The governance trail remains erasable by anything that can reach `:4000`.
- **AND THE BLAST RADIUS IS SMALLER THAN THE ROW IMPLIES, measured.** "Put a credential in front of `:4000` carried by the Collector, the TTL hook, vmalert, Perses and all seven zones" reads estate-wide; it is not. **23 of the fleet's deployments export OTLP to `rask-otel-collector:4318`, not to Greptime** — every one of them is insulated, and 0 deployments name `:4000` in an OTEL endpoint. The direct consumers are the Collector and the TTL job (writers) plus vmalert, Perses and the home zone's audit viewer (readers). Five, in seven chart templates. That is a bounded change once the delivery question above is answered.
- **THE TABLE SPLIT IS DONE AND OBSERVED (2026-09-18); the CREDENTIAL clause is what is left.** The Collector routes `body == "audit"` to `lance_audit` via `x-greptime-log-table-name` (verified against the running GreptimeDB v1.1.1 with a real protobuf record, not from docs), the retention hook gives that table its own TTL, and the viewer reads it from the same chart value. **Measured after the upgrade:** `lance_audit` carries `ttl = '1year 1month 4days'` against `opentelemetry_logs`' `14days`; over 90 s, **672** audit rows landed in `lance_audit` and **0** in the shared table. The hook WAITS for the table (36 × 5 s) because the Collector creates it on the first audit record and creates it INHERITING the database TTL — a single attempt left the live table on 14 days, which is how this was found.
- *What is left:* The credential clause, unchanged and now measured rather than assumed. `:4000/v1/sql` accepts an unauthenticated in-cluster caller: CREATE 200, INSERT 200, `DELETE … WHERE v = 'probe'` **200 `affectedrows: 1`**, DROP 200, from inside the cluster with no auth at all. So the governance trail is erasable by anything in the cluster; the split changed what ages out, never who may erase it. Put a credential in front of `:4000` carried by the Collector, the TTL hook, vmalert, Perses and all seven zones, with read separated from write — and note it collides with the 'never a secret through env' rule, so the delivery path is part of the decision. The subchart supports `auth.enabled` with a static `passwd` file (`GREPTIMEDB_STANDALONE__USER_PROVIDER`).
- *Closes when:* An unauthenticated in-cluster `POST /v1/sql` DELETE is refused (the table, its TTL and the viewer are done).
- *Evidence:* `chart/templates/otel-collector.yaml (`grep -n 'routing|audit'` → no pipeline hits)` · `frontend/microfrontends/home/src/lib/server/audit-core.ts:144` · `chart/values.yaml:2888 (retention: "14d") and chart/templates/greptimedb-ttl-job.yaml:8` · `chart/charts/greptimedb-standalone-0.4.5.tgz values.yaml:231-239 (auth block unused by the estate)`

**LH-160 · 30 rendered secrets still arrive through the environment, `compute` cannot reach the secret store, and the Ray head's six live entries are outside the gate**
`chart, service-kit, compute` · **HIGH** · PARTIAL
- **blocked:** For the 29 no-sidecar and infra entries only: XC-002's owner ruling on whether an ESO-written Secret delivered by `secretKeyRef` satisfies 'never secret through envs' — it decides whether those move to file mounts or are excluded from the count. The `compute` flip needs no ruling.
- *What is left:* The ratchet stands at `SECRET_ENV_BASELINE = 30` / `WITH_SIDECAR_BASELINE = 1`, the one sidecar entry being `compute`, recorded in `_UNREACHABLE_STORE` because `lance.secretScopes` never appends its app-id. Add `compute` to that scope derivation in `_helpers.tpl:1332-1360`, render `RASK_APP_TOKEN_FROM_STORE` for it via `lance.appTokenEnv`, drop its `APP_API_TOKEN` row, lower both baselines (30→29, 1→0) and observe a real Dapr delivery, not a boot log. Then the 22 no-sidecar entries (`LINEAGE_SERVICE_TOKEN`, `OIDC_CLIENT_SECRET`, `SESSION_SECRET`) and the 7 infra rows go to mounted files, or are excluded, per the ruling; whether each third-party image accepts a file mount is unverified. Count the out-of-chart plane: `deploy/ray-lance-demo.yaml` carries 6 `secretKeyRef` entries the gate cannot see, so a green 0 reads as enforcement while that manifest is uncounted.
- *Closes when:* The baseline reaches 0 (excluding LH-161) with the ruling's exclusions named in the gate, and a test counts secret env delivery in every manifest the estate applies, chart or not.
- *Evidence:* `tests/unit/test_secret_env_delivery_only_shrinks.py:41,46,221` · `chart/templates/_helpers.tpl:1332-1360 (scopes: catalog, lineage, maintenance, medallion, explorer services — no compute)` · `chart/templates/_helpers.tpl:1395 (lance.appTokenEnv renders RASK_APP_TOKEN_FROM_STORE)` · `grep -c secretKeyRef deploy/ray-lance-demo.yaml → 6`

**XC-004 · `externalSecrets.enabled` defaults false, and the Dapr app token and the zones' OIDC/session secrets are helm-rendered Secrets outside ESO**
`chart, frontend zones, explorer` · **HIGH** · PARTIAL
- **blocked:** A `helm upgrade` release with `externalSecrets.enabled=true` — the estate carries hand-deployed images a values-mismatched upgrade would revert to chart defaults
- *What is left:* `chart/templates/external-secrets.yaml` (gated on the toggle) already syncs `infra-credentials` (postgres, minio, ray-compute, dex-client-secret, every `service-token-*` including the zones' `LINEAGE_SERVICE_TOKEN`), `observability-s3` and `ray-auth-token`. Still outside ESO: `<release>-dapr-app-token` (`dapr-app-token.yaml:29-31`, consumed as `APP_API_TOKEN` via `_helpers.tpl:655,1399`) and `<release>-frontend-session` (`frontends.yaml:397`, the zones' `OIDC_CLIENT_SECRET`/`SESSION_SECRET`). Add ExternalSecret entries for those two, set `externalSecrets.enabled: true` in `chart/values.yaml` (:2817), deploy via `make k3s-up`. Before migrating any ref, fix how its consumer READS it — ESO rewrites the Secret out of band, so a value bound at boot goes silently stale and a `checksum/` annotation cannot see it. The `MEDIA_S3_ACCESS_KEY_ID` clause is resolved: the viewer renders the scoped `minio.viewerAccessKey` (`explorer.yaml:222`) and no secret half ships in env (`secrets.yaml:15-19`). Live ESO state was not verified this session.
- *Closes when:* Every secret-bearing env ref is ESO-synced or STS-vended, the toggle is on in values.yaml, and consumers re-read rotated values.
- *Evidence:* `chart/values.yaml:2817 (externalSecrets.enabled: false)` · `chart/templates/external-secrets.yaml:1,27-29,61-70,78,136-138,174-176` · `chart/templates/dapr-app-token.yaml:29-31; chart/templates/frontends.yaml:273-303,397` · `chart/templates/explorer.yaml:222; chart/templates/secrets.yaml:15-19`

**XC-005 · The OpenBao seed Job and the three ExternalSecrets carry no `helm.sh/hook`, so adding one property to an ExternalSecret destroys the whole Secret for ~12 minutes**
`chart` · **HIGH**
- **blocked:** Owner decision: turn today's silent ~12-minute Secret outage into a loudly aborted release via a bounded pre-upgrade hook.
- *What is left:* `chart/templates/openbao.yaml:137` (`kind: Job`, the seed) carries only `helm.sh/resource-policy: keep` (`:50`), and the three ExternalSecrets (`external-secrets.yaml:27,136,174`) run `creationPolicy: Owner` (`:39,150,188`), so seed and sync apply in arbitrary order within one upgrade. Extract the seed into a named template and add a `helm.sh/hook: pre-upgrade` copy (not pre-install — on first install OpenBao does not exist and the wait hangs) with a bounded `activeDeadlineSeconds`, leaving install-time behaviour unchanged. Measured outage: 696 s with five web zones in CreateContainerConfigError; each scoped identity the zero-trust work adds changes an ExternalSecret's data list and re-triggers it.
- *Closes when:* Adding one property to an ExternalSecret in a `helm upgrade` either keeps the Secret continuously present or fails the release before any pod restarts.
- *Evidence:* `chart/templates/openbao.yaml:50,137` · `chart/templates/external-secrets.yaml:27,39,136,150,174,188` · ``grep -n helm.sh/hook chart/templates/openbao.yaml chart/templates/external-secrets.yaml` → empty`

**XC-006 · OpenBao has no auto-unseal, so any restart leaves it sealed and the fail-closed fleet hangs at startup**
`chart, catalog, lineage, medallion, notifications` · **LOW**
- **blocked:** Owner ruling 2026-09-21 — **there is no production estate yet** (*"no not yet so we work with our locally dummies"*), so this and six sibling rows are PARKED at LOW rather than closed: the evidence stands and the row returns at its old priority the day a prod estate exists. The question it was waiting on, unchanged: Owner decision on the unseal mechanism: bank-vaults, vault-operator, or a KMS/transit auto-unseal stanza. Note external-secrets (`externalSecrets.enabled`, values.yaml:2811) is a secret READER and cannot unseal, so it is not a candidate for this row.
- *What is left:* `openbao.yaml:5,118` and `values.yaml:2798` state an operator must `bao operator init` and unseal by hand; `values-prod.yaml:146` carries only that prose; `chart/alerting/rules.yml` has no seal-status rule. After the ruling, wire the chosen auto-unseal into `chart/templates/openbao.yaml` and `values-prod.yaml`, and add a sealed-status alert to `rules.yml`.
- *Closes when:* A restarted OpenBao pod serves secrets without operator action, and a sealed instance fires an alert.
- *Evidence:* `chart/templates/openbao.yaml:4-5,118` · `chart/values.yaml:2796-2798,2811-2817` · `chart/values-prod.yaml:146` · `rg -i seal chart/alerting/rules.yml → 0`

**XC-007 · Every in-cluster store the fleet dials is plaintext: RustFS S3, OpenFGA, the AGE DSN (sslmode=disable), OpenBao, NATS and OTLP**
`catalog, lineage, maintenance, medallion, chart` · **LOW**
- **blocked:** Owner ruling 2026-09-21 — **there is no production estate yet** (*"no not yet so we work with our locally dummies"*), so this and six sibling rows are PARKED at LOW rather than closed: the evidence stands and the row returns at its old priority the day a prod estate exists. The question it was waiting on, unchanged: Owner decision on introducing a certificate source — the estate has neither cert-manager nor a chart-generated certificate.
- *What is left:* With a certificate source chosen, flip each rendered scheme: chart/templates/_helpers.tpl:682 (RustFS https, ALLOW_HTTP=false), :744 (tls:// NATS), :1208 (OpenFGA https plus a preshared key or OIDC), :729/:741 (OpenBao https), the AGE DSNs at infra-credentials.yaml:93, external-secrets.yaml:81 and openbao.yaml:178 (sslmode=disable -> verify-full), and dapr-component.yaml:343 whose skipVerify is derived from the scheme. Then add the missing TLS-on-store-hops test; the only plaintext test today (test_invariants.py:2447) checks credential VALUES, not transport. Dapr Sentry mTLS covers sidecar hops only.
- *Closes when:* helm template renders no http://, nats:// or sslmode=disable store URL for an in-cluster store, and a unit test refuses a plaintext store scheme.
- *Evidence:* `chart/templates/_helpers.tpl:682,729,741,744,1208 (http:// / nats:// stores)` · `chart/templates/infra-credentials.yaml:93; external-secrets.yaml:81; openbao.yaml:178 (sslmode=disable)` · `chart/templates/dapr-component.yaml:343 (skipVerify from scheme)` · `grep -in cert-manager chart/Chart.yaml chart/values.yaml -> no matches`

**XC-008 · `rask-age` serves TLS-off Postgres for the lineage graph and OpenFGA; the AGE→CNPG cutover is built but not taken**
`lineage, catalog, chart` · **LOW**
- **blocked:** Owner ruling 2026-09-21 — **there is no production estate yet** (*"no not yet so we work with our locally dummies"*), so this and six sibling rows are PARKED at LOW rather than closed: the evidence stands and the row returns at its old priority the day a prod estate exists. The question it was waiting on, unchanged: owner decision to run the data migration of the lineage AGE graph and OpenFGA's tables off the `rask-age` StatefulSet PVC into the CNPG Cluster
- *What is left:* Set `age.cnpgCluster.enabled: true` and `age.enabled: false` (`age-cluster.yaml:1-3` fails the render if both are on; defaults are `age.enabled: true` at values.yaml:2736 and `cnpgCluster.enabled: false` at :2763). Move the lineage graph and OpenFGA tables into the CNPG Cluster, retire the `age-postgres.yaml` StatefulSet, and prove the round-trip with `scripts/age_restore_drill.sh`. Then, and only then, drop `sslmode=disable` from the four client strings (`external-secrets.yaml:81`, `infra-credentials.yaml:93`, `openbao.yaml:178`, `otel-collector.yaml:335`) — a client-side `require` before the server change is an outage. The extension image builds from `.docker/cnpg-age-ext.dockerfile`. Live TLS state was not re-measured this session.
- *Closes when:* The CNPG Cluster serves both databases with TLS, the `rask-age` StatefulSet is gone, and the restore drill passes.
- *Evidence:* `chart/templates/age-cluster.yaml:1-3` · `chart/values.yaml:2736,2762-2763` · `chart/templates/{external-secrets.yaml:81,infra-credentials.yaml:93,openbao.yaml:178,otel-collector.yaml:335} (`sslmode=disable`)` · `scripts/age_restore_drill.sh; .docker/cnpg-age-ext.dockerfile; chart/templates/age-postgres.yaml (StatefulSet)`

**XC-025 · `chart/templates/dex.yaml` ships in-memory storage, static demo users and a plaintext client secret in a ConfigMap, and `values-prod.yaml` has no `dex:` stanza**
`chart, catalog, lineage, gateway` · **LOW**
- **blocked:** Owner ruling 2026-09-21 — **there is no production estate yet** (*"no not yet so we work with our locally dummies"*), so this and six sibling rows are PARKED at LOW rather than closed: the evidence stands and the row returns at its old priority the day a prod estate exists. The question it was waiting on, unchanged: Which real IdP prod federates to
- *What is left:* Add a `dex:` block to `chart/values-prod.yaml` (none exists) with an externally-reachable HTTPS issuer, a Postgres storage backend and an org-IdP connector. In `chart/templates/dex.yaml` (a ConfigMap): replace `storage: type: memory` (:13), keep `staticPasswords` (:20) off the prod path, and stop rendering `staticClients[].secret: {{ .Values.dex.clientSecret }}` (:40) — the ESO template already syncs a `dex-client-secret` key (`external-secrets.yaml:78`), so read it from there. The default issuer is `http://rask-dex:5556/dex` (`values.yaml:2773`).
- *Closes when:* The prod render has a real issuer, durable storage, an org connector, no static users and no client secret in a ConfigMap.
- *Evidence:* `chart/templates/dex.yaml:13,20,37,40 (kind: ConfigMap)` · `grep '^dex:' chart/values-prod.yaml — no match` · `chart/values.yaml:2769-2777` · `chart/templates/external-secrets.yaml:78`

**XC-011 · Estate bootstrap writes no `_control/bootstrap.json` record; `provision()` is already content-gated**
`chart, service-kit, catalog` · **MED** · PARTIAL
- **blocked:** the *Closes when* is an either/or and both branches are now measured, so what remains is the CHOICE, not the work. Owner picks: build the `_control/bootstrap.json` record (a durable estate-provenance artefact nothing currently writes), or close this row on the content gate that already shipped.
- **BOTH BRANCHES RE-MEASURED 2026-09-21.** `_control/bootstrap.json` is genuinely ABSENT — `lance-catalog/_control` raises `FileNotFoundError` over S3 while the bucket itself lists normally, so this is a real negative and not a wrong-path artefact (a first attempt via the MinIO pod's filesystem was inconclusive: that container has no `mount`, `grep`, and `/data/*` listed nothing). The content gate IS shipped and sits at exactly the lines this row cites: `_canonical_model` at `service_kit/governed/fga.py:401`, the comparison and `openfga_model_unchanged` log at `:574-575`.
- *What is left:* `provision()` no longer rewrites every boot: it compares `_canonical_model(current) == _canonical_model(model)` and logs `openfga_model_unchanged` instead of writing (fga.py:574-575), which is the content-hash answer to C-Q2, with `RASK_FGA_MODEL_ID` as the production pin. The `_control/bootstrap.json {subject, store_id, model_id, at}` record via `records.create_json` is not written: `bootstrap-admin.yaml` remains check-then-write and treats a duplicate 400/409 "already exists" as success (:21, :251). Its original purpose (gating provision) is now served, so either write the record for its audit value alone or close the row on the content gate.
- *Closes when:* Either `_control/bootstrap.json` exists after a fresh install with 409-on-exists treated as success, or the row is closed on the shipped content gate.
- *Evidence:* `packages/service-kit/src/service_kit/governed/fga.py:508, :574-575` · `chart/templates/bootstrap-admin.yaml:21, :251` · ``grep -n 'bootstrap.json|create_json' chart/templates/bootstrap-admin.yaml` → none`

**XC-016 · OpenFGA's pgxpool defaults (MaxOpenConns 30 / MaxIdleConns 10) are untuned against the shared AGE Postgres at `max_connections=100`**
`openfga, chart, lineage` · **MED**
- **MEASURED ON THE DEPLOYED ESTATE 2026-09-21, and the default is BINDING rather than theoretical.** `SHOW max_connections` on `rask-age-0` is **100**; `pg_stat_activity` holds **47** connections, of which **openfga=30**, `lineage=6` and `daprstate=6`. Thirty is exactly pgxpool's default `MaxOpenConns`, so OpenFGA is sitting AT its ceiling — it holds **64% of every in-use connection and 30% of the whole budget**, while the lineage graph this Postgres primarily exists for uses six. The risk this row describes is therefore live: the authz store is the largest consumer by a factor of five, and every governed read and write blocks on a Check against it.
- *What is left:* No `datastore.maxOpenConns` / `maxIdleConns` is set anywhere in `chart/` and OpenFGA v1.18.3 (`chart/values.yaml:2683`) runs pgxpool defaults against the Postgres that also carries AGE, `lance-statestore` and the backup Job; `values.yaml:2695` explicitly defers `datastore_throttling` to a measurement. Measure observed connections per consumer on the AGE Postgres (`pg_stat_activity`, or OpenFGA's own metrics once XC-047 exports them — the estate scrapes no OpenFGA endpoint today), then set both pool values and decide `datastore_throttling` from the numbers rather than blind.
- *Closes when:* The chart carries measured `datastore.maxOpenConns` / `maxIdleConns` values with the measurement cited beside them.
- *Evidence:* ``grep -n 'maxOpenConns\|maxIdleConns' chart/values.yaml chart/templates/*.yaml` → 0 hits` · `chart/values.yaml:2683 (image.tag v1.18.3), 2695 (datastore_throttling deferred)` · ``grep -in openfga chart/templates/otel-collector.yaml` → no scrape target`

**XC-017 · The zero-trust posture is asserted per control, not re-derived as one checked list of the 19 §F controls**
`catalog, lineage, maintenance, medallion` · **MED** · PARTIAL
- *What is left:* Owner acknowledgement of R11 is given (R1–R11 stand). Individual tests now pin §F2-1, -2, -3, -4, -8, -9 and -11 from the sweep's ordered gap list, but no single test or `make` target encodes the 19 §A controls and re-derives matched/partial/missing. Encode that list, then close the remaining §B items against it — 5 (Dapr access-control policy + NetworkPolicy on by default), 6 (TLS to every store), 7 (`register_table` location validation), 10 (audit correlation ids), 12 (image signing/attestation). Whether any of those five has since shipped is not re-measured this session.
- *Closes when:* One checked list re-derives the 19 §F controls' status on every run and reports zero missing and zero partial.
- *Evidence:* `docs/audits/lakehouse-2026-09/sweeps/zero-trust.md:5-27 (§A control table), :29-42 (§B ordered gaps)` · `grep -rhoE '§F[0-9]+-[0-9]+' tests services/*/tests packages/*/tests → §F2-1,2,3,4,8,9,11` · `open_backlog_left.md:164 (R1–R11 STAND)` · `grep -rln 'zero.trust|§F' Makefile scripts → none`

**XC-031 · No ordered prod install runbook exists; the FGA seed / OpenBao unseal / PSA-label ordering is documented only as warnings in values-prod**
`chart` · **LOW**
- *What is left:* Write docs/runbooks/RUNBOOK-prod-install.md with the ordered sequence: secrets -> scripts/seed_medallion_fga.sh -> OpenBao init and unseal -> flip governance (auth.enabled, medallion.fgaEnabled) -> verify. docs/runbooks/ holds only llm-cluster.md, RUNBOOK-oncall.md and RUNBOOK-restore.md. Decide whether chart/templates/bootstrap-admin.yaml (a post-install/post-upgrade hook that seeds standing tuples) should absorb the stage-runner grants seed_medallion_fga.sh applies, so the prerequisite at values-prod.yaml:18 and the alert text at chart/alerting/rules.yml:294 stop naming a manual script.
- **blocked:** Owner ruling 2026-09-21 — **there is no production estate yet** (*"no not yet so we work with our locally dummies"*). EXTENDED to this row on 2026-09-21: it was not in the seven put to the owner as cluster E, but its closing bar is purely prod (`values-prod.yaml` / the prod install), so parking the seven and leaving this one workable was an inconsistency in the application, not a distinction in the rows. Parked at LOW on the same terms — evidence stands, returns at its old priority the day a prod estate exists. The question it waits on, unchanged: see *Closes when* below.
- *Closes when:* docs/runbooks/RUNBOOK-prod-install.md exists with the ordered steps, and the FGA seed is either a hook or a named runbook step.
- *Evidence:* `ls docs/runbooks/ -> llm-cluster.md, RUNBOOK-oncall.md, RUNBOOK-restore.md` · `chart/values-prod.yaml:16-24 (seed prerequisite warning, fgaEnabled: true)` · `chart/templates/bootstrap-admin.yaml:2,30-32 (post-install hook seeding standing tuples)` · `chart/alerting/rules.yml:294 (re-seed with scripts/seed_medallion_fga.sh)`

**XC-032 · No first-party image is registry-qualified or digest-pinned in `chart/values-prod.yaml`**
`chart` · **LOW** · PARTIAL
- *What is left:* The `imagePullSecrets` half is shipped for the six first-party pod-spec templates and pinned by `tests/unit/test_a_first_party_pod_can_pull_from_a_private_registry.py`. `chart/values-prod.yaml:11-14` has `image:` with only `pullPolicy` and two commented-out per-component tags, and `chart/values.yaml:529-533` defaults `image.repository: ""` / `digest: ""` — set a registry-qualified `image.repository` and per-component `tag` or `digest` values in `values-prod.yaml` so a non-k3s cluster can pull. The airgap/mirror question for the 17 pinned third-party images is separate and only answered if asked.
- **blocked:** Owner ruling 2026-09-21 — **there is no production estate yet** (*"no not yet so we work with our locally dummies"*). EXTENDED to this row on 2026-09-21: it was not in the seven put to the owner as cluster E, but its closing bar is purely prod (`values-prod.yaml` / the prod install), so parking the seven and leaving this one workable was an inconsistency in the application, not a distinction in the rows. Parked at LOW on the same terms — evidence stands, returns at its old priority the day a prod estate exists. The question it waits on, unchanged: see *Closes when* below.
- *Closes when:* `helm template -f chart/values-prod.yaml` renders every first-party image with a registry-qualified repository and a release tag or digest.
- *Evidence:* `chart/values-prod.yaml:11-14` · `chart/values.yaml:529-533` · `tests/unit/test_a_first_party_pod_can_pull_from_a_private_registry.py (exists)`

**XC-033 · Nothing runs `scripts/e2e_live.sh` routinely, so the 133 e2e functions across 30 files only ever hit the deployed estate by hand**
`e2e, ci, lineage` · **MED** · PARTIAL
- **RUN BY HAND 2026-09-20 AND IT PAID IMMEDIATELY, which is the argument this row needs.** `bash scripts/e2e_live.sh tests/e2e-py/test_{catalog_live,lineage_e2e,maintenance_e2e}.py` against the deployed release: 13 passed, 1 skipped, **1 failed** — and the failure was real, not fixture rot. `test_maintenance_e2e` found eleven datasets reporting `maintain: Ref is invalid: …` every sweep, branch directories under `<dataset>/tree/` whose names Lance will not parse (`has space`, `tilde~name`, `a\b`, `feat.lock`, `trailing`). Fixed the same day by classifying them as a REFUSAL rather than a per-tick failure (`optimize.classify_maintain_failure`). No unit or integration test saw it: the names only exist on the deployed estate's disk.
- **ALL 11 `test_lineage_e2e.py` CASES ARE GREEN AGAINST THE DEPLOYED RELEASE (2026-09-20) — the row's other clause, met.** `LINEAGE_E2E_DESTRUCTIVE=1 bash scripts/e2e_live.sh tests/e2e-py/test_lineage_e2e.py` → **11 passed in 17.69s**. The 11th had never run: it is opt-in destructive by design ("so the suite stays runnable against a real estate with only this one test sitting out"), and the standing position that the deployed estate holds test and demo data only is what makes opting in correct here rather than reckless. `LINEAGE_DATABASE_URL` needed nothing — `e2e_live.sh:269` already derives it from the `rask-age` service and the postgres password, and the other direct-AGE legs were passing all along.
- **THE CLOSING CONDITION NEEDS A RUNNER THAT CAN REACH THE RELEASE, and GitHub Actions cannot.** "A scheduled job runs `scripts/e2e_live.sh` against the deployed release" — the release is this host's k3s, discovered through a kubeconfig no hosted runner has. So the cadence is a self-hosted runner or a timer on the host, not a `schedule:` block in `ci.yml`; deciding which is the work, and it is not blocked on anything.
- **blocked:** the closing bar as written is not reachable from this CI. Owner picks: stand up a self-hosted runner with a path to the cluster, expose the deployed release to a hosted runner, or RESTATE the bar as a scheduled run against an ephemeral stack (which `e2e-stack` already approximates).
- **RE-MEASURED 2026-09-21, and the missing piece is NOT the cadence.** A nightly schedule already exists (`ci.yml`, `cron: '0 3 * * *'`). What does not exist is a runner that can reach the estate: **all 14 `runs-on` across `.github/workflows/` are `ubuntu-latest`**, and a GitHub-hosted runner has no network path to this k3s cluster — so "runs `scripts/e2e_live.sh` against the DEPLOYED RELEASE on a cadence" cannot be satisfied by adding a schedule. Two line references in this row had also drifted: `make e2e-live` is `Makefile:895` (cited 885) and `e2e-lineage` is `ci.yml:436` (cited 417-420).
- *What is left:* `make e2e-live` (Makefile:885) and `scripts/e2e_live.sh` exist, and the lineage suite runs hermetically in CI as job `e2e-lineage` (`dagger call test-lineage`, .github/workflows/ci.yml:417-420). The two failing `test_lineage_e2e.py` cases are unnamed in the row; LH-109 records the suite at 14 passed after the AGE memory fix and the cases cannot be reproduced offline (needs AGE), so treat them as unconfirmed rather than open. Wire `scripts/e2e_live.sh` into a scheduled run against the k3s release — no workflow or cron references it. Note the CI signal itself is absent at HEAD: the last three ci.yml runs on e198b61b/6a6ecc3a are `failure` with `ms-test` red, `e2e-lineage` is skipped because it `needs: ms-test`, and `gh api …/workflows/ci.yml/runs?status=success` returns no run at all. Update the script header's `111` to the current 133.
- *Closes when:* A scheduled job runs `scripts/e2e_live.sh` against the deployed release on a cadence and its latest run is green including all 11 `test_lineage_e2e.py` cases.
- *Evidence:* `Makefile:885-886; scripts/e2e_live.sh:1-20` · `rg -n 'e2e_live|e2e-live' .github/workflows/ → no hits; ci.yml:417-420 runs dagger call test-lineage` · `gh run list --workflow=ci.yml --limit 3 → all failure; jobs of 35301962075: ms-test failure, e2e-lineage skipped` · `rg -c '^(async )?def test_' over the 30 e2e-marked files → 133`

**LH-161 · The GreptimeDB subchart pulls the whole `rask-observability-s3` Secret into its environment via `envFrom`**
`chart` · **MED**
- **blocked:** Whether a third-party subchart's own env handling (greptimedb-standalone's `envFrom: secretRef`) is out of scope for the envFrom ban — an owner ruling that must be written next to the exemption, not assumed.
- *What is left:* The vendored `greptimedb-standalone-0.4.5` subchart's `templates/statefulset.yaml:114-122` emits `envFrom: secretRef: <existingSecretName>` whenever `objectStorage.credentials` is set, and its `env` map renders plain string values only (no `valueFrom`), so no values-level keyed-ref override exists. `test_secret_env_delivery_only_shrinks.py:129` names this one workload in `_ENVFROM_EXCEPTIONS`. Either change the subchart upstream to take the credential keyed or by file, override that template in the estate, or record the out-of-scope ruling beside the exemption.
- *Closes when:* The greptimedb StatefulSet no longer renders `envFrom`, or the exemption carries a written owner ruling.
- *Evidence:* `chart/charts/greptimedb-standalone-0.4.5.tgz → templates/statefulset.yaml:103-122` · `tests/unit/test_secret_env_delivery_only_shrinks.py:122-129` · `chart/values.yaml:3023 (existingSecretName: "rask-observability-s3")`

**XC-002 · Whether an ESO-written Secret delivered by `secretKeyRef` satisfies 'never secret through envs' is undecided, and the Ray head's six entries hang on it**
`chart, medallion, storage` · **MED**
- **blocked:** Owner: does an ESO-written Secret delivered by `secretKeyRef` satisfy the rule 'never secret through envs; ESO, Dapr secret store or STS'? Two in-tree statements say yes; LH-160's gate counts it as the banned path.
- *What is left:* The credential itself works and is chart-derived; what is open is delivery. `deploy/ray-lance-demo.yaml` carries six `secretKeyRef` entries off `rask-infra-credentials` (`S3_SECRET`, `LINEAGE_SERVICE_TOKEN`, 4× `RASK_LINEAGE_TOKEN_SERVICE_*`), no file mount, no STS. Take the ruling. If NO: move the six to a projected file mount, `S3_SECRET` to STS, and rewrite `tests/unit/test_the_ray_credential_has_one_source.py:3-8` and `.claude/skills/rask-dapr/SKILL.md:151-154` in the same commit, since both pin `secretKeyRef` as the sanctioned no-sidecar path. If YES: close, and make LH-160's gate exclude ESO-written refs explicitly. Either way delete the hand-applied `Secret/rask-ray-compute-s3` whose `last-applied-configuration` annotation holds the base64 credential (live state not re-verified this session).
- *Closes when:* The ruling is in `docs/DECISIONS.md`, the test and skill agree with it, and the Ray head's delivery path matches.
- *Evidence:* `deploy/ray-lance-demo.yaml:83,101,117,120,123,126` · `tests/unit/test_the_ray_credential_has_one_source.py:3-8` · `.claude/skills/rask-dapr/SKILL.md:151-154` · `grep -n secretKeyRef docs/DECISIONS.md → 0 hits`

**XC-013 · pg dumps land in the lakehouse bucket the VolumeSnapshot protects, old VolumeSnapshots are never pruned, and `snapshotClassName` is empty in prod**
`chart, lineage` · **LOW** · PARTIAL
- **blocked:** Owner ruling 2026-09-21 — **there is no production estate yet** (*"no not yet so we work with our locally dummies"*), so this and six sibling rows are PARKED at LOW rather than closed: the evidence stands and the row returns at its old priority the day a prod estate exists. The question it was waiting on, unchanged: Where off-cluster pg dumps go — the bucket/endpoint `chart/values-prod.yaml` should point `backups.pgDump` at instead of the lakehouse's own `minio.bucket`.
- *What is left:* pg-dump retention is shipped (`backups.pgDump.keep: 7`, pruning at `backup-pg.yaml:90-95`). Point the dump at an off-cluster destination: `backup-pg.yaml:88-89` still writes to `{{ .Values.minio.bucket }}/_backups/pg/` via `lance.s3Endpoint`, the same store a PVC loss takes out. Add pruning of old VolumeSnapshots in `backup-snapshot.yaml`, which only `kubectl create`s (line 85) and never deletes — this clause needs no ruling. Set a real `snapshotClassName` in `chart/values-prod.yaml:144`, still `""`.
- *Closes when:* Prod dumps land outside the lakehouse bucket, `backup-snapshot.yaml` prunes snapshots beyond a `keep` count, and `values-prod.yaml` names a real VolumeSnapshotClass.
- *Evidence:* `chart/templates/backup-pg.yaml:88-95 (same bucket; keep-N pruning present)` · `chart/templates/backup-snapshot.yaml:85 (kubectl create only, no delete)` · `chart/values-prod.yaml:139-144 (snapshotClassName: "")` · `chart/values.yaml:1965-1973 (pgDump.keep, volumeSnapshot.snapshotClassName)`

**XC-030 · Images are unsigned and the estate runs no admission-time signature verifier**
`chart, dagger` · **LOW**
- **blocked:** Owner ruling 2026-09-21 — **there is no production estate yet** (*"no not yet so we work with our locally dummies"*), so this and six sibling rows are PARKED at LOW rather than closed: the evidence stands and the row returns at its old priority the day a prod estate exists. The question it was waiting on, unchanged: Owner names a signing-key custodian and approves an admission-time verifier (Kyverno or sigstore policy-controller); signing without a verifier is decoration.
- *What is left:* `.dagger/images.go:46-53` emits only the three OCI provenance labels; no cosign or attestation code exists in any `.dagger/*.go` (the SBOM at `scan.go:346` is the only supply-chain artefact); `chart/` has no kyverno, policy-controller, sigstore or ClusterImagePolicy. After the ruling, add cosign signing to the `dagger call image … publish` path and the verifying admission policy to the chart.
- *Closes when:* A published image carries a cosign signature and an unsigned image is refused at admission on the deployed cluster.
- *Evidence:* `.dagger/images.go:46-53` · `rg -i cosign|signature|attest .dagger/*.go → 0` · `.dagger/scan.go:346 (SBOM only)` · `rg -i kyverno|policy-controller|sigstore|ClusterImagePolicy chart/ → 0`

**XC-035 · ~154 single-component test files sit in `tests/unit` instead of their component's own testpath**
`catalog, service-kit, annotator, lineage, maintenance, medallion` · **MED**
- **blocked:** Owner decision on relocating the single-component files out of tests/unit
- *What is left:* `tests/unit` holds 394 files; a direct-import classifier finds ~154 importing exactly one component (46 catalog, 41 service_kit, 21 annotator, 17 lineage, 15 maintenance, 11 medallion, 2 search, 1 ingest) while `services/catalog/tests` holds 93. The count grew since the row was written. Read the 41 `service_kit` files first to separate shared fixtures from misplaced tests, then move the rest into each component's `tests/` with import fixes, keeping the per-commit selection always including the invariant and integration layers.
- *Closes when:* A catalog-only change can be verified by `uv run pytest services/catalog/tests` alone, with tests/unit holding only multi-component and chart-render files.
- *Evidence:* `ls tests/unit/*.py | wc -l = 394` · `find services/catalog/tests -name 'test_*.py' | wc -l = 93` · `grep-based single-component classifier over tests/unit (this session): 154 files` · `pyproject.toml:279 (testpaths)`

**XC-048 · No service propagates `request_id` or actor to its downstream clients, and no supersession verdict is recorded**
`service-kit, catalog, lineage` · **MED**
- **blocked:** Owner records in `docs/DECISIONS.md` §9 that OTel tracing plus the audit trail supersede request_id/actor propagation, or orders the build.
- *What is left:* `docs/DECISIONS.md` has zero mentions of request_id or `X-Request-ID`. What exists: the gateway mints and forwards the header (`gateway/__init__.py:494-513`), each service's `RequestIDMiddleware` echoes it, and `CorrelationFilter` (`context.py:38`) stamps it on log records. No outbound httpx hook carries it from one service to the next, and no actor propagation exists. After the ruling, either write the §9 entry or add the outbound hook in service-kit plus the actor field on the downstream clients.
- *Closes when:* Either DECISIONS.md §9 records supersession, or a service-to-service call carries the caller's request id and actor, pinned by a test.
- *Evidence:* `rg -i request_id|X-Request-ID docs/DECISIONS.md → 0` · `packages/service-kit/src/service_kit/context.py:27-38 (no client hook)` · `services/gateway/src/gateway/__init__.py:494-513`

**XC-052 · A helm-LABELLED `rask-assist` Deployment/Service the release does not own will fail the next upgrade that renders it**
`chart` · **MED**
- **KEPT IN PHASE 1 AGAINST THE AUDIT'S RECOMMENDATION (2026-09-20).** The audit grouped this with the
  annotator rows because the orphan is named `rask-assist` and its hazard "only fires if someone enables
  the annotator's assist runner". That reads the SUBJECT and not the FAILURE: a helm-labelled object the
  release does not own fails the next `helm upgrade` that renders it, and a failed upgrade is estate-wide —
  it blocks every service in this chart, lakehouse included. The blast radius is the chart, not the
  annotator, so it stays where the chart is.
- **blocked:** Whether `rask-assist` becomes chart-rendered and helm-adopted (enable `runners.enabled` and annotate the live objects for adoption) or is deleted as hand-applied residue.
- *What is left:* The chart renders `Deployment/<fullname>-assist` + its Service behind `runners.enabled` (`chart/templates/runners.yaml:1-15`), which defaults `false` (`chart/values.yaml:1851`), so the live hand-applied pair carries release labels no release owns and the first upgrade with the flag on meets an object it cannot adopt. Either set `runners.enabled` in the deploy values and add `meta.helm.sh/release-name` / `release-namespace` annotations plus `managed-by: Helm` on the live objects so the upgrade adopts them, or delete them; record the choice. Live state not re-verified (no cluster access).
- *Closes when:* `helm get manifest rask` contains `rask-assist`, or no such objects exist in the cluster, and the choice is recorded.
- *Evidence:* `chart/templates/runners.yaml:1-20` · `chart/values.yaml:1850-1853 (runners.enabled: false)`

**XC-020 · `transaction.can_set_property` and `transaction.can_cancel` are defined in `model.fga` and used by no relation and no code path**
`service-kit, catalog` · **LOW**
- **blocked:** [[LH-077]]'s ruling, which is the SAME decision this row states as two alternatives — "authorize `alter_transaction` per state-action (making `can_set_property`/`can_cancel` real doors) or keep one check and delete both relations". One decision gated two rows while only one of them said so; measured 2026-09-20 and the choice is unchanged, so this row now names it too.
- **THE SWEEP THIS ROW ASKED FOR IS RUN — the `fga` CLI IS available, contrary to the row's own evidence.** It reads "`which fga` → not found"; measured 2026-09-20 it is at `.localbin/fga` (v0.6.4, installed by `make bootstrap`), so "where the CLI is available, re-run the usage sweep" is satisfiable here and is done. `fga model test` is **green as it stands: 51/51 tests, 361/361 checks, 8/8 ListObjects, 2/2 ListUsers** — so neither alternative is being held up by a red model. The relations are defined at `model.fga:528,530` (not 514/516) and asserted at `model.fga.yaml:664-670` (not 621-627, which holds materialized-view assertions); the only non-model reference is still a DOCSTRING, now at `catalog/api/fga_deps.py:446-447`. Every line number in the row's evidence had drifted, which is worth stating because a reader checking them would conclude the claim was wrong rather than merely stale.
- *What is left:* Where the `fga` CLI is available (it is not on PATH here), re-run the usage sweep, then either delete both relations from `packages/service-kit/src/service_kit/governed/auth/model.fga` (lines 514 and 516) and their assertions in `model.fga.yaml:621-627`, or grow `alter_transaction` into the property/cancel actions so they are used. Run `fga model test` green and regenerate `model.json`. The only non-model references are a docstring at `catalog/api/fga_deps.py:423-424`; `fga_deps.py` picks only between `can_describe` and `can_set_status`.
- *Closes when:* `fga model test` is green with the two relations either removed or exercised by a code path, and `model.json` is regenerated.
- *Evidence:* `packages/service-kit/src/service_kit/governed/auth/model.fga:505-516` · `packages/service-kit/src/service_kit/governed/auth/model.fga.yaml:621-627` · `grep -rn 'can_set_property\|can_cancel' services/ packages/ --include=*.py → only fga_deps.py:423-424 docstring` · `which fga → not found`

**XC-036 · Two subchart values hardcode `rask-`-prefixed Secret names while the estate renders them as `<release>-…`, so any release not named `rask` points at Secrets that do not exist**
`chart` · **LOW**
- *What is left:* The mechanism is inverted from the row's wording: `lance.fullname` IS `{{ .Release.Name }}` (helpers.tpl:478), so the estate's own templates render `<release>-infra-credentials` and `<release>-observability-s3`, while two SUBCHART values hardcode the literal — `openfga.datastore.existingSecret: rask-infra-credentials` (values.yaml:2719) and `greptimedb-standalone.objectStorage.credentials.existingSecretName: "rask-observability-s3"` (:3023). Subchart values cannot template, so either give those two Secrets a release-independent name in the estate templates or add a render-time gate that fails any release not named `rask`. No test pins the pairing today.
- *Closes when:* `helm template` under a release name other than `rask` either renders matching Secret names for the openfga and greptimedb subcharts or fails loudly at render.
- *Evidence:* `chart/templates/_helpers.tpl:478 (lance.fullname = .Release.Name)` · `chart/values.yaml:2719 and :3023 (hardcoded rask- names)` · `chart/templates/observability.yaml:10 and external-secrets.yaml:138 (`{{ .Release.Name }}-observability-s3`)` · `grep over tests/unit for the pairing → no gate`

**XC-039 · Live e2e legs skip on a 5 s `/livez` timeout while the medallion producer is up and serving the cascade**
`medallion, e2e` · **LOW**
- **CLOSED 2026-09-20 on its own condition — the medallion legs no longer skip a slow producer.** `tests/e2e-py/liveness.py::wait_until_live` polls within a BUDGET instead of probing once: 15 s per attempt, 3 s apart, 60 s total, and the medallion fixture now reports `not reachable after {waited}s ({error})` rather than a bare "not reachable". **The budget is the fix, not a bigger timeout** — one long timeout still fails a service restarting mid-probe and makes every genuinely-absent target cost the full timeout, while a bounded retry waits for a healthy-but-slow service and gives up on an absent one in predictable steps. 60 s is measured against what the estate does: a rolling Deployment's replacement pod passes readiness in ~30 s here, so a shorter budget would still skip across an ordinary rollout. **The probe, the clock and the sleep are INJECTED**, so `tests/unit/test_the_liveness_probe_waits_before_it_skips.py` runs in the ordinary suite with no live estate and no real waiting. It sits in `tests/unit` rather than beside the helper because `test_e2e_collection_gate` refuses a module in the live-suite directory that no marker or make target selects — correctly, since a file that collects, deselects and never runs is this row's own failure in miniature, and it caught my first placement. Eight legs, including that ONE attempt is made even at a zero budget (checking the clock first would call a service absent having never asked) and that the per-attempt timeout actually reaches the probe. Mutation-checked: collapsing it back to a single attempt reds 2, moving the budget check before the attempt reds 1.
- *What is left:* **Sixteen identical probes in fifteen OTHER e2e modules**, named rather than swept: `test_governance_e2e` (2), and one each in `test_e2e`, `test_client_direct_e2e`, `test_outbox_crash_e2e`, `test_dummy_lane_e2e`, `test_outbox_e2e`, `test_governed_union_e2e`, `test_warehouses_e2e`, `test_auth_e2e`, `test_media_e2e`, `test_maintenance_e2e`, `test_credential_isolation_e2e`, `test_user_state_e2e`, `test_the_container_tier_deletes_are_driven`, `test_multibase_e2e`. The helper they need now exists and the change is one line each; they are left for a deliberate sweep because none of these suites can be RUN without a live estate, so a batch edit would land unverified — which is the same "reports green, covers nothing" problem this row is about.
- *Closes when:* All 16 remaining `timeout=5)` livez probes wait within a budget the way `liveness.py::wait_until_live` does, so no live leg can skip a slow-but-healthy service. **This row carried NO closing bar at all** — re-measured 2026-09-21, the count is unchanged at **16 probes** across 18 files carrying both patterns (the row says fifteen modules, a slight undercount).
- *Evidence:* `tests/e2e-py/liveness.py` · `tests/unit/test_the_liveness_probe_waits_before_it_skips.py` · `tests/e2e-py/test_medallion_e2e.py:82-92` · `grep -rn 'timeout=5)' tests/e2e-py/*.py | grep livez | wc -l → 16`
**XC-042 · `/capi/v1/me` 502s under `dev-micro.sh` because the fleet starts no catalog; the BFF's 502 is honest and the consumer already degrades**
`annotator, home-zone, scripts` · **LOW** · **REWRITTEN — the original ask would be wrong**
- *What is left:* Do not make the BFF answer anything but 502 for an unreachable catalog: `makeBackendProxy` reports the failure, `fetchMeViaBff` returns `null` on ANY failure, and the layout renders base entries fail-closed — silencing the 502 would mask an outage. The `:8103` clause is shipped (`dev-micro.sh` starts `annotator` on `ANNOTATOR_PORT`). The only residue is that `scripts/dev-micro.sh` starts no catalog service at all, so `/capi/v1/me` 502s there by construction; if a local identity call matters, add the catalog to the roster as its own row.
- *Closes when:* Closed as written; a catalog-in-dev-micro row exists if wanted.
- *Evidence:* `scripts/dev-micro.sh:41,96 (`ANNOTATOR_PORT` 8103, `run annotator`); grep -i catalog → no catalog process` · `frontend/packages/api/src/bff.ts:224-228 (fetch failure → 502), :353-358 (`makeCatalogProxy`)` · `frontend/packages/api/src/client.ts:67-73 (`fetchMeViaBff`: null on any failure)` · `frontend/microfrontends/annotator/src/routes/+layout.svelte:113-116 (null → base entries, fail-closed)`

**XC-022 · A NACK operator under GitOps and a query engine are owner-parked with no ruling**
`chart, nats` · **LOW** · PARTIAL
- **blocked:** Owner ruling on whether JetStream streams are provisioned as NACK Stream CRs under GitOps, and whether a query engine is in scope
- *What is left:* The NATS HA clause is shipped: `nats.config.cluster.enabled: true, replicas: 3` (chart/values.yaml:2408-2410) with JetStream on (:2388-2389). What remains unruled is the NACK-operator-under-GitOps question — today NACK appears only as a commented prod option (chart/values-prod.yaml:273-274) and no NACK CR exists in chart/templates — and whether a query engine is in scope. No work until the ruling.
- *Closes when:* An owner ruling records yes/no on NACK Stream CRs under GitOps and on a query engine; if yes, the CRs and engine land in the chart.
- *Evidence:* `chart/values.yaml:2408-2410 `cluster: enabled: true / replicas: 3`; :2388-2389 `jetstream: enabled: true`` · `chart/values-prod.yaml:273-274 — NACK named only in a comment` · `grep -rn -i 'nack' chart/ --include=*.yaml → no template/CR hits`

**XC-023 · The Dapr retreat (D5) has not started: 37 of 523 src files import the SDK and 39 of 59 chart templates mention dapr**
`medallion, notifications, ingest, lineage, service-kit, chart` · **LOW**
- **blocked:** Owner sequencing: the retreat (§K) is ordered after §A–§D of the lakehouse audit and has not been released to start.
- *What is left:* Work the stated order once released: secrets (OpenBao direct) → pub/sub (JetStream durable consumers behind a `Publisher` protocol replacing `dapr_publish`) → state (JetStream KV with CAS; notifications' actors become KV rows with revision CAS) → bindings (in-process scheduler + KV lease) → invocation (plain HTTP + mTLS) → workflow last (BYO engine; `promotion_review` becomes a record + door + scheduled message). The per-block replacement map is `docs/audits/lakehouse-2026-09/dapr-coupling-analysis.md` §4 and the ranked loss list §6.
- *Closes when:* No file under `services/` or `packages/` imports `dapr`, and no chart template renders a Dapr Component or sidecar annotation.
- *Evidence:* ``grep -rlE '^(from|import) dapr' services/ packages/ --include=*.py | grep -v /tests/ | wc -l` → 37 of 523 src files` · ``grep -rl dapr chart/templates/ | wc -l` → 39 of 59` · `docs/audits/lakehouse-2026-09/dapr-coupling-analysis.md:171,333`

**XC-045 · No ruling on where a rask-operator chart and its CRD would be installed from**
`chart` · **LOW**
- **blocked:** Whether a rask-operator chart (with its `helm.sh/resource-policy: keep` CRD) is installed via a split infra chart in this repo or shipped from the separate rask-operator repo
- *What is left:* Record the ruling in `docs/DECISIONS.md`. Two standing decisions constrain it: the chart is NOT split (infra vs app), reopened only by the named triggers at `DECISIONS.md:845-849`; and CRDs are a rask-operator-repo concern — landing one here without its controller is ruled a regression (`:920-924`, `:1492-1494`).
- *Closes when:* docs/DECISIONS.md names the operator chart's home.
- *Evidence:* `docs/DECISIONS.md:810-812,834-849` · `docs/DECISIONS.md:920-924,1492-1494`

**XC-046 · Remote branch `claude/flyte-2-dapr-audit-19cyc2` still exists on origin**
`—` · **LOW**
- **blocked:** Owner action: push rights to delete the remote branch
- *What is left:* `git ls-remote --heads origin` still lists the branch at `d6f13ff3`. It carries one commit not on main (`docs: add the governed-lakehouse backlog…`); 10 of its 11 files exist on main and the eleventh, `open_lakehouse.md`, is the register this file superseded. Run `git push origin --delete claude/flyte-2-dapr-audit-19cyc2` from a machine with push rights.
- *Closes when:* `git ls-remote --heads origin claude/flyte-2-dapr-audit-19cyc2` returns nothing.
- *Evidence:* `git ls-remote --heads origin | grep flyte → d6f13ff3 refs/heads/claude/flyte-2-dapr-audit-19cyc2` · `git log --oneline main..origin/claude/flyte-2-dapr-audit-19cyc2 | wc -l = 1` · `git cat-file -e main:<each file>: 10/11 present, open_lakehouse.md absent`

**XC-049 · Two Kueue controllers reconcile one set of CRDs, producing kueue-ca handshake spam, and two otel-collector scrape targets fail**
`chart` · **LOW**
- **blocked:** Cluster-operator decision to remove the foreign kueue-system Kueue install (or the chart-owned one) so a single controller writes the CRD conversion-webhook CA bundle.
- *What is left:* The chart still owns a Kueue 0.18.1 dependency gated on kueue.enabled (default true) with chart/templates/kueue-queues.yaml; the foreign kueue-system install is outside this repo. Once one controller remains, confirm the x509 'kueue-ca' spam stops. Separately identify and repair the two failing otel-collector scrape targets among the four scrape jobs in chart/templates/otel-collector.yaml:116-276 (dapr-sidecars, dapr-control-plane, ray-pods, greptimedb). Cluster state — the foreign install, the spam rate, which two targets fail — is not verifiable from this session.
- *Closes when:* One Kueue controller reconciles the CRDs and every otel-collector scrape target reports up.
- *Evidence:* `chart/Chart.yaml:65-68 (kueue 0.18.1, condition kueue.enabled)` · `chart/values.yaml:2601-2602 (kueue.enabled: true)` · `chart/templates/otel-collector.yaml:116-276 (four scrape jobs)`
- *Confidence LOW* — re-measure before acting on this row.

**XC-051 · Undecided whether the estate shares one GreptimeDB or runs one per workload**
`chart` · **LOW**
- **blocked:** One shared GreptimeDB, or one per workload
- *What is left:* Record the ruling in `docs/DECISIONS.md` (no GreptimeDB sharing decision exists there) and make `chart/values.yaml`'s observability stanza state it. De facto there is one (`rask-greptimedb-standalone`); the question bites once a runner's telemetry volume competes with the cascade's RED metrics and traces.
- *Closes when:* docs/DECISIONS.md and chart/values.yaml both state the topology.
- *Evidence:* `grep -n -i greptime docs/DECISIONS.md — none of the 8 hits is a sharing ruling` · `grep -n -i 'per-workload|one shared' chart/values.yaml — no observability match`


**XC-055 · The chart version is inert: every release revision reads `rask-0.3.0`, so no revision can be identified or rolled back by what it deployed**
`chart, ci` · **MED**
- *What is left:* 193 revisions of the release carry one chart version, so `helm history` cannot distinguish them and `helm rollback <rev>` is chosen by ordinal rather than by content. Upstream bumps `version` and `appVersion` automatically on every release. Pair with a `make helm-history` / `make helm-rollback` seam — [[XC-056]] — because the documented recovery path currently says "run bare helm".
- *Closes when:* Two consecutive releases render distinct chart versions and `helm history rask` names them.
- *Evidence:* the `antoniocali/polaris-k8s` audit, 2026-09-20 · measured: 193 revisions, all `rask-0.3.0`

**XC-056 · Helm's recovery verbs have no seam behind them, so the documented recovery is "run bare helm" — which bypasses `scripts/helm.sh`**
`chart, scripts` · **LOW**
- *What is left:* `make helm-history` and `make helm-rollback` do not exist, so the recovery instruction routes around the project's own helm seam and its values handling — the same class of mistake that lands a fleet on chart-default images. Add both targets through `scripts/helm.sh`.
- *Closes when:* Both targets exist, go through the seam, and the runbook names them instead of bare `helm`.
- *Evidence:* the `antoniocali/polaris-k8s` audit, 2026-09-20

**XC-057 · Destructive cluster targets guard on "a cluster answered", not on cluster IDENTITY — `make e2e-ci` can helm-upgrade the live k3s release**
`scripts, ci, chart` · **HIGH**
- *What is left:* A target that mutates a cluster checks reachability rather than which cluster it reached, so a stale or wrong kubeconfig is indistinguishable from the intended one. The estate already carries two kubeconfigs, one of them a dead kind cluster that still answers. Guard on a cluster-identity assertion (context name plus a marker object the release owns) before any mutating target runs.
- *Closes when:* A mutating target refuses a cluster whose identity it cannot confirm, proven by a test that points it at the wrong context.
- *Evidence:* the `antoniocali/polaris-k8s` audit, 2026-09-20 — upstream guards identity before mutation

**XC-058 · The audit tier is keyed on the log MESSAGE, and nothing stops a message being an f-string**
`service-kit, catalog, lineage` · **HIGH**
- *What is left:* Audit records are selected downstream by their log message, so one interpolated message silently drops that record out of the audit stream — a compliance record that vanishes without failing anything. Gate the log CALL: a constant message plus structured `extra=`, refused by a test over the audit call sites.
- *Closes when:* A gate refuses an audit call whose message is not a literal, and the existing call sites pass it.
- *Evidence:* the `antoniocali/polaris-k8s` audit, 2026-09-20

**XC-060 · The Python plane has no lockfile-drift gate while the JS plane does, so a dependency edit without a re-lock is green**
`ci` · **MED**
- *What is left:* A `pyproject.toml` dependency change that never reached `uv.lock` passes CI, so the lock and the declaration disagree until something fails at build time in an unrelated change. The JS plane already gates this; mirror it with `uv lock --check` over the root and each runner lock.
- *Closes when:* CI fails on a dependency edit with no corresponding lock change, mutation-checked by making one.
- *Evidence:* the `antoniocali/polaris-k8s` audit, 2026-09-20

**XC-061 · Container hardening is restated per template and applied unevenly — 7 first-party containers and 3 Jobs render with none of it, including OpenBao and Dex**
`chart` · **HIGH**
- *What is left:* `securityContext` is repeated per template rather than defaulted chart-wide, so coverage drifts silently and the two workloads that most need it — the secret store and the IdP — render without it. Hoist the baseline to one chart-wide default that a template opts OUT of with a stated reason, and gate the render.
- *Closes when:* Every first-party container and Job renders the baseline, and a test refuses a new one that does not.
- *Evidence:* the `antoniocali/polaris-k8s` audit, 2026-09-20 — upstream sets the baseline once, chart-wide

**XC-062 · Nothing proposes dependency bumps: the estate detects CVEs and has no mechanism that fixes them**
`ci` · **MED**
- *What is left:* `make audit` scans six lockfiles plus `.dagger/go.mod` for CVEs, so a vulnerable dependency is DETECTED — and then nothing raises the PR that would close it. There is no `renovate.json` and no `.github/dependabot.yml` anywhere in the tree (verified 2026-09-21). Detection without remediation means the scan's output ages into noise, which is the state every unattended scanner reaches. Add one bot, scoped to the six locks and the Go module, with the runner locks grouped separately so a workload's heavy stack cannot churn the fleet's.
- *Closes when:* A dependency bump reaches `main` as a bot-raised PR that the existing gates ran against.
- *Evidence:* the `antoniocali/polaris-k8s` audit, 2026-09-20 · `ls renovate.json .github/dependabot.yml` → neither exists

**XC-063 · A pinned tool version that never re-installs is not a pin — `.localbin` keeps whatever it downloaded first**
`scripts, ci` · **MED**
- *What is left:* `make bootstrap` installs `kind`, `kubectl`, `fga`, `k9s` and friends into `.localbin`, and a host that already has the file keeps the version it fetched the first time — so raising the pinned version in the Makefile changes nothing on any developer machine that ran bootstrap before the bump, and CI and local silently diverge. The install step must compare the installed binary's VERSION against the pin and re-fetch on mismatch, not test for the file's existence.
- *Closes when:* Changing a pinned version and re-running bootstrap replaces the binary, proven by a test or a scripted check that asserts the version after a downgrade.
- *Evidence:* the `antoniocali/polaris-k8s` audit, 2026-09-20 · `Makefile:151,198,204-205,248,816`

**XC-064 · Every kind/e2e stack renders with `observability.enabled=false`, so the estate's OTLP path is gated by nothing**
`ci, chart, observability` · **MED**
- *What is left:* `scripts/e2e_stack.sh:112` and `scripts/ray_e2e_stack.sh:103` both pass `--set observability.enabled=false`, so the Collector, GreptimeDB and every OTLP export are absent from the one place the estate is assembled automatically. The telemetry plane is therefore proven only by hand on the live k3s estate — which is exactly how [[CP-040]]'s permanently-firing `RayMetricsMissing` and [[XC-047]]'s zero OpenFGA series survived. Turn it on for at least one lane and assert a span and a metric land, rather than only that the fleet starts.
- *Closes when:* An e2e lane runs with observability ON and asserts a trace and a metric series reached the backend.
- *Evidence:* the `antoniocali/polaris-k8s` audit, 2026-09-20 — upstream proves the telemetry plane in CI rather than in a drill · `scripts/e2e_stack.sh:112` · `scripts/ray_e2e_stack.sh:103`

**XC-067 · `HttpServerLatencyHigh` CANNOT FIRE — its threshold is above the histogram's top bucket, for every service**
`observability, chart` · **HIGH**
- *What is left:* Make the fleet's latency alert able to fire. Either give `http_server_duration_milliseconds` explicit bucket boundaries that cover the real range (an OTel View in `service_kit.setup_otel`; `maintenance` legitimately runs ~40s and the default layout stops at 10s), or key the rule on a statistic that is not bucket-bounded. Raising buckets alone is not enough without re-checking the threshold against them, which is the mistake being fixed.
- **MEASURED LIVE 2026-09-21, by evaluating the alert's OWN expression against the deployed GreptimeDB.** `chart/alerting/rules.yml:1123` is `histogram_quantile(0.95, sum by (service_name, le) (rate(http_server_duration_milliseconds_bucket[10m]))) > 15000`. Run without the threshold it answers `maintenance -> 10000 ms`; run **as written it returns ZERO series**. Ground truth from the same store at the same moment: `maintenance` mean duration **39,504 ms**. A service running at 39.5 seconds reports a p95 of 10 seconds and pages nobody.
- **THE CAUSE IS THE BUCKET LAYOUT, not the engine.** The exported histogram carries the OTel default explicit boundaries `0,5,10,25,50,75,100,250,500,750,1000,2500,5000,7500,10000,+Inf`. Counted live for `maintenance`: `le=10000` holds **4** observations and `le=+Inf` holds **37** — so **89% of requests are past the top finite bucket**. `histogram_quantile` returns at most the upper bound of the highest finite bucket when the quantile lands in `+Inf`, so the expression is CAPPED at 10,000 and a threshold of 15,000 is unreachable at any latency, for any service, forever.
- **THE ROW'S OWN COMMENT SHOWS HOW IT PASSED REVIEW, which is the lesson.** It records verifying that "`histogram_quantile` over the bucket series is supported by GreptimeDB — verified by running this exact expression against the live store, because promtool accepting it proves nothing about the engine that evaluates it". That check was real and insufficient: it proved the expression EVALUATES, never that its threshold was REACHABLE given the buckets. Same family as [[XC-054]]'s first two gates and the `compaction_mode` reading in `docs/DECISIONS.md` — a number that cannot move, read as a measurement.
- **THE BLAST RADIUS IS WIDER THAN ONE RULE:** `chart/templates/perses-dashboards.yaml` uses `histogram_quantile` in **11** panels, all against the same capped buckets, so every latency panel in the estate saturates at 10s too.
- **SEVERITY CORRECTED, AND THE CORRECTION IS MINE.** This row first read as though a live alert were silently failing on-call. It is not: `observability.alerting.enabled` defaults **false** ("dev has no on-call; values-prod flips it on"), and measured live 2026-09-21 there is **no vmalert pod, no Alertmanager and no rules ConfigMap** in the cluster — `alerting.yaml` renders only under `observability.enabled AND alerting.enabled`. So the rule was dead AND unevaluated, and nobody was under-served by it. What was verified was that the EXPRESSION returns zero series against GreptimeDB; what was inferred, wrongly, was an operational consequence. The defect is real and the fix stands — `values-prod.yaml` flips alerting on, so the first production enable would have shipped a rule that cannot fire, joined by 11 Perses panels capped at 10s — but the urgency is a prod-readiness one, not an incident. Same class of error the row itself is about: a check that proves one thing, read as proving a neighbouring thing.
- **ATTEMPTED AND IT DOES NOT WORK — DEPLOYED AND FALSIFIED 2026-09-21.** `service_kit.otel.HTTP_DURATION_BUCKETS_MS` keeps every sub-100ms boundary and extends the tail to 300,000 ms, applied through a View on `http.server.duration` / `http.client.duration`. The alert excludes `maintenance` by name — reaching the buckets would otherwise have made it fire on every evaluation, trading a silent rule for a muted one — and `MaintenanceSweepOverrunsItsSchedule` covers that service at 120s, its own cron interval, where ticks begin to overlap. **THE VIEW IS PROVEN TO REACH THE INSTRUMENT**, not merely to exist: a 50,000 ms observation is recorded against a real `MeterProvider` and the boundaries read back off the exported point, mutation-checked against the other plausible instrument name (`http.server.request.duration`). Three firing tests in `rules_test.yml`, and a generic gate refuses any HTTP quantile rule whose threshold sits at or above the top bucket.
- **WHY THE VIEW NEVER APPLIES, and it is a bigger finding than this row.** The buckets and the View are correct and were deployed (release 196; the running pod carries `HTTP_DURATION_BUCKETS_MS` with a top of 300,000). Measured against GreptimeDB for 7 minutes afterwards, with metrics exporting FRESH (`http_server_duration_milliseconds_count` sample age 0s, counts 1/2/5 on the restarted pod): the boundaries are unchanged and `le="30000"` has **zero series**. The instrument name is right — `MetricInstruments.HTTP_SERVER_DURATION` resolves to `http.server.duration` in the image, which is what the View targets — and `instrument_app` runs after `set_meter_provider`, so the ordering inside `setup_otel` is right too.
  **The container runs `opentelemetry-instrument`** (`command: ["opentelemetry-instrument","uvicorn"]`, rendered whenever otel is on). The agent installs its OWN MeterProvider before app code executes, and `opentelemetry.metrics.set_meter_provider` is documented in the installed source as *"This can only be done once, a warning will be logged if any further attempt is made."* So **`setup_otel`'s MeterProvider — reader, exporter and views alike — is never the one in use**; the metrics that reach GreptimeDB come from the agent's own exporter. Any metric configuration written there is dead code under the agent, which is why a View that a unit test proves correct changes nothing in the cluster.
  **THE UNIT TEST PASSED AND WAS STILL INSUFFICIENT**, which is worth stating because it is this row's own lesson recurring: it built its own `MeterProvider` and its own histogram, proving the VIEW MECHANISM works. It could not prove that the provider `setup_otel` builds is the provider the instrumentation uses. Proving a mechanism is not proving the wiring.
  **What a real fix has to do:** either stop running the agent and let `setup_otel` own the providers, or ship an OTel *configurator/distro* entry point that the agent loads and that installs these views. Both are larger than a bucket list and neither should be bolted on without measuring the metrics plane first.
- *Closes when:* The alert fires against a service exceeding its threshold, proven by a mutation (point it at a real duration and watch it go from silent to firing) rather than by the expression parsing.
- *Evidence:* live evaluation 2026-09-21 — alert expression returns 0 series while `maintenance` mean is 39,504 ms · bucket counts `le=10000` 4 vs `le=+Inf` 37 · `chart/alerting/rules.yml:1122-1134` · `chart/templates/perses-dashboards.yaml` (11 `histogram_quantile` uses)

## PHASE 2 · COMPUTE

**LH-043 · Unknown whether MemWAL server-id sharding fits append-only bronze landing (coordinator-free ingest)**
`ingest, medallion` · **MED**
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** An ingest lander spike, double-gated behind [[XC-023]], with ZERO MemWAL consumers at HEAD. The work is unchanged; only the label is, so phase 1 stops claiming it.
- **blocked:** §K — the Dapr-retreat / BYO-engine cutover must land first; the row sequences itself after it (a sequencing gate, not a ruling).
- *What is left:* Prototype MemWAL one-shard-per-pod (`uuid5(instance_id)`, PUT-IF-NOT-EXISTS with epoch fencing, reads unioning all shards) against bronze landing and record whether it fits append-only ingest. The only MemWAL awareness at HEAD is the maintenance orphan scan recognising `_mem_wal/` as a known layout; no ingest path uses it. The audit records that blob v2 columns read `None` through the MemWAL scanner, so the prototype must re-check that on the installed pylance (11.0.0) before any design rests on it.
- *Closes when:* A prototype run against bronze landing answers, with a recorded result, whether MemWAL server-id sharding fits coordinator-free ingest.
- *Evidence:* `docs/audits/lakehouse-2026-09/lakehouse-analysis.md:230 (option C′, blob v2 reads None through MemWAL)` · `services/maintenance/src/maintenance/services/orphans.py:84-87,354-355 (only MemWAL awareness in code)` · `uv.lock:3276-3277 (pylance 11.0.0)`


**XC-037 · `pytest-xdist` is not a dependency and ~20 test files roll their own `subprocess` helm render**
`chart, service-kit` · **MED**
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** xdist shipped; the only blocker left is the COMPUTE service's import-order conftest. The work is unchanged; only the label is, so phase 1 stops claiming it.
- **RAISED LOW -> MED, and HALF SHIPPED 2026-09-19, because the cost was measured rather than guessed.** `pytest-xdist` is added and `make check-fast` runs the pre-push suite (`tests/unit` + `tests/integration`) at `-n 16 --dist loadfile`: **1m17s against 9m57s serial, 4,947 passed, zero failures** — 7.7x on a 64-core host, because the suite is a broad front with no hotspot. The three file-level unsafe suites this row names are handled by `--dist loadfile` exactly as it predicted. What made this MED rather than LOW: measured over one session, **519 minutes — 8.6 hours — went to 58 serial runs of that suite.** It is the iteration loop, not a nicety.
- **A FOURTH UNSAFE SUITE, which this row did not name:** `services/compute/tests/test_ray.py` passes 16/16 serially and fails 8 under `-n 16`, with `404 == 422` — the routes are not mounted. Its conftest sets `RASK_API_PREFIX`/`RAY_DASHBOARD_URL`, imports `compute` (whose `make_service_app` BAKES settings at import), then restores the environment. Correct serially, where pytest collects every module before running any test; under xdist a worker may import `compute` from another file first and bake the wrong prefix. That is an import-order dependency, not a parallelism bug — parallelism only made it visible.
- *What is left:* Add `pytest-xdist` (absent from `pyproject.toml` dev deps and `uv.lock`) and group the three parallel-unsafe suites (the `lance.audit` process-global logger, `configure_audit`'s level, the registry CAS markers) with `--dist loadfile`. Convert the cleanly-convertible hand-rolled helm renders onto the cached `_rendered_docs`/`render(*flags)` helper in `tests/unit/test_invariants.py` keyed on the verbatim flag tuple; 21 files invoke helm through their own `subprocess` today (20 excluding `test_invariants.py`, which hosts the helper). Leave alone the ones needing a real subprocess (`check=False` x3, `CalledProcessError` in `test_invariants`) and the two deliberate variants (`test_chart_gitops_ready._render` omits `image.localImages=true`; `test_prod_ha_posture` renders `-f chart/values-prod.yaml`).
- *Closes when:* the WHOLE suite passes green in parallel (`make check-fast` already covers the pre-push half) and the only `subprocess` helm calls left are the named exceptions. The compute conftest's import-order dependency is the remaining blocker on the full run.
- *Evidence:* `rg 'xdist' pyproject.toml uv.lock -> no hits` · `rg -l subprocess tests services packages | xargs rg -l '"helm"' -> 21 files` · `tests/unit/conftest.py:21 (nineteen files import _rendered_docs from test_invariants)`


**XC-014 · The Ray head is a hand-applied `deploy/ray-lance-demo.yaml` the chart does not render**
`chart, compute, medallion` · **MED** · PARTIAL
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** Every remaining step is Ray-lane — flip `ray.cluster.enabled`, repoint `medallion.rayAddress`, delete `deploy/ray-lance-demo.yaml`; its blocking template already landed. The work is unchanged; only the label is, so phase 1 stops claiming it.
- **blocked:** The same owner ruling as CP-012: a chart-owned RayCluster/RayService versus the standing hand-applied head. Reconciling the head presumes that answer.
- *What is left:* The OpenBao half is done and must not be retouched: `chart/templates/openbao.yaml:137` is a chart-owned Job that seeds KV (`:174`, `:283`) and enables/configures the kubernetes auth mount, policy and role idempotently (`:310-318`). Only the Ray head remains. After the ruling, render the head from the release (`chart/templates/rayservice.yaml` is gated on `ray.enabled && singleTenant.enabled`, default false) and delete `deploy/ray-lance-demo.yaml` together with its apply at `scripts/ray_e2e_stack.sh:121` and the references at `Makefile:433`, `chart/values.yaml:766` and `chart/templates/medallion.yaml:549`.
- *Closes when:* A fresh `make k3s-up` produces the Ray head from the chart and `deploy/ray-lance-demo.yaml` no longer exists.
- *Evidence:* `chart/templates/openbao.yaml:137,174,310-318` · `deploy/ray-lance-demo.yaml (9,845 bytes at HEAD; last touched 85fe0830)` · `scripts/ray_e2e_stack.sh:121` · `chart/templates/rayservice.yaml:1 + chart/values.yaml:65-66 (singleTenant.enabled: false)`


**LH-129 · The three Ray job scripts read `S3_KEY`/`S3_SECRET` from process env, `RASK_CREDENTIAL_REF` has no consumer, and nothing gates dead work-order fields**
`medallion, ray-kit, service-kit, chart, scripts` · **HIGH**
- **MOVED FROM PHASE 1 (2026-09-20): its own marker said so.** The row was blocked in phase 1 on "the row's own phase ruling — 'Phase 2, do not work ahead of the lakehouse'", which is a PHASE placement rather than a decision: it names where the work belongs, not something an owner must answer. Its subject is the three Ray job scripts' credential path, which is the Ray lane — phase 2 by the focus's own split, the same reading that moved [[LH-085]] and [[LH-010]]. Filed here it is workable rather than blocked, and phase 1's count stops claiming it.
- *What is left:* Wire the trust chain: the catalog accepts the cluster's OIDC issuer for the Ray identity; that identity gets `can_write_data`/`can_maintain` tuples on the tables a write-tier vend checks; `scripts/ray_stage_job.py:87-88`, `ray_train_job.py:64-65` and `ray_lance_job.py:45-46` read `RASK_CREDENTIAL_REF` plus the projected service-account token FILE and vend a 900 s triple through the catalog's STS door (`lance_storage_options` already takes `session_token`, `objectfs.py:35`); `deploy/ray-lance-demo.yaml` drops `S3_KEY` (:64) and `S3_SECRET` (:83) and moves the five `RASK_LINEAGE_TOKEN_SERVICE_*` secretKeyRefs (:101-126) to a mounted file. A scoped static key in env is not an acceptable interim. Add a gate over `work_order.to_env` (`work_order.py:126`) asserting every emitted name has a consumer — `RASK_TASK`, `RASK_MERGE_KEY`, `RASK_WRITE_MODE`, `RASK_CODE_VERSION`, `RASK_CREDENTIAL_REF` have zero (`RASK_IDEMPOTENCY_KEY` has one at `ray_stage_job.py:832`); `test_the_submitter_and_the_job_agree_on_the_wire.py:107` covers only the reverse direction. The head is hand-applied, so a chart-only fix cannot reach it.
- *Closes when:* The Ray job vends its storage credential keyed on RASK_CREDENTIAL_REF, no secret rides pod env, and the to_env consumer gate is green.
- *Evidence:* `scripts/ray_stage_job.py:87-88; scripts/ray_train_job.py:64-65; scripts/ray_lance_job.py:45-46` · `grep -rn RASK_CREDENTIAL_REF services packages scripts runners --include=*.py — only work_order.py:159` · `tests/unit/test_the_submitter_and_the_job_agree_on_the_wire.py:107` · `deploy/ray-lance-demo.yaml:64,83,101-126`


**LH-010 · The htr runner drives `build_source`/`build_sink` into ALTO writers, never reads bronze Lance or emits gold rows, and no geometry stage exists**
`runners/htr, chart` · **MED** · PARTIAL
- **MOVED FROM PHASE 1 (2026-09-20): the PLATFORM half is done, and what remains is one workload's own shape.** Re-measured before working it. Two of the row's three clauses are closed: the in-dataset `lineage` column shipped end-to-end (`transform.py:750` → `work_order.py:74,155` → `ray_stage_job.py:450` → `stage_stamp.py:146`), and the placement constraint — "no workload-named module under `services/medallion`" — is now PINNED by `tests/unit/test_the_medallion_names_no_workload.py`, which derives the vocabulary from the `runners/` directory names so a tenth workload inherits the gate without an edit, and checks module names AND identifiers while deliberately leaving PROSE alone (a comment reading "an audio deriver slots into `_DERIVERS` later" is the agnostic argument being made, not a modality leaking in). Mutation-checked against the exact file it exists to prevent: re-creating `medallion/schemas/htr.py` fires both legs, and an `HtrGoldRow` class dropped into the neutral `tier.py` fires the identifier leg alone. **AND THE CASCADE'S TIER CONTRACT IS NOT UNDEMONSTRATED** — measured across all nine runners, `runners/dummy/src/dummy_runner/job.py` opens a source Lance dataset and writes the target with `data_storage_version="2.2"` and stable row ids, so bronze→gold IS exercised by a runner. htr still driving `build_source`/`build_sink` into `AltoExportActor` is therefore that WORKLOAD's shape, not a hole in the platform — which is exactly what the seal says ("whatever a workload's stage graph, model or output format is, it reaches the platform as config"). Phase 2 is where BYO lives, and a runner is the BYO workload.
- *What is left:* Re-cut the htr runner's stage job to read bronze Lance and emit gold rows: `runners/htr/src` imports no `lance` at all, `main.py:24,109,111` still drives `build_source`/`build_sink`, and `pipeline.py:8,114` still ends in `AltoExportActor` (line numbers re-measured 2026-09-20; the row's were stale). Add the bronze→silver geometry stages inside the sealed runner (its own Ray job/image), surfacing to the platform as stage-runner config rows beside the three at `chart/values.yaml:1472-1480` over the generic transform — never as a module under `services/medallion`, which the gate above now enforces rather than merely asks for. The owner-directed P7b shape is recorded nowhere in `docs/DECISIONS.md`.
- *Closes when:* `runners/htr` opens a bronze Lance dataset and writes gold rows through the governed stamp, and the geometry stages run as stage-runner config rows with no workload-named module under `services/medallion`.
- *Evidence:* `runners/htr/src/runner/main.py:22,107; runners/htr/src/runner/pipeline.py:8,180-186` · `grep -rln 'import lance\|from lance' runners/htr/src → empty` · `chart/values.yaml:1462-1480 (three stageRunners rows, no geometry)` · `scripts/ray_stage_job.py:450; packages/service-kit/src/service_kit/lakehouse/stage_stamp.py:146`


**LH-085 · The media write lane is driver-only for its DERIVERS, not for blob typing — distributing it is untried**
`medallion (RAY half), scripts` · **MED**
- **MOVED FROM PHASE 1 (2026-09-20): what remains is the RAY driver, not the lakehouse.** Re-measured before working it. The derivers themselves live in `services/medallion/services/derivers.py` and are consumed by `services/medallion/services/compute.py` — the in-service media path, which is phase 1 and is DONE. What this row still asks for is distributing `scripts/ray_stage_job.py`'s lane into `map_batches`, and that script is baked into the `ray-lance` image and gated by `MEDALLION_RAY_ENABLED`, which defaults **false** in the medallion's own config (`core/config.py:316`). So the medallion's media lane works with no Ray at all, and an undistributed Ray driver does not make the LAKEHOUSE incomplete — condition 3 is that Ray is something the lakehouse can be driven BY, never something it depends ON. Same class as "maintenance's Ray half", which the focus already places here.
- **THE UNMEASURED ASSUMPTION IS MEASURED AND FALSE (2026-09-20).** In the deployed `ray-lance` image at the row's exact pins (pylance 11.0.0, pyarrow 25.0.0, lance-ray 0.5.0): `write_lance(..., data_storage_version="2.2")` PRESERVES blob-v2 — `extension<lance.blob.v2<BlobType>>` round-trips intact. Omit that argument and it writes V2_1, and Lance refuses the column outright ("Blob v2 requires file version >= 2.2"); that refusal is what the "strips blob typing / exposes plain LargeBinary" reading came from. The three copies of that claim in `ray_stage_job.py` are corrected and the measurement sits beside `MEDIA_BATCH_ROWS`.
- *What is left:* distributing the lane, which now rests on the REAL constraint: the derivers (inline thumbnail + embedding) run on the driver because they need the payload bytes. Moving them into `map_batches` is the change — `_derivable_blob_column`'s "first non-null decides" contract has to survive it, since the derived columns are part of the schema and a later batch that disagreed with the first would fail the append.
- *Closes when:* the media lane writes through `lance_ray` with the derivers distributed.
- *Evidence:* `scripts/ray_stage_job.py:13-17,168,747` · `services/medallion/src/medallion/core/config.py:316 (ray_enabled defaults false)` · measured on pod `ray-lance-head` 2026-09-20

**LIN-003 · The runner lanes emit no START and emit inline, where the reference implementation does neither**
`runners, lineage-kit` · **MED**
- *What is left:* Measured against `datafusion-contrib/datafusion-openlineage`, the closest reference for a lakehouse query/compute engine: it emits "`START` at plan time, `COMPLETE` / `FAIL` at end of execution, all under one run id" and sends events "through a bounded queue drained by a background task — lineage never stalls or fails a query". rask's runner lanes do neither. `runners/dummy` REFUSES a START in terms ("a START notifies nobody"), which is a notifications argument applied to a lineage decision: a START is what makes a run observable WHILE it runs and what a child's `ParentRunFacet` attaches to. `runners/htr` emits only COMPLETE/FAIL. Both call `emit()` inline, so a slow ingest slows the job. `medallion/services/transform.py:316` and `scripts/ray_train_job.py:461` DO emit START, so this is a runner-lane gap rather than an estate-wide one. **`lineage_kit.runs.LineageRun` already carries the lifecycle** — `start()`, terminal-once protection, per-run facets, the `on_undelivered` hook — and neither runner lane uses it: both hand-roll ~150 lines that duplicate it and omit the terminal-once guard, so either can emit COMPLETE twice.
- *Closes when:* Both runner lanes drive `LineageRun`, emit START, and emit off the critical path; `_NOT_A_PERSON` and the originator/project read live in `lineage-kit` rather than once per lane.
- *Evidence:* `runners/dummy/src/dummy_runner/lineage.py:91-92 (TERMINAL_STATES refusal)` · `runners/htr/src/runner/lineage.py (no START, inline emit)` · `packages/lineage-kit/src/lineage_kit/runs.py:188-215 (_emit_terminal, start)` · https://github.com/datafusion-contrib/datafusion-openlineage
- **MOVED HERE FROM PHASE 1 (2026-09-20).** Tagged `runners, lineage-kit`, and its close condition is entirely runner-lane work — "Both runner lanes drive `LineageRun`, emit START, and emit off the critical path". The lakehouse half of this row is already CORRECT and was measured rather than assumed: the medallion emits START (`transform.py:343`), and both catalog emit transports are bounded and best-effort, with the Dapr one carrying a comment that a hung sidecar cannot pin the request path. So nothing here is one of the four services.

**LH-096 · Outside the four lakehouse services, Lance is still opened bare per request and the bundled runtime-hygiene clauses are unfinished**
`ingest, viewer, search, catalog, service-kit` · **MED** · PARTIAL
- *What is left:* The session half is complete and gated: catalog 10, lineage 8, medallion 15, maintenance 9, service-kit 7 opens all thread a session, zero bare (AST count this session; pinned by tests/unit/test_a_lakehouse_open_shares_the_process_session.py). Remaining bare opens are ingest 9 (adapters.py:207, lander.py:121/170/174/266, catalog.py:174/250, workflow.py:1122 — phase 2), viewer 5 and search 1 (do-not-work list); ingest also never calls `instrument_lance_if_available`. Of the bundled residue: the catalog's `allow_http` is a settings bool (`LANCE_S3_ALLOW_HTTP`, default True, catalog/core/config.py:242) rather than derived from the endpoint scheme as service_kit/media/config.py:299 does; `create_tag` (dataplane.py:1834) has no door-side name refusal while branches have `refuse_a_branch_name_the_backend_cannot_use` (:1737, called :1914); pooled HTTPX clients in notifications/lifespan.py:102 and service_kit/media/lifespan.py:59 set no timeout (ingest/http.py is per-call by design, :19). No blob-threshold setting exists anywhere in the tree, so that clause has nothing to pin. Do NOT add `LANCE_CPU_THREADS` (falsified, [[LH-172]]); `LANCE_IO_THREADS`/`LANCE_LOG` are untested here.
- **`allow_http` IS DERIVED NOW (2026-09-20), and it was a downgrade permit rather than a tidiness clause.** It was a settings bool the chart pinned to `"true"` on every deployment, so the catalog held permission to speak plaintext to object storage whatever `LANCE_S3_ENDPOINT` named — silent, because object_store consults the flag only when a request would otherwise be refused. `namespace_properties()` now derives it from the endpoint scheme, the field and its env var are deleted from the chart and both side stacks, and no env can re-enable it (pinned by `services/catalog/tests/test_allow_http_follows_the_endpoint_scheme.py`, whose stale-override leg sets the real variable). The catalog matters more than the sibling it copies: it also VENDS these options to clients. **OBSERVED on the deployed catalog (`lance-rest-catalog:lh096-allowhttp`):** the pod still carries the unrolled chart's `LANCE_S3_ALLOW_HTTP=true`, the field is absent from the settings object, and with the endpoint overridden to `https://` in that same process the derived value is `false` — so a stale env cannot re-enable plaintext, and an image may roll BEFORE the chart drops the variable. Data plane proven by real traffic on the new image (a `vend_credentials` success and `POST /management/v1/table/{id}/compaction_plan` 200), not by a health probe.
- **THE TAG-NAME CLAUSE IS STRUCK — measured, the door is already right and a guard would BREAK it.** The row asked for tag names to be refused at the door "like branch names". On pylance 11.0.0 every case `refuse_a_branch_name_the_backend_cannot_use` exists for is BRANCH-specific: `main` is the default branch and an ordinary TAG (creating it succeeds — a copied guard would refuse a legal name); an empty tag answers `Ref is invalid: Ref cannot be empty` where a branch hits pylance's bug-report text; and a traversal segment cannot reach the object-store path parser because a tag rejects `/` outright (`feature/x` is a legal branch and an illegal tag). All of them already carry the marker `_classify_ref_error` maps to InvalidInput 13. Pinned as a TRIPWIRE by `services/catalog/tests/test_a_malformed_tag_name_needs_no_door_side_guard.py`, so a pylance that stops mapping one reds there — which is the day a guard becomes correct.
- *Closes when:* ingest's 9 opens thread a session and it calls `instrument_lance_if_available`; every pooled HTTPX client carries a timeout; the AST gate's ingest exemption is removed. (All three are phase 2 — the catalog's own clauses are done.)
- *Evidence:* `AST walk this session: ingest 0/9 bare, viewer 0/5, search 0/1; catalog/lineage/medallion/maintenance/service-kit all 0 bare` · `grep -rn 'instrument_lance_if_available(' → catalog/main.py:102, lineage/main.py:51, medallion/producer.py:76, stage_runner.py:61, maintenance/service.py:106 only` · `services/catalog/src/catalog/core/config.py:242 `s3_allow_http: bool = Field(default=True, alias="LANCE_S3_ALLOW_HTTP")`; packages/service-kit/src/service_kit/media/config.py:299 derives from `startswith("http://")`` · `services/catalog/src/catalog/services/dataplane.py:1737 branch-name refusal, :1834-1838 `create_tag` without one`
- **MOVED HERE FROM PHASE 1 (2026-09-20) on the row's own words.** Its catalog clauses are done — `allow_http` derived from the endpoint scheme, and the tag-name clause struck as measurably wrong — and its close condition names only ingest's nine bare opens, `instrument_lance_if_available`, the pooled HTTPX timeouts and the AST gate's ingest exemption. A row counted against phase 1 whose every remaining clause is phase 2 makes the phase-1 number mean less than it says.

**CP-032 · Six of the nine runners have no `uv.lock`, so the parametrized runner image cannot build them at all**
`runners, chart` · **HIGH**
- *What is left:* Give `asr`, `diarize`, `insid3`, `kg`, `topics` and `voiceprint` a `uv.lock` and a build backend, or stop counting them as runners. `.docker/ray-runner.dockerfile:103` is `uv sync --project runners/${RUNNER} --locked --no-editable`, and `--locked` refuses a project with no lockfile — measured 2026-09-18: `uv sync --project runners/asr --locked --dry-run` → `error: Unable to find lockfile at 'uv.lock', but '--locked' was provided`, while the same command on `runners/htr` resolves. Only **3 of 9** carry a lock (`htr`, `dummy`, `assist`). Five of the six also ship no `build-backend` and no `[project.scripts]`, so they are loose `.py` files at the runner root rather than installable projects — no `src/` package either (only `htr` and `dummy` have one). `runners/asr` additionally resolves to **CPython 3.11.15**, not the estate's 3.13. This is what bounds [[LIN-001]]: "wire the eight silent runners through lineage-kit" is not eight comparable units of work, because six of them cannot take a path dependency until they are projects.
- **IT INHERITS [[LIN-001]]'s REMAINDER, which closed 2026-09-20 with every clause of its own done.** A runner that becomes buildable then follows `runners/htr` exactly: `lineage-kit` as a path dep (the package is dependency-capped so the seal does not leak), a `lineage.py` holding lane policy only, and terminal events around the pipeline. It is one step after the lock, not a separate campaign — and `tests/unit/test_a_runner_that_emits_does_it_through_lineage_kit.py` already refuses the hand-rolled alternative, so a newly-locked runner cannot mint its own envelope by accident.
- *Closes when:* Every directory under `runners/` either builds through `.docker/ray-runner.dockerfile` with `--locked`, or is removed from the runner count in `CLAUDE.md` and the architecture docs, and every runner that emits does so through `lineage-kit`.
- *Evidence:* `.docker/ray-runner.dockerfile:100,103` · `git ls-files runners/<r> → no uv.lock for asr, diarize, insid3, kg, topics, voiceprint` · `uv sync --project runners/asr --locked --dry-run (2026-09-18) → Unable to find lockfile` · `grep -c 'build-backend' runners/{asr,diarize,insid3,kg,voiceprint}/pyproject.toml → 0`

<!-- These rows came from a 20-agent research workflow (2026-09-18) over the Ray/KubeRay docs plus a
     measurement of this estate, with every row adversarially verified against the tree. 27 claims were
     REFUTED and corrected before filing: an id collision, a dangling cross-reference, an inverted Kueue
     claim, a backwards causal attribution, and a false 'replicas: 1 is single-writer'. Two drafted rows
     (Kueue admission, Ray-image lineage) were FOLDED into CP-012 and CP-018 rather than filed, because
     the verifier showed both were re-filings of clauses those rows already carry. -->

**CP-033 · The workflow instance id omits `code_version` while the Ray submission id includes it, so a rolling deploy re-attaches across builds — the exact defect the `code` axis was added to close**
`medallion` · **HIGH**
- *What is left:* `transform.py:164` derives the workflow instance id as `f"stage-{stage_submission_id(stage, token, from_uri, to_uri)}"` with no `code=` argument (it defaults to `""`), while `ray_submit.py:181` derives the Ray submission id with `code=code_version`. During a rolling deploy, one (stage, token, from→to) therefore maps to ONE workflow instance id and TWO different Ray submission ids: a redelivered trigger landing on the new pod computes the same instance id, `DaprSagaClient.start` catches the duplicate, `_exists` confirms the old instance is alive, returns `ALREADY_RUNNING`, and the handler acks — so the new build's job is NEVER submitted and the old instance keeps watching the old build's job. `ray_jobs_api.py:57-66` documents that outcome as the reason the `code` axis exists at all ("The run then carried the new build's provenance over the old build's output, which is worse than a failure because nothing is red"), and `ray_submit.py:112-114` states the rule the instance id breaks: "`code` (B3) must therefore reach BOTH calls". This is not theoretical — measured live 2026-09-18 on `rask-bronze-to-silver` and `rask-silver-to-gold`: `MEDALLION_RAY_CODE_VERSION=localhost:5000/lance-rest-catalog:main-334ab652` is set, so the axis is active and the divergence is live. Fix: pass `code=code_version` at `transform.py:164` (it must be resolved there the same way `ray_submit.py:169` does — `spec.code_version if spec else settings.ray_code_version`), or derive the instance id from the submission id the submitter returns rather than re-deriving it.
- *Closes when:* A test drives two triggers for one (stage, token, from→to) under two different `code_version` values and asserts two distinct workflow instance ids, with the second build's job actually submitted rather than reported as a re-attach.
- *Evidence:* `services/medallion/src/medallion/services/transform.py:164` (no `code=`) · `services/medallion/src/medallion/services/ray_submit.py:169,181` · `services/medallion/src/medallion/services/ray_submit.py:107-116` (`stage_submission_id`, `code: str = ""`) · `services/medallion/src/medallion/services/ray_jobs_api.py:57-66` (the `code` axis's stated purpose) · `services/medallion/src/medallion/services/dapr_saga.py:46-54` (duplicate → ALREADY_RUNNING) · live 2026-09-18: `kubectl get deploy rask-bronze-to-silver -o json` → `MEDALLION_RAY_CODE_VERSION=localhost:5000/lance-rest-catalog:main-334ab652`

**CP-034 · A stage job that SUCCEEDED but whose wake-up publish was exhausted is written to the lineage graph as an eventType FAIL, for a hop whose data is committed**
`medallion, lineage` · **HIGH**
- *What is left:* `workflow.py:399-405` sets `verdict="unnotified"` when `publish_stage_ready` exhausts `ACTIVITY_RETRY`, and its own docstring is explicit that "the JOB succeeded and the data landed". The metric, the log and the span all honour that. The lineage event does not: `report_stage_outcome` calls `_publish_fail_event(_build_stage_fail_event(...))` unconditionally for every non-succeeded verdict, and `_build_stage_fail_event:722-762` hard-codes `event_type="FAIL"` with "a bare output (the WROTE edge, no version — nothing was written)". The reason string at `workflow.py:626-627` branches only on `failed`, so an `unnotified` run lands in the graph reading literally "the watch was abandoned after N poll(s) with the job still SUCCEEDED (unnotified)". Consequences, all three real: the graph asserts a failure for a write that is on disk; no COMPLETE can ever follow, because the only path to measure/emit is the publish that just failed; and the reconcile sweep later back-fills the same dataset as a synthetic `reconcile-<name>-v<version>` run with `author='reconcile'`, no inputs and no DERIVED_FROM — so one successful run ends up represented by a FAIL plus an authorless repair. Fix: make `report_stage_outcome` emit nothing on `unnotified` (the counter, the log, the ERROR span status and `MedallionStageOutcomesFailing` already carry it — `rules.yml:204-214` names `unnotified` by hand), or give it an event that does not assert an unwritten output. Do not widen `_build_stage_fail_event`.
- *Closes when:* A test drives an `unnotified` outcome and asserts the lineage graph carries no FAIL run for a destination whose write committed, and that the operator signal (counter + alert + log) still fires.
- *Evidence:* `services/medallion/src/medallion/workflow.py:399-405` (the `unnotified` branch and its docstring) · `services/medallion/src/medallion/workflow.py:208-213` (verdict vocabulary: "the JOB succeeded and the data landed") · `services/medallion/src/medallion/workflow.py:626-627` (reason string branches only on `failed`) · `services/medallion/src/medallion/workflow.py:722-762` (`event_type="FAIL"`, bare output) · `services/lineage/src/lineage/services/repository.py:14-19` (FAIL keeps a WROTE edge with no version) · `chart/alerting/rules.yml:204-214`

**CP-035 · The chart points `compute` — the estate's only Ray job-history reclaimer — at a different cluster from the one the cascade submits to, so the head that accumulates job records is never pruned**
`compute, medallion, chart` · **HIGH**
- *What is left:* Two independent values name two Ray planes and nothing ties them. `chart/values.yaml:2202` is `ray.dashboardUrl: "https://dev-kuberay.ra.se"`, rendered into `rask-config` at `configmap.yaml:107` and read by `service_kit.config:64` as `ray_dashboard_url` — which is what `compute`'s pruner (`pruner.py:34`, `dependencies.py:37`), the jobs board, `/api/serve`, the explorer's Serve discovery (`explorer.yaml:259-261`) and the frontend's `serveOrigin` (`frontends.yaml:179`) all use. The cascade submits to a SEPARATE key, `medallion.rayAddress` (`config.py:311`, default `http://ray-lance-head:8265`). On the chart as declared, the `compute-prune-jobs-cron` binding therefore issues `DELETE /api/jobs/{id}` against a cluster this repo does not own, while the cluster the cascade uses accumulates unpruned — which is exactly the accumulation measured at 81,155 jobs / 164.7 MB that OOMKilled compute (`ray-kit/prune.py:1-8`). `RayJobHistoryGrowing` cannot catch it either: `ray_control_jobs_known` is produced by compute against its own target. Live it only agrees because `rask-compute` carries a container-level `RAY_DASHBOARD_URL=http://ray-lance-head:8265` that the chart renders nowhere (the ConfigMap still says `https://dev-kuberay.ra.se`) — so the running estate's Ray target is not in git and nothing reconciles it. Fix: make the cascade's Ray plane one name, or add an invariant test refusing a render where `ray.dashboardUrl` and `medallion.rayAddress` resolve to different clusters without an explicit opt-in value, and delete the out-of-band env.
- *Closes when:* `helm get values` plus a render show one Ray plane for submission, pruning and introspection (or an explicit two-cluster opt-in), the live `rask-compute` target comes from the release, and a prune tick is observed deleting terminal records on the cluster the medallion submits to.
- *Evidence:* `chart/values.yaml:2202` · `chart/templates/configmap.yaml:106-109` · `packages/service-kit/src/service_kit/config.py:64` · `services/compute/src/compute/pruner.py:34-47`, `services/compute/src/compute/dependencies.py:37` · `services/medallion/src/medallion/core/config.py:311` · `packages/ray-kit/src/ray_kit/prune.py:1-8` · `chart/alerting/rules.yml:710-723` (`RayJobHistoryGrowing`) · live 2026-09-18: `kubectl get cm rask-config -o jsonpath='{.data.RAY_DASHBOARD_URL}'` → `https://dev-kuberay.ra.se`, while `kubectl get deploy rask-compute` env → `RAY_DASHBOARD_URL=http://ray-lance-head:8265`

**CP-036 · CP-021's blocked ruling is framed on a false dichotomy — Ray ships an embedded RocksDB GCS backend that needs no Redis, so "accept job loss or break the no-Redis rule" is not the choice**
`chart, compute, medallion, deploy (ray head)` · **HIGH**
- **blocked:** Owner ruling on CP-021, now re-framed: (a) accept job loss and record it, (b) grant the Redis exception, or (c) take the ALPHA RocksDB backend behind a values toggle with a PVC. (c) needs an explicit decision because it puts an alpha Ray subsystem on the cascade's critical path.
- *What is left:* CP-021 is parked on a two-way owner call: accept job loss on head restart, or grant a scoped exception to the estate-wide no-Redis rule. Ray documents a THIRD backend, verified against the source 2026-09-18: "**External Redis** (officially supported)" and "**Embedded RocksDB** (alpha)", enabled by `RAY_gcs_storage=rocksdb` plus `RAY_gcs_storage_path=<dir>` ("required; Ray fails fast at startup if it's unset"), with the restrictions "Linux only" and "single-writer: exactly one GCS may open the storage path at a time". The estate's head already satisfies both structurally — `deploy/ray-lance-demo.yaml:26` is `replicas: 1` on Linux — so what this needs is a PVC, not a datastore. **State the limits in the ruling, because they are load-bearing:** it is ALPHA and "may change before becoming stable"; it persists the GCS's cluster metadata, not application state; and on a head-only cluster (`_ray-cluster-config.tpl:268` `workerGroupSpecs: []`) the driver still dies with the head, so this converts `UNKNOWN` into a known terminal state rather than resuming work. That conversion is the whole value: it lets `RayJobsApiExecutor` claim `Capability.DURABLE_RECORD` and retires the guess-and-resubmit machinery whose only licence is `may_resubmit`'s `DURABLE_RECORD not in capabilities`. Also record what is unavailable during GCS recovery — actor creation/deletion/reconstruction, placement groups, resource management, worker registration, worker process creation — and that a raylet that cannot reconnect for 60 s exits (`RAY_gcs_rpc_server_reconnect_timeout_s`). Re-frame CP-021 with the third option and take the ruling.
- *Closes when:* `docs/DECISIONS.md` records the ruling across THREE options, not two. If RocksDB is taken: the head carries `RAY_gcs_storage=rocksdb` + `RAY_gcs_storage_path` on a reattachable PVC, a head restart is MEASURED to leave a submitted job's record readable on `GET /api/jobs/<id>`, and `RayJobsApiExecutor.capabilities` gains `DURABLE_RECORD` with `MAX_UNSEEN_POLLS`/`MAX_RESUBMITS` retired in the same commit.
- *Evidence:* https://docs.ray.io/en/latest/ray-core/fault_tolerance/gcs.html (fetched and quoted 2026-09-18: two backends; `RAY_gcs_storage=rocksdb`; `RAY_gcs_storage_path`; "alpha and may change before becoming stable"; "Linux only"; single-writer; "officially supported only if you are using KubeRay for Ray serve fault tolerance") · `grep -rn -i gcsFaultTolerance chart/ deploy/` → no matches · `deploy/ray-lance-demo.yaml:26,43-51` · `chart/templates/_ray-cluster-config.tpl:268` · `packages/service-kit/src/service_kit/lakehouse/executor.py:52-53,99-106` · `services/medallion/src/medallion/services/rayjobs_api_executor.py:20-25,72` · `services/medallion/src/medallion/workflow.py:106,123`

**CP-037 · Every compute-plane lineage emit is fire-and-forget: no outbox, no undelivered hook, and the one caller's return value is discarded — so a Ray job that commits its write and loses its terminal event leaves data the graph never learns of**
`runners, medallion, lineage-kit, service-kit` · **HIGH**
- *What is left:* Everything the medallion SERVICE emits is staged through `service_kit.lakehouse.outbox.publish_lineage_with_outbox` — stage object, publish, drop on ack, with a publish failure leaving the staged copy for `reconcile_cron._drain_outbox` to re-ingest with its inputs, author and columnLineage intact. Nothing on the Ray side has this. `lineage_kit.emitter.ClientEmitter.emit:85-99` catches, records a drop and returns `False`; `runners/dummy/src/dummy_runner/job.py:87-91` discards that boolean ("Best effort, and deliberately AFTER the work"), and on the FAIL path re-raises immediately after, so a FAIL that fails to POST is simply gone. `scripts/ray_train_job.py:191-243` is two `urllib.request` attempts then a stderr line. `lineage-kit` already ships the seam for this — `LineageRun(on_undelivered=...)` at `runs.py:88,112-143` — and it has exactly ONE production caller anywhere in the estate, `services/ingest/src/ingest/lineage.py:220`, none on the compute plane. Consequence: the only repair is the reconcile sweep's synthetic run (`repository.backfill_write:925-970`) — `reconcile-<name>-v<version>`, `author='reconcile'`, NO inputs, no DERIVED_FROM, no columnLineage — so the provenance edge from bronze to that silver version is lost permanently. This is distinct from LIN-001 (which is emit COVERAGE and envelope authorship, and is already ruled) and from LH-004 (catalog-side atomicity, which already HAS the outbox). Fix: wire `on_undelivered` on every compute-plane emitter to stage the event to the same `_lineage_outbox` prefix the services use, and stop discarding `emit()`'s return.
- *Closes when:* A compute-plane emit whose POST is forced to fail leaves a durable artefact that `reconcile_cron._drain_outbox` re-ingests with its inputs and author intact, pinned by a test, and no caller discards `emit()`'s boolean.
- *Evidence:* `packages/lineage-kit/src/lineage_kit/emitter.py:85-99` (swallow, record_drop, return False) · `runners/dummy/src/dummy_runner/job.py:87-91,101,109,113` · `runners/dummy/src/dummy_runner/lineage.py:138-148` · `scripts/ray_train_job.py:191-243,199-201` · `packages/lineage-kit/src/lineage_kit/runs.py:88,112-143` · `grep -rn on_undelivered --include=*.py services/ packages/ runners/ scripts/ | grep -v test` → one production caller, `services/ingest/src/ingest/lineage.py:220` · `packages/service-kit/src/service_kit/lakehouse/outbox.py:333-380` · `services/lineage/src/lineage/services/repository.py:925-970`

**CP-038 · The train watcher deliberately publishes no terminal on `abandoned`, which is exactly the verdict a head restart produces — so a lost training run stays OPEN in the lineage graph forever and its originator is never told**
`medallion, notifications` · **MED**
- *What is left:* `workflow.py:1024-1031` returns early on `abandoned` with a log line and no lineage event, and the reasoning it gives is "the job is still running… Emitting a lineage FAIL for it would send somebody hunting a healthy four-hour run." That holds for ONE of the three arms. `workflow.py:930-947` assigns the same `abandoned` verdict to the poll ceiling, to `watch_lost`, AND to `vanished`/`never_registered` — and `vanished` means the head forgot a job it had registered, which on a head-only cluster (`_ray-cluster-config.tpl:268` `workerGroupSpecs: []`) also means the driver died with the head. The train lane has no resubmit (`MAX_RESUBMITS` is stage-only; `submit_train_job` uses `on_terminal_failure="report"`), so nothing else covers it. Consequence: the job's own START and RUNNINGs are in the graph (`ray_train_job.py:524,532,539`), the COMPLETE or FAIL it would have emitted died with the head, and the watcher declines to supply one. Nothing distinguishes that run from one still in progress; `record_train_outcome("abandoned")` is a number, not a run closure; and the notifications plane has no terminal to target, so the person who asked for the training hears nothing at all. Fix: split the arms `_abandon_reason` already names apart — emit a terminal (ABORT, or FAIL with the vanished reason) on `vanished`/`never_registered`, keep the silence for the ceiling arm where the job may genuinely still be alive.
- *Closes when:* A train run whose job record vanishes from the head reaches a terminal state in the lineage graph, while a run still RUNNING at the poll ceiling still emits nothing — both pinned by tests.
- *Evidence:* `services/medallion/src/medallion/workflow.py:1024-1031` ("deliberately publishes no lineage FAIL") · `services/medallion/src/medallion/workflow.py:930-947` (all three arms → `abandoned`) · `services/medallion/src/medallion/workflow.py:216-229` (`_abandon_reason` already names them apart) · `services/medallion/src/medallion/workflow.py:106,123` (`MAX_UNSEEN_POLLS`, `MAX_RESUBMITS` stage-only) · `services/medallion/src/medallion/services/ray_submit.py:429` (`on_terminal_failure="report"`) · `scripts/ray_train_job.py:521,524,532,539,542,546` · `chart/templates/_ray-cluster-config.tpl:268`

**CP-039 · When a stage job dies, the only record that outlives the head is an 800-character summary on a FAIL event — the job record, the driver log and Ray's own error detail all vanish, and the upstream answer (Ray History Server) is not adoptable here**
`medallion, compute, chart, deploy (ray head)` · **MED**
- *What is left:* The estate's whole durable post-mortem for a dead Ray job is `_read_stage_failure`'s three fields (`error_type`, `message`, `driver_exit_code`) read off `GET /api/jobs/<id>` and truncated into the FAIL event's `errorMessage` facet at `_STAGE_FAIL_MESSAGE_CAP = 800`. Both of the sources behind it are transient: the job record dies with the head (no GCS FT — CP-036) or is deleted by the 6-hourly pruner beyond the newest 500/100, and `ray-kit/prune.py:29-32` states the consequence verbatim — "once the row is deleted the question 'why did that stage die' has no answer anywhere"; the driver log is a file under `/tmp/ray` that nothing mounts and nothing tails (CP-018, still open). **Do not close this with the Ray History Server**, and record why so it is not re-proposed: it preserves logs and event records and executes, resumes or retries nothing; it requires a collector sidecar plus `RAY_enable_ray_event`, `RAY_enable_core_worker_ray_event_to_aggregator`, `RAY_DASHBOARD_AGGREGATOR_AGENT_EVENTS_EXPORT_ADDR` and `RAY_DASHBOARD_AGGREGATOR_AGENT_EXPOSABLE_EVENT_TYPES` on every workload pod, with a shared emptyDir at `/tmp/ray`; this estate has ZERO KubeRay CRs live, so nothing injects them and its `rayclusters-reader` ClusterRole has nothing to correlate; its documented install is `curl … | envsubst | kubectl apply` of `quay.io/kuberay/{collector,historyserver}:nightly`, outside both Helm and Dagger; it states no retention by design; `--enable-live-clusters` makes it an unauthenticated fan-out proxy to every RayCluster it can reach and `enableK8sTokenAuth` is unsupported; and its only worked storage config is GCS, with no S3-compatible example for a RustFS estate. Close it instead with CP-018's log shipping plus the FAIL event, so the cause survives the pod.
- *Closes when:* The cause of a stage failure is answerable after the head pod is gone and after retention has run — from the FAIL event plus shipped driver logs in GreptimeDB — without reading the Ray dashboard; and `docs/DECISIONS.md` records the History Server as declined with these reasons.
- *Evidence:* `services/medallion/src/medallion/workflow.py:690-700` (`_STAGE_FAIL_MESSAGE_CAP = 800`), `:636-648` (`_read_stage_failure` only on the `failed` verdict) · `services/medallion/src/medallion/services/ray_job_failure.py:26-33` · `packages/ray-kit/src/ray_kit/prune.py:27-32` · `services/compute/src/compute/config.py:55-56,62-63` + `chart/templates/compute-prune-cron.yaml:19` · `chart/templates/_ray-cluster-config.tpl:94-99` (/tmp/ray unmounted, untailed) · https://docs.ray.io/en/master/cluster/kubernetes/user-guides/kuberay-history-server.html (sidecar env table; RBAC; retention note; live-cluster security note; GCS-only walkthrough) · live 2026-09-18: `kubectl get rayclusters,rayservices,rayjobs -A` → `No resources found`

**CP-040 · The in-cluster Collector's `ray-pods` scrape job keeps on a label only KubeRay creates, and the estate's only Ray pod carries neither that label nor a metrics port — so `RayMetricsMissing` fires permanently and every downstream Ray alert is structurally blind**
`chart, deploy (ray head)` · **MED**
- *What is left:* Measured live 2026-09-18: `rask-otel-collector` IS running (so `observability.otelCollector.externalEndpoint` is empty here and the in-cluster Collector renders), its ConfigMap carries `job_name: ray-pods` at line 127 keeping on `__meta_kubernetes_pod_label_ray_io_is_ray_node` regex `"yes"`, and `kubectl get pods -A -l ray.io/is-ray-node=yes` returns `No resources found`. The estate's only Ray pod is `ray-lance-head`, a hand-applied Deployment labelled `app: ray-lance-head` (`deploy/ray-lance-demo.yaml:25,31`), which fails the keep on TWO independent counts: it carries no `ray.io/*` label, and it declares only `gcs`/`dashboard`/`client` container ports (`:128-130`) with no port named `metrics` — `ray start` is given no `--metrics-export-port`, so Ray's Prometheus endpoint is not exposed at all. Consequence: `RayMetricsMissing` (`absent(ray_node_cpu_utilization)`) fires forever, and its own description says the rest out loud — "Until it clears, RayWorkerOOMKills and RayServeNoHealthyReplicas cannot fire." That means the one failure this research identifies as most likely for a head-only cluster (driver exit 137, a host-RAM OOM) is invisible on the cluster the cascade actually uses. This is DISTINCT from CP-017, which is scoped to the external `dev-kuberay.ra.se` cluster and blocked on its operators; this half is in-repo and unblocked. Fix with the head: expose Ray's metrics port and add the `ray.io/is-ray-node: "yes"` label (or land XC-014 and let KubeRay do both), then carry the existing `metric_relabel_configs` drop at `otel-collector.yaml:247-254` — an unfiltered scrape adds 113 `ray_data_*` families and OOMKills the store.
- *Closes when:* `ray_node_cpu_utilization` returns a series in GreptimeDB for the cluster the medallion submits to, and `RayMetricsMissing` clears.
- *Evidence:* live 2026-09-18: `kubectl get cm rask-otel-collector -o yaml` → line 127 `job_name: ray-pods`, line 133 keep on `__meta_kubernetes_pod_label_ray_io_is_ray_node`; `kubectl get pods -A -l ray.io/is-ray-node=yes` → `No resources found`; `kubectl get pods -A | grep -i ray` → only `kuberay-operator` and `ray-lance-head` · `deploy/ray-lance-demo.yaml:25,31,128-130,43-51` (no metrics port, no `--metrics-export-port`) · `chart/templates/otel-collector.yaml:20,190-205,247-254` · `chart/alerting/rules.yml:725-737` (`RayMetricsMissing`)

<!-- A second 16-agent workflow (2026-09-18) asked whether rask uses RayJob/KubeRay the way the docs
     prescribe. Its verdict splits: the SUBMISSION mechanism is doc-aligned — the Ray docs present
     RayJob-ephemeral, RayJob-with-clusterSelector and a standing RayCluster fed by the Jobs API as three
     legitimate configurations and pick no winner — so the 2026-09-15 RayJobExecutor deletion was reasoned
     and stands. The CLUSTER is the defect. Adversarial verification refuted 4 of 8 claims before filing,
     most importantly downgrading the two-Ray-address finding from a live outage to a LATENT chart defect:
     the running estate hides the split only because RAY_DASHBOARD_URL was hand-set, which is drift, not
     health. -->

**CP-041 · The compute plane and the cascade address two different Ray clusters**
`compute, medallion, chart` · **HIGH**
- *What is left:* `RAY_DASHBOARD_URL` is derived with an external-wins fallback (chart/templates/configmap.yaml:106-109); `MEDALLION_RAY_ADDRESS` has no such branch and goes straight to `<release>-ray-head-svc:8265` (chart/templates/medallion.yaml:132 and :551), a Service that renders only under `singleTenant.enabled` (false by default). Extract one `rask.rayDashboardUrl` helper and use it in both templates.
- **THE CHART HALF IS DONE AND GATED 2026-09-18.** `configmap.yaml`'s fallback now covers `ray.cluster.enabled` beside `singleTenant` — a RayCluster creates the same `<release>-ray-head-svc`, so an estate rendering one had a real head while this key kept the external default — and `tests/unit/test_one_ray_plane_not_two.py` asserts every `MEDALLION_RAY_ADDRESS` equals the ConfigMap's `RAY_DASHBOARD_URL`, plus that compute renders no env row of its own. That last assertion is the live defect made durable: the running split existed because someone had hand-set an explicit `env` row, which SHADOWS `envFrom` and which `helm upgrade` never removes since the chart never rendered it. Removed on the estate; compute now reaches `rask-ray-head-svc` (200).
- *Closes when:* The prune cron is observed reclaiming job history on the cluster the cascade submits to.
- *Evidence:* Live, KUBECONFIG=/etc/rancher/k3s/k3s.yaml: `kubectl get cm rask-config -o jsonpath='{.data.RAY_DASHBOARD_URL}'` -> `https://dev-kuberay.ra.se`; all three stage runners (rask-bronze-to-silver, rask-media-to-silver, rask-silver-to-gold) carry `MEDALLION_RAY_ADDRESS=http://ray-lance-head:8265`. Consequence: services/compute/src/compute/dependencies.py:37 builds the prune client from `settings.ray_dashboard_url`, so packages/ray-kit/src/ray_kit/prune.py reclaims on dev-kuberay while `ray-lance-head` grows unbounded — the exact growth that measured 81,155 jobs / 164.7 MB and OOM-killed compute (packages/ray-kit/src/ray_kit/dashboard.py:54-56). `RayJobHistoryGrowing` (chart/alerting/rules.yml:710) watches the pruned cluster.

**CP-042 · The Ray head is a hand-applied Deployment, so three chart seams select zero pods**
`chart, observability, medallion` · **HIGH**
- *What is left:* Render a `RayCluster` CR for the batch lane — the second consumer chart/templates/_ray-cluster-config.tpl:3-12 was extracted for and never got. Gate on a new `ray.batchCluster.enabled` defaulting false; enable it on the local profile and retire deploy/ray-lance-demo.yaml as the cascade's Ray.
- **TWO OF THREE OBSERVED 2026-09-18.** `kubectl get raycluster` went from *No resources found* to `rask-ray` STATUS **ready**; `kubectl get pods -l ray.io/is-ray-node=yes` returns the head where it returned nothing; and `ray_node_cpu_utilization` went from **0 rows** (289 `ray_*` tables existed, every one empty — definitions outliving a cluster that was gone) to carrying series. The head also carries the baked jobs and imports `lineage_kit`, and the dummy lane e2e passes 7/7 against it.
- *Closes when:* `RayMetricsMissing` is observed not firing — unobservable on this estate, where `observability.alerting.enabled` is false so vmalert renders at all; its expression `absent(ray_node_cpu_utilization)` is now false, but that is an inference rather than a measurement.
- *Evidence:* Live: all four Ray CRDs installed 2026-07-28, `kubectl get rayservice,raycluster,rayjob -A` -> No resources found, `kuberay-operator` Running 52d, head is Deployment `ray-lance-head` with labels exactly `{"app":"ray-lance-head","pod-template-hash":"85999c7996"}`; `kubectl get pods -A -l ray.io/is-ray-node=yes` -> No resources found. Three seams select that label and match nothing: chart/templates/otel-collector.yaml:195-197 (`__meta_kubernetes_pod_label_ray_io_is_ray_node`, action keep), chart/templates/network-policy.yaml:251, and consequently chart/alerting/rules.yml:725 (`absent(ray_node_cpu_utilization)`) and :740 (`ray_memory_manager_worker_eviction_total`). `grep -rn "rask.rayClusterConfig" chart/` -> one definition, one includer (rayservice.yaml:70). deploy/ray-lance-demo.yaml:7-10 states the misconfiguration against itself.

**CP-043 · Every Ray task runs on a 2-CPU head because there are no worker groups**
`chart, medallion` · **HIGH**
- *What is left:* Add `num-cpus: "0"` to the head's `rayStartParams` and at least one `workerGroupSpecs` entry carrying the GPU limits and accelerator nodeSelector. Ships with the RayCluster above; it is the same change.
- *Closes when:* A stage job's tasks are scheduled on a worker pod and the head runs only GCS, the dashboard and the Serve controller.
- *Evidence:* chart/templates/_ray-cluster-config.tpl:268 is the literal `workerGroupSpecs: []`; :37-39 is the complete rayStartParams block (`dashboard-host`, `num-gpus`, and a conditional tracing hook at :51) with no `num-cpus`; `grep -rn "num-cpus" chart/` -> zero hits; `grep -rn "enableInTreeAutoscaling" chart/` -> zero hits. Live head args: `[start --head --dashboard-host=0.0.0.0 --port=6379 --num-cpus=2 --disable-usage-stats --block]` with `limits: {cpu: 2, memory: 3Gi}`. The cost is already being paid: chart/alerting/rules.yml:740-750 exists for kernel OOM-kills with no traceback, and services/medallion/src/medallion/services/rayjobs_api_executor.py:33-35 classifies exit 137 as "the host-RAM OOM that kills a stage with no other symptom".

**CP-044 · The Ray lane is the last bypass of the Executor port, so the capability seam is unreachable**
`medallion, service-kit` · **MED**
- **blocked:** Nothing. The wire objection that blocked it is measured-false as of 2026-09-17.
- *What is left:* Give the chart's undeclared lanes a real `TaskRegistration` at producer boot instead of the submitter-side fallback, then replace workflow.py's five direct imports with `executor_for(RAY_ENGINE, ...)` and gate the resubmit branch on `may_resubmit(state, capabilities=...)`.
- *Closes when:* `grep -rn "ray_submit\|ray_jobs_api" services/medallion/src/medallion/workflow.py` returns nothing, the cascade runs end to end, and no behaviour changed (the Jobs API adapter still withholds DURABLE_RECORD).
- *Evidence:* `grep -rn "executor_for(" services/ packages/ | grep -v /tests/` -> 2 hits: the definition at services/medallion/src/medallion/services/engine_registry.py:54 and one caller, services/medallion/src/medallion/services/transform.py:793, resolving `IN_PROCESS_ENGINE`. The Ray lane imports directly at workflow.py:488, :528-529, :708-709, :979-980. Two derivations of one id result: rayjobs_api_executor.py:105 posts `order.idempotency_key`, ray_submit.py:181,302 posts `stage_submission_id(...)`. The stated blocker is already narrowed to one item at docs/DECISIONS.md:1926-1942, and the older "0 of 6 overlap" objection is corrected there — scripts/ray_stage_job.py:449 now reads `RASK_SOURCE_URI`/`RASK_DEST_URI`/`RASK_STAGE`, matching `WorkOrder.to_env()` (packages/service-kit/src/service_kit/lakehouse/work_order.py:126).

**CP-045 · A lost watcher over a succeeded job strands live data with a human as the only owner**
`medallion` · **MED**
- **blocked:** The Executor-port row (S2) and a KubeRay-managed cluster to submit CRs against (S3), or confirmation that dev-kuberay.ra.se is operator-managed.
- *What is left:* Land the `RayJobCrExecutor` behind `medallion.rayExecutor`, advertising `DURABLE_RECORD` — which makes `may_resubmit` return False and retires the `vanished`/`never_registered` ambiguity without deleting a line of workflow code. Requires the port row above first.
- *Closes when:* With the flag on, killing a stage-runner pod mid-job lets another replica resume and report the real outcome from `status.jobDeploymentStatus`, instead of burning MAX_UNSEEN_POLLS and resubmitting work that may have landed.
- *Evidence:* The cascade only advances through `publish_stage_ready` (services/medallion/src/medallion/workflow.py:387-400), so an `abandoned` job that later succeeds writes its data and rings nothing (:207-213, :355-372), and `unnotified` means the data landed and the wake-up failed (:209-211). Repair is the operator-driven `POST /api/.../rerun` (services/medallion/src/medallion/api/rerun.py:18-23). Root cause at workflow.py:315-326 and packages/service-kit/src/service_kit/lakehouse/executor.py:14-19: Ray's GCS is not fault-tolerant here, so a head restart takes every job record with it. The seam already exists — `Capability.DURABLE_RECORD` at executor.py:53 and `may_resubmit` at :104-106.

**CP-046 · Terminating a stage leaves the Ray job running although the adapter can cancel it**
`medallion` · **LOW**
- *What is left:* Have `terminate_stage` resolve the executor and call `cancel(handle)` when `Capability.CANCEL` is declared, then correct the response body.
- *Closes when:* `POST /stages/{instance_id}/terminate` stops the watch AND the Ray job, and the response says so truthfully.
- *Evidence:* `grep -rn "\.cancel(" services/medallion/` -> one hit, services/medallion/tests/test_the_second_executor_makes_the_port_a_contract.py:122. The capability is advertised at services/medallion/src/medallion/services/rayjobs_api_executor.py:76 and implemented as `DELETE /api/jobs/{id}` at :142-144. services/medallion/src/medallion/api/stage_ops.py:110 tells the operator "the Ray job it was polling keeps running and must be stopped through Ray" — honest, and the reason this is LOW rather than higher, but an operator told 'terminated' reasonably frees the GPUs in their head and they are not free.

**CP-047 · The GPU quota described as the estate's concurrency lever governs nothing**
`chart` · **LOW**
- *What is left:* Either wire it (only meaningful with a CR submitter: `spec.suspend: true` plus the queue label) or rewrite the prose so the quota stops reading as an active lever. Do not adopt Kueue as its own piece of work.
- *Closes when:* Either a submitted workload carries `kueue.x-k8s.io/queue-name` and is admitted by unsuspension, or chart/values.yaml's description of the nominalQuota matches what it actually controls.
- *Evidence:* `grep -rn "kueue.x-k8s.io/queue-name" chart/ services/ packages/` -> one hit, a comment at chart/values.yaml:2620. `grep -rn "kueueQueue" chart/ services/ packages/` -> one hit, its own declaration at chart/values.yaml:1322 — zero readers. The queues render via a post-install hook (chart/templates/kueue-queues.yaml:72-90) with `nvidia.com/gpu` nominalQuota at chart/values.yaml:2613-2637. chart/values.yaml:1318-1320 concedes the inertness. `grep -n -i ray chart/templates/kueue-queues.yaml` finds only a comment.

**CP-048 · Falsified prose points readers at a finished port and a deleted module**
`medallion, docs` · **LOW**
- *What is left:* Rewrite four sites in whatever commit next touches the port. Per CLAUDE.md the old claim goes, not gets annotated.
- *Closes when:* No file claims both lanes go through the port while `executor_for` has one caller, and no file references a module that does not exist.
- *Evidence:* docs/DECISIONS.md:1892 is titled "`RESULT` is a capability, and both lanes now go through the port" while `grep -rn "executor_for(" ... | grep -v /tests/` returns one caller resolving `IN_PROCESS_ENGINE` (transform.py:793) — the entry's own body concedes this at :1934-1942, but the heading is what gets read. docs/DECISIONS.md:1467 still lists `rayjob_executor` as a live adapter. services/medallion/src/medallion/services/dapr_saga.py:4 calls itself "the workflow-plane twin of `inprocess_executor` / `rayjob_executor`"; `ls services/medallion/src/medallion/services/rayjob_executor.py` fails (deleted in b3a10799). services/medallion/pyproject.toml carries a comment about "The generic Ray submitter (R2). It moved OUT of this service" dangling above `fastapi`, describing a dependency the file does not declare — the medallion depends on no `ray-kit` and carries its own `ray_jobs_api.py`.

**CP-049 · GCS job-record loss on head restart is an accepted cost that no decision record states**
`docs, medallion` · **LOW**
- *What is left:* Record the ruling so the backlog row that keeps re-asking can close: job-record loss on head restart is accepted, the compensation is the resubmit budget, and a durable record comes from a CR rather than from Redis.
- *Closes when:* docs/DECISIONS.md carries the ruling and the open backlog row citing it is closed.
- *Evidence:* `grep -rn "gcsFaultTolerance|GcsFaultToleranceOptions|redisAddress|RAY_external_storage_namespace"` over the tree -> zero hits each. docs/DECISIONS.md:1482 uses the non-durability as a premise ("Ray's GCS is not fault-tolerant here, so the Jobs-API watcher carries MAX_UNSEEN_POLLS/MAX_RESUBMITS") without ever ruling on it; open_backlog_left_new.md:1056-1058 records the ruling as still outstanding. The Ray docs are unambiguous that GCS FT is not the remedy — recommended for Ray Serve, and for other workloads "isn't recommended and the compatibility isn't guaranteed" — and no-Redis is a standing estate rule (chart/templates/dapr-statestore.yaml:21-26). So the honest close is a written ruling, not an adoption.

**CP-050 · The Ray cluster image is outside the estate's image mechanism: it bypasses `rask.image`, so it resolves to Docker Hub locally and no per-component pin reaches it**
`chart` · **MED**
- *What is left:* `chart/templates/_ray-cluster-config.tpl:64` renders `image: "{{ .Values.ray.image.repository }}:{{ .Values.ray.image.tag }}"` — bare, while every other workload in the chart goes through `include "rask.image" (list . "<name>")`. Two consequences, both measured 2026-09-18. (1) With `image.localImages=true` a default render gives `ray-cluster:dev`, which resolves to Docker Hub and ImagePullBackOffs on any side-loaded estate; the live head only works because `deploy/ray-lance-demo.yaml` hard-codes `localhost:5000/ray-lance:<tag>` by hand. (2) The per-component pin mechanism cannot reach it: `chart/values-live-pins.yaml:22` carries `ray-lance: "main-2c6363d0-lin001"` under `image.perComponent`, read by `rask.image` — which the Ray templates never call — so that pin is inert and `scripts/k3s-pins.sh`'s stem-convergence guarantee excludes the Ray plane entirely. Route the Ray image through `rask.image` and retire `ray.image.repository`/`ray.image.tag`, or state in values why Ray is deliberately exempt from both.
- *Closes when:* A default local render gives a pullable Ray image and a `perComponent` pin for it changes what the Ray head runs.
- *Evidence:* `chart/templates/_ray-cluster-config.tpl:64 (bare repository:tag)` · `grep -rn 'include "rask.image"' chart/templates/ → every other workload` · `chart/values-live-pins.yaml:22 (ray-lance pin, unread by the Ray templates)` · `live head image localhost:5000/ray-lance:main-2c6363d0-lin001 vs a default render's ray-cluster:dev`

**CP-005 · `ensure_dataset` runs before enumeration, so a source that enumerates zero units leaves a registered empty bronze table behind a COMPLETE run**
`ingest, medallion, catalog` · **HIGH**
- *What is left:* `ensure_dataset` is the first activity (workflow.py:520), `enumerate_chunks` follows (:523), and the `units_total == 0` short-circuit (:629) returns COMPLETE without touching the table it registered. Do NOT reorder: `enumerate_chunks`' incremental anti-join reads existing ids at the location `ensure_dataset` returned and refuses an absent table as a read failure (workflow.py:1203-1215, F12c), so the table must exist before enumeration. Make `ensure_dataset` report created-vs-found and, on `units_total == 0`, roll back a table THIS run created while leaving a pre-existing one untouched (a quiet source against an existing table is the ruled-legitimate case). Pin with a test that points a source at an empty prefix and asserts no table remains registered.
- *Closes when:* A run whose source enumerates zero units leaves no newly-registered bronze table, proven by a test that asserts the catalog holds no table afterward.
- *Evidence:* `services/ingest/src/ingest/workflow.py:520 (ensure_dataset first) and :523 (enumerate_chunks after)` · `services/ingest/src/ingest/workflow.py:629 (units_total==0 returns COMPLETE, no rollback)` · `services/ingest/src/ingest/workflow.py:1203-1215 (anti-join requires the table to exist)` · `services/ingest/tests/test_empty_source.py:1-10 (empty = COMPLETE ruling)`

**CP-007 · Ingest's reads of the estate-default store and of secretless registered stores run on ambient credentials, not a scoped identity**
`ingest, catalog, chart` · **HIGH** · PARTIAL
- **blocked:** Which source buckets ingest reads, and which scoped identity (a name in the Dapr secret store) each registered store declares — an operator decision per bucket.
- *What is left:* Path 1 is shipped: `POST /v1/outbox/credentials` vends a write-tier STS credential scoped to `settings.lineage_outbox_uri`, gated on `can_stage_events` (`event_stager` rung, granted to ingest by bootstrap-admin.yaml), and `_outbox_storage_options` vends through it, degrading to endpoint-only on refusal. Not verified live this session: staging from ingest and watching the reconcile cron drain the object. Paths 2 and 3 remain: `objectstore._s3_prefix`'s `is_estate_default` branch returns a `pafs.S3FileSystem` on pyarrow's ambient AWS_* chain, and a registered store declaring no `secret` falls to `without_credentials`. Register each source bucket as a store that declares a `secret` naming a scoped identity so `_own_store_for` stops falling back; the machinery exists and is inert until aimed.
- *Closes when:* Every bucket ingest reads is a registered store with a declared scoped secret, and no code path in objectstore.py reaches `without_credentials` or the ambient chain.
- *Evidence:* `services/catalog/src/catalog/api/v1/endpoints/outbox_credentials.py:51-55 (the door)` · `services/ingest/src/ingest/lineage.py:252-293 (_outbox_storage_options vends)` · `packages/service-kit/src/service_kit/governed/auth/model.fga:255 (can_stage_events)` · `services/ingest/src/ingest/objectstore.py:239-252 (estate-default + secretless fallbacks)`

**CP-010 · dev-kuberay.ra.se's job-submission API answers 200 with no token and with a wrong token**
`external-kuberay, compute` · **HIGH**
- **blocked:** The operators of the KubeRay cluster at dev-kuberay.ra.se must enable token verification (or front it with ingress auth / a network policy); plus their answer on whether the host is reachable from outside the network and whether anything already fronts it, which decides urgency.
- *What is left:* rask's half is in place: ray-kit sends `Authorization: Bearer <token>` and the chart wires KubeRay-native authOptions; nothing in this repo can make the external cluster check it. Get the cluster's operators to enable token verification, then re-run the three probes from inside rask's cluster: no-token and wrong-token must 401 on `/api/version`, `/api/jobs/` and `/api/cluster_status`. Note `chart/values.yaml` defaults `ray.auth.enabled: false` with `dashboardUrl: https://dev-kuberay.ra.se`; the live release's value and the cluster's behaviour are not re-measured this session (no cluster access).
- *Closes when:* No-token and wrong-token requests to `/api/version`, `/api/jobs/` and `/api/cluster_status` on dev-kuberay.ra.se all answer 401.
- *Evidence:* `packages/ray-kit/src/ray_kit/auth.py:41-45 (Bearer header built from the token env)` · `chart/values.yaml:2190 (dashboardUrl dev-kuberay.ra.se), :2202-2203 (ray.auth.enabled default false)` · `services/compute/src/compute/lifespan.py:29-31 (auth_headers as client default)`

**CP-011 · `runners/htr` is not re-cut as a stage runner: the prefetch pipeline and loader/writer endcaps remain, `main.py` has no stage entrypoint, and no per-job image seam exists**
`runners/htr, medallion, compute, chart` · **HIGH**
- **blocked:** Which image seam carries the htr lane: per-lane `runtime_env.image_uri` on the Jobs-API submit path plus the task/spec record, or a baked platform-side job calling a `/htrflow` Serve door (deployed nowhere today). Baking htr into the head image is forbidden by the 2026-08-25 seal ruling.
- *What is left:* Merge with LH-010 (same P7b re-cut; it carries the better anchor). Decide the image seam: `ray_submit.py` posts `runtime_env: {env_vars}` only, `grep -rn image_uri services/ packages/ scripts/` is empty, and `MEDALLION_RAY_ENTRYPOINT` (`core/config.py:312`) can only name a script baked into the runner-free head image. Give the htr runner an env-parameterised stage entrypoint shaped like `runners/dummy/src/dummy_runner/job.py` (`RASK_SOURCE_URI`/`RASK_DEST_URI`/`RASK_VERSION_FLOOR`, not a typer subcommand), declare the lane as a `TransformSpec` + `TaskDeclaration`, and retire `prefetch_pipeline` (`pipeline.py:143`, entry `:230`) with the `PageLoaderActor`/`AltoWriterActor` endcaps (`pipeline.py:10`) — deliberately taking `htr/iiif.py::IIIFCachedSource` with them, whose only consumer is prefetch. Rewrite `chart/values.yaml:1485-1491`, which still prescribes baking the entrypoint into the ray-cluster image. Prove the HTR lane's own bronze→silver→gold green with lineage on the head; the generic cascade is already green without htr and does not discriminate.
- *Closes when:* An htr stage job submitted through the medallion cascade succeeds on the head with lineage populated, and `runners/htr` carries no prefetch pipeline or ALTO endcaps.
- *Evidence:* `runners/htr/src/runner/pipeline.py:10,143,230` · `runners/htr/src/runner/main.py:43 (single @app.command, no subcommands)` · `services/medallion/src/medallion/core/config.py:312` · `chart/values.yaml:1485-1491`

**CP-012 · The Ray lane still bypasses the executor port, the live Ray head is a hand-applied manifest outside the chart, and Kueue admits nothing**
`medallion, compute, chart` · **HIGH** · PARTIAL
- **blocked:** Owner: bring the standing Ray head under the chart (a RayCluster/RayService rendered by the release) or keep it hand-applied — and, now that the RayJob CR adapter is deleted, whether Kueue stays installed with nothing to admit.
- *What is left:* The CR adapter branch is settled: `rayjob_executor.py` and `chart/templates/medallion-rayjob-rbac.yaml` no longer exist and `RayJobsApiExecutor` is what `engine_registry.executor_for(RAY_ENGINE)` returns. Route the Ray lane through that port instead of `workflow.py:487`'s direct `submit_stage_job` import; the one gap is `ray_submit.py:167`'s fallback to `settings.ray_entrypoint` when no task is declared, which the adapter has no equivalent for (overlaps LH-159). After the ruling, render the head from the chart (`rayservice.yaml` is gated on `singleTenant.enabled`, default false) and delete `deploy/ray-lance-demo.yaml` plus its apply at `scripts/ray_e2e_stack.sh:121`. Give job records a home that survives a head restart (Ray history server) or record that `DURABLE_RECORD` is declined on purpose, and either set gang/priority policy on the `rask` ClusterQueue behind a CR path or drop `kueue.enabled`. Live CR count and head annotations were not re-verified this session (no kubectl).
- *Closes when:* `workflow.py` reaches Ray only through `executor_for`, the release renders the Ray head and `deploy/ray-lance-demo.yaml` is gone, and Kueue is either admitting Ray work or removed.
- *Evidence:* `services/medallion/src/medallion/workflow.py:487` · `services/medallion/src/medallion/services/ray_submit.py:167` · `docs/DECISIONS.md:1850 (adapter deleted 2026-09-15; executor_for had zero production callers on the Ray lane)` · `scripts/ray_e2e_stack.sh:121 + deploy/ray-lance-demo.yaml (9,845 bytes at HEAD)`

**CP-025 · Dapr Workflow definitions are registered by bare `__name__` with no versioning seam, so every deploy replays in-flight instances against new code**
`medallion, ingest, flows` · **HIGH**
- **blocked:** Owner sequencing of the Dapr retreat (XC-023 / D5): build a workflow version-pinning seam now, or accept the replay-on-deploy cost until the BYO-engine cutover
- *What is left:* Medallion (`workflow.py:829-848`), ingest (`ingest/__init__.py:165,266`), promotions (`api/promotions.py:185`) and flows all host Dapr workflows registered by function name, and `workflow.py`'s own docstring records 'the estate has no versioning seam'. `ingest/replay_guard.py` and `test_replay_hygiene.py` only ban env reads inside workflow bodies; `service_kit/draining.py` only refuses new admissions on a draining pod. Neither pins a workflow definition to a version or drains in-flight instances before a rollout. Add versioned workflow names plus a deploy-time drain gate, or take the retreat.
- *Closes when:* A rollout with an in-flight medallion or ingest instance either replays against the pinned old definition or is held until that instance completes, pinned by a test.
- *Evidence:* `services/medallion/src/medallion/workflow.py:829-848` · `services/ingest/src/ingest/__init__.py:165,266` · `packages/service-kit/src/service_kit/draining.py:1-40 (admission gate only)` · `grep -ri 'workflow_version|drain gate' services/ packages/ chart/: no versioning seam`

**CP-001 · The Ray stage/train job signs S3 writes with the pod's static `rask-ray-compute` key, whose policy is `arn:aws:s3:::*` wide and enumerates every bucket**
`medallion, catalog, chart` · **MED** · PARTIAL
- *What is left:* The `runtime_env` half is shipped: no key, secret or token rides the job body (ray_submit.py:206-220), the job reads `S3_KEY`/`S3_SECRET` from the pod's own Secret (ray_stage_job.py:86-91). What remains is the per-table vend on the Ray lane: have the job obtain a table-scoped STS triple itself from the catalog's credentials door (services/catalog/src/catalog/api/v1/endpoints/credentials.py) with a pod-held service token, and feed it to `lance_storage_options`, which already accepts `session_token`. Never carry the triple in `runtime_env`. Then narrow the `ray-compute` policy in chart/templates/minio-scoped-users.yaml:271-289 away from `arn:aws:s3:::*`, keeping the runtime-minted-warehouse case green (`tests/unit/test_scoped_policies_reach_runtime_minted_warehouses.py`). The 104/105-bucket enumeration count is from the row, not re-measured this session.
- *Closes when:* A cascade tier is written under a credential scoped to that table's bucket+prefix and the pod's static key can no longer list the estate's buckets.
- *Evidence:* `services/medallion/src/medallion/services/ray_submit.py:206-220` · `scripts/ray_stage_job.py:81-91` · `chart/templates/minio-scoped-users.yaml:271-289` · `chart/templates/minio-scoped-users.yaml:184-195`

**CP-006 · `StagingOverlapError`'s docstring still claims the two-batch redelivery merge is reachable, though the worker now stages redeliveries singly**
`ingest` · **MED** · PARTIAL
- *What is left:* The batching fix is shipped by a stricter rule than the row asks for: `worker.py` stages ONE fragment per redelivered unit (`worker.py:540-562`), so the staged family is laminar and the two-batch fragment cannot form. Rewrite `StagingOverlapError`'s docstring at `staging.py:195-217`, which still asserts "This is REACHABLE" and "Both are open". Keep the class rather than deleting it: the `verdict.chosen is None` branch at `staging.py:322` still needs a loud refusal for a non-laminar family produced by hand-written or foreign manifests, and two test files construct exactly that.
- *Closes when:* `staging.py`'s `StagingOverlapError` docstring describes it as the guard for a manifest family the worker cannot produce, with no claim that `drain_chunk` batches redeliveries together.
- *Evidence:* `services/ingest/src/ingest/worker.py:540-562 (ONE FRAGMENT PER REDELIVERED UNIT, commit 83912acd)` · `services/ingest/src/ingest/staging.py:195-217 (docstring still says REACHABLE / Both are open)` · `services/ingest/src/ingest/staging.py:322 (raise site on chosen is None)` · `services/ingest/tests/test_partial_ack_duplication.py:140 (hand-built overlap still raises)`

**CP-014 · `submit_or_reattach` answers REATTACHED on any 4xx after reading only the existing job's status, never its identity**
`medallion` · **MED** · PARTIAL
- *What is left:* The code_version half is shipped: `ray_jobs_api.submission_id` folds `code` into the id, `ray_submit.py:181` passes it, and the chart sets `MEDALLION_RAY_CODE_VERSION` from the image. What remains is the re-attach check itself: `submit_or_reattach` treats ANY >=400 on `POST /api/jobs/` as a possible duplicate, GETs the job and inspects only `status`. Narrow the duplicate branch to the actual duplicate-id status, and before answering `reattached` compare the existing job's entrypoint and `runtime_env.env_vars` to the body, raising `RayJobError` (or resubmitting) on mismatch. Pin it with a test in `services/medallion/tests` that presents a same-id job with a different entrypoint.
- *Closes when:* A same-id job with a different entrypoint or runtime_env is never reported as `reattached`, and a non-duplicate 4xx is never read as a collision, both pinned by a test.
- *Evidence:* `services/medallion/src/medallion/services/ray_jobs_api.py:153-200 (POST, then GET, then `status` only)` · `services/medallion/src/medallion/services/ray_jobs_api.py:44-75 (`code` hashed into the id)` · `services/medallion/src/medallion/services/ray_submit.py:169,181,204` · `chart/templates/medallion.yaml:582 (`MEDALLION_RAY_CODE_VERSION` = catalog image)`

**CP-015 · `POST /train` derives a feature's URI from the tier segment alone and discards the `$name` half, so training reads the tier dataset rather than the named table**
`medallion, catalog` · **MED**
- *What is left:* Resolve each `features[].dataset` (`stage$name`) against the catalog inside the `/train` door instead of `stage_uri_for`, which builds `<stage_base>/<stage>` from the segment before `$` and never reads the name. Hand the job the resolved table URI (the `uri` field at train.py:278) and pin the version off that table. Refuse an unresolvable reference with a 4xx problem+json (the door already maps `resolve_failed` to 422 at api/train.py:120-121); do not forward it.
- *Closes when:* A `/train` naming `silver$features` submits a job whose `FEATURES[].uri` is the catalog table's location, and a name the catalog does not hold is refused 4xx.
- *Evidence:* `services/medallion/src/medallion/services/train.py:87-96 (`stage_uri_for` splits on `$` and uses only the stage segment)` · `services/medallion/src/medallion/services/train.py:118-123 (`_resolve_version` opens that tier URI)` · `services/medallion/src/medallion/services/train.py:278 (`uri: stage_uri_for(...)` forwarded to the Ray job)` · `services/medallion/src/medallion/api/train.py:120-121 (`resolve_failed` → 422)`

**CP-016 · No test proves a Ray dashboard endpoint rejects a missing or wrong token; every Ray-auth test asserts chart render or a mocked transport**
`compute, chart, ray-kit` · **MED**
- *What is left:* Add a test that calls a Ray dashboard endpoint (`/api/version`, `/api/jobs/`) with no token and with a wrong token and asserts 401. Run it against a Ray brought up with `dagger core container … as-service up`, never docker. `tests/unit/test_ray_auth.py` holds 8 render-time tests only and `packages/ray-kit/tests/test_auth.py` asserts on `httpx.MockTransport`; `tests/e2e-py/test_ray_*_e2e.py` never issue a tokenless call to Ray.
- *Closes when:* A suite test observes 401 from a live Ray dashboard for a missing and for a wrong token.
- *Evidence:* `tests/unit/test_ray_auth.py:78-191 (8 tests, all helm-render assertions)` · `packages/ray-kit/tests/test_auth.py:1-12,94 (mocked transport; docstring defers the live proof to 'cluster gates')` · `grep -n '401\|token' tests/e2e-py/test_ray_batch_e2e.py tests/e2e-py/test_ray_train_e2e.py — no tokenless Ray assertion`

**CP-018 · Ray core logs (driver/task/actor) stay as files under /tmp/ray in the head container; nothing mounts or tails them**
`chart, deploy (ray head)` · **MED**
- *What is left:* Add a `ray-logs` emptyDir (sizeLimit 2Gi) at `/tmp/ray` on the ray-head container and a `ray-log-agent` otel-collector-contrib sidecar mounting it read-only, with a filelog receiver over `/tmp/ray/session_latest/logs/**/*.{log,out,err}` (`include_file_path: true`, `start_at: end`, json_parser; `RAY_LOGGING_CONFIG_ENCODING=JSON` is already set) exporting otlphttp to the same GreptimeDB as the metrics. Poll frequently at first; the directory does not exist until Ray creates it. Land it on both in-repo head definitions, `deploy/ray-lance-demo.yaml` (the hand-applied head, whose only volume is `dshm`) and `chart/templates/rayservice.yaml`; the remote dev-kuberay.ra.se cluster is outside this repo and needs its operators. Consider `RAY_DEDUP_LOGS=0` (already set on the demo head).
- *Closes when:* A `job-driver-*.log` line from a medallion stage job is queryable in GreptimeDB.
- *Evidence:* `chart/templates/_ray-cluster-config.tpl:95-98 — comment records /tmp/ray is unmounted and untailed` · `deploy/ray-lance-demo.yaml:138-143 — the head's only volume is dshm` · `grep -rn 'ray-logs|session_latest' chart/ deploy/ — no sidecar or mount` · `chart/templates/otel-collector.yaml:231-232`

**CP-027 · No test asserts `medallion.stage.outcome` carries `verdict=failed` on the dying path**
`medallion` · **MED** · PARTIAL
- *What is left:* The metric is shipped: `medallion.stage.outcome` counts the application's own terminal verdict (succeeded|failed|abandoned|unnotified) at `workflow.py:652`, its docstring records why Dapr's `status=success` label is false, and `MedallionStageOutcomesFailing` alerts on it. Add one test that drives the failed verdict through `report_stage_outcome` and asserts the counter records `lance.medallion.verdict=failed`; the existing failed-path tests assert the FAIL lineage event and one monkeypatches `record_stage_outcome` to a no-op.
- *Closes when:* A medallion test fails if `record_stage_outcome` stops receiving `failed` when a stage job dies.
- *Evidence:* `services/medallion/src/medallion/core/metrics.py:133-175 (counter + record_stage_outcome)` · `services/medallion/src/medallion/workflow.py:652 (record_stage_outcome(outcome.verdict))` · `services/medallion/tests/test_stage_workflow.py:293-332 (failed verdict driven, FAIL event asserted)` · `services/medallion/tests/test_activity_bodies_are_reexecution_safe.py:133 (record_stage_outcome monkeypatched away)`

**CP-029 · `compute` is an introspection shell — no submit door with vended credentials, no idempotent outcome door, no plan document on a control lane**
`compute, medallion` · **MED**
- *What is left:* Build the two BYO-engine artefacts from `docs/audits/lakehouse-2026-09/lakehouse-analysis.md` §11 D on compute's management API: a submit door that vends credentials via the catalog's existing `POST /v1/table/{id}/credentials`, and an idempotent outcome door backed by `ray_kit.submit_or_reattach`, with the plan document published on a control lane. Today compute's router is GET-only (`/ray/health|jobs|jobs/{id}/logs|cluster|actors|tasks|overview|logs`) plus the `/api/serve/*` proxy, `submit_or_reattach` is called only in-process by the medallion, and no `/plans` or `/outcome` route exists on any service.
- *Closes when:* Compute exposes a credential-vending submit door and an idempotent outcome door keyed on the action id, and the plan document is published on a control lane.
- *Evidence:* `services/compute/src/compute/routes.py:26-71 (GET-only router under /ray)` · `services/medallion/src/medallion/services/ray_submit.py:322,429 and rayjobs_api_executor.py:109 (only callers of submit_or_reattach)` · `services/catalog/src/catalog/api/v1/endpoints/credentials.py:44-47 (POST /v1/table/{id}/credentials)` · `docs/audits/lakehouse-2026-09/lakehouse-analysis.md:222 (§11 D)`

**CP-017 · Nothing scrapes the external Ray cluster — zero `ray_*` / `ray_serve_*` / `ray_data_*` / `autoscaler_*` series reach GreptimeDB**
`external-kuberay` · **MED**
- **blocked:** The operators of the external KubeRay cluster at dev-kuberay.ra.se applying the scrape on their collector — nothing in the rask chart can do it (chart/templates/otel-collector.yaml:20 renders nothing when observability.otelCollector.externalEndpoint is set)
- *What is left:* On the collector beside the external cluster, add a `job_name: ray-pods` scrape block mirroring chart/templates/otel-collector.yaml:190-254 — keep on `__meta_kubernetes_pod_label_ray_io_is_ray_node="yes"` and container port name `metrics`; relabel `ray_io_cluster`, `ray_node_type`, `namespace`, `pod`. Carry the `metric_relabel_configs` drop (lines 247-254) that keeps only `ray_data_num_tasks_submitted` and `ray_data_task_submission_backpressure_time` — an unfiltered scrape adds 113 `ray_data_*` families and OOMKills the store. Confirm the head and every worker group declares `containerPort: 8080, name: metrics`, and export to `http://<greptimedb-host>:4000/v1/otlp` with `x-greptime-db-name` only. Ray metrics are per-node pull endpoints, so this cannot be pushed from rask's side.
- *Closes when:* `ray_node_cpu_utilization` returns a series on that GreptimeDB's `:4000/v1/prometheus`.
- *Evidence:* `chart/templates/otel-collector.yaml:20 — `{{- if and $o.enabled $c.enabled (not $c.externalEndpoint) }}` wraps the whole in-cluster Collector` · `chart/templates/otel-collector.yaml:190 `job_name: ray-pods`; :196 keep on `ray_io_is_ray_node`; :247-254 `ray_data_*` drop` · `chart/values.yaml:2878 `externalEndpoint: ""` (prod posture sets it and deploys nothing)`

**CP-019 · No Serve proxy/router/replica span has ever been observed, and the external KubeRay cluster does not set the Serve tracing switch**
`external-kuberay, compute, service-kit, chart` · **MED** · PARTIAL
- **blocked:** External operators of the KubeRay cluster at dev-kuberay.ra.se must set the Serve tracing env on an image that carries `service_kit`, and a Serve application must actually come up to receive a traced request.
- *What is left:* rask's own chart already wires the whole switch: `tracing-startup-hook: service_kit.ray_tracing:setup_tracing` on the head (`_ray-cluster-config.tpl:51`) and `RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH` + `RAY_SERVE_TRACING_SAMPLING_RATIO` on the head container (`:82-86`), gated on `lance.otelEnabled`; `workerGroupSpecs` is empty (`:268`), so add the two Serve env lines to any future worker group. On the external cluster set the same three (hook on the HEAD only; on a worker group it is a silent no-op), verify `python -c "import service_kit.ray_tracing"` in its image, then with a live Serve application send one request through the gateway and find ONE trace_id in `opentelemetry_traces` carrying both a gateway span and a Serve proxy/replica span. `ray_tracing.py:89` is a silent no-op without `OTEL_EXPORTER_OTLP_ENDPOINT` and both planes fail soft, so a healthy pod proves nothing — only the observed span does.
- *Closes when:* One trace_id in `opentelemetry_traces` carries both a gateway span and a Serve proxy/replica span.
- *Evidence:* `chart/templates/_ray-cluster-config.tpl:51,82-86,268` · `packages/service-kit/src/service_kit/ray_tracing.py:69,89,100`

**CP-020 · The chart's Ray telemetry env renders only under `singleTenant.enabled`, which no values file turns on**
`chart` · **MED**
- **blocked:** Owner decision: flip the gate so `rayservice.yaml` renders, bring the externally-managed `rask-ray` RayService under the Helm release, or delete the `RAY_SERVE_TRACING_*` / `RAY_SERVE_LOG_ENCODING` wiring from the chart.
- *What is left:* `rayservice.yaml:1` is gated on `and .Values.ray.enabled .Values.singleTenant.enabled`; `singleTenant.enabled` is false in `values.yaml` and `values-prod.yaml` names it only in a comment. The tracing and log-encoding env now lives in `_ray-cluster-config.tpl:82-109`, and that template is included only from the gated `rayservice.yaml:70`, so no install renders it. After the ruling, make the matching single edit and record the choice in `docs/DECISIONS.md` (no entry exists).
- *Closes when:* A default render either produces a RayService carrying the telemetry env, or the chart no longer carries the env at all, per the recorded ruling.
- *Evidence:* `chart/templates/rayservice.yaml:1,69-70` · `chart/templates/_ray-cluster-config.tpl:82,86,109` · `chart/values.yaml:65-66` · `chart/values-prod.yaml:153`

**CP-021 · Ray GCS is not fault-tolerant: a head restart kills in-flight jobs, and the only supported fix needs an external Redis the estate forbids**
`compute, chart` · **MED**
- **blocked:** Owner ruling: accept job loss on Ray head restart (recorded in docs/DECISIONS.md), or grant a scoped exception to the no-Redis rule for the Ray GCS store.
- *What is left:* Obtain the ruling. If job loss is accepted, record it in docs/DECISIONS.md and close. If an exception is granted, add GCS fault-tolerance options plus a Redis for the GCS store to the chart behind a values toggle. No gcsFaultTolerance wiring exists in chart/ today; chart/templates/dapr-statestore.yaml:19 records owner approval of Redis for CACHE only, which is a precedent the ruling can cite, not the ruling itself.
- *Closes when:* docs/DECISIONS.md carries the ruling, and if it is the exception, the chart renders a GCS store behind a toggle.
- *Evidence:* `grep -rn -i gcsFaultTolerance chart/ -> no matches` · `chart/values.yaml:2253 (only redisPort: 6379, no Redis object)` · `docs/DECISIONS.md:1482 states GCS is not fault-tolerant as a premise, not a ruling` · `chart/templates/dapr-statestore.yaml:19 (Redis approved for cache, not GCS)`

**CP-030 · The `Transform` CRD is deferred to `rask-operator`, so a lane declaration cannot live in git as a CR with the catalog record as a projection**
`chart, catalog, medallion` · **MED**
- **blocked:** `rask-operator` (the separate controller repo) existing — the CRD ships only together with its controller, never in this chart alone.
- *What is left:* Ship the `Transform` CRD with its controller in `rask-operator`. Nothing lands in `chart/` for this: no `kind: Transform` exists in `chart/templates` and none should until the controller does, because unreconciled CRs render as objects stuck mid-provision. A lane declaration stays a catalog record until then.
- *Closes when:* `rask-operator` reconciles a `Transform` CR into the catalog's declaration record.
- *Evidence:* `docs/DECISIONS.md:1492-1495 (step 5 deferred to rask-operator, precedent from the Project CRD)` · `grep -rn 'kind: Transform' chart/templates → 0 hits` · `no rask-operator directory in the repo root`

**CP-031 · A stage runner row carries stageJob / ray_entrypoint / ray_job_params beside the declared TransformSpec that supersedes them, with engine_choice arbitrating at runtime**
`medallion, catalog, chart` · **MED**
- **blocked:** CP-030: the Transform CRD shipping with rask-operator (a separate repo), which gives lane declarations a git-backed seeding path.
- *What is left:* Once lane declarations can be seeded from a CR, delete stageJob from the stage-runner row (chart/templates/medallion.yaml:559-562, chart/values.yaml:1463), the ray_entrypoint and ray_job_params settings (services/medallion/src/medallion/core/config.py:312,349), and the `spec else settings.*` fallback in ray_submit.py:167-168 and transform.py:753. Collapse the declared-vs-env arbitration in transform.py:836-838 so a lane runs only what its declaration names. Do not remove any of it before a seeding path exists, or the default deploy runs no cascade.
- *Closes when:* A stage runner row and MedallionSettings carry no Ray entrypoint or params of their own, and every lane runs from its declared record.
- *Evidence:* `chart/templates/medallion.yaml:559-562 (stageJob -> MEDALLION_RAY_ENTRYPOINT)` · `services/medallion/src/medallion/core/config.py:312,349,362-371` · `services/medallion/src/medallion/services/ray_submit.py:167-168` · `services/medallion/src/medallion/services/transform.py:753,836-838`

**CP-003 · The `/api/serve` proxy is a `{path:path}` catch-all although every consumer reads only `GET /api/serve/applications/`**
`compute` · **LOW** · PARTIAL
- *What is left:* The enumeration and the FGA half are done: the only client call set is `/api/ray/{health,jobs,jobs/{id}/logs,cluster,actors,tasks,overview,logs}` plus `GET /api/serve/applications/` (compute zone via `@rask/api/ray.ts`, studio via `serveApplications`, annotator via `serve_discovery.APPLICATIONS_PATH`), and `services/compute` serves exactly those eight `/ray` routes with `require_read` at the router. Narrow `proxy.py`'s `_register_proxy` from `/api/serve/{path:path}` to the one `applications/` resource so the whole Ray dashboard is no longer reachable through the gateway, and keep the traversal test in `services/compute/tests/test_ray.py:234` green. The row's blocker `D1` resolves to nothing in the register or `docs/`; treat it as unblocked.
- *Closes when:* `GET /api/serve/applications/` still answers through the gateway and any other `/api/serve/<x>` path returns 404, pinned by a test in `services/compute/tests/test_ray.py`.
- *Evidence:* `services/compute/src/compute/routes.py:24-77 (eight /ray routes, router-level require_read)` · `services/compute/src/compute/proxy.py:63-69 (`{path:path}` catch-all, GET/HEAD)` · `frontend/packages/api/src/ray.ts:426 and services/annotator/src/annotator/api/v1/endpoints/serve_discovery.py:53 (`/api/serve/applications/` only)` · `grep '\bD1\b' over open_backlog_left.md, docs/DECISIONS.md, docs/OPERATORS.md: no definition`

**CP-008 · Ingest enumeration has no BYTE ceiling — one enormous object enters unbounded**
`ingest, chart` · **LOW**
- *What is left:* Add `max_bytes` to `RunLimits` (`services/ingest/src/ingest/workflow.py:122-157`) and to settings beside `max_units` (`config.py:102-104`, env `RASK_INGEST_MAX_BYTES`, zero = unbounded), refused at enumeration before the fan-out exactly as `max_units` is, with a chart default next to `RASK_INGEST_MAX_UNITS` (`chart/values.yaml:282`). The enumerated `UnitTask` (`queue.py:100`) carries no size field, so the source listing must first supply per-unit bytes. RED test first. The run-hours and unit ceilings already exist and need nothing.
- *Closes when:* An enumeration whose summed bytes exceed `RASK_INGEST_MAX_BYTES` is refused before fan-out, pinned by a test.
- *Evidence:* ``grep -rn max_bytes services/ingest/src` → 0 hits` · `services/ingest/src/ingest/config.py:102-104 (only max_run_hours / max_units / incremental_max_rows)` · `chart/values.yaml:266,282` · `services/ingest/src/ingest/queue.py:100 (UnitTask has no size)`

**CP-022 · `ray-kit` depends on `ray[default]` solely for `JobSubmissionClient`/`JobStatus`, keeping `compute` on a private 1536Mi memory tier**
`ray-kit, compute, chart` · **LOW**
- *What is left:* Reimplement `build_client`, `health`, `list_jobs` and the prune protocol in packages/ray-kit over httpx against the dashboard's `/api/jobs/` and `/api/version`, replace the `JobStatus` import from `ray.dashboard.modules.job.common` (schemas.py:12) with a local enum, and map `ray.exceptions.AuthenticationError` to an httpx 401. Drop `ray[default]>=2.58` from packages/ray-kit/pyproject.toml:14 and re-lock. `compute` is the only fleet importer (five files under services/compute/src; medallion uses its own `ray_jobs_api`). Then revert `resources.compute` in chart/values.yaml:568-570 (512Mi request / 1536Mi limit) to the shared tier and measure the pod's steady state before calling it done.
- *Closes when:* `uv tree --package ray-kit` shows no `ray` and the compute pod runs on the shared memory tier without an OOMKill.
- *Evidence:* `packages/ray-kit/pyproject.toml:14` · `packages/ray-kit/src/ray_kit/dashboard.py:23-27` · `packages/ray-kit/src/ray_kit/schemas.py:12` · `chart/values.yaml:568-570`

**CP-024 · `ray_gcs_*` is unconfirmed to survive Ray token auth, so no GCS alert rule may be written yet**
`external-kuberay, chart` · **LOW**
- *What is left:* `chart/alerting/rules.yml` carries no `ray_gcs_*` rule and must not until measured. `values-prod.yaml:160-161` sets `ray.auth.enabled: true` while local `values.yaml` sets it false, so the local cluster cannot answer. The Collector's `ray-pods` scrape job exists (otel-collector.yaml:190), so the scrape precondition is met. On a cluster with `ray.auth.enabled=true`, curl the head's `:8080/metrics` and confirm `ray_gcs_update_resource_usage_time_bucket` is present (ray-project/ray#59361 reports token auth drops the family); only then add GCS rules. Not measured this session — no cluster access.
- *Closes when:* The head's `:8080/metrics` under token auth is observed to include `ray_gcs_update_resource_usage_time_bucket`, or observed not to and the row records that GCS rules are impossible on this Ray version.
- *Evidence:* `chart/values-prod.yaml:160-161 (ray.auth.enabled: true)` · `chart/values.yaml ray.auth.enabled: false (awk over the ray block)` · `chart/templates/otel-collector.yaml:190 (ray-pods scrape job)` · ``grep -rn ray_gcs chart/` → no matches`

**CP-002 · `services/compute` runs an image without `DiagnosticFormatter`, so its `extra=` diagnostics are dropped**
`compute, service-kit` · **LOW**
- **blocked:** owner go-ahead for one `compute` image build and roll
- *What is left:* Build and roll one compute image from `.docker/compute.dockerfile` (`dagger call image --name=compute publish …`), then confirm an `extra=`-carrying log line renders with its fields in the pod's logs. The code needs nothing: `services/compute/src/compute/__init__.py:29` builds the app through `make_service_app`, which installs `DiagnosticFormatter` at `service_kit/app.py:97`, so any image built from HEAD carries it. Which image the pod runs today is not verifiable without the cluster and was not re-measured this session.
- *Closes when:* An `extra=`-carrying line renders with its fields in the compute pod's logs.
- *Evidence:* `packages/service-kit/src/service_kit/app.py:27,97` · `services/compute/src/compute/__init__.py:17,29` · `.docker/compute.dockerfile:33,40`

**CP-004 · Nothing in the FGA model governs execution — no zone, compute-job or run type**
`compute, catalog, service-kit` · **LOW**
- **blocked:** owner ruling on whether execution/zone access becomes a governed dimension: add zone/run types to `model.fga` with gates in `services/compute`, or record that execution rights are data rungs on what the surface reads and a zone is a deployment surface, never a governed object
- *What is left:* Record the ruling in `docs/DECISIONS.md`, which today has no entry on execution governance. `model.fga` holds exactly ten types (user, team, role, project, warehouse, namespace, table, materialized_view, transaction, annotation_project) and no relation matching `zone|compute|submit|job|run`. `services/compute` gates both routers on `reader` over `settings.fga_root_object` (`security.py:42-54`) — a door, not a model. If the ruling adds types, add them to `model.fga` and gate `services/compute` on them; otherwise the DECISIONS entry alone closes the row.
- *Closes when:* A `docs/DECISIONS.md` entry states the ruling and, if it adds types, `model.fga` carries them with matching gates in `services/compute`.
- *Evidence:* `packages/service-kit/src/service_kit/governed/auth/model.fga:41-530 (ten `type` lines, no zone/job/run)` · `services/compute/src/compute/security.py:42-54` · `services/compute/src/compute/routes.py:26; proxy.py:17` · `grep -n -i 'execution|compute-job' docs/DECISIONS.md → no ruling`

**CP-023 · `RAY_LOGGING_CONFIG_ENCODING` / `RAY_SERVE_LOG_ENCODING=JSON` render on no cluster that runs, and no Serve replica line has been seen to land**
`external-kuberay, chart` · **LOW**
- **blocked:** the operators of the external KubeRay cluster named by `ray.dashboardUrl` (chart/values.yaml:2190) must set the two env vars on its head and worker containers
- *What is left:* Both env vars render only in `chart/templates/_ray-cluster-config.tpl:107-110`, consumed by `rayservice.yaml`, whose gate is `and ray.enabled singleTenant.enabled` while `singleTenant.enabled` defaults false (values.yaml:66) — so no rendered cluster carries them. Get `RAY_LOGGING_CONFIG_ENCODING=JSON` and `RAY_SERVE_LOG_ENCODING=JSON` onto the external cluster's head container env and every worker-group container (must precede `import ray`, which a container env satisfies). Then confirm a Serve replica exception appears in `opentelemetry_logs` with a populated `severity_text` and queryable deployment/replica fields. Do not use `RAY_LOG_TO_STDERR=1` (it stops Ray writing log files and breaks the driver-log reader behind `/api/ray/jobs/{id}/logs`); `RAY_BACKEND_LOG_JSON=1` converts only the Job Supervisor.
- *Closes when:* A Serve replica exception row is queryable in `opentelemetry_logs` with `severity_text` and deployment/replica fields populated.
- *Evidence:* `chart/templates/_ray-cluster-config.tpl:107-110` · `chart/templates/rayservice.yaml:1 (`if and .Values.ray.enabled .Values.singleTenant.enabled`)` · `chart/values.yaml:65-66 (`singleTenant.enabled: false`), :2185-2190`


**LH-189 · A failed kueue hook outlives its ServiceAccount, and every later `helm upgrade` fails on a 401 nobody can read**
`deploy, kueue` · **MEDIUM** · OPEN
- **MEASURED LIVE 2026-09-22**, found while diagnosing why a converge reported `UPGRADE FAILED:
  post-upgrade hooks failed: timed out waiting for the condition`. `rask-kueue-setup` had been
  `Error` with **11 restarts over 32 minutes**, failing on:
  `error validating "/manifests/queues.yaml": failed to download openapi: the server has asked for
  the client to provide credentials`.
- **IT IS A 401, NOT A 403, and that distinction is the whole diagnosis.** RBAC was not the problem:
  the pod HAD its projected token volume (`kube-api-access-r56h5` at the standard path) and the
  ClusterRole/Binding the chart grants. The ServiceAccount it names — `rask-kueue-setup` — **did not
  exist**. A projected token is minted for a SA UID, so deleting the SA invalidates every token
  already handed out and the API server rejects the holder as UNAUTHENTICATED. An error that reads
  like a credentials bug is a lifecycle bug.
- **THE LIFECYCLE THAT PRODUCES IT.** All four hook resources carry
  `hook-delete-policy: before-hook-creation,hook-succeeded`, with SA/ClusterRole/Binding at weight
  `-5` and the Job at `0`. A hook run that FAILS leaves its Job behind — `hook-failed` is not in the
  policy — and that Job keeps retrying on `backoffLimit: 20`. The next run deletes the SA at weight
  -5 and, on success, deletes it again; the stale Job is still holding a token for the SA that is
  gone. It then crash-loops against a 401 until `before-hook-creation` reaps it.
- **IT SELF-HEALS, WHICH IS WHY IT HAS SURVIVED.** The following converge recreated the SA with a
  fresh Job, the hook succeeded, and both were removed by `hook-succeeded` + `ttlSecondsAfterFinished`.
  So the evidence disappears with the failure, and what is left is a deploy that took the 20m hook
  timeout for no reason anyone can reconstruct afterwards.
- **IT WAS INVISIBLE UNTIL THIS SESSION** because `k3s-converge` reported the failed upgrade as
  success — a shell's exit status is its LAST command's, and the recipe ended `; rm -f "$LIVE"`.
  That is fixed and gated (`tests/unit/test_a_deploy_cannot_report_success_when_helm_failed.py`), so
  the NEXT occurrence will fail a deploy loudly instead of being swallowed. That makes this row more
  urgent than its severity suggests: the swallow was also what stopped it blocking anyone.
- **A SECOND FAILURE MODE IN THE SAME HOOK, measured 2026-09-22 on a later converge — and it is NOT
  the 401.** `rask-kueue-setup` crash-looped five times on
  `conversion webhook for kueue.x-k8s.io/v1beta2, Kind=LocalQueue failed: Post
  "https://rask-kueue-webhook-service.default.svc:443/convert?timeout=30s": tls: failed to verify
  certificate`, then SUCCEEDED (`localqueue.kueue.x-k8s.io/rask serverside-applied`) and the converge
  exited 0. It is a startup RACE, not a broken cert: the job's own `restart-kueue-controller` init
  container restarts the controller to force cert regeneration, `await-kueue-controller` waits for
  Ready, and `apply-queues` then races the controller's asynchronous patch of the CRD's `caBundle` —
  the CRD carries one (1,516 bytes) and the controller was Running throughout.
- **SO THE COST IS THE ROW'S REAL SUBJECT, and it is paid on a SUCCESSFUL deploy too.** That converge
  took ~17 minutes wall-clock, nearly all of it `helm --wait --wait-for-jobs` watching a job back off.
  This row's bar — "a deploy following a failed hook run does not pay the 20m timeout" — is therefore
  not only about the 401 aftermath: the ordinary path pays minutes per deploy, every deploy, and
  `backoffLimit: 20` is what makes both modes eventually invisible. Whatever is ruled about the delete
  policy should also make `apply-queues` wait for the webhook it is about to call rather than for the
  controller's readiness probe, which answers a different question.
- *What is left:* Decide whether the Job should carry `hook-failed` in its delete policy so a failed
  run does not leave a retrying Job behind, or whether the SA should outlive the hook (drop
  `hook-succeeded` from the SA/RBAC trio so the token stays valid for any Job still running). The
  second is smaller and fixes the 401 directly; the first stops the stale Job existing at all. They
  are not exclusive.
- *Closes when:* A hook run that fails leaves nothing behind that can crash-loop on an invalidated
  token, and a deploy following a failed hook run does not pay the 20m timeout.
- *Evidence:* `chart/templates/kueue-queues.yaml:17-25,128` (the four hook resources, weights -5/0,
  and `backoffLimit: 20`) · live 2026-09-22: `rask-kueue-setup-j7gmn 0/1 Error 11 (6m12s ago) 32m`,
  `kubectl get sa rask-kueue-setup` -> NotFound, pod volume `kube-api-access-r56h5` present ·
  converge log `UPGRADE FAILED: post-upgrade hooks failed`


## PHASE 3 · CONTROLPLANE

**XC-027 · `chart/values-prod.yaml` sets `ingress.enabled/className/host` but no `tls:` block, so OIDC tokens and vended S3 credentials traverse plaintext at the edge**
`chart, gateway` · **MED**
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** Prod TLS Ingress — edge work, and blocked on a hostname that does not exist. The work is unchanged; only the label is, so phase 1 stops claiming it.
- **blocked:** Owner decision on the prod hostname and the certificate issuer.
- *What is left:* `chart/values-prod.yaml:221-224` supplies only `enabled`, `className: nginx` and an empty `host`; `chart/templates/ingress.yaml:44-45` renders `tls:` only `with .Values.ingress.tls`. Add an `ingress.tls` block plus the cert-manager issuer annotation to `values-prod.yaml`, then re-run `bash scripts/prod_render_check.sh` to pin it. In-cluster Dapr mTLS covers service invocation only, not the edge.
- *Closes when:* `helm template -f chart/values-prod.yaml` renders an Ingress with a `tls:` entry and an issuer annotation, and `scripts/prod_render_check.sh` fails without them.
- *Evidence:* `chart/values-prod.yaml:221-224` · `chart/templates/ingress.yaml:44-45` · `scripts/prod_render_check.sh (exists)`


**LH-082 · The gateway proxies the catalog's full all-method write surface to the public ingress and nothing says whether that is intended**
`gateway, catalog` · **LOW**
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** The defect note is catalog; every deliverable is GATEWAY plus a `docs/DECISIONS.md` line. The work is unchanged; only the label is, so phase 1 stops claiming it.
- **blocked:** Owner ruling on whether all-method public exposure of `/api/catalog/*` (behind catalog-side OIDC+FGA only) is intended
- *What is left:* `Route("/api/catalog", "", *catalog)` at services/gateway/src/gateway/__init__.py:225 forwards every method from the ingress `- path: /api` rule (chart/templates/ingress.yaml:66); the row carries no rationale comment and docs/DECISIONS.md has no entry for it. Record the ruling — a rationale comment on that Route row plus a line in docs/DECISIONS.md stating the write surface is deliberately internet-facing behind catalog OIDC+FGA — or narrow the row's method set.
- *Closes when:* Either the Route row carries the rationale and docs/DECISIONS.md records the ruling, or the row forwards a narrowed method set.
- *Evidence:* `services/gateway/src/gateway/__init__.py:225 `Route("/api/catalog", "", *catalog)` with no rationale comment (context :200-226)` · `grep -n -i 'api/catalog\|all-method\|write surface\|internet-facing' docs/DECISIONS.md → empty` · `chart/templates/ingress.yaml:66 `- path: /api``



**XC-009 · No Dapr `accessControl` policy exists in any chart template and the actor/workflow invocation planes are uncharacterised**
`chart, medallion, notifications, gateway, annotator, ingest` · **MED**
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** `defaultAction: deny` is one Configuration every sidecar references unconditionally, and its closing condition needs a live drive of every GATEWAY route. The work is unchanged; only the label is, so phase 1 stops claiming it.
- *What is left:* `accessControl`, `defaultAction`, `trustDomain` and `WorkflowAccessPolicy` appear in zero files under `chart/`; `networkPolicy.enabled` is false (`values.yaml:761`). Characterise actor-to-actor (ActorProxy) and Dapr Workflow invocation on the live estate first — Dapr excludes workflows from service-invocation access control — then write `policies:` plus `defaultAction: deny` and `trustDomain` into the shared `lance-tracing` Configuration (`chart/templates/observability.yaml:72`), add a `WorkflowAccessPolicy`, validate by driving every gateway route live, and add the missing test. NetworkPolicy stays a separate prod-hardening half (no-op on k3s flannel).
- *Closes when:* The rendered Configuration carries `defaultAction: deny` with per-app policies and every gateway route and cascade hop still succeeds on a live drive.
- *Evidence:* `grep -rn 'accessControl|defaultAction|WorkflowAccessPolicy|trustDomain' chart/: no hits` · `chart/values.yaml:761 (networkPolicy.enabled: false)` · `chart/templates/observability.yaml:72-76 (Configuration lance-tracing)`


**LH-073 · Right to erasure is a Lance row delete only — it reaches no blob sidecar, clone/branch or version-pinning tag**
`catalog, maintenance, notifications` · **HIGH**
- **RAISED MED -> HIGH 2026-09-19.** Not new evidence about the defect — new evidence about what rests on it. Erasure traceability is named as a PRIMARY driver for column-level lineage in the reference material (GDPR Art. 17, plus CCPA/HIPAA/SOX): "show me every system that touched this record". rask HAS the column-level lineage that question needs (`ColumnLineageDatasetFacet`, emitted by catalog and medallion) and cannot act on the answer, which is the worse half of the pair to be missing: the estate can prove what it would have to erase and cannot erase it.
- **THE HOLE IS MEASURED AND REPRODUCIBLE (2026-09-19), and it is FOUR surfaces, not one.** Against pylance 11.0.0, a table with a branch, a tag and history, after `delete_from_table` removes the subject:
  ```
  main after delete   ['bob', 'carol', 'dan']
  branch 'work'       ['alice', 'bob', 'carol', 'dan']
  tag 'pinned'        ['alice', 'bob', 'carol', 'dan']
  version 1           ['alice', 'bob', 'carol', 'dan']
  ```
  The erasure reaches ONE of the four surfaces the estate serves this table from, and the other three are LIVE: a branch answers through `?branch=` on every read door, a tag and an old version through `checkout_version`. The subject's data is one request away from anyone who can read the table, after the operation reported success. A tag is the worst of the three — it PINS the version against reclamation, so the one mechanism that would eventually remove the bytes is the one a tag exists to prevent. Pinned by `tests/unit/test_an_erased_row_survives_in_three_places.py`, a characterisation test that asserts the DEFECT: each surface is its own case, so the fix flips them one at a time and the remaining reds are the remaining work.
- **THE PROPAGATION IS BUILT (2026-09-19): `catalog.services.erasure.erase`.** Five ordered steps — every branch, every pinning tag, main, reclaim, then VERIFY — and the order is forced by the format rather than chosen: a branch and a tag both pin the version that holds the row, so reclaiming first is a no-op. It ends with a verification pass over every retained version, because every step before it is an ATTEMPT and only that is evidence; `complete` is False whenever anything still answers the predicate, so the estate cannot report a false erasure. 11 tests drive real Lance. **What it CANNOT finish is [[LH-178]]**, found by building it: deleting rows on a branch does not remove that branch's pin, and deleting the branch destroys someone's work. *What is left here:* the notifications delete-subject clause ONLY — **the blob-sidecar question is answered and fixed (2026-09-20)**: a predicate delete writes a deletion file and leaves the data file live for the surviving rows, so the subject's payload stayed on storage and the door reported `complete: True` over it. Measured on pylance 11.0.0 with two 5 MiB blob rows — after the delete the directory is still 10.5 MB and cleanup frees 1,188 bytes; compacting first rewrites the fragment without the row and the same cleanup frees 10.5 MB. The erasure now compacts between the deletes and the reclaim, best-effort and reported as its own surface. (The row's hypothesis that `orphans.py:221`'s accounting already covered it was wrong in the other direction too: `cleanup_old_versions` DOES take a sidecar with its data file, so the 'reclamation gap' that comment named no longer exists and is corrected.) — **the door is wired**: `POST /management/v1/table/{id}/erasure`, owner-gated (`can_drop`), answering the `ErasureReport` rather than a status code. `delete_from_table` (`data.py:451`) is a predicate delete plus a DELETE lineage event and propagates nowhere. Propagate a row delete to clones/branches through the referrer registry (`service_kit.lakehouse.base_refs` / `work_items`, consumed by maintenance `sweep.py` and `purge.py`) and to tags pinning old versions, and add a delete-subject door in notifications (no erasure/delete-subject code exists there). For blob sidecars, check whether `orphans.py:221`'s per-data-file `.blob` accounting already reclaims them once old versions are cleaned up; if so the sidecar clause reduces to making erasure trigger version cleanup past the retention floor.
- *Closes when:* A row erasure removes the subject from every clone, branch and pinned version and its sidecar bytes, and the notifications plane has a delete-subject door.
- *Evidence:* `services/catalog/src/catalog/api/v1/endpoints/data.py:447-462 (predicate delete + DELETE event only)` · `services/maintenance/src/maintenance/services/orphans.py:221 (blob sidecar accounting)` · `rg -l referrer services/maintenance/src packages -> sweep.py, purge.py, base_refs.py, work_items.py` · `rg -i 'delete.subject|erasure' services/notifications/src -> no hits`
- **MOVED HERE FROM PHASE 1 (2026-09-20), and what moved is NOT the erasure.** The catalog and maintenance halves are done: `POST /management/v1/table/{id}/erasure` propagates across branches, pinning tags, main, a COMPACT step (added today, after the erasure freed 1,188 bytes of a 5 MiB subject because a predicate delete leaves the data file live) and a reclaim, then VERIFIES every retained version so `complete` cannot lie. This row's own words: "what is left here: the notifications delete-subject clause ONLY". **That clause is a DATA gap, not a delivery one** — the inbox holds claim-check pointers carrying `author.sub`, so a subject erased from the lakehouse still exists there, and no door removes them. Phase 3 by the FOCUS's split, HIGH because an Art. 17 erasure that stops at the lakehouse boundary is incomplete. The branch-pin case it cannot finish is [[LH-178]], which stays where it is.

**CTL-001 · The gateway's lineage guard is a two-entry prefix blocklist, so every lineage/catalog path it does not name is reachable at the edge**
`gateway, chart` · **HIGH**
- *What is left:* Replace `lineage_sidecar_guard`'s blocklist (rendered by `lance.lineageSidecarOnlyRoutes` as exactly `lineage-events[,<reconcile-binding>]`) with a per-row allowlist in gateway/__init__.py — catalog `/v1/*`; lineage `/runs`, `/events`, `/v1/*` — so anything unlisted 404s. Add a test that the currently-exposed paths (`/api/lineage/lineage-dlq`, `/api/catalog/control-events`, both `/dapr/subscribe`, `/ui/*`, `/demo/*`) 404 while the allowlisted ones proxy. When HTTPRoutes land, express the same allowlist there, delete `lineage_sidecar_guard`, and prove the sidecar paths still reach the service from its sidecar.
- *Closes when:* A request to any unlisted `/api/lineage/*` or `/api/catalog/*` path 404s at the edge under a test, and the guard's env blocklist no longer exists.
- *Evidence:* `services/gateway/src/gateway/__init__.py:517-534 (prefix blocklist loop, 403)` · `chart/templates/_helpers.tpl:809-821 (`lineage-events{,bindingName}` is the whole list)` · `services/gateway/tests/test_lineage_guard.py:52-95 (tests assert the blocklist, not an allowlist)`

**CTL-002 · How the north-south path authenticates once the edge bypasses the fleet gateway and its dapr-api-token guard is undecided**
`gateway, service-kit, chart` · **HIGH**
- **blocked:** Owner decision: when the edge calls Services directly (kgateway), keep dapr-api-token and have the edge mint it, replace it with a different edge-injected credential, or drop it and re-argue the allowlist?
- *What is left:* Record the owner decision in `docs/DECISIONS.md` (the plan it was to be recorded in, `open_gateway.md`, no longer exists at HEAD), then change `packages/service-kit/src/service_kit/governed/dapr_auth.py` and the chart to match. Today the Ingress routes `/api` to the fleet gateway, the gateway reaches services through the sidecar at `127.0.0.1:<DAPR_HTTP_PORT>/v1.0/invoke/...` when `RASK_DAPR_ENABLED` is on, and `dapr_auth.py` refuses any call whose `dapr-api-token` does not match `APP_API_TOKEN`; an edge-to-Service backendRef sends no such header. kgateway is recorded as the intended future edge and is not implemented.
- *Closes when:* The decision is recorded and `dapr_auth.py` plus the chart implement it.
- *Evidence:* `packages/service-kit/src/service_kit/governed/dapr_auth.py:7-14,75` · `services/gateway/src/gateway/__init__.py:330-339 (_target_base → /v1.0/invoke when dapr_enabled)` · `chart/templates/ingress.yaml:55-72 (/api → fleet gateway; 'kgateway is the intended future edge (not implemented on this branch)')` · `ls open_gateway.md → No such file`

**CTL-003 · Nothing replaces the dapr-sentry mTLS that an edge->Service backendRef drops**
`gateway, chart` · **HIGH**
- **blocked:** Choose the in-cluster transport security for an edge->service hop that bypasses the Python gateway's daprd — mesh/Envoy TLS origination, or an explicit accepted-plaintext ruling — before any /api row moves off the Python gateway
- *What is left:* Today every /api call rides browser -> Ingress (chart/templates/ingress.yaml:66) -> rask-gateway -> its own daprd (`http://127.0.0.1:{dapr_http_port}/v1.0/invoke/{app_id}/method`, services/gateway/src/gateway/__init__.py:338-339) -> service, so caller->service is sentry-issued mTLS. No HTTPRoute exists in the chart yet (grep `HTTPRoute|gateway.networking.k8s.io` over chart/ hits only prose in chart/templates/_helpers.tpl:807). Decide and wire the replacement, or write the accepted-plaintext ruling into the edge plan, before the first Gateway-API backendRef lands.
- *Closes when:* The edge migration plan names the edge->service transport security (mesh TLS origination or a recorded accepted-plaintext ruling) and no HTTPRoute backendRef exists without it.
- *Evidence:* `services/gateway/src/gateway/__init__.py:338-339 — dapr invoke URL when `settings.dapr_enabled`` · `grep -rln 'HTTPRoute\|gateway.networking.k8s.io' chart/ → only chart/templates/_helpers.tpl:807 (prose) and crds-bootstrap/cnpg-crds.yaml` · `chart/templates/ingress.yaml:66 `- path: /api``

**CTL-004 · Dapr invocation resiliency (retries, timeouts, breakers) is scoped to the gateway sidecar and has no edge equivalent for the kgateway migration**
`gateway, chart` · **HIGH**
- **blocked:** The kgateway edge migration (gateway/P1-edge browser-proven, gateway/P2-dapr-token) landing — the chart has no HTTPRoute or kgateway policy object to attach a policy to
- *What is left:* Express the policy that `chart/templates/dapr-resiliency.yaml:143-149` applies from the gateway sidecar (scoped to `gateway` at lines 255-256: 30 s connect / 300 s read matching the gateway's httpx client, breaker sheds a dead upstream for 30 s after 5 consecutive failures) on the kgateway HTTPRoutes or a kgateway policy CRD for each absorbed backend. Then drop the dead gateway scope from `dapr-resiliency.yaml`. kgateway is only 'the intended future edge' (`chart/values-prod.yaml:223`); no HTTPRoute exists under `chart/templates`.
- *Closes when:* Each absorbed backend has an equivalent edge policy and `dapr-resiliency.yaml` no longer scopes to `gateway`.
- *Evidence:* `chart/templates/dapr-resiliency.yaml:143-149,255-256` · `chart/values-prod.yaml:223` · `grep -rl 'HTTPRoute' chart/templates — no match`

**CTL-005 · The no-k8s dev loop (`scripts/dev-micro.sh`) has no /api origin derived from the chart's HTTPRoutes, and no HTTPRoute exists to derive it from**
`gateway, chart, scripts` · **HIGH**
- **blocked:** CTL-007 / gateway P2-routes: no Gateway or HTTPRoute exists in `chart/templates/`, so a proxy rendered from that table cannot be built until the owner-sequenced gateway dissolution lands it.
- *What is left:* Once CTL-007 renders Gateway + HTTPRoute, build a thin dev-only proxy whose route table is RENDERED from those HTTPRoutes (never a second hand-kept table), wire it into `scripts/dev-micro.sh` in place of the `gateway :8888` row (`dev-micro.sh:34,65`), and add a contract test that fails when the rendered table and the chart's HTTPRoutes disagree. `frontend/packages/zone-contract/src/proxy.ts` composes zones only and does not proxy `/api`, so it is not a starting point.
- *Closes when:* `make dev-micro` serves `/api/*` from a proxy whose table is rendered from the chart's HTTPRoutes, and a test fails when the two diverge.
- *Evidence:* ``grep -rln 'kind: HTTPRoute' chart/templates/` → empty` · `scripts/dev-micro.sh:34,65` · `open_backlog_left.md:8972 (CTL-007: no Gateway/HTTPRoute exists)`

**CTL-006 · The gateway mounts no body cap, no rate limit and emits no access line**
`gateway, service-kit` · **MED** · PARTIAL
- *What is left:* Shipped: inbound `X-Forwarded-*` is stripped and re-stamped from uvicorn's resolved client (`gateway/__init__.py:96-119`), `RequestIDMiddleware` is mounted (:513), and 400/404/502 answer problem+json (:475-490). Drop the `code`-in-problem+json clause: `:467-472` records that the gateway deliberately carries no Lance numeric code. Still missing at the edge: the gateway runs neither `register_middleware` nor `BodySizeLimitMiddleware` (the cap exists only on the services behind it, `service_kit/middleware.py:119`); `service_kit/rate_limit.py` is per-route and unused by the gateway; the only per-request log is the 502 error line (:678). Mount the body cap, add a per-subject/IP bucket via `service_kit.rate_limit` (honour its single-replica gate), and emit one structured access line per proxied request.
- *Closes when:* An over-cap upload is refused 413 at the gateway, a burst from one subject is refused 429, and every proxied request produces one structured access line, each pinned in `services/gateway/tests`.
- *Evidence:* `services/gateway/src/gateway/__init__.py:96-119,467-490,494,513,678` · `packages/service-kit/src/service_kit/middleware.py:107-131` · `packages/service-kit/src/service_kit/rate_limit.py:1-21`

**CTL-007 · No Gateway/HTTPRoute exists: chart/templates/ingress.yaml is the only edge template and kgateway is named only in comments**
`gateway, chart` · **MED**
- *What is left:* Add a kgateway toggle to chart/values.yaml on the cnpg.enabled pattern (toggle gates operator and resources; nginx stays default). Render Gateway + HTTPRoute with routing identical to ingress.yaml:66-124 — /api -> <fullname>-gateway:8888, /<zone> -> <fullname>-web-<zone>:3000 specific-first, /dex -> dex:5556 (ingress.yaml:92-98, which the original ask omitted), / -> home last, no path rewriting — emitting one rule per future backend (catalog, lineage, produce, train, explorer, ray/serve, projects) rather than one /api rule. Prove both edges with helm template in each toggle position and a real browser reaching /, /lakehouse, /compute and /api/catalog with the toggle on. Leave OpenFGA ClusterIP-only and the rask-gateway Deployment untouched.
- *Closes when:* helm template renders a working Gateway + HTTPRoute with the toggle on and the nginx Ingress with it off, and a browser reaches /, /lakehouse, /compute and /api/catalog through the kgateway edge.
- *Evidence:* `ls chart/templates | grep -i 'ingress|gateway|route' -> ingress.yaml only` · `grep -rn HTTPRoute chart/ -> only a comment at chart/templates/_helpers.tpl:807` · `chart/values-prod.yaml:223 ('kgateway is the intended future edge')` · `chart/templates/ingress.yaml:66-124 (the routing to mirror)`

**CTL-012 · Envoy path-normalization parity with `_normalize_path` (merge_slashes, `..` segments) is unproven**
`gateway, chart` · **MED**
- *What is left:* No kgateway/Envoy edge exists at HEAD: `HTTPRoute` appears only in `_helpers.tpl` prose and `chart/values.yaml` has no `kgateway` key, so this waits on CTL-007 rendering the edge. Once it exists, write a test or documented comparison showing Envoy's `merge_slashes` / `normalize_path` / escaped-slash settings produce the same paths as `services/gateway/src/gateway/__init__.py:276 _normalize_path` for repeated slashes and `..` segments, and pin those settings in the chart.
- *Closes when:* A test (or committed comparison) proves the rendered edge normalizes slashes and dot segments as `_normalize_path` does, with the Envoy settings pinned in `chart/`.
- *Evidence:* `services/gateway/src/gateway/__init__.py:276 (_normalize_path)` · `rg -l HTTPRoute chart/templates -> only chart/templates/_helpers.tpl` · `rg '^kgateway' chart/values.yaml -> no hits`

**CTL-017 · No test refuses a rendered `platform.rask.io` CustomResourceDefinition; the only CRD-aware scan skips CRDs**
`chart, controlplane` · **MED**
- *What is left:* Add a test to tests/unit/test_invariants.py that renders the chart and asserts no document has `kind: CustomResourceDefinition` with `spec.group` (or any apiGroup) equal to `platform.rask.io`. Leave the RBAC `apiGroups: ["platform.rask.io"]` reference at chart/templates/controlplane.yaml:126 allowed.
- *Closes when:* The new invariant test passes at HEAD and fails when a `platform.rask.io` CRD template is added.
- *Evidence:* `tests/unit/test_invariants.py:1782 (`continue` on `kind: CustomResourceDefinition`)` · `grep -rn 'platform.rask.io' chart/ → only chart/templates/controlplane.yaml:126` · `grep -n 'CustomResourceDefinition' tests/unit/*.py → only the skip at :1782`

**CTL-022 · Notifications has no subject-erasure door and no TTL on watch/prefs/cursor state**
`notifications` · **MED** · PARTIAL
- *What is left:* The reverse-index clause is shipped: `WatchIndexActor` (`watch_actor.py:61`, project → subjects) and `InboxWatches` (`models.py:331`, subject → projects) already make a subject's watches enumerable, and the sent ledger rides the inbox pointer rather than being separate state (`models.py:174-179`). Add a delete-subject door under `services/notifications/api/` (the only DELETE today is `watches.py:117` for one project) that sweeps the subject's inbox, `ChannelPrefs` (`models.py:289`), `InboxCursor` (`models.py:211`) and removes it from every project's `WatchIndexActor`. Only the inbox is bounded today (`feed.compact`, `feed.py:61`); `ActorStateTTL` is off on the estate and `actor_state_ttl_enabled` defaults False (`config.py:65`), so a TTL on watches/prefs/cursor needs either that Dapr feature enabled or an in-app sweep.
- *Closes when:* One door erases every record a subject holds across inbox, prefs, cursor and watch indexes, and a test proves the subject is unrecoverable afterwards.
- *Evidence:* `services/notifications/src/notifications/watch_actor.py:61` · `services/notifications/src/notifications/models.py:211,289,331` · `services/notifications/src/notifications/config.py:65` · ``grep -rn '@router.delete' services/notifications/src/notifications/api/` → only watches.py:117`

**CTL-008 · No kgateway/HTTPRoute timeout equivalent of nginx's 3600s proxy-read-timeout exists, so `query.live` streams die on the future edge**
`gateway, chart` · **MED**
- **blocked:** CTL-007 — no Gateway/HTTPRoute exists in chart/ to carry a timeout; the kgateway toggle and rendered routes must land first (prerequisite work, not an owner ruling)
- *What is left:* Once CTL-007 renders HTTPRoutes, set the route/policy timeout to match `nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"` (chart/values.yaml:2326) and keep `idleTimeoutSeconds: 0` on the zone Bun servers (chart/values.yaml:849) — the edge value alone is not sufficient, a stream died at 256.8s with the edge at 3600s until Bun's idle timeout was disabled. Prove it by holding a notification-bell `query.live` stream open for more than 90s through the new edge in a browser, not by reading the rendered object. The edge is still ingress-nginx today (chart/templates/ingress.yaml is the only edge template; kgateway is named only as the intended future edge in chart/values-prod.yaml:223).
- *Closes when:* A bell `query.live` stream stays connected for more than 90s through kgateway in a browser.
- *Evidence:* `chart/values.yaml:2326` · `chart/values.yaml:849` · `chart/values-prod.yaml:223` · `rg -n 'HTTPRoute' chart/ --glob '!*.md' → only comments in _helpers.tpl and ingress.yaml`

**CTL-009 · Longest-prefix `/api` routing lives in `gateway/__init__.py::_routes()` and not in per-service HTTPRoute rules**
`gateway, chart` · **MED**
- **blocked:** CTL-007 (a Gateway/HTTPRoute rendered behind a kgateway values toggle and browser-proven) and CTL-002 (how the north-south path authenticates once the edge calls Services directly instead of via dapr-api-token)
- *What is left:* `_routes()` at `services/gateway/src/gateway/__init__.py:183-264` holds 15 `Route(...)` rows and no `kind: HTTPRoute` exists under `chart/templates/` (the only Gateway-API text in `chart/` is inside `crds-bootstrap/cnpg-crds.yaml`). Once CTL-007 has rendered the edge, add one HTTPRoute rule plus backendRef per row in `chart/` and delete that row from the Python table. Gateway API matches most-specific-first natively, so the longest-prefix ordering needs no re-encoding.
- *Closes when:* Every `Route(...)` row in `_routes()` has a matching HTTPRoute rule in `chart/` and the Python table is empty.
- *Evidence:* `services/gateway/src/gateway/__init__.py:183,217-264 (15 Route rows)` · `grep -rln 'kind: HTTPRoute' chart/ → only chart/crds-bootstrap/cnpg-crds.yaml` · `open_backlog_left.md:8972-8982 (CTL-007, CTL-008 still open)`

**CTL-010 · Zones reach the gateway server-side through two env vars: compute/studio/models read `RASK_GATEWAY_URL`, home/lakehouse read `LANCE_GATEWAY_URL`**
`gateway, frontend, chart` · **MED**
- **blocked:** Gateway Phase-2 route plan (P2-routes): whether zone SSR fetches target the gateway's in-cluster address or the services directly
- *What is left:* `compute`/`studio` `hooks.server.ts` and every zone's `inbox.remote.ts` plus `models/src/lib/server/doors.ts` read `RASK_GATEWAY_URL`; `home`/`lakehouse` go through `makeZoneHooks`, whose `bff.ts:341` reads `LANCE_GATEWAY_URL` with a `:8001` default; `chart/templates/frontends.yaml:203-210` injects both. Collapse to one SSR base-URL variable across all seven zones and `@rask/api/bff.ts`, set it once in the chart, and delete the two-var gotcha from `.claude/skills/rask-frontend/SKILL.md:272`.
- *Closes when:* `grep -rn LANCE_GATEWAY_URL frontend/ chart/ .claude/` returns nothing and every zone's SSR fetch resolves through the same variable.
- *Evidence:* `frontend/packages/api/src/bff.ts:341` · `frontend/microfrontends/compute/src/hooks.server.ts:21 and studio/src/hooks.server.ts:22` · `chart/templates/frontends.yaml:203-210` · `.claude/skills/rask-frontend/SKILL.md:272`

**CTL-011 · Deleting the gateway removes the north-south OTLP span the Perses 'Fleet — RED' panels read**
`gateway, chart` · **MED**
- **blocked:** The gateway→Gateway API migration phase (gateway/P2-routes), itself gated on CTL-002: how the north-south path authenticates once Dapr service invocation leaves it (edge-minted dapr-api-token, a different edge-injected credential, or drop it and re-argue the allowlist).
- *What is left:* The Python gateway still exists and calls `setup_otel(app, service_name="gateway")`; the 'Fleet — RED' dashboard reads `http_server_duration_milliseconds_count` by `service_name`. kgateway is named only as the intended future edge (values-prod.yaml:223) and no HTTPRoute exists. Before the gateway is deleted, pipe kgateway/Envoy access logs and metrics into the Collector→GreptimeDB path and confirm the RED panels still show north-south rate, errors and duration.
- *Closes when:* With the Python gateway removed, the 'Fleet — RED' dashboard shows north-south request rate, error rate and duration sourced from the edge.
- *Evidence:* `services/gateway/src/gateway/__init__.py:454 (setup_otel)` · `chart/templates/perses-dashboards.yaml:45-64 (RED queries)` · `chart/values-prod.yaml:223 (kgateway is future)` · `open_backlog_left.md:8949-8961 (CTL-002 gate on P2)`

**CTL-013 · `services/gateway`, its dockerfile, chart Deployment and dev-micro.sh entry all still exist**
`gateway, chart, scripts` · **MED**
- **blocked:** CTL-002's owner decision on how the north-south path authenticates once the edge calls Services directly (the gateway plan's Phase 2 cannot proceed until it is answered); Phase 3 does not start while Phase 1 is open.
- *What is left:* Remove `services/gateway`, `.docker/gateway.dockerfile`, the chart's gateway Deployment/Service/resiliency scope (`chart/templates/fleet.yaml`, `dapr-resiliency.yaml`, and the `/api` → `-gateway` rules at `chart/templates/ingress.yaml:66-70,118-122`) and the gateway process in `scripts/dev-micro.sh`. The chart renders no HTTPRoute and `dev-micro.sh` has no derived proxy, so every `/api/*` row still reaches the fleet only through the gateway; prove each zone's `/api/*` in-cluster through the edge and through a derived dev proxy before deleting. Update `docs/architecture/system-overview.md`, `deployment.md` and `.claude/skills/rask-services-fleet` in the same commits. The `open_gateway.md` deletion clause is already done (folded into the register).
- *Closes when:* `services/gateway` no longer exists and every zone's `/api/*` works in-cluster and under `make dev-micro` without it.
- *Evidence:* ``ls services/gateway .docker/gateway.dockerfile` → both present` · `chart/templates/ingress.yaml:66-70,118-122` · ``grep -rln HTTPRoute chart/templates/` → only _helpers.tpl prose` · `open_backlog_left.md:3 (open_gateway.md folded); `ls open_gateway.md` → absent`

**CTL-021 · notifications cannot present a dedicated lineage credential: its reconciler reaches lineage through Dapr service invocation and daprd overwrites dapr-api-token**
`notifications, lineage, chart` · **MED**
- **blocked:** Move the notifications reconciler's `GET /events` call off Dapr service invocation onto direct HTTP (as ingest does), or accept that sidecar-invoked hops authenticate as the estate
- **THE ROW'S PREMISE WAS WRONG AND THE LANE WAS DEAD — MEASURED LIVE 2026-09-22, FIXED HERE.** The
  choice is framed as dedicated-credential vs "authenticate as the estate", which reads as though the
  second was what the reconciler did. It was not authenticating at all. `POST
  /notifications-reconcile-cron` answered **500 three thousand and eighty-seven times** (~9.6 per five
  minutes, still climbing when found), every one an unhandled `httpx` 401 from
  `invoke/lineage/method/events`; lineage's own counter shows the matching **401 x 169**. So the
  durable half of the notifications design — the walk that exists *because the bus alone is provably
  incomplete*, for ingest, Ray TRAIN and external OpenLineage producers that emit over HTTP only — has
  **never run**, on a Ready pod with a healthy bus lane beside it.
- **THE CAUSE IS A SECOND ACCESSOR FOR ONE SECRET, not the transport this row is about.**
  `feed_token()` fell back to `settings.app_api_token`, which is env-only, while the pod carries
  `RASK_APP_TOKEN_FROM_STORE=true` and no `APP_API_TOKEN` — so it resolved to `None`, `_headers()`
  sent NEITHER header (both-or-neither, by design), and lineage routed the walk to OIDC. It now reads
  `dapr_auth.expected_app_token()`, the same single resolver the INBOUND doors use, with the env value
  kept as the fallback behind it. **`medallion` and `maintenance` already carried this exact fix**
  (medallion's measured at 2,700 failed catalog calls in twenty-five minutes); this subject was the
  one left reading env, and `ingest` is moved with it — not live there, because its dedicated resolver
  answers first, but the same second accessor waiting for the day that path is turned off.
- **GATED ESTATE-WIDE, because three sites fixed three times is a class rather than three bugs:**
  `tests/unit/test_an_outbound_credential_follows_the_token_to_the_store.py` refuses any production
  function that reads `app_api_token` without reaching `expected_app_token`, plus a leg that fails if
  the walk finds nothing. Both mutation-checked.
- **WHAT THIS DOES NOT DECIDE:** the ruling stands exactly as written. The shared-token path now
  WORKS; whether this hop should instead move to direct HTTP and present a dedicated credential is
  still open, and the fix here is what the row's second branch actually requires in order to be a
  real option rather than a description of a lane that was dark.
- *What is left:* Take the ruling. If direct HTTP: change `IngressSettings.feed_base_url` (`services/notifications/src/notifications/api/settings.py:157-181`, which routes through `127.0.0.1:3500/v1.0/invoke/lineage/method` when Dapr is on) to call lineage's own URL so the dedicated token survives, add `notifications` to the lineage-privileged subject list in `chart/templates/services.yaml` (deliberately absent, lines 718-726), and re-drive both directions at lineage's service door as ingest's were (own token 200, shared bearer 401). If accepted: record the boundary in `docs/DECISIONS.md`.
- *Closes when:* Either the reconciler authenticates at lineage with its own credential in both directions, or the estate-identity boundary is recorded in docs/DECISIONS.md.
- *Evidence:* `services/notifications/src/notifications/api/settings.py:157-181` · `chart/templates/services.yaml:718-726`

**CTL-014 · The Dapr helper comment's "routes become HTTPRoutes" note is neither acted on nor deferred**
`gateway, chart` · **LOW**
- *What is left:* The note at `chart/templates/_helpers.tpl:807` still reads "On the kgateway/Envoy migration these become 'no HTTPRoute declared'" with no deferral beside it; no HTTPRoute or Gateway API object exists in the chart. Either rewrite that comment to reflect the Gateway API, or leave an explicit deferral at that site. The deferral's reason already exists elsewhere (`ingress.yaml:65`, `frontends.yaml:38`: "kgateway is the intended future edge (not implemented on this branch)"), so the deferral branch needs no edge migration to write.
- *Closes when:* `_helpers.tpl`'s lineageSidecarOnlyRoutes comment either describes the Gateway API shape or carries an explicit deferral saying why it stays as-is.
- *Evidence:* `chart/templates/_helpers.tpl:807 (the HTTPRoute note)` · `chart/templates/ingress.yaml:64-65 and frontends.yaml:37-38 (kgateway deferral wording at other sites)` · `grep -rn HTTPRoute chart/ → only _helpers.tpl:807`

**CTL-023 · Notifications actor proxies surface sidecar transport failures as bare 500s and open a fresh channel per call**
`notifications` · **LOW**
- *What is left:* `proxies.py:105-114` re-raises anything not carrying `InboxUnreadable` unchanged, so a Dapr SDK transport error falls to service-kit's `_unexpected` catch-all (`exceptions.py:192`) as a 500; nothing in service-kit maps `DaprHttpError`/`DaprInternalError`. `typed_proxy` (`proxies.py:117-125`) calls `ActorProxy.create` per call over a fresh sidecar channel. Map transport errors to a 503 problem+json (`ServiceUnavailableError`) inside `_translating`, and build the proxy factory once in the notifications lifespan for `inbox_for`/`watch_index_for` to reuse.
- *Closes when:* A sidecar connection failure answers 503 problem+json, and one lifespan-built proxy factory serves every call, both pinned by unit tests.
- *Evidence:* `services/notifications/src/notifications/proxies.py:85-135` · `packages/service-kit/src/service_kit/exceptions.py:192` · `rg DaprInternalError|DaprHttpError packages/service-kit/src services/notifications/src → only the proxies.py docstring`

**CTL-024 · `.claude/skills/rask-notifications/SKILL.md` contradicts `services/notifications` on the reason count, line refs, and omits WatchIndexActor, `named_subjects` and the `/events/projection` rung**
`notifications` · **LOW**
- *What is left:* `NotificationReason` has 12 members (`models.py:63-98`, incl. ORIGINATOR, four TASK_*, PROMOTION_REVIEW_REQUESTED, TASK_LEASE_EXPIRED) while the skill says 'one of four reasons' (line 12) and 'six targeting sources' (line 3). Its line refs are stale: `notifiable()` is at `api/lineage_events.py:171` not `:154`, `enforce_author` at `lineage/api/fga_deps.py:178` not `:96`, and `fanout.py:37/87/88` land on comment lines. `WatchIndexActor` (10 code hits) and `named_subjects` (`api/control_events.py:84`) appear nowhere in the skill, nor does lineage's `GET /events/projection` / `can_observe_events` rung (`lineage/api/v1/endpoints/runs.py:163`, `fga_deps.py:125-146`). `lease_expired` is already covered (skill lines 61, 274). Rewrite the skill against the code for the remaining items.
- *Closes when:* Every reason, actor, line ref and rung the skill names matches `services/notifications/src` and `services/lineage/src` at HEAD.
- *Evidence:* `services/notifications/src/notifications/models.py:63-98 (12 NotificationReason members)` · `services/notifications/src/notifications/api/lineage_events.py:171 vs SKILL.md:84` · `services/notifications/src/notifications/api/control_events.py:84 (named_subjects; 0 skill hits)` · `services/lineage/src/lineage/api/v1/endpoints/runs.py:163 (/events/projection; 0 skill hits)`

**CTL-015 · The 502-with-detail → 503 change for an unreachable upstream is not named anywhere the edge migration will be read from**
`gateway` · **LOW** · PARTIAL
- **blocked:** gateway/P2-routes — the first `/api` row moving off the Python gateway onto a kgateway HTTPRoute (no HTTPRoute exists in `chart/templates` yet); writing the note before that lands would describe a change that has not happened.
- *What is left:* The client check is done: no zone branches on a gateway 502 — the BFF `doors.ts` files and `rows-arrow.ts` emit their own 502s and bucket non-401/403/404 failures generically, and `single-health-poll.test.ts:81` only mocks a 502 body. What remains is the note itself. In the commit that moves the first `/api` row onto an HTTPRoute, record in `docs/architecture/system-overview.md` (or `deployment.md`) and in `.claude/skills/rask-services-fleet` §5 that an unreachable upstream answers 503 from the edge rather than the Python gateway's `HTTPException(502, "upstream ... unreachable")`.
- *Closes when:* The status-code change is stated in the architecture doc and the fleet skill in the same commit as the first HTTPRoute row.
- *Evidence:* `services/gateway/src/gateway/__init__.py:679 (the 502 contract at HEAD)` · `.claude/skills/rask-services-fleet/SKILL.md:53 (§5 '502 contract')` · `grep -rn 'kind: HTTPRoute' chart/templates → 0 hits` · `frontend/microfrontends/annotator/src/lib/server/doors.ts:17 (non-401/403/404 bucketed generically)`

**CTL-016 · The gateway's merged `/docs` + fleet-wide `openapi.json` aggregation has no home once the gateway dissolves**
`gateway` · **LOW**
- **blocked:** Decide whether the merged `/docs` + `openapi.json` aggregation is re-homed onto one service endpoint or retired outright.
- *What is left:* `_merged_openapi` (`services/gateway/src/gateway/__init__.py:365-400`) and the `/docs` + `/openapi.json` handler (`:626-632`) still fetch every upstream's `openapi.json` and serve a merged Swagger UI; nothing else hosts it. Once decided, either move the aggregation onto one service endpoint or delete it, and record the decision in `docs/DECISIONS.md` or the commit rather than dropping it silently.
- *Closes when:* The aggregation code is gone from `gateway/__init__.py` and the decision is recorded.
- *Evidence:* `services/gateway/src/gateway/__init__.py:365 (_merged_openapi)` · `services/gateway/src/gateway/__init__.py:626-632`

**CTL-018 · controlplane ProjectStatus carries only `phase` and `namespace`; no conditions[], observedGeneration or catalogProjectId**
`controlplane, home` · **LOW**
- **blocked:** Owner decision (C-Q3): which fields of the controlplane Project DTO freeze once conditions[] exists, and the matching home-zone render contract?
- *What is left:* Get the ruling on the frozen fields. Then add a typed `conditions[]` carrying `observedGeneration`, plus `catalogProjectId` and `namespace` as external facts, to `ProjectStatus` additively, keeping `phase` for the home-zone render. `ProjectStatus` is exactly `phase: str = ""` and `namespace: str = ""` at HEAD.
- *Closes when:* `ProjectStatus` exposes `conditions[]` with `observedGeneration` and the home zone can distinguish 'not yet reconciled' from 'reconciled and failed'.
- *Evidence:* `services/controlplane/src/controlplane/schemas.py:59-64` · `grep -rn 'conditions\|observedGeneration\|catalogProjectId' services/controlplane/src → no hits`

**CTL-019 · No managed surfaces for roles and identities over the FGA model**
`controlplane, catalog` · **LOW**
- **blocked:** Owner ruling that the estate becomes long-lived and shared (Section I item 7 marks this CONDITIONAL on that call)
- *What is left:* Nothing ships: services/controlplane/src/controlplane/ holds config, dependencies, health, k8s, lifespan, routes, schemas, security, service — no roles or identities router, and no `/roles` or `/identities` route anywhere under services/controlplane/src. If the ruling lands, build role and identity management surfaces over the FGA model in the controlplane.
- *Closes when:* An owner ruling that the estate is long-lived/shared exists and role + identity management routes are served behind the controlplane's FGA gate.
- *Evidence:* `ls services/controlplane/src/controlplane/ — no roles/identities module` · `grep -rn '/roles\|/identities\|identit' services/controlplane/src → no route hits`

**CTL-020 · The models registry has no MLflow-parity feature set**
`controlplane, models zone, catalog` · **LOW**
- **blocked:** C2 (the product-works pass) must run first; then an owner decision naming which MLflow capabilities the models plane must match.
- *What is left:* Nothing is buildable until the gate opens. Run C2, then obtain the owner's list of MLflow capabilities to match before any is built. At HEAD no MLflow exists in code (`docs/RAY-TRAIN.md:215` states it; the models zone's Experiments view says "not MLflow").
- *Closes when:* C2 has run and the owner has named the MLflow capabilities the models plane must match.
- *Evidence:* `frontend/microfrontends/models/src/lib/models/Experiments.svelte:4,88 (MLflow not used)` · `docs/RAY-TRAIN.md:206-215 (no MLflow anywhere in the code)`


## FRONTEND

**LH-127 · Eight orphaned Dapr durables sit on six streams until the chart-durables orphan pass runs, and nothing surfaces an unexpected consumer**
`lineage, compute, chart` · **MED** · PARTIAL
- *What is left:* The orphan pass is written: `chart/templates/_durables.tpl` renders `lance.chartDurables`, `nats-stream-job.yaml:255-258` deletes any `*-durable` on a walked stream absent from `EXP_DURABLES`, and `tests/unit/test_a_durable_the_chart_owns_is_a_durable_the_drift_loop_walks.py` pins the set. **THE EIGHT ORPHANS ARE GONE, verified 2026-09-18** against the live NATS monitor (`/jsz?consumers=true`): no `lance-ray`, no `pages-to-gold-htr`, no `maintenance-durable`, no `maintenance-work-durable` on any stream. What remains is exactly the chart-rendered set — MEDALLION 4, DLQ 6, CATALOG_CONTROL 2, LINEAGE 2, TRAINING 1. Then surface the inverse signal: the lakehouse admin `/streams` view shows `num_pending` and `push_bound` and flags an EXPECTED group that is unbound (`jetstream.ts:113-122`), but nothing flags a consumer that is NOT in the expected set, so the next orphan is still invisible without a NATS client. **IT MUST FLAG UNEXPECTED DURABLES, NOT UNEXPECTED CONSUMERS** — measured the same day, CATALOG_CONTROL carries an EPHEMERAL consumer (`xP0FWBl7`, no `durable_name`, `_INBOX` delivery, `num_pending: 0`) which is the catalog's own control-buffer subscription: each replica subscribes WITHOUT a queueGroupName by design (`core/control_buffer.py`), so broadcast subscribers are ephemeral and auto-named. A signal keyed on "not in the expected set" would flag that one permanently, and a permanently-firing signal is how the real orphan gets ignored. The orphan PASS has the same blind spot from the other side: `nats-stream-job.yaml:255-258` deletes `*-durable` names only, so a non-durable orphan survives it. `MAINTENANCE_WORK` and the index lane stay excluded from the walk by design (work-sized backoff).
- *Closes when:* The admin streams view marks an unexpected DURABLE (the consumer listing is verified clean, and the flag must not fire on legitimate ephemerals). Frontend-only work, so it waits for a change to the service it belongs to rather than a campaign of its own.
- *Evidence:* `chart/templates/_durables.tpl (exists); chart/templates/nats-stream-job.yaml:255-258 (EXP_DURABLES)` · `tests/unit/test_a_durable_the_chart_owns_is_a_durable_the_drift_loop_walks.py (exists)` · `frontend/microfrontends/lakehouse/src/lib/admin/jetstream.ts:39-44,113-122 (push_bound; expected-but-unbound only)` · `frontend/microfrontends/lakehouse/src/lib/admin/StreamsPanel.svelte:181-182`
- **MOVED HERE FROM PHASE 1 (2026-09-20) on the row's own words:** "Frontend-only work, so it waits for a change to the service it belongs to rather than a campaign of its own." The eight orphaned durables it was filed for are GONE (verified 2026-09-18); what is left is the admin streams view flagging an UNEXPECTED durable, which is the FOCUS's opportunistic-frontend rule exactly.

**FE-002 · Six lineage route pages still hand-roll a `lastStatus`/`settled` fetch triple around `$lib/api`, and three of them keep governed rows on screen after a 401**
`lineage, lakehouse-zone` · **MED** · PARTIAL
- *What is left:* The zone now has 11 `.remote.ts` modules, `+page.ts` loads for admin/catalog/stores and zero `fetch(` calls in route `.svelte` files, and `lineage/datasets/+page.svelte:50-55` nulls its rows on 401. Still hand-rolled: 13 files import `$lib/api` directly and 26 files (10 route pages + 16 lib components) carry the `lastStatus`/`settled` pattern. Move the six lineage route pages (columns, datasets, datasets/[name], jobs, jobs/[...job], runs) and `lib/lineage/store.svelte.ts` onto `query` remote functions (`lib/lineage/remote/lineage.remote.ts` exists) or `+page.ts` load so 401 is handled once. Until that lands, `runs/+page.svelte:17-26`, `jobs/+page.svelte:36-45` and `columns/+page.svelte:55-64` must null their rows on 401 instead of keeping the last-good list; the graph store only flips `online` and never clears.
- *Closes when:* No route `.svelte` in the lakehouse zone imports `$lib/api` directly, and an expired session clears rows on every lineage list page.
- *Evidence:* `frontend/microfrontends/lakehouse/src/routes/lineage/runs/+page.svelte:17-26` · `frontend/microfrontends/lakehouse/src/routes/lineage/datasets/+page.svelte:44-56` · `rg -l "from '\$lib/api'" frontend/microfrontends/lakehouse/src → 13 files` · `rg -l 'lastStatus|settled' frontend/microfrontends/lakehouse/src → 26 files`

**FE-005 · No UI reaches the namespace rung's `POST /v1/namespace/{id}/undrop` or its `/tasks` deadline**
`catalog, lakehouse-zone` · **MED** · PARTIAL
- *What is left:* The table rung is shipped: `fetchTableTasks` (`GET /v1/table/{id}/tasks`) and `undropTable` (`POST /v1/table/{id}/undrop`) in `catalog.remote.ts`, rendered by `RecoverCard.svelte` inside `TableDetail.svelte`. The namespace rung has no zone surface: nothing in the lakehouse zone calls `/v1/namespace/{id}/tasks` or `/v1/namespace/{id}/undrop`, although `@rask/api`'s generated client already types both. Add a namespace-level recover surface (deadline + undrop), and a trash listing across objects if per-table-detail is not enough.
- *Closes when:* A cascade-dropped namespace can be recovered from the lakehouse zone without curl, showing its deadline first.
- *Evidence:* `frontend/microfrontends/lakehouse/src/lib/data/remote/catalog.remote.ts:108-116` · `frontend/microfrontends/lakehouse/src/lib/data/table-detail/RecoverCard.svelte` · `frontend/packages/api/src/generated/catalog.ts:897 (namespace undrop typed)` · `grep for `namespace/${…}/(undrop|tasks)` in the lakehouse zone → no matches`

**FE-006 · The project-scoped maintenance policy has an API but no UI**
`catalog, lakehouse-zone` · **MED**
- *What is left:* Build the project policy screen in the lakehouse zone over `POST /v1/project/{id}/policy/{set,describe,delete}` (`policies.py:228/285/296`), which the generated client already types (`frontend/packages/api/src/generated/catalog.ts:1017-1068`). Only the TABLE policy has a surface today: `MaintenanceSection.svelte` wires `setTablePolicy`/`deleteTablePolicy` and nothing calls the project operations. The lakehouse zone has no project route; `routes/catalog/warehouses/[id]` (a warehouse belongs to a project) is the nearest anchor.
- *Closes when:* A lakehouse page sets, shows and deletes a project's maintenance policy through the shipped project-policy operations.
- *Evidence:* `services/catalog/src/catalog/api/v1/endpoints/policies.py:228,285,296` · `frontend/packages/api/src/generated/catalog.ts:1017-1068` · `frontend/microfrontends/lakehouse/src/lib/data/table-detail/MaintenanceSection.svelte:15-18 (table-only)` · `rg 'project.*policy' frontend/microfrontends -> no non-generated hits`

**FE-012 · `home`/`lakehouse` proxy `/api` to a dead `:8001` (`LANCE_BACKEND`) while `compute`/`models`/`studio` proxy to the gateway `:8888` (`VIEWER_BACKEND`)**
`frontend, zone-contract` · **MED**
- *What is left:* Point every proxied zone's vite `/api` dev proxy at one env var and one default (`VIEWER_BACKEND`, `http://localhost:8888`): delete `LANCE_BACKEND` from `home/vite.config.ts` and `lakehouse/vite.config.ts`. Update `zone-contract/src/dev-zone.ts` (which sets `LANCE_BACKEND` for home/lakehouse and `VIEWER_BACKEND` for the rest) to set the one var for all five. Rewrite the `rask-services-fleet` and `rask-frontend` skills and the CLAUDE.md conventions bullet that document the split. The `gateway/P2-devproxy` prerequisite the row cites has no row anywhere in the register; nothing prevents doing this now.
- *Closes when:* `grep -rn LANCE_BACKEND frontend/` returns nothing and every proxied zone's `/api` proxy reads the same variable.
- *Evidence:* `frontend/microfrontends/home/vite.config.ts:5,18 and lakehouse/vite.config.ts:5,22 (`LANCE_BACKEND` → :8001)` · `frontend/microfrontends/{compute,models,studio}/vite.config.ts:5,20 (`VIEWER_BACKEND` → :8888)` · `frontend/packages/zone-contract/src/dev-zone.ts:123-146,188 (sets both vars per zone)` · `grep -n 'P2-devproxy' open_backlog_left.md → only line 9174 (FE-012 itself)`

**FE-007 · The chart ships only the ingress-nginx proxy-read-timeout annotation, inert on k3s's Traefik — the zones' live SSE bell has no edge-level guard there**
`chart, gateway` · **LOW**
- *What is left:* `chart/values.yaml:2326` sets only `nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"` while `ingress.className` is `""` (`:2287`, k3s default Traefik), so the guard is inert locally; `ingress.yaml:22-24` and `docs/architecture/edge-baseline.md:27-40` both say so. Add the Traefik equivalent (a `ServersTransport` / `traefik.ingress.kubernetes.io/*` annotation, or `respondingTimeouts`) under `ingress.annotations`, then run `HOLD_S=270 node scripts/verify_live_stream_timeout.mjs` against the k3s ingress and record the result. Only the runs feed's app-level 20 s keepalive mitigates it today.
- *Closes when:* A 270 s idle live stream through the Traefik edge survives unsevered, measured by `scripts/verify_live_stream_timeout.mjs`.
- *Evidence:* `chart/values.yaml:2287,2326` · `chart/templates/ingress.yaml:22-24` · `docs/architecture/edge-baseline.md:27-40` · `scripts/verify_live_stream_timeout.mjs exists`

**FE-008 · Eight mutation sites do a trailing `await load()` after every write instead of `form` + single-flight + `withOverride`**
`lakehouse, annotator` · **LOW**
- *What is left:* Convert the mutation sites that reload after a write to `form`/`command` + single-flight + `withOverride`, deleting each trailing `await load()`. At HEAD there are 8, not 6: `lakehouse/src/lib/data/WarehouseAdmin.svelte:107,122`, `TableRegistry.svelte:126,176`, `NamespaceRegistry.svelte:149`, `lakehouse/src/lib/admin/DlqPanel.svelte:74`, `annotator/src/lib/projects/ProjectsLanding.svelte:277`, `annotator/src/routes/tasks/[id]/+page.svelte:161`. `withOverride` is used nowhere yet; both zones already ship `.remote.ts` seams to build on. Sequence after FE-002 (the `load`/`query` adoption), which supplies the queries these writes invalidate.
- *Closes when:* `grep -rn 'await load()' frontend/microfrontends` returns zero post-write reloads and each converted site uses a remote form/command with an override.
- *Evidence:* `grep -rn 'await load()' frontend/microfrontends → 8 sites (listed)` · `grep -rln withOverride frontend/microfrontends → none` · `frontend/microfrontends/lakehouse/src/lib/data/remote/*.remote.ts, annotator/src/lib/projects/remote/tasks.remote.ts (existing command/form seams)`

**FE-011 · Two Estate Settings rows (new-project defaults, credentials) are named on `/settings` but have no backing store to write to**
`home-zone, controlplane, catalog` · **LOW** · PARTIAL
- *What is left:* The zone question is settled in code: home owns `/settings` (estate-admin gated in `+layout.server.ts`), serving `/settings/access`, `/settings/audit` and `/projects`. Build the two rows the page lists as unwired: new-project defaults needs the catalog to accept a defaults payload on project creation; credentials needs the OpenBao-backed secret store (never a browser-editable form). No eighth zone is needed; a formal IA ruling was never recorded, so an owner who wants a separate zone must say so.
- *Closes when:* `/settings` renders no `UNWIRED` rows because both surfaces have a store behind them.
- *Evidence:* `frontend/microfrontends/home/src/routes/settings/+page.svelte:7-28 (platform-level settings, three surfaces served, two named-not-built)` · `frontend/microfrontends/home/src/routes/settings/+page.svelte:40-48 (`needs:` strings for defaults and credentials)` · `frontend/microfrontends/home/src/routes/settings/+layout.server.ts:21-33 (estate-admin gate)` · `find frontend/microfrontends -path '*routes*' -iname '*setting*' → home and annotator only`

**FE-013 · The WebGPU atlas is zone-local in explorer instead of hoisted to `frontend/packages/atlas`**
`explorer, annotator` · **LOW**
- *What is left:* Create `frontend/packages/atlas` (zone-agnostic props, no `$app/*` imports, transport injected), move the 14 files in `frontend/microfrontends/explorer/src/lib/atlas/` (AtlasMap.svelte, gpu-scatter.svelte, cross-filter.svelte.ts, mount-atlas.svelte, atlas-send.ts, legend/geometry/colors/grid, gpu-support) into it, re-point the explorer zone, and add the annotator's bulk-labeling embed. `frontend/packages/` holds api, config, dockview, engine, explorer-api, flow, labeling, media-api, ui, zone-contract — no atlas. The annotator references the atlas only in comments (the deep-link bridge), not as an embed.
- *Closes when:* `frontend/packages/atlas` exists, explorer imports from it, and the annotator's bulk-labeling view renders the same selection surface.
- *Evidence:* `ls frontend/packages -> no atlas` · `ls frontend/microfrontends/explorer/src/lib/atlas -> 14 files` · `frontend/microfrontends/annotator/src/lib/labeling/review-selection.svelte.ts:3 (comment-only reference)`

**FE-009 · No ruling says whether a catalog-scoped governance page returns to the lakehouse zone or home `/settings/access` stays the single governance surface**
`lakehouse-zone, home-zone, catalog` · **LOW** · PARTIAL
- **blocked:** Owner IA ruling: does a project/catalog-scoped governance page return to the lakehouse zone, or does the estate-scoped explorer at home `/settings/access` stay the only governance surface?
- *What is left:* The trace is done: `857e9d5c` moved Access/Audit to `/lakehouse/governance/*`, `4a2177c0` replaced the four tabs with the query-driven access explorer, the #105 port moved that explorer to `home/src/routes/settings/access/+page.svelte` because it reads the whole tuple store across projects, and `e59549b1` swept the last two `governance/` files without naming it. Take the ruling and record it in `docs/DECISIONS.md`. If the lakehouse gets a page back, it is project-scoped and built over the catalog's project registry, not a second copy of the estate explorer.
- *Closes when:* `docs/DECISIONS.md` carries the ruling and the chosen surface exists.
- *Evidence:* `frontend/microfrontends/home/src/routes/settings/access/+page.svelte:4-6 (estate surface, ported from /lakehouse/governance/access)` · `git log --oneline -- frontend/microfrontends/lakehouse/src/routes/governance (857e9d5c, 4a2177c0, 5e942940, e59549b1)` · `grep -n -i governance docs/DECISIONS.md → no surface ruling`


## LOW PRIORITY

**XC-043 · Ten docs still describe the orchestrator, `core_api`/`search_api`/`volumes_api`, `packages/htr` or `/default/<zone>` bases in their body, tombstone or not**
`docs` · **LOW** · PARTIAL
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** 5 of its 7 stale files are MFE docs. `microservices.md` and `system-overview.md` fold into a lakehouse docs pass opportunistically. The work is unchanged; only the label is, so phase 1 stops claiming it.
- *What is left:* Rewrite the bodies, not only the headers. Tombstone admonitions are in place on `system-overview.md`, `microservices.md`, `packages/htr.md`, `frontend-conventions.md` and `frontend-microfrontends.md`, but the prose under them still presents the dead things as live (`microservices.md:59-60` lists volumes-api/search-api as services, `frontend-microfrontends.md:94-95` diagrams `/default/` bases, `components/ui.md:8`, `components/progress.md:191,258`, `layout.md:64` names `media`/`train` zones, `frontend-conventions.md:55`). The gate grep returns 10 files at HEAD, not 6: `DECISIONS.md`, `deployment.md` and `packages/htr.md` hit only as explicit tombstones; the other seven carry live stale text. `docs/reference/htr.md` no longer exists. Keep the zensical nav gate green (`zensical.toml` serves layout, deployment, microservices, frontend-microfrontends, system-overview, packages/htr, components/ui, DECISIONS).
- *Closes when:* `grep -rl "core_api\|search_api\|volumes_api\|packages/htr\|/default/" docs/ --exclude-dir=superpowers --exclude=lance-ns-merge.md --exclude=OPEN-WORK.md` returns only files whose every hit is an explicit tombstone, with the nav gate green.
- *Evidence:* `grep -rl … docs/ → 10 files (DECISIONS 1, frontend-microfrontends 26, layout 1, packages/htr 1, deployment 1, frontend-conventions 8, microservices 10, system-overview 10, progress 2, ui 2)` · `docs/architecture/microservices.md:59-60,185-200 (live prose for volumes-api/search-api/orchestrator, 'Auth: none')` · `docs/architecture/frontend-microfrontends.md:94-95; docs/components/ui.md:8` · `zensical.toml:22-59`


**XC-040 · The dangling-locator gate checks pointers INTO a register but nothing gates a register's sidecar outliving it**
`e2e` · **LOW**
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** A gate over register sidecars with ZERO instances — measured, it would guard nothing. The work is unchanged; only the label is, so phase 1 stops claiming it.
- *What is left:* `tests/unit/test_no_locator_names_a_deleted_register.py` has two tests (locator into a gone register; carried list only shrinks) and its `_register_exists` at :52-53 accepts a `.findings.json` as a valid target, but no test fails when an `open_*.findings.json` (or similar sidecar) exists with no `open_*.md` beside it. Add that assertion over `git ls-files`. Decide how it classifies the tracked, uncited root `open_stack.html` (no `open_stack.md` exists) — sidecar or standalone document — so the gate is not vacuous: at HEAD no `*.findings.json` exists.
- *Closes when:* A test fails on a root `open_*` sidecar whose register is gone, and it passes at HEAD with `open_stack.html` classified explicitly.
- *Evidence:* `tests/unit/test_no_locator_names_a_deleted_register.py:52-53,64,85` · `find . -maxdepth 2 -name '*.findings.json' → none` · `git ls-files open_stack.html → tracked; no open_stack.md; grep -rn open_stack.html → uncited`


**LH-168 · The live `lance-secrets` Dapr Component is two scopes short of the chart and nothing detects Component-scope drift**
`chart, viewer, search` · **LOW**
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** Its two missing scopes are VIEWER and SEARCH, both on the do-not-work list. The work is unchanged; only the label is, so phase 1 stops claiming it.
- *What is left:* The chart's `lance.secretScopes` (`chart/templates/_helpers.tpl:1332-1357`) grants every `explorer.services` app-id (search, viewer) and the `lance-secrets` Component (`chart/templates/dapr-component.yaml:317`) renders them; helm does not re-patch an unchanged field, so out-of-band drift survives every upgrade. Re-apply the rendered Component so the live scopes match, then add a render-vs-live diff on Dapr Component scopes — `scripts/k3s-pins.sh --check-only` (`make k3s-stem-check`, `Makefile:766-776`) compares image stems only. Live drift (13 rendered vs 11 live) not re-verified (no cluster access).
- *Closes when:* The live `lance-secrets` scopes equal the rendered set and a pre-upgrade check refuses on Component-scope drift.
- *Evidence:* `chart/templates/_helpers.tpl:1332-1357` · `chart/templates/dapr-component.yaml:317-320` · `Makefile:766-776 (k3s-stem-check is image-stem only)` · ``grep -in 'component\|scopes' scripts/k3s-pins.sh` → image parsing only`


**XC-041 · `make seed-dev` chmods the whole corpus root, hardcodes one release name, host path and ports, and seeds labeling doc ids with a literal `dataset_version`**
`scripts` · **LOW**
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** A dev seeding script; it touches no lakehouse service. The work is unchanged; only the label is, so phase 1 stops claiming it.
- *What is left:* `scripts/seed_demo_corpus.py:344-362` `_make_world_readable(root)` walks `root.rglob("*")` and chmods everything under the corpus root rather than the paths the run wrote, and does nothing for the re-seed case. `scripts/seed_dev_estate.sh` hardcodes `rask-search`/`rask-viewer`/`rask-annotator`/`rask-minio`/`rask-catalog` (:28,34,48,113-114,126,128), the host path `/home/gabriel/media-corpus` (:22) and ports 19900/12433. `scripts/seed_labeling_task.sh:124-126` hardcodes `fe00cd746463ad2c/{0,1,2}` keys with `"dataset_version": 1`. Track and chmod only what the run wrote; take release name, namespace, host path and ports as parameters; derive the labeling keys and `dataset_version` from the live fixture and the catalog's table version, the way the corpus half already reads `MEDIA_DB` and `/v1/me`.
- *Closes when:* A seed against a differently named release on another host, run twice, leaves only its own files readable and a labeling task whose keys and version match the fixture it read.
- *Evidence:* `scripts/seed_demo_corpus.py:344-362` · `scripts/seed_dev_estate.sh:22,28,34,113-114,126,128` · `scripts/seed_labeling_task.sh:124-126` · `Makefile:527-528 (seed-dev → scripts/seed_dev_estate.sh)`


**XC-019 · The Lance `TableWriter` seam has no create-if-absent verb, so the first annotation save on a fresh estate has no table to merge into**
`service-kit, annotator` · **MED**
- **MOVED FROM PHASE 1 (2026-09-20, backlog audit).** The seam is `service-kit`, but only the ANNOTATOR reaches it, and the annotator is on the do-not-work list. The work is unchanged; only the label is, so phase 1 stops claiming it.
- *What is left:* Add a create-if-absent verb to the `TableWriter` Protocol and its three implementations in `service_kit/lancekit/writer.py` (today: `merge_upsert`, `merge_insert_only`, `delete` only). Call it from `annotator/annotations/save.py` before `reader.table_version()` at :86 and the merge; the annotator's only `create_table` is the publish saga's in `projects/lakehouse.py`, which does not create the annotations table. Pin with a RED test that saves into an estate where the annotations table does not exist. Do not widen an except clause instead.
- *Closes when:* A save against a project with no annotations table creates it and commits, under a test.
- *Evidence:* `packages/service-kit/src/service_kit/lancekit/writer.py:44-51 (Protocol: three verbs), :98-108, :118-120, :131-134 (implementations)` · `services/annotator/src/annotator/annotations/save.py:82-86 (`open_reader` then unguarded `reader.table_version()`)` · `grep -rn 'create_table' services/annotator/src → only projects/lakehouse.py:161-294`


**LOW-002 · Consensus replicas mint `{gid}-r{k}` task ids that can exceed the 64-char task route bound, wedging that project's publish**
`annotator` · **MED** · PARTIAL
- *What is left:* The draft-sharing half is addressed by construction: each replica is its own `Task` with its own actor and draft (project_events.py:543-553; actor.py:74 `DRAFT_KEY`, :440), and `_refuse_second_replica` (tasks.py:186) allows one replica per annotator per group. The wedge remains: the send door accepts a client `task_id` up to 64 chars (project_events.py:166), replicas are minted `f"{group_id}-r{k}"` (:548, `k` ≤ `consensus_n` ≤ 5), and every task route bounds ids at 64 (tasks.py:92, project_events.py:327) — so a 62-64 char client id yields replicas no route can address. Bound the client id on a consensus send to 61, or hash it; RED-first with the wedged-publish case as the failing test. `annotator` is on the register's do-not-work list.
- *Closes when:* A consensus send with a 64-char client `task_id` either refuses at the door or mints replica ids every task route accepts, and a test pins it.
- *Evidence:* `services/annotator/src/annotator/api/v1/endpoints/project_events.py:166 `task_id: str | None = Field(default=None, max_length=64, ...)`; :528 `group_id = item.task_id or new_id()`; :548 `task_id=f"{group_id}-r{k}"`` · `services/annotator/src/annotator/api/v1/endpoints/tasks.py:92 `TaskId = Annotated[str, Path(min_length=1, max_length=64, ...)]`` · `services/annotator/src/annotator/projects/actor.py:74 `DRAFT_KEY = "draft"` (per-task actor state); tasks.py:186 `_refuse_second_replica`` · `services/annotator/src/annotator/projects/models.py:335 `consensus_n: int = Field(default=1, ge=1, le=5)``

**LOW-003 · The annotator canvas still saves through the per-row Lance write path (`POST /annotations/{doc}/{speech}/{chunk}`), one dataset version per state flip**
`annotator, explorer` · **MED** · PARTIAL
- *What is left:* Sequenced after the task draft/publish path (S7/S8) is proven live. Point the canvas save (`review-selection.svelte.ts:36` and `BulkGrid.svelte:250` build `/api/annotations/${key}`) at `PUT /tasks/{id}/draft` (`tasks.remote.ts:104`); the `?task=`-opened snapshot-into-draft half already exists. Write a fresh draw → draft → publish-with-shapes drive script against the cluster — the row's `frontend/microfrontends/annotator/drive2.tmp.mjs` does not exist. Then delete `services/annotator/src/annotator/annotations/save.py` (route at :55), `tags.py`, `versions.py` and `commit.py:check_base_version_value` (:30), keeping the Arrow-IPC read.
- *Closes when:* Shapes drawn on the canvas travel into a publish via the task draft, and the per-row write modules are deleted.
- *Evidence:* `services/annotator/src/annotator/annotations/save.py:55` · `services/annotator/src/annotator/annotations/commit.py:30` · `frontend/microfrontends/annotator/src/lib/labeling/review-selection.svelte.ts:36; src/lib/projects/remote/tasks.remote.ts:104` · `ls frontend/microfrontends/annotator/drive2.tmp.mjs — absent`

**LOW-009 · The annotator publish saga emits no `annotation.project.published` control event after a successful publish**
`annotator, catalog, lineage, service-kit` · **MED** · PARTIAL
- *What is left:* The publish path exists as a token-keyed idempotent saga, not a Dapr Workflow, by design: `projects/saga.py` (collect accepted drafts, `build_plan`, create-by-derived-id, tag `publish-<publish_id>`, record, fire `publish_succeeded`), the catalog REST publisher in `projects/lakehouse.py`, and crash-after-each-step tests in `tests/unit/test_publish_saga.py`. Do not rebuild it as a Dapr Workflow. Unshipped: the control event — `ControlAction` has no published-project action and neither `saga.py` nor `lakehouse.py` calls `emit_control`. Add the action to `service_kit/control_events.py`, emit it after `record_publish` naming the project's members per the rask-notifications contract, and decide whether the catalog's `table_published` (`endpoints/publication.py:326`) already suffices for #102/#125 consumers.
- *Closes when:* A successful publish lands one control event that a consumer can key `query.live` on, pinned by a saga test.
- *Evidence:* `services/annotator/src/annotator/projects/saga.py:171-290` · `services/annotator/src/annotator/projects/lakehouse.py:276-309` · `tests/unit/test_publish_saga.py:261,420` · `packages/service-kit/src/service_kit/control_events.py:36-101 (no published-project action; grep emit_control in saga.py/lakehouse.py: none)`

**LOW-017 · `proxyServeInfer` buffers every uploaded byte with `await request.arrayBuffer()` in a SvelteKit pod sized for rendering HTML**
`flows, frontend, storage` · **MED**
- *What is left:* Add a TTL scratch bucket, presign a PUT from the browser straight to RustFS (credentials from the Dapr secret store, never env), and add an `objectRef` payload kind to `services/flows`. The buffering site is `frontend/packages/api/src/serve-proxy.ts:106` (shared by the studio zone's `/api/infer` route), and the studio executor also reads whole files with `cfg.file.arrayBuffer()` at `executor.ts:326`. `BODY_SIZE_LIMIT` is set per zone in `chart/templates/frontends.yaml:150`, which bounds the damage but rules out audio and video. No `objectRef` exists in `services/flows/src`.
- *Closes when:* An upload reaches RustFS without passing through a zone pod and `services/flows` accepts an `objectRef` payload.
- *Evidence:* `frontend/packages/api/src/serve-proxy.ts:83,106` · `frontend/microfrontends/studio/src/lib/flows/executor.ts:326` · `grep -rn 'objectRef\|object_ref' services/flows/src → empty` · `chart/templates/frontends.yaml:141-150`

**LOW-018 · The studio node library shows every Ray-dashboard-reported Serve app's status verbatim, so an app with no ingress route appears callable**
`flows, compute, frontend` · **MED**
- *What is left:* `getServeApps` (studio serve.remote.ts:14-27) reduces `/api/serve/applications/` to `{name, app, status}` and passes the dashboard's `status` through; palette.ts:124 and ModelNode.svelte:22 render `${name} · ${status}`. Nothing in services/flows probes reachability. Either probe each app's ingress route from `services/flows` and mark unreachable apps, or stop rendering the dashboard status as a callability signal. `flows` is on the register's do-not-work list.
- *Closes when:* The Model node's picker distinguishes a reachable app from one the ingress does not route, or shows no callability claim.
- *Evidence:* `frontend/microfrontends/studio/src/lib/flows/remote/serve.remote.ts:20 `status: a.status`` · `frontend/microfrontends/studio/src/lib/flows/palette.ts:124 `blurb: \`/${a.app} · ${a.status}\``; nodes/ModelNode.svelte:22` · `services/flows/src/flows/catalog.py:59 and models.py:138 — options come from `/api/serve/applications/`; no probe in services/flows/src`

**LOW-023 · No lines FTS surface exists, and a 'lines' gold contract with text/geometry/confidence columns is a workload-shaped tier schema the estate forbids**
`search, catalog, medallion, viewer` · **MED** · **REWRITTEN — the original ask would be wrong**
- **blocked:** The HTR gold wave (LH-010 / D2c) publishing the HTR runner's lines output as a governed table.
- *What is left:* Do not build a lines table as a gold contract or add a `lines` name to search, viewer or catalog: medallion/schemas holds only tier.py and events.py and every governed row is {id, payload, stage, lineage, source_rowid} with an opaque payload. The modality-agnostic surface already exists — the search service derives modes generically from a dataset's declared search bindings (fts is always present) and the viewer serves DatasetRegistry descriptors. When the HTR runner publishes its lines table, declare it as a search binding and a registry descriptor, with thumb crops as a blob column. That is the whole remaining step.
- *Closes when:* An HTR-published lines table answers /api/explorer/search?mode=fts through the generic bindings with no lines-specific code in a shared service.
- *Evidence:* `grep -rni '"lines"' services/search/src services/viewer/src -> no matches` · `services/search/src/search/services/spec.py:22-44 (modes derived generically; FTS always)` · `services/viewer/src/viewer/api/v1/endpoints/datasets.py:20,52 (DatasetRegistry)` · `services/medallion/src/medallion/schemas/ (tier.py + events.py only)`

**LOW-027 · The explorer trio reads the media corpus off a mounted volume rather than the catalog**
`explorer, viewer, search, annotator, chart` · **MED**
- **blocked:** Owner decision, taken against the destination cluster: PVC (`explorer.corpus.mode=pvc`) as the permanent answer, the governed-bucket path (`explorer.corpusS3Root`), or #103 catalog-registered project tables.
- *What is left:* `explorer.yaml:113-115` still derives `MEDIA_DB_ROOT`/`MEDIA_DB`/`MEDIA_DESCRIPTOR_DIR` from `explorer.corpusMountPath`, with `explorer.corpus.mode` defaulting to `emptyDir`. A third shape now exists: `explorer.corpusS3Root` (default empty) renders `MEDIA_S3_ENDPOINT`/`MEDIA_S3_DB_ROOT` for all three readers (`explorer.yaml:202-208`, read by `service_kit/media/config.py`) — a governed bucket, not catalog tables. `docs/DECISIONS.md` carries no ruling. After the ruling either land the catalog-registered tables and drop `corpus.mode`, or record the chosen mode as permanent and drop #103.
- *Closes when:* Either the three readers resolve the corpus through the catalog and `explorer.corpus.mode` is gone, or DECISIONS.md records the volume/bucket mode as permanent.
- *Evidence:* `chart/templates/explorer.yaml:113-115,202-208` · `chart/values.yaml:1775,1791-1792` · `packages/service-kit/src/service_kit/media/config.py (MEDIA_S3_DB_ROOT reader)`

**LOW-007 · The annotator's two named proofs — concurrent claims yield one 200 + one 409, and a fired lease reminder returns the task to `unassigned` with its draft intact — are pinned by no test**
`annotator` · **LOW** · PARTIAL
- *What is left:* Both actors exist on `lance-statestore` against the OIDC-verified subject: `AnnotationProjectActor` (project doc + task index, `projects/project_actor.py`), `AnnotationTaskActor` (task state + `LEASE_REMINDER`, `projects/actor.py`), `tenant_actor.py`, and `api/security.py` builds `current_subject` from `make_auth_deps` — no `X-User` header is read, so the §10 blocker is gone. Write the two proofs: two concurrent claims on one task → one 200 + one 409 (the existing 409 tests in `test_send_writes_the_index_once.py:207,222` cover item ownership at send, not claims), and `receive_reminder` (actor.py:466-480) → state `unassigned` with the draft still present and `assignee` cleared. No test under services/annotator/tests names `receive_reminder` or `lease_expired`.
- *Closes when:* Both scenarios are pinned by tests in services/annotator/tests and pass.
- *Evidence:* `services/annotator/src/annotator/projects/actor.py:466-480` · `services/annotator/src/annotator/projects/project_actor.py:1-25` · `services/annotator/src/annotator/api/security.py:40-53` · `rg -n 'lease_expired|receive_reminder' services/annotator/tests → no hits`

**LOW-010 · `publish.py` stamps the PROJECT ontology instead of each task's captured one, and `modality` is cross-checked against nothing**
`annotator` · **LOW** · PARTIAL
- *What is left:* `template_kind` is gone (zero hits in services/annotator/src). Two clauses remain. `publish.py:432` stamps the PROJECT's ontology (`ontology=project.ontology.model_dump(...)`) into the run facet although each task captures its own at send (models.py:265-270); stamp the captured ones, or report mixed when they differ. `LabelOntology.modality` (ontology.py:150) is read nowhere, so an `audio` ontology accepts image items; cross-check it against `MediaRef.kind` at send. Shape `source`/`model_version`/`confidence` (publish.py:258-260) remain client-supplied; the "Server-stamped provenance" comment now sits below them and scopes to the fields that follow.
- *Closes when:* The run facet carries each task's captured ontology and a send whose item kind contradicts the ontology's modality is refused.
- *Evidence:* `services/annotator/src/annotator/projects/publish.py:258-260, :432` · `services/annotator/src/annotator/projects/models.py:265-270 (per-task captured ontology)` · `services/annotator/src/annotator/projects/ontology.py:150 (modality declared)` · ``grep -rn '\.modality' services/annotator/src` → no reads; `grep -rn template_kind` → none`

**LOW-011 · Batch-labeling submit has no runner behind it — `runners.jobsUrl: ""` in every default deploy**
`annotator, chart, explorer` · **LOW** · **REWRITTEN — the original ask would be wrong**
- *What is left:* The gap is real: `chart/values.yaml:1857` ships `jobsUrl: ""`, and the annotator service's `POST /apply` and `GET /{job_id}` answer `backend="mock"` when `jobs_url` is empty (`jobs.py:110-123`), forwarding to a remote `{job_id, status}` contract only when set. But the ask names a "compute service's Ray job door" that does not exist — `compute/routes.py` exposes only `GET /jobs` and `GET /jobs/{id}/logs` — and adding a raw Ray-submit door there creates an ungoverned fourth ingest door outside the medallion's three token-guarded write doors and its lineage/idempotency path. Route the batch-labeling submit through a medallion-governed door (`/ingest-media` or a stage trigger) instead, keep the annotator's status read, and witness one submitted batch job in-cluster.
- *Closes when:* A batch-labeling submit from the annotator reaches a governed medallion door, runs as a real job, and its status reads back `backend="remote"` in-cluster.
- *Evidence:* `chart/values.yaml:1857 (jobsUrl: "")` · `services/annotator/src/annotator/api/v1/endpoints/jobs.py:98-123 (mock branch when jobs_url empty)` · `services/compute/src/compute/routes.py:39-44 (GET-only job routes)` · `frontend/microfrontends/annotator/src/lib/viewer/annotator.svelte.ts:1407-1412 (HONEST MOCK submit)`

**LOW-012 · The annotator's wire and state still say `project` where the ruled unit of work is a labeling TASK**
`annotator, service-kit` · **LOW**
- *What is left:* Layer 2 of the rename has not happened: `type annotation_project` (`packages/service-kit/src/service_kit/governed/auth/model.fga:530`), `AnnotationProjectActor` + `AnnotationTaskActor` (`services/annotator/src/annotator/main.py:23-24,93-94`), `prefix="/projects"` and `"/tasks"` (`api/v1/endpoints/projects.py:39`, `members.py:37`, `tasks.py:84`, `project_events.py:67`) and `can_create_annotation_project` (`projects/models.py:317-318`) are unchanged — 88 hits across annotator, service-kit and chart. Run the rename as its own red-first slice with a state migration for existing actor ids and FGA tuples, updating `tests/unit/test_actor_proxy_names.py` (root `tests/unit`, not `services/annotator/tests`) — not a find-and-replace.
- *Closes when:* The FGA type, actor ids and routes name a labeling task, and existing actor state and tuples migrate under a test.
- *Evidence:* `packages/service-kit/src/service_kit/governed/auth/model.fga:530` · `services/annotator/src/annotator/main.py:23-24,93-94` · `services/annotator/src/annotator/api/v1/endpoints/projects.py:39, tasks.py:84` · `tests/unit/test_actor_proxy_names.py (exists at root tests/unit)`

**LOW-014 · No send surface emits an audio item, and reading order is an int attribute with no ordering tool**
`annotator, explorer, labeling` · **LOW** · PARTIAL
- *What is left:* Character spans exist: the ontology's `span: true` maps to the `text` tool on the wire, `char_start`/`char_end` ride `@rask/labeling/annotations-client.ts:37-38`, and the annotator edits them in its document text lane — they belong there, not as a `@rask/engine` canvas `Shape`, so do not add a span tool to the engine. A relations editor exists in the annotator viewer. Remaining: make `explorer/src/lib/components/SendToProjectDialog.svelte:109` emit `media.kind` from the corpus descriptor instead of the hardcoded `{ kind: 'image' as const }` (and widen `projects.ts:58`'s type), then drive one audio item claim→submit→publish end to end. Add an `order` tool with a sequencing editor behind the `order` int attribute (`templates.ts:51`). Relations-editor behaviour was not exercised this session.
- *Closes when:* One audio item has gone through claim→submit→publish, and reading order is authored by a tool rather than typed as an integer.
- *Evidence:* `frontend/microfrontends/explorer/src/lib/components/SendToProjectDialog.svelte:109` · `frontend/microfrontends/explorer/src/lib/projects/projects.ts:58` · `frontend/microfrontends/annotator/src/lib/projects/task-yaml.ts:27,48-49 (span capability, text tool)` · `frontend/microfrontends/annotator/src/lib/projects/templates.ts:41,51 (relations in the ontology; READING_ORDER int attr)`

**LOW-019 · The flows Dapr Workflow lane has not been observed end to end; runs degrade to the inline executor whenever the scheduler fails**
`flows` · **LOW** · PARTIAL
- *What is left:* The durable lane is already first choice: `services/flows/src/flows/routes.py:189-220` schedules through `DaprFlowScheduler` whenever a scheduler exists and falls back to inline only on a confirmed-not-created failure (logged at exception level), so 'make it the default lane' needs no code. Drive a flow against the cluster, confirm `record_run(DURABLE)` fires and the workflow instance's state transitions are visible end to end, and fix whatever makes the lane degrade (the known trap is `lance-statestore` not scoped to app-id `flows`). Live behaviour was not verified in this session.
- *Closes when:* A run against the cluster is recorded as DURABLE and its per-node transitions are read back from the workflow history.
- *Evidence:* `services/flows/src/flows/routes.py:189-220` · `services/flows/src/flows/routes.py:220 — the inline fallback log line`

**LOW-020 · Studio's node palette ships its own 11-kind registry while `services/flows` declares 5 kinds, so the two vocabularies have already drifted**
`flows, frontend/studio` · **LOW**
- *What is left:* `services/flows/src/flows/catalog.py` declares `CATALOG` (5 v0 kinds) served at `GET /flows/catalog` (`routes.py:82-90`); studio reads it only for a status chip (`serve-contract.ts:21-27`: "the palette is client-side") while `lib/flows/node-types.ts:17-29` maps 11 kinds (image, text, dataset, prompt, model, mcp, alto, extract, regex, compare, inspect). Have the studio palette fetch the catalog from flows and delete the client-side vocabulary, reconciling the 6 extra kinds into the server catalog first. `catalog.py` also names `/htrflow` in shared-service prose — a modality name in a shared seam to remove on the way.
- *Closes when:* The studio palette renders kinds it fetched from `/flows/catalog`, and no node-kind list exists in `frontend/microfrontends/studio` beyond the component map.
- *Evidence:* `services/flows/src/flows/catalog.py:1-9` · `services/flows/src/flows/routes.py:82-90` · `frontend/microfrontends/studio/src/lib/flows/node-types.ts:17-29` · `frontend/microfrontends/studio/src/lib/flows/serve-contract.ts:21-27`

**LOW-021 · Flows has no IIIF loader node, no bucket attach and no run streaming**
`flows, frontend, storage` · **LOW**
- *What is left:* `services/flows/routes.py` exposes `/catalog`, `/validate`, a run POST, `/runs/{id}` and `/runs/{id}/terminate` and nothing streams; no IIIF code exists in flows or the studio zone; no presigned lane exists in any service or `@rask/api`; the studio zone reads neither `/api/explorer/object*` nor a bucket. Add an allowlisted IIIF fetch proxy plus a manifest→canvas→image node; wire bucket load over the governed `/api/explorer/object*` read; add SSE to `services/flows` with a per-node progress surface in studio. The upload half of bucket attach depends on a presigned-upload lane that does not yet exist.
- *Closes when:* A studio flow can load from IIIF and from a bucket, and a running flow shows per-node progress before it completes.
- *Evidence:* `services/flows/src/flows/routes.py:82-292` · `rg -i iiif services/flows/src frontend/microfrontends/studio/src → 0` · `rg -i presign services/*/src frontend/packages/api/src → 0`

**LOW-024 · The EAD `archive_catalog` table has no governed landing and is served by nothing**
`search, catalog, medallion` · **LOW**
- *What is left:* Nothing outside docs/ references `archive_catalog`; `scripts/harvest_ead.py` (Makefile:520) only downloads OAI-PMH XML. Land the EAD rows through the catalog — via `POST /produce` or a sealed `runners/<workload>`, never a `scripts/` CLI and never a protocol-specific ingest door — with a dataset descriptor declaring an `FtsBinding` (packages/service-kit/src/service_kit/lancekit/descriptor.py:80) and `filterable` (descriptor.py:104) naming `archive_code` and the date fields. The search router already selects any descriptor-declared dataset via `?dataset=` (services/search/src/search/api/v1/router.py:7-9,136), so no search-side change is needed.
- *Closes when:* `/api/explorer/search?dataset=archive_catalog&mode=fts&archive_code=<x>` returns EAD hits from a catalog-governed Lance table.
- *Evidence:* `rg -n 'archive_catalog' --glob '!open_backlog*' --glob '!docs/**' . → no hits` · `Makefile:517-521` · `packages/service-kit/src/service_kit/lancekit/descriptor.py:80,104` · `services/search/src/search/api/v1/router.py:7-9,136`

**LOW-026 · Search binds to ONE declared `row_table` keyed by the identity triple — no table chooser, no non-identity joins, no external-pointer declaration**
`search, viewer, explorer, service-kit` · **LOW**
- *What is left:* `service_kit/lancekit/descriptor.py:101` declares `row_table: str` (single) and its validator (`:251-257`) checks only that table and the identity fields; `viewer/api/v1/endpoints/system.py:98,150` and `viewer/api/security.py:126` consume it as one table. Extend the descriptor to declare multiple row tables plus external pointers, add a table chooser to the explorer search surface, and support a join key other than the identity triple. Explorer/viewer are deprioritised: the corpus they read is a mounted volume, not catalog-governed tables.
- *Closes when:* A dataset descriptor with two row tables and an external pointer validates, and the explorer search surface can choose between them.
- *Evidence:* `packages/service-kit/src/service_kit/lancekit/descriptor.py:101,251-257` · `services/viewer/src/viewer/api/v1/endpoints/system.py:98,150` · `services/viewer/src/viewer/api/security.py:126`

**LOW-016 · COCO/YOLO/CSV/HF export serializers do not exist, and the P7c exporter service they belong in does not exist**
`annotator, exporter (P7c)` · **LOW** · PARTIAL
- **blocked:** Owner decision: schedule the export serializers, and stand up the P7c exporter service they must live in (ruling R4 puts serialization in a separate microservice)?
- *What is left:* Once scheduled and once a P7c exporter service exists, add the projection functions there (ALTO 4.4 first, then COCO/YOLO/CSV/HF), never inside the annotator. The managed label taxonomy half is done: `services/annotator/src/annotator/projects/ontology.py` is the labeling task's one definition (taxonomy, tools, attributes, relations) and closed-set label enforcement reads it. No `exporter` directory exists under `services/`.
- *Closes when:* An exporter service serializes an annotation project to at least ALTO 4.4 and one of COCO/YOLO/CSV/HF, with no second export path in the annotator.
- *Evidence:* `services/annotator/src/annotator/projects/ontology.py:1-9 (taxonomy shipped)` · `ls services → no exporter` · `grep -rni 'coco\|yolo' services/annotator/src frontend/microfrontends/annotator/src → no hits`

**LOW-022 · `https://dev-kuberay.ra.se/htr/transcribe` answers a bare `text/plain 404` — the external Serve HTTP ingress exposes no `/htr` route**
`flows, compute` · **LOW**
- **blocked:** Ops on the external dev-kuberay cluster adding the `/htr` prefix route to its Serve ingress (as `dev-kuberay.ra.se/gemma-31b/v1` has) — not code in this repo
- *What is left:* The studio's model node POSTs `{serve_url}/{app}` (services/flows/src/flows/config.py:36) with the app name the user picks, so the repo side is config-driven and needs no change. The external route is the fix. Not re-probed this session (no cluster contact), so the 404 is the row's own measurement, not a fresh one. Note rask's own chart routes its workload at `/htrflow` (chart/values.yaml:2233), so verify the external app's name before asking for `/htr`.
- *Closes when:* `POST https://dev-kuberay.ra.se/htr/transcribe` with raw image bytes returns ALTO XML.
- *Evidence:* `services/flows/src/flows/config.py:35-36 `serve_url` — `A model node POSTs to {serve_url}/{app}`` · `chart/values.yaml:2231-2233 `- name: htrflow` / `routePrefix: "/htrflow"` (rask's own cluster, not the external one)`
- *Confidence LOW* — re-measure before acting on this row.

**LOW-028 · No opaque-asset FGA type exists; the model registry authorizes on `table:models$<model>`**
`catalog, openfga` · **LOW**
- **blocked:** Owner ratification of the rung's shape: a governed opaque blob type with no format tag, schema interpretation or data ops
- *What is left:* `endpoints/models.py:9,48,106-109` lists and checks models as `table:` objects and `model.fga` has no blob/asset type; `docs/DECISIONS.md` carries no entry. After ratification, add the distinct FGA type and repoint the registry's `list_objects`/checks off `table`. LANCE-ONLY constrains the shape (opaque blob, no format tag) and does not itself authorize the rung.
- *Closes when:* `model.fga` declares the asset type and `models.py` no longer queries `object_type="table"`.
- *Evidence:* `services/catalog/src/catalog/api/v1/endpoints/models.py:9,48,106,109` · `packages/service-kit/src/service_kit/governed/auth/model.fga:41-530` · `grep -i 'opaque|K-F9' docs/DECISIONS.md: no hits`


## Dropped as ALREADY DONE by the audit

Re-measured at HEAD and found shipped. Listed so a reader who remembers the row can see where it went, not to keep a changelog.

- **CP-026** — `GET/POST /stage-runners/{stage runner}/stages/{instance_id}` ignores both path parameters
- **CP-028** — Dapr `daprstate` workflow-history rows accumulate with no retention and no alert
- **FE-001** — No in-tab browser memo for /api/atlas/points, so the 6.6 MB projection refetches on every mount and Text/Visual toggle
- **FE-003** — Lakehouse panels still `setInterval`-poll and four admin surfaces make zero requests instead of riding a live lineage feed
- **FE-004** — Every lakehouse lineage window render already passes `summary: true`
- **FE-010** — `/projects/<id>` is built in the home zone with the hierarchy graph on it
- **LH-014** — The provenance recipe (`stamp_stage`, `source_rowid`, the tier contract) is written down
- **LH-049** — Table descriptions are unbound to the catalog object
- **LH-060** — Nothing detects a table stranded between the Lance write and the ownership grant — a catalog object with ZERO FGA tuples is invisible to the drift rep
- **LOW-001** — services/annotator keys ownership on a client-settable `X-User` header
- **LOW-004** — The annotator projects entities and the pure transition machine with its illegal-transition, lease-holder and self-review rules are missing
- **LOW-005** — The `annotation_project` FGA type and `can_create_annotation_project` are missing from `model.fga`
- **LOW-006** — services/annotator/projects/publish.py lacks PUBLISHED_LABELS_SCHEMA and a published-table builder
- **LOW-008** — No HTTP surface for annotation projects/tasks/drafts behind the `annotation_project` FGA doors
- **LOW-013** — S9 — the annotator zone lands on the refused `DataSelection.svelte` gallery
- **LOW-015** — `GET /projects/{project_id}/tasks` honours `limit` and `cursor`
- **LOW-025** — `GET /api/search` silently ignores `dataset` and `mode`
- **XC-024** — With frontend.oidc.enabled false, locals.authEnabled is false and the serve-proxy 401 guard never fires, so anonymous callers reach GPU inference
