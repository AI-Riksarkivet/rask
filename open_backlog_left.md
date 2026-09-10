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

**264 open items**, deduped from 325 raw rows mined out of the seven files above.

| Phase | Items | High |
| --- | --- | --- |
| **1 · Lakehouse** (catalog, lineage, medallion, maintenance) | 117 | 21 |
| **1 · Cross-cutting** (service-kit, storage, chart, build, tests) | 51 | 9 |
| **2 · Compute** (compute, ingest, ray-kit) | 31 | 5 |
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

**LH-122 · A namespace listing filtered to empty is indistinguishable from a namespace with no tables**
`catalog, lakehouse` · low

- *Why open:* `GET /v1/namespace/{id}/table/list` returns `{"context": {"authorization_truncated": "true"}, "tables": []}` when the reader holds no grant on the tables under it — while `POST /v1/table/{id}/describe` on a table in that same namespace answers REGISTERED. Measured 2026-09-10: `acme-silver` and `lakehouse-silver` both listed `[]` for an estate admin while `acme-silver$features` and `lakehouse-silver$features` were registered with bytes on disk. The truncation flag is present and correct — the catalog is not lying — but it rides `context`, which a reader scanning `tables` never looks at. This cost a false diagnosis in the same session: an empty list read as "the cascade registers nothing" and LH-015 was nearly re-opened on it.
- *Closes when:* Decide whether a filtered listing should say so where it is READ rather than only in `context` — a count of withheld entries beside the visible ones, the way the estate's own "show disabled, never hide" ruling treats actions — and if so, surface it on the lakehouse zone's namespace view in the same change.

**LH-124 · The ingest lane's bronze output half-declares the governed-tier contract, so the catalog refuses to publish it**
`ingest, catalog, medallion` · med

- *Why open:* Surfaced 2026-09-10 by the first end-to-end lane run that reached COMPLETE. The run landed 4 rows at version 2 with no provenance defect, and the publish was refused 400: *"version 2 carries some governed-tier columns but is not a conforming tier: missing 'lineage'; missing 'source_rowid'. A tier that carries any of `stage`, `lineage` or `source_rowid` is claiming ..."*. The gate is behaving correctly — a half-declared tier is exactly what it exists to refuse, and the run records `published: false` honestly rather than claiming success. What is open is the WRITER: the lane's bronze schema carries one of the three governed columns and not the other two, so it claims a contract it does not meet. Everything downstream that resolves `source_rowid` back to bronze depends on those columns existing from version 1.
**MEASURED 2026-09-10 AND THE WRITER IS NOT THE PROBLEM.** Read off the live datasets: ingest's bronze carries `['stage']`, the CASCADE's own bronze (`s3://lance-catalog/medallion/bronze`, written by `/produce`) carries `['stage']` — the same shape — and silver carries all three. Bronze is the ROOT tier: it descends from nothing, so `source_rowid` and `lineage` are meaningless on it, while `stage` legitimately names which tier it is. Both writers agree; the gate refuses both. `quality.tier_contract_violations` triggers on `names & _TIER_PROVENANCE_COLUMNS`, so ANY of the three demands all three, and its own docstring shows the target it was designed against — *"refuses exactly the shape that ships today (`source_rowid` present, `stage` and `lineage` absent)"*. A root tier carrying `stage` alone was never a case it considered. - *Closes when:* an owner ruling, because the two readings differ in what PUBLICATION means and the evidence does not settle it. **(a) The gate is too strict:** the claim to be a DERIVED tier is `source_rowid` or `lineage`, not `stage`, so `stage` alone should pass. The cost is that a derived tier which DROPS `source_rowid` while keeping `stage` would then pass — the exact hole the docstring says the job-side check already has. **(b) Bronze is not published at all:** publication is the quality gate on a PROMOTION, and the cascade never publishes bronze — it announces it with an event. Then ingest should not attempt the publish, and the gate is right as written. (b) is the smaller change and fits the architecture; (a) is the one that makes the gate's own rule true for every tier. Either way, re-run `scripts/ingest-lane.sh run` and assert the outcome rather than the absence of an error.

**LH-127 · `lance-ray-durable` has been dead for 26 days and is accumulating the whole stream**
`lineage, compute, chart` · med

- *Why open:* Measured 2026-09-10: on the LINEAGE stream, consumer `lance-ray-durable` reports 2,445 unprocessed messages and a last delivery of 26 days ago — it is bound, durable, and nothing is draining it. Every event the estate emits accrues to it forever. A durable consumer nobody reads is not free: JetStream cannot age messages out of a stream while a consumer still needs them, so this one pins the entire retention window and the 5.8 MiB grows without bound. Nothing reports it — the depth is visible only by asking NATS directly.
**NOT the same shape as lineage's own consumer, which was filed beside this and STRUCK.** Lineage's is ephemeral BY DESIGN — the chart states it ("a durable cursor would defeat its replay-rebuilds-the-graph recovery story") and `_is_replay` accepts what the replay re-presents, logging at INFO. This one is the opposite: a DURABLE consumer with a queue group that nothing is attached to, so nothing accepts and nothing acks.
- *Closes when:* Establish whether anything is meant to consume `lance-ray-durable` (the name suggests the Ray lane). If yes, fix the subscriber and drain it; if no, delete the consumer so retention can do its job. Then add the depth of every consumer on the estate's streams to whatever the maintenance sweep already reports, so a dead subscriber is visible without a NATS client.

**LH-002 · The reconcile sweep warns every tick on 32 `storage_loss` + 2 `unreadable` datasets that are all test residue**
`lineage, maintenance` · **HIGH**

- *Why open:* Retention (30d) and `prune_orphan_datasets` landed and the graph is converging (79→2 unreadable, 37→32 storage_loss), but both warnings still fire on every 5-minute tick over 1,163 Dataset nodes, so a real storage loss would arrive indistinguishable from the noise. The 19 genuinely-dead nodes have runs dated 2026-08-31..09-07 and the one-off purge was refused as a destructive graph write.
- *Closes when:* Let 30-day retention reach the 2026-08-31 runs (2026-09-30), then re-measure the reconcile warnings and confirm `datasets=[...]` names only live datasets.

**LH-003 · The `parent`, `processingEngine` and engine-version run facets are neither emitted nor stored, and the graph has no version/branch/tag/clone/base nodes**
`lineage, medallion, catalog, ingest, service-kit` · **HIGH** · **blocked:** the clone-edge clause waits on the clone→(source, version) registry

- *Why open:* `services/lineage` contains no `parent` handling at all and 0 of 3,181 durable feed events and 0 `(:Run)` nodes carry one, even though `lineage_kit` can stamp it — fixing one side alone is unobservable. `_RESERVED_RUN_FACETS`'s `parent` has only a rejection test as its consumer and `lineage_emit.py:240`'s docstring overstates it; versions remain WROTE-edge properties, column lineage is latest-only and a rename strands history on the old vertex.
- *Closes when:* One change covering both halves: stamp `run.facets.parent` (plus `processingEngine` and the engine-version facet) on the cascade's stage runs AND ingest+persist it in `services/lineage` (a `PARENT_OF` edge or `r.parent_run_id` in `cypher.py::MERGE_RUN`), gated by a test driving a REAL cascade; then add Version and Branch nodes, a clone edge, carry history on rename, fix the line-240 docstring, update `LINEAGE.md`'s captured-facets table and record the RunEvent-only scope in `docs/DECISIONS.md`.

**LH-004 · Four OpenLineage emit kernels and three `RunEvent` builders, all swallowing transport failures, with no outbox**
`lineage, catalog, maintenance, medallion, service-kit` · **HIGH** · **blocked:** owner acknowledgement of R10

- *Why open:* Measured at HEAD 2026-09-09: builders are `lineage_kit/runs.py`, `medallion/schemas/events.py`, `lineage/seed.py`; kernels are `lineage_kit/{emitter,runs}.py`, `service_kit/lancekit/lineage_emit.py`, `catalog/core/lineage_emit.py`, `maintenance/core/lineage_emit.py`. Only the producer-URI defect was fixed. The bronze-write emit is the cascade head, so a swallowed emit means the whole bronze→silver→gold run never happens and nothing reports it.
- *Closes when:* Delete `service_kit.lancekit.openlineage`/`lineage_emit` and the per-service `lineage_emit.py` copies, route every producer through `packages/lineage-kit`'s emitter and one `RunEvent` builder, and stage each event in an outbox before transport so a failed emit is retried rather than dropped.

**LH-006 · `UPSTREAM`/`DOWNSTREAM`/column-lineage Cypher is unbounded `*1..`, and Dataset nodes carry no `latest_version`**
`lineage` · med

- *Why open:* The `/producers` and retention/index clauses closed; traversal depth did not. Verified at HEAD: `services/lineage/src/lineage/services/cypher.py:329,334,445,448` are all `*1..`, and `age.py:44` names the unbounded path over a grown graph as why a pooled connection cannot be pinned. `with_depth` (cypher.py:326) exists but no door applies a ceiling.
- *Closes when:* Apply `cypher.with_depth` (or a validated integer literal) with a `Query(ge=1, le=N)` bound to the `UPSTREAM`, `DOWNSTREAM`, `COLUMN_UPSTREAM` and `COLUMN_DOWNSTREAM` statements, and add a `latest_version` property to the Dataset node maintained on write.

**LH-007 · `ray_stage_job.py:642-649` re-creates its target with `mode="overwrite"` every run, re-minting `_rowid` for the whole tier**
`medallion` · med · **blocked:** owner decision: accept an extra full write of the data to preserve tier row identity

- *Why open:* Re-measured 2026-09-09: the branch writes an empty `out_schema` table with `overwrite` then fans fragments in with `lr.write_lance(mode="append")`. Q10-6's fix does not transpose — `lance_ray.write_lance` on 0.5.0 accepts only create/append/overwrite, there is no distributed merge, and appending into the previous run's dataset re-mints `_rowid` anyway. Tabular stages survive via the plain `source_rowid` column, but the tier's own `_rowid` — what the tier ABOVE resolves against — is destroyed each run.
- *Closes when:* Stage the distributed output into a scratch dataset, then perform ONE `merge_insert` from staging into the target, accepting the extra full write.

**LH-008 · Publication deltas are insert-only: neither in-place updates nor deletions reach a consumer**
`medallion, catalog, annotator` · med · **blocked:** owner decision: deleted-row set served on demand from the publication door vs stamped into the publish event

- *Why open:* `PublicationResult`'s contract has consumers resolve `_row_created_at_version > from AND <= to` and keep no bookmark, so it finds rows CREATED and is blind to rows UPDATED and DELETED. `_row_last_updated_at_version` is built in J4's change-feed door but the publication path still uses `ray_stage_job._delta_filter`, and the annotator's whole write path is `merge_insert`. Driven on a real dataset, `delta(begin,end).get_deleted_row_ids()` answered `{2}` while `get_inserted_rows()` answered 0 — while `compute.py:380` deletes rows with `when_not_matched_by_source_delete`, so a retracted row is served forever.
- *Closes when:* Use the `updated` predicate (both clauses, including `_row_created_at_version <= begin`) in the publication delta computation, and extend `PublicationResult` in `publication.py` to carry `LanceDataset.delta(begin, end).get_deleted_row_ids()`.

**LH-009 · The lineage graph keys a dataset version by `(dataset, N)` where Lance's identity is `(branch, N)`, so a branch write reconciles as `storage_loss`**
`lineage, maintenance` · med · **blocked:** the branch-governance item (branch-aware FGA object + vending)

- *Why open:* `lineage/core/reconcile.py:118` takes `(dataset, graph_version, storage_version)` and nothing else, and `read_storage_version(uri)` opens MAIN — `lance.dataset(uri)` has no branch argument there. The only `branch` the lineage models handle is `models.py:305`, the git `sourceCodeLocation` facet. Not live only because nothing inside rask branches yet; the fix changes the node key every reader resolves against.
- *Closes when:* Key the graph's dataset-version node by `(branch, N)` and give `read_storage_version` a branch argument; land it with the branch-aware `canonical_object_id` work.

**LH-010 · HTR-lane cascade residuals: the P7b re-cut, the bronze→silver geometry stage runners, and populating the in-dataset `lineage` column**
`medallion, catalog` · med

- *Why open:* #88 closed witnessed end-to-end 2026-08-05 but its residuals were folded here at `open_htr_governance.md`'s retirement and none have landed: the owner-directed P7b re-cut, the geometry stage runners, and the in-dataset `lineage` column that rides when the stage runner supplies the LineageDoc.
- *Closes when:* Re-cut the runner's stage job to read bronze Lance and emit gold rows directly (reusing the lane's parser/register/facet seams), add the bronze→silver geometry stage runners, and populate the in-dataset `lineage` column from the stage runner's LineageDoc.

**LH-011 · `runRetentionDays: 0`, `compaction.lineageEmit: false` and `freshnessBudgetHours: 0` ship in the prod render**
`lineage, maintenance, chart` · low · **blocked:** owner decision on the retention window (14d flagged as short for a compliance trail)

- *Why open:* Recorded as a NICE gap and never flipped: prod has the reconcile pruner deployed with the retention knob off, so Run nodes grow forever, and the compaction FAILURE lineage surface stays dark.
- *Closes when:* Set `runRetentionDays`, `compaction.lineageEmit: true` and a real `freshnessBudgetHours` in `chart/values-prod.yaml`, then confirm the reconcile pruner actually deletes Run nodes past the window.

**LH-012 · `scripts/medallion_demo.py::write_gold` silently falls back to a `pa.string()` lineage column when `pa.json_()` raises**
`medallion` · low

- *Why open:* On a plain `pa.string()` column every JSON function and the JSON scalar index fail (`json_get_string` coercion error; 'A JSON index can only be created on a Binary or LargeBinary field') — a silently unqueryable provenance column. pyarrow is pinned to 24.0.0 where `pa.json_()` works, so the fallback is dead code today, which is exactly why nobody will notice when it stops being dead.
- *Closes when:* Delete the `except (AttributeError, ArrowNotImplementedError, TypeError)` fallback in `scripts/medallion_demo.py::write_gold` so a `pa.json_()` failure raises loudly.

**LH-014 · The DIY provenance recipe (`stamp_stage`, `source_rowid`, the tier contract) is written down nowhere**
`medallion, lineage` · low

- *Why open:* Marked Doc in §O1 — anyone writing a new lane has to reconstruct the contract from the code.
- *Closes when:* Write the recipe (`stamp_stage`, `source_rowid`, the `{id, payload, stage, lineage, source_rowid}` tier contract) into `docs/architecture/` or the `rask-lance-catalog` skill.

### Catalog is correct for lance-ns

_The catalog is the estate's only door to Lance, so a spec deviation, an unregistered table or a silently-dropped parameter is a lie told to every client that trusts the spec._

**LH-016 · `bronze-media` and `silver-media` still hijack medallion namespaces inside `lakehouse-wh`, and unbind is refused because they hold real tables**
`catalog` · **HIGH** · **blocked:** owner decision (destructive on real tables) plus a human bearer — no service identity holds `can_administer` on `project:lakehouse`

- *Why open:* The unbind door landed and deployed (`DELETE /v1/warehouses/{id}/namespaces/{ns}`, 0280adfb) and `gold` unbound 200, but `bronze-media` answered 409 NamespaceNotEmptyError ('still holds 1 table(s): objects') and `silver-media` holds `features` at `s3://lakehouse-wh/a76d1ca5_silver-media$features`. The plan claimed both prefixes were empty (a pyarrow FileSelector returning 0 entries); the catalog disagreed.
- *Closes when:* Owner decides drop-or-relocate for `bronze-media$objects` and `silver-media$features`, then calls the unbind door for both namespaces with a human bearer holding `project:lakehouse#can_administer`.

**LH-018 · The governed commit door is the non-spec `/commit`; `CreateTableVersion`/`BatchCommitTables` carry no lineage, gate, protection or replay marker**
`catalog` · **HIGH** · **blocked:** owner acknowledgement of R1

- *Why open:* Version routes at `endpoints/versions.py` are mounted and FGA-gated (`_BATCH_PATHS`, `_action_relation` → `can_write_data`) but nothing else runs on them, and `managed_versioning` is never advertised in `DescribeTable`, so a stock Lance client's commit bypasses the whole governance chain. `batch_commit_tables` is `UnsupportedOperationError` on the dir backend and always will be.
- *Closes when:* Owner acknowledges R1 (the governed commit path IS the spec's managed-versioning path); then attach lineage emit, the quality gate, the replay marker and protection to `CreateTableVersion` in `endpoints/versions.py`, advertise `managed_versioning=true` in `DescribeTable`, alias then remove `/commit` (data.py:326-364, dataplane.py:556-637), and back `batch_commit_tables` with rask's own staged-manifest KV.

**LH-019 · rask-only governance side effects still run inside spec handlers (warehouse-scoped namespace refusal, trash soft-delete, protection 409, lineage keys in schema metadata, implicit BTREE, insert pre-coercion, maintenance 503 on POST reads)**
`catalog` · **HIGH** · **blocked:** the management-API carve, plus an owner ruling on the protection error code

- *Why open:* Only the `branch`-honouring clause closed; eight ops still refuse `branch` and the side effects change what a spec client observes — 7 of the 8 conformance blockers. Four refusals were measured as typed spec errors, but the protection refusal mints `NamespaceNotEmptyError` code 3 for a protected TABLE, so a generated client empties-and-retries forever; the recorded reason for not using code 19 was measured false and the 19-vs-3 split is an undecided tie.
- *Closes when:* Move the rask-only side effects behind the management API, re-express each remaining refusal with the spec's own code, decide the protection code (3 vs `InvalidTableStateError` 19) for all four protected object kinds, and honour `branch` on the eight refusing ops via the plumbing at `dataplane.py:1085`.

**LH-020 · Two of the three stock Lance clients still do not drive the deployed catalog: lancedb cannot address a nested namespace, lance-ray is untested**
`catalog` · **HIGH** · **blocked:** lancedb upstream fix; the lance-ray leg waits for the compute pass

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

**LH-024 · Every Q13 `LANCE CLAIM` verdict was measured on pylance 10.0.0 and is unverified against the locked 11.0.0**
`catalog, maintenance` · med

- *Why open:* pylance was locked 10.0.0 → 11.0.0 on 2026-09-09 and only three rows were re-checked that day; the rest are provenance rather than current evidence. 11.0.0 already changed behaviour that matters — lance #8206 stops reusing fragment ids across an overwrite, exactly where the orphan scan names deletion files via `deletion.path(fragment.fragment_id)`.
- *Closes when:* Re-measure each remaining Q13 row's LANCE CLAIM against pylance 11.0.0 in an isolated env and rewrite the verdict in place with its new measurement.

**LH-025 · `create_table`'s `properties` land only in the namespace declare and the response echo, never on the dataset's schema metadata**
`catalog` · med

- *Why open:* Verified still true: `catalog/services/dataplane.py:324` passes `properties` into `ns.declare_table(...)` and lines 305/308/369 echo them back, but nothing writes them onto the dataset — while `update_schema_metadata` (dataplane.py:1423) proves the write path exists. So §7.1's 'stamped at create' is not readable off the table.
- *Closes when:* In `catalog.services.dataplane.create_table`, merge `parsed_properties` into the dataset's schema metadata through the same seam `update_schema_metadata` uses (never `replace=True`, so internal `lineage.*` keys survive), pinned by a test that reads the properties back OFF the Lance dataset.

**LH-026 · Schema and data evolution is neither exposed nor governed — no add/alter/drop column door, no compatibility check, no owner gate, no schema history**
`catalog` · med

- *Why open:* Lance supports add/alter/drop column and schema evolution but no catalog door does, so an evolving table can only be rewritten; where a schema change does get through, callers see raw pylance errors with no compatibility check and no gate on a breaking change.
- *Closes when:* Add the data-evolution ops to the catalog's table surface with the usual guard/authz path and spec-conformant error codes, plus a compatibility check, an owner rung for a breaking change and a schema history door.

**LH-027 · `register_table`'s door enforces neither location containment nor stable row ids — both live only below it**
`catalog, ingest` · med · **blocked:** owner decision on the stable-row-id shape (refuse on register INTO a governed tier, per D1)

- *Why open:* Driving the deployed door refutes the cross-tenant hole (absolute location 400s, traversal 400s, a relative path resolves inside the caller's own warehouse) but `endpoints/tables.py:514-536` has no check of its own and no rask test covers containment (re-measured TRUE 2026-09-10). Separately the catalog's own create sets the stable-row-id flag and ingest gate A14 refuses without it, but A14 guards the ingest path only — `ingest/lander.py:68` claims the catalog refuses and it does not, so `source_rowid` provenance can be dishonest beyond repair.
- *Closes when:* Add the location check to `tables.py:514-536` (under the namespace's warehouse root, outside `reserved_bucket_set`, as `warehouses.py:164/641` does) and, in the same door, refuse a dataset lacking stable row ids when the target is a governed tier; pin absolute/traversal/reserved-bucket refusal with rask tests.

**LH-028 · Three container-tier deletion paths were never driven live: warehouse delete, project delete, cascade DETACH + plural undrop, and bucket-purge sole-ownership**
`catalog` · med

- *Why open:* Table-level drop/protect/force/undrop are proven; the CONTAINER tier is not, and that is exactly where `force` and cascade interact.
- *Closes when:* Drive warehouse delete, project delete, cascade DETACH + the plural undrop and `projects_claiming_bucket` bucket-purge against the deployed release (`scripts/e2e_live.sh`) and pin each.

**LH-029 · `batch_commit_tables` cannot converge: a retry re-runs the atomic native commit, hits `TableAlreadyExists`, and never reaches the ownership seeds**
`catalog` · med · **blocked:** owner ruling choosing between the three shapes

- *Why open:* Partly landed (6772a35c): the seed loop no longer abandons at the first failure and the error names every table that landed without ownership plus the fact that a retry will not repair them. Convergence itself is still missing — the batch cannot be retried into a correct state.
- *Closes when:* Pick one of the three shapes and implement it in `batch_commit_tables`: pre-flight the seeds, seed-then-commit, or an idempotent re-seed keyed on the declared ids (the last also needs the native declare sub-op to be idempotent).

**LH-030 · A partially-failed warehouse delete does not report which parts failed**
`catalog` · med

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

**LH-033 · Nine catalog operations answer 501: branch-scoped `query`/`explain_plan`/`analyze_plan`, `create_index`/`create_scalar_index`, `stats`, `index/list`, `index/{n}/stats`**
`catalog` · med

- *Why open:* Named explicitly 'so a 501 never reads as finished'. The line drawn was the OPTION SURFACE — a fixed-shape op is served, an open option surface is refused — and each needs a faithful mapping plus its own tests before it can be served.
- *Closes when:* Give each of the nine a faithful mapping onto pylance and its own tests, replacing the 501.

**LH-034 · Compression is never configured anywhere and there is no decision record**
`catalog, medallion` · med

- *Why open:* Listed Medium in §O1 with no note and no work. The setting is schema-resident, so retrofitting it later costs a rewrite and gets dearer with corpus size.
- *Closes when:* Choose a compression configuration on the create path and record it in `docs/DECISIONS.md`.

**LH-035 · The query door serves only the blob DESCRIPTOR shape with no `all_binary` opt-in, and `read_blob_ranges` is undocumented as the batched byte-fetch path**
`catalog` · med · **blocked:** owner acknowledgement of R8

- *Why open:* Descriptor-first reads are already the default (`POST /v1/table/{id}/query` returns `struct<kind, position, size, blob_id, blob_uri>` with `lance-encoding:blob` metadata), so the main clause is refuted — but a caller that wants bytes inline has no opt-in, and the `/blobs` door streams `take_blobs` with Range/ETag one object at a time while pylance's batched `read_blob_ranges` is documented nowhere. It is the contract a Spark connector or BYO engine expects.
- *Closes when:* Add an `all_binary`/`blob_handling` opt-in flag to the query door's request model so a caller can ask for inline bytes, and document `read_blob_ranges` beside the `/blobs` Range/ETag door.

**LH-036 · Body-id reconciliation (A1) is missing on four catalog routes**
`catalog` · med

- *Why open:* A pointer row marked `= A1`, which the register's own order of work lists as still to do; the branch-body clause of the branch-governance item also waits on it.
- *Closes when:* Reconcile the id in the request body against the path id on the four routes, with A11's stock-client drive as the RED gate.

**LH-037 · `POST /v1/namespace/{id}/create` and `POST /v1/table/{id}/register` accept `mode` and answer 409 whatever it says**
`catalog` · med

- *Why open:* Classed silently-weaker in the dropped-parameter sweep: a caller asking for an idempotent or overwrite mode gets the same 409 as a caller asking for strict create.
- *Closes when:* Implement the `mode` values on both doors (or refuse an unsupported one 400 rather than 409), with tests per mode.

**LH-038 · `POST /v1/table/{id}/version/list` accepts `page_token` and ignores it**
`catalog` · med

- *Why open:* Classed read-from-wrong-target in the sweep; a paging caller silently re-reads the first page forever.
- *Closes when:* Honour `page_token` in `version/list` (or refuse it 400), with a test that pages twice and gets different rows.

**LH-039 · `POST /produce` accepts a governed-tier claim in `settings` and disregards it**
`medallion` · med

- *Why open:* Classed silently-weaker: the ingest door lets a caller state which governed tier it is seeding and then ignores the statement, so the claim is unenforced at the cascade head.
- *Closes when:* Honour the governed-tier claim in `POST /produce`'s `settings` and refuse a mismatch, the same door family as the `register_table` stable-row-id decision.

**LH-040 · Put-if-not-exists is verified only on RustFS, so Lance's CAS commit model is untested on any other store**
`catalog, storage` · med

- *Why open:* The Lance commit model assumes conditional put; on any store rask might run on other than RustFS (COS/GooseFS need commit locks per the guide) that assumption is untested and nothing refuses such a store at registration.
- *Closes when:* Add a per-store CAS probe to the warehouse validation endpoint so an unsupported store is refused at registration.

**LH-041 · Branch/tag writes are unconditional at every layer including pylance's `Tags::update` — a lost update in waiting**
`catalog` · med

- *Why open:* `_set_tag` is unconditional all the way down and nothing has verified whether RustFS honours `If-Match` on the tag object.
- *Closes when:* Use an object-store conditional put on `_refs/tags/<name>.json` and verify RustFS honours `If-Match` there.

**LH-042 · Blob v2 default thresholds disagree three ways (64 KB/4 MB vs 16 KiB/2 MiB vs rask's measured 64 KiB/4 MiB)**
`catalog, service-kit` · med

- *Why open:* Three sources disagree and the defaults are pinned against only one of them.
- *Closes when:* Keep pinning the measured values and re-measure them on each pylance bump.

**LH-043 · Unknown whether MemWAL server-id sharding fits append-only bronze landing (coordinator-free ingest)**
`ingest, medallion` · med · **blocked:** §K

- *Why open:* Blob v2 columns read `None` through the MemWAL scanner today, so the shape cannot be evaluated without a prototype.
- *Closes when:* Prototype MemWAL server-id sharding against bronze landing after §K.

**LH-044 · `tags/create` drops `branch`, `branches/create` drops `from_branch`+`from_version`, `branches/delete` drops `name`**
`catalog` · low

- *Why open:* Classed cosmetic in the sweep and untouched — but a `branches/create` that ignores `from_branch`/`from_version` silently branches from the wrong point.
- *Closes when:* Honour `branch` on `tags/create`, `from_branch`/`from_version` on `branches/create` and `name` on `branches/delete`, each with a test that a non-default value changes the result.

**LH-045 · rename / backfill / alter_transaction / MV create+refresh / batch-create + batch-commit versions return 501 from the native backend**
`catalog` · low · **blocked:** upstream lance-namespace implementation

- *Why open:* Confirmed 7 (not 8) against `docs/COVERAGE.md`: 47/54 ops backed. They stay 501 until the upstream Rust `DirectoryNamespace` (or a REST/managed backend) implements them — a parallel upstream contribution, open but not startable here.
- *Closes when:* Either contribute the missing ops upstream to lance-namespace's `DirectoryNamespace`, or record them in `docs/COVERAGE.md` as permanent 501s with the same finality the Lance-only format guard has.

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

**LH-051 · `rask-catalog` is the last service presenting the RustFS root key `rustfsadmin` as its S3 identity**
`catalog, storage, chart` · **HIGH** · **blocked:** owner decision — what may the thing that grants access itself reach, when the set of warehouses is minted at runtime (held jointly with open_ingest_design.md §2 and open_gateway.md Phase 2)

- *Why open:* Re-measured on the running pods 2026-09-10: maintenance, medallion, lineage, viewer and ingest each hold their own scoped identity (`rask-*`, secret half delivered via `LANCE_SECRETS_FROM_DAPR`), and only `LANCE_S3_ACCESS_KEY_ID=rustfsadmin` remains (chart/templates/services.yaml:92-94, values.yaml:1517-1518). It cannot be fixed by copying the other four: an STS session policy can only RESTRICT the role it is cut from, while the catalog vends for warehouses minted at RUNTIME — a role narrowed to today's buckets cannot vend tomorrow's, and a role covering every future bucket is root wearing another name.
- *Closes when:* Owner picks the identity shape for a runtime-vending service (a role policy the warehouse registry maintains as warehouses are minted, an STS role assumed per vend, or accepting the widest role bounded by network/audit/rotation); then provision `rask-catalog` the way `rustfs.medallionAccessKey`/`maintenanceAccessKey` are — chart pre/post-upgrade provisioning hook, key defaulted to the provisioned user — and extend `tests/unit/test_a_provisioned_identity_is_one_the_service_uses.py`.

**LH-052 · `LANCE_FGA_CASCADE_WRITERS` grants every stage runner `owner` on EVERY tenant warehouse, and the bounding control is off by default**
`catalog, medallion, lineage, chart` · **HIGH** · **blocked:** owner decision on the narrowed grant and on the shipped default posture

- *Why open:* The bounding control (`LANCE_PRIVILEGED_SUBJECTS`) now renders on catalog and lineage (79512bb0) and this estate sets `dedicatedServiceCredentials: true`, but the grant itself is still estate-wide — a stage runner holds `can_drop`/`can_deregister`/`can_restore`/`manage_grants` on every tenant's warehouse, not just the ones it writes — and `chart/values.yaml:807` still defaults the control OFF, so a fresh install ships the unbounded shape. Same over-grant Q5-2 solved for maintenance with a `can_maintain` rung.
- *Closes when:* An owner ruling narrowing the cascade writer's grant to the warehouses it writes, then change how `LANCE_FGA_CASCADE_WRITERS` is rendered/seeded in the chart plus `.fga.yaml` cases for the narrowed shape, and flip `values.yaml:807` to `true` with `LANCE_PRIVILEGED_SUBJECTS` rendered by default.

**LH-053 · Bucket claims are keyed by warehouse ID, so two warehouse IDs can both claim the SAME bucket — and the four control-root JSON stores it must live in are not collapsed**
`catalog` · **HIGH** · **blocked:** owner ruling: pull the bucket claim forward as its own store, or confirm it stays behind the #85 record primitive

- *Why open:* Deferred by diff2's F1 landing note rather than by omission: the fix belongs with #85's collapse of the four control-root JSON stores, not as a fifth ad-hoc store. No code path detects a double-claimed bucket and recovery is manual — Mallory ends up holding `owner` on a warehouse whose `root_uri` is another tenant's bucket, and `set_project_policy` resolves through the same registry so her maintenance policy can destroy their version history.
- *Closes when:* Collapse the four control-root JSON stores into a single conditional-create record primitive, then express the warehouse-id mint and a bucket-KEYED claim (`_warehouses/bucket-claims/<bucket>.json`, written with the same `IfNoneMatch: *` primitive) on top of it.

**LH-054 · The credential-isolation e2e SKIPS against the shipped stack, so cross-tenant credential refusal is proven only by rask's own offline policy evaluator**
`catalog, storage, chart` · **HIGH**

- *Why open:* The code landed (3cacdd91) but every test skips on the shipped stack and a skip reads identically to a pass — RustFS never evaluates the session policy. `scripts/e2e_stack.sh` provisions neither web-identity vending nor a second tenant admin, and the sabotage half needs a deliberately-widened-policy lever that must not be reachable in production.
- *Closes when:* Provision `vending.mode=web_identity` (requires `rustfs.oidc.enabled` + `auth.enabled`) plus a SECOND tenant with its own admin subject in `scripts/e2e_stack.sh`, add the widened-policy sabotage lever to the harness chart values, then run the credential attack e2e unskipped in CI.

**LH-055 · The FGA model has no `branch`/`column`/`base`/`estate` type, `can_set_protection` collapses onto `can_drop`, and `project` has no security_admin/data_admin/role_creator split or machine identity**
`catalog, service-kit, openfga` · **HIGH** · **blocked:** owner decision on the model shape (and on introducing `estate` vs documenting the warehouse-as-root convention) — coordinate with the `role`→`project` edge so `model.fga` changes once

- *Why open:* Re-measured 2026-09-09: all five Lakekeeper per-action rungs are zero. `_OWNER_SUFFIX_RELATION` maps `protection` to `can_drop`, so the person protection is meant to stop holds the rung that disarms it — four-eyes is unexpressible. `project` carries only admin+member, so one principal holds both data power and granting power. And root-ness is a convention: `model.fga` declares no `estate`, so `can_observe_events`/`can_browse_storage` exist on EVERY warehouse and resolve to that warehouse's owner — only the app checking them against `settings.fga_root_object` keeps them estate-scoped, and a future check on a non-root warehouse would silently grant estate-wide privilege and still pass `fga model test`.
- *Closes when:* Add a `can_set_protection` rung remapped out of `_OWNER_SUFFIX_RELATION`; add the project role split plus a machine/operator identity; add the `branch` type, a column-policy relation and an `estate` root with `can_create_project`, moving `can_observe_events`/`can_browse_storage` onto it and repointing `fga_root_object` (`catalog/core/config.py`) with its one seeded tuple; `.fga.yaml` cases and `_CHILD_EDGE_PARENT_TYPES` for each.

**LH-056 · Branch-scoped governance is missing: no FGA `branch` type, vending/protection/trash are branch-blind, and branch/tag creation emits no control event**
`catalog, lineage, notifications` · **HIGH** · **blocked:** owner acknowledgement of R5, plus an owner decision on who is TARGETED by a tag/branch control event (rask-notifications: an event naming nobody is undeliverable); the stats/index body clause waits on A1

- *Why open:* The nine data doors were fixed and pinned; the governance half is untouched. `model.fga:349,357` has only `can_create_branch: owner`, so branch writes fall through to the table's `can_write_data`, and `canonical_object_id` joins the table's path segments only — the FGA object is `table:<ns>$<table>` whatever branch a request names, so a `can_write_data` holder writes ANY branch and no grant can cover main alone. Vending is not scoped to `tree/<b>/`, protection and trash have no per-branch records, `parent_branch`/`parent_version` facets are absent, and driven against the deployed catalog `tags/create` and `branches/create` both answered 200 with zero control events because `ControlAction` is a 38-value `Literal` with no tag or branch action. The branch refusal on `stats`, `index/list` and `index/{n}/stats` was added as a QUERY parameter while those routes declare no body, so the spec's `{"branch": …}` body is still dropped.
- *Closes when:* Add `type branch { parent:[table]; reader/writer; can_write_data }` to `model.fga` with `.fga.yaml` cases; make `canonical_object_id` branch-aware; scope vended STS prefixes to `tree/<b>/` via `vending.build_session_policy`; add per-branch protection and trash records; emit `parent_branch`/`parent_version` facets; add tag/branch values to `ControlAction` in `control_events.py` and regenerate `docs/catalog-openapi.json` + the TS client; land A1 so stats/index read `branch` from a declared body.

**LH-057 · Per-base credential vending is unimplemented — the vendor refuses any table whose fragments carry a `base_id` instead of vending a union of bases**
`catalog, maintenance, storage` · **HIGH** · **blocked:** owner acknowledgement of R4; the bases-as-storage-profile framing it rides on

- *Why open:* Two of three clauses closed (the `session_token` seam and `expires_at_millis` on both vend paths) and the falsy-zero guard is fixed, but `endpoints/credentials.py:76-130` and `core/vending.py:213,278` still refuse rather than vend. §H12 is the measured cost: 69 datasets a tick refused compaction because the vended session policy cannot reach a base the manifest declares. Latent behind `settings.multibase_data_base_list` (deployed empty), so it fails on the first estate that enables the feature.
- *Closes when:* In `core/vending.py`, vend the union of the manifest's `base_paths` with per-base rights — read on inherited bases, write on `target_bases`, never on reference-only bases — resolving each path by `BasePath.is_dataset_root`, reusing the manifest `credentials.py::_current_version` already reads, and applying `build_session_policy`'s `*`/`?` metacharacter refusal to manifest-sourced paths.

**LH-058 · No column-level classification or policy exists: `columns.py` has no FGA check and `pii` survives only as a key in seed data**
`catalog, lineage, openfga` · **HIGH** · **blocked:** the FGA model-shape decision (the `column` relation is part of it)

- *Why open:* Section I names it 'the lever the estate cannot express' — governed tables carry no per-column sensitivity, so a GDPR/secrecy classification cannot be recorded, checked or enforced, masking/deny per column is unexpressible, and only column LINEAGE exists.
- *Closes when:* Put a classification field on the dataset/column metadata and the dataset node, add a `column` relation to `model.fga`, apply masking on `query` and on descriptor-first reads in `columns.py`, and add a door to set and read the classification.

**LH-059 · The bronze write doors never self-check `can_create_table` on `namespace:bronze` — the FGA model describes a rung nothing enforces**
`medallion, catalog` · med

- *Why open:* `medallion/api/produce_auth.py::authorize_produce` gates the door on `can_administer` on `project:<id>` (or the Dapr app-api-token) but nothing checks a WRITER rung before the Lance write: `grep can_create_table services/medallion/src` hits only `core/config.py`, `services/transform.py` and `services/train.py`. `scripts/seed_medallion_fga.sh` grants `writer` to `user:service-lance-ray`, so the model describes a rung the producer never enforces.
- *Closes when:* Add a `can_create_table` check on `namespace:bronze` as `service-lance-ray` in `medallion/services/produce.py` and `media_produce.py` behind `RASK_FGA_ENABLED`, matching `transform.py`'s enforce-not-describe posture, red-first.

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
`lineage` · med

- *Why open:* The owner delegated the decision 2026-09-02 and the row explicitly stays in the backlog — the bus door is the integrity of the lakehouse's write record. The decided shape (mTLS SPIFFE app-id policy while Dapr is the transport, a transport-independent producer signature that survives a Dapr retreat, `enforce_output_authz` stamping the subject either way) is a design, not landed code.
- *Closes when:* Add the Dapr `accessControl` policy naming the permitted producer app-ids on the lineage subscription, verify a producer signature over the CloudEvent in the bus door, and stamp the subject through `enforce_output_authz`.

**LH-065 · Prod vending is `mode_b` everywhere, so the tenant-isolation machinery is dormant on every shipped estate (and no `/refresh-credentials` door exists for the alternative)**
`catalog, chart, storage` · med · **blocked:** owner posture ruling on web_identity vs mode_b

- *Why open:* The isolation apparatus is built and tested offline but never exercised by the shipped posture; switching it on is a posture decision with real cost (a RustFS roll) rather than a code change. The refresh door is conditional on the same decision: `mode_b` credentials never expire, so it is only needed once vending leaves `mode_b`.
- *Closes when:* A posture ruling setting `vending.mode=web_identity` (requires `rustfs.oidc.enabled=true`, accepts a RustFS roll, needs `ROLE_POLICY` provisioned); if taken, add a `/refresh-credentials` endpoint plus a `revalidation_window_ms` field on the vended response.

**LH-066 · The maintenance identity is one key across every warehouse rather than a per-warehouse scoped credential**
`maintenance, chart` · med · **blocked:** the per-base `managed`/`reference-only` warehouse-record policy this depends on

- *Why open:* The row's first clause landed 2026-09-07 (`delete_location` refuses a location holding files but no `_versions/` marker, pinned by `test_purge_refuses_a_location_that_is_not_a_dataset.py`). The second is untouched: the service that rewrites and deletes in every bucket in the estate does so under one long-lived identity.
- *Closes when:* Scope the maintenance identity per warehouse in `chart/templates/maintenance.yaml:123` and `chart/values.yaml:1517` instead of one estate-wide key.

**LH-067 · Warehouse storage cannot be expressed as bases: one endpoint and one key for the whole estate, and a warehouse-rooted connection swaps only `root`**
`catalog, storage` · med · **blocked:** owner acknowledgement of R3; the `aws_provider_scheme` clause additionally waits on an upstream pylance release

- *Why open:* `config.py:60-62,101-102` hardcodes the single-endpoint/single-key connection shape, so a warehouse record cannot declare per-base endpoints or credentials, a write door cannot say which base it targets, and a warehouse in another bucket/account/region (or on its own RustFS instance) is inexpressible — per-tenant or per-region storage cannot be modelled and failover cannot be an edit of `base_paths`. `base_store_params` exists but the keyed `base_<id>.<key>` form and `aws_provider_scheme` are unverified on the installed pylance, so code depending on them would be guessing. Per-base vending rides on this.
- *Closes when:* Probe the installed pylance for the `base_<id>.<key>` keyed form and `aws_provider_scheme` and record the result; then add `initial_bases` and `base_<id>.<key>` option fields plus per-warehouse endpoint/credentials to the warehouse record, add `target_bases` to the catalog write doors, and delete the storage-profile-per-warehouse code path.

**LH-068 · Legacy data that predates warehouses has no migration path**
`catalog, maintenance` · med

- *Why open:* Data landed before the warehouse layer existed is attached to no warehouse, so the multi-warehouse sweep, policy and vending paths cannot reach it.
- *Closes when:* A migration binding legacy no-warehouse data to a warehouse record (or explicitly quarantining it), plus a test over a pre-warehouse fixture.

**LH-069 · Pre-registry 'ghost' project ids were never migrated, which is why the consume-side id rule stays looser than the mint rule**
`catalog, medallion` · med · **blocked:** owner decision: adopt the ghost ids into the registry, or revoke them

- *Why open:* The ghost ids never passed the mint rule, so tightening the consume rule would refuse them and the adopt-vs-revoke call has to come first. No live defect from the asymmetry itself — `is_safe_project` uses `fullmatch` and the Pydantic models anchor — but tightening is wire-visible on medallion's generated clients.
- *Closes when:* Decide adopt-vs-revoke for pre-registry project ids and run the migration over the live control root; then tighten the consume-side project-id rule to match the mint rule and regenerate medallion's clients.

**LH-070 · No versioned authz-model migration (`ACTIVE_MODEL_VERSION` + an idempotent `migrate()`) — the 3-axis model shipped without it**
`service-kit, catalog` · med

- *Why open:* The study ruled it mandatory before the 3-axis model and the model shipped anyway; there is no recorded active model version and no idempotent migration, so a model change cannot be rolled out safely.
- *Closes when:* Add an `ACTIVE_MODEL_VERSION` constant plus an idempotent `migrate()` that writes the authz model and pins the active version, called at service start.

**LH-071 · Tuple helpers were never split into `tuples.py` and there are no golden tuple tests**
`catalog, service-kit` · med

- *Why open:* The study's #10 has not landed; `grant_on_create` is still one inline grant and the register notes the existing FGA contract test is NOT this, so tuple shapes are asserted nowhere golden.
- *Closes when:* Extract tuple construction into a `tuples.py` seam and add golden tuple tests covering `grant_on_create` and the other grant paths.

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

**LH-080 · `can_promote` buys nothing on `table` because `validator ⊇ owner`**
`catalog, openfga` · low

- *Why open:* The relation exists but grants no discrimination at the table level.
- *Closes when:* Either narrow `validator` so it is not a superset of `owner` on `table`, or remove `can_promote` from the table type in `model.fga`.

**LH-081 · Nothing proves `_authorize_transaction`'s two branches gate equivalent privilege**
`catalog` · low

- *Why open:* The claim — that a namespaced `<ns>$<txn>` id resolving against `namespace:` and an opaque root id resolving against `transaction:` gate the same privilege — is read from a docstring, never tested. `tests/unit/test_fga_model_contract.py` drives both branches (:181) only to collect (type, relation) pairs, and its transaction case (:266) pins only the namespaced branch, so a divergence would pass every gate.
- *Closes when:* Add a case to `tests/unit/test_fga_model_contract.py` asserting the privilege reachable through the opaque branch (`transaction:<id>#can_describe`/`#can_set_status`) equals that through the parent-scoped branch (`namespace:<ns>#can_get_metadata`/`#can_update_properties`) for the same subject, evaluated against the compiled model rather than a recording fake.

**LH-082 · The gateway proxies the catalog's full all-method write surface to the public ingress and nothing says whether that is intended**
`gateway, catalog` · low · **blocked:** owner ruling on whether all-method public exposure of `/api/catalog/*` is intended

- *Why open:* Verified still present: `services/gateway/src/gateway/__init__.py:225` is `Route("/api/catalog", "", *catalog)` — every method, straight through from the ingress `- path: /api` rule. The home BFF is GET-only on purpose and writes go through SvelteKit remote functions, but this row bypasses that shape entirely; the catalog's safety then rests wholly on its own OIDC + FGA, and no ruling for it exists.
- *Closes when:* Record the ruling — either a rationale comment on the `Route("/api/catalog", …)` row plus a line in `docs/DECISIONS.md` stating the full write surface is deliberately internet-facing behind catalog-side OIDC+FGA, or narrow the row's method set.

### Not coupled to a workflow engine or Ray

_The platform claims to run any workload on any engine, and today the deployed stage lane, the catalog's published API and the media write path all name Ray._

**LH-083 · `engine_registry.executor_for` has ZERO callers — the deployed stage lane still calls `ray_submit` directly at `workflow.py:495`**
`medallion, ray-kit` · **HIGH** · **blocked:** Q17-2 (the Ray adapter's fate)

- *Why open:* Corrected with `ast` rather than grep: exactly one in-scope bypass remains, `workflow.py:495` (`submit_stage_job`, the deployed stage lane) — `train.py:280` is the TRAIN lane and `ray_submit.py:318,425` are the adapter's own calls. The load-bearing half is the opposite error: nothing resolves an engine through the port, `transform.py:778` constructs `InProcessExecutor` by hand and `RayJobExecutor` is constructed by nothing. Migrating `transform.py` would be cosmetic; `workflow.py:495` is the site that CHOOSES an engine.
- *Closes when:* Route `workflow.py:495`'s stage submission through `engine_registry.executor_for(...)` once the Ray adapter's fate is decided, then drop `ray-kit` from `services/medallion/pyproject.toml`.

**LH-084 · `BAKED_JOBS_DIR`/`BAKED_CLUSTER_JOBS` live in the shared library and the catalog enforces them, so a non-Ray lane cannot be declared and the word 'Ray' reaches every API client via the published OpenAPI**
`catalog, service-kit, medallion` · **HIGH** · **blocked:** the BYO half (K / D5)

- *Why open:* Marked 'In progress'. The lakehouse's agnosticism claim rests on this contract, and today the declaration surface names Ray — so the catalog is coupled to one distributed compute engine at the API level.
- *Closes when:* Finish the executor contract so a lane declares an engine-neutral executor (Ray becoming one implementation), and remove `Ray` from the catalog's published OpenAPI schema.

**LH-085 · The multimodal write lane is single-driver even though `lance_ray` 0.5.0 does not strip blob typing**
`medallion` · med

- *Why open:* NOT refuted — the measurement was reproduced first-hand on the exact library set the cascade image pins (`.docker/ray-lance.dockerfile:67`: lance-ray==0.5.0, pylance==10.0.0, pyarrow==25.0.0, held equal to the venv by `tests/unit/test_ray_job_images.py:105`; ray 2.58.0). The row's verdict column is truncated in the register at the script path, so §Q13's header rule applies: re-derive before working it.
- *Closes when:* Re-derive the blob-typing measurement against lance-ray 0.5.0 inside the ray-lance image, then remove the single-driver constraint from the media write path if blob typing survives a distributed `lance_ray.write_lance`.

### Events are correct

_The cascade, the inbox and every downstream consumer are driven by events, so a trigger that does not fire or a payload that names the wrong thing fails silently and is reported by nothing._

**LH-086 · `test_outbox_e2e::test_reconcile_sweep_drains_a_staged_outbox_event` still fails — a staged lineage event strands instead of draining**
`lineage` · **HIGH**

- *Why open:* The strike was withdrawn 2026-09-10: the key-format half is fixed but the test fails on a second cause. The reconcile tick ran (`checked: 350`), the event stayed staged, and `lineage_outbox_event_stranded` fired 4x in 40 minutes; transport is ruled out (sidecar publish answered 204), so the strand is a graph write inside `reconcile_cron.py:338-375`. The outbox is empty right now, so it cannot be read off the running estate today.
- *Closes when:* Deliberately STAGE an event into the outbox and follow that one through `reconcile_cron.py:338-375` (`repository.ingest_event` → re-publish → `outbox.drop_event`), reading the strand cause now that the log formatter emits `extra`.

**LH-088 · Every lineage restart replays the retained stream and logs ~125 `dapr_dead_letter_parked` ERRORs claiming provenance was lost**
`lineage, notifications` · med · **blocked:** owner decision (a)/(b)/(c) — both repairs touch security-relevant behaviour

- *Why open:* Measured and explained: 126 `lineage_event_unauthorized` WARNINGs, 125 `dapr_dead_letter_parked` ERRORs and 783 `ingest_run_mutation_denied` in a burst 4 minutes after pod start, then 0 in the following 15 minutes — a restart artifact of an ephemeral `deliverPolicy=all` subscriber. Nothing is lost, but a replayed event whose subject no longer holds the grant is refused by `enforce_bus_authz` → `_DROP` → `{"status":"DROP"}`, which Dapr routes to the dead-letter topic instead of acking. An ERROR that fires routinely is one nobody reads, and chasing it cost twenty minutes once already.
- *Closes when:* Owner picks: (a) leave it and document the replay, (b) skip re-authorizing a replayed event the graph already holds, or (c) distinguish a replay from a live delivery and log it below ERROR.

**LH-089 · Neither `medallion.bronze` trigger head can be retired: no remaining writer can publish, so disabling either strands a lane**
`medallion, ingest, lineage` · med · **blocked:** the stage-runner catalog-registration item

- *Why open:* The stated retirement condition ('once every writer publishes') is unreachable as written — stage runners compose targets and register nothing, so they have no catalog table to publish, and `/produce` / `/ingest-media` write bronze with no catalog involvement at all. Both heads stay, with stage-runner token de-duplication the only thing stopping a double cascade.
- *Closes when:* After the stage-runner registration lands, make `POST /produce` and `POST /ingest-media` catalog-mediated, then delete the `/bronze-arrival` route and leave `/publication-arrival` as the single `medallion.bronze` trigger.

**LH-090 · `POST /v1/table/{id}/publish` emits the `table_published` control event carrying the WRONG `to_version`**
`catalog, notifications` · med

- *Why open:* Classed read-from-wrong-target in the dropped-parameter sweep and never addressed — a consumer acting on the event acts on the wrong version.
- *Closes when:* Stamp the published version into the `table_published` control event from the commit result, and pin it.

**LH-091 · The change feed has no control-lane EVENT, so a BYO consumer must poll `POST /v1/table/{id}/changes`**
`catalog, lineage, notifications` · med

- *Why open:* The door landed and was driven live 2026-09-09 (three defects found and fixed), leaving exactly one residue: a consumer can only poll, never be told.
- *Closes when:* Emit a control-lane event on `catalog.control.v1` when a governed table's version advances, so a change-feed consumer is notified rather than polling.

**LH-092 · The ingest-lane slice proves the TRIGGER chain but not the DATA chain — `MEDALLION_FROM_URI`/`TO_URI` are unset there**
`medallion` · med

- *Why open:* `medallion/services/transform.py:970` guards the whole compute path on `settings.compute_enabled and from_uri and to_uri`; in the lane slice those URIs are unset and no project routing is configured, so the stage runner wakes, emits and writes nothing. Partly overtaken — `chart/templates/medallion.yaml:502-503` now renders both for the chart deploy path — so what remains is the lane proving bronze→silver→gold moves BYTES.
- *Closes when:* Configure the tier URIs (or the per-project warehouse registry) in `scripts/ingest-lane.sh` and assert a committed silver and gold version with row counts, not just a `POST /medallion-event 200`.

**LH-093 · The event actor is one `author.sub` string where it should be a closed union (Anonymous | Principal | Role)**
`lineage, notifications, medallion, catalog` · low

- *Why open:* Lakekeeper's CloudEvent actor is a sum type (`Anonymous | Principal(UserId) | Role { principal, assumed_role }`), so an assumed role records both the human behind it and the role assumed and neither can be spelled as the other; rask stamps one string. The consequence is a documented live failure mode: `rask-notifications` warns that a producer stamping a role literal in `author.sub` targets nobody and the event is still acked SUCCESS, so the miss is reported by nothing.
- *Closes when:* Change the OpenLineage/control emit boundary to take a closed union (Anonymous / Principal(sub) / Role{principal, assumed_role}) instead of a bare `author.sub` string, making a role literal unrepresentable rather than merely warned about.

### Resilience & maintenance

_Nothing has ever been reclaimed on the live estate, the sweep is unleased and unbudgeted, and the Lance-serving processes are one shared handle away from an OOM — this is where the lakehouse fails quietly at scale._

**LH-094 · The orphan scan refuses every flag-16 dataset wholesale — 490 of 611 reconcile scans incomplete — because there is no per-base managed vs reference-only policy**
`maintenance, catalog, ingest` · **HIGH**

- *Why open:* Ingest registers the source BUCKET as a base (`ingest/adapters.py:296-306`, `lander.py:311-325`), so flag 16 makes the orphan scan refuse the dataset (`orphans.py:294-295`), `report_is_clean` blocks every purge (`purge.py:223-224`) and `protected_roots` protects the whole bucket (`features.py:113-114`). Measured out of GreptimeDB: `reconcile_report total 611, incomplete 490, orphan_files 598, orphan_buckets 12`, every skip reason the flag refusal — nothing is being reclaimed, and the 220 `maintenance_refused_protected_base` refusals per six hours each protect an entire bucket because a base reference names a bucket rather than a base. Widening the scan's flag mask is explicitly ruled out (a dataset with `add_bases` and all-`None` `base_id` passed `checked=True` with live files named as orphans). Whether `cleanup_old_versions` reclaims external in-base blobs is still unmeasured.
- *Closes when:* Write a RED test pinning what pylance does to an external blob under `initial_bases` before changing any GC behaviour; parse `BasePath.is_dataset_root`; add a per-base `managed`/`reference-only` policy field to the warehouse record and consult it from `service_kit/lakehouse/base_refs.py::protected_roots`; issue cleanup credentials WITHOUT delete rights on reference-only bases.

**LH-095 · The maintenance sweep takes no lease per tick or per dataset, and refusal state (`attempts`/`last_refusal`) is never persisted on the trash record**
`maintenance, service-kit` · **HIGH**

- *Why open:* Only the deployment-strategy clause is done (`strategy: Recreate`, so a 25%-maxSurge rollout can no longer run two cron-ticking pods). `bindings.cron` is uncoordinated — every replica runs the schedule independently and the service holds no lease, so nothing but the replica pin prevents concurrent ticks — and a repeatedly-refused trash record is indistinguishable from a transient one.
- *Closes when:* Take a conditional-put lease per tick and per dataset using the existing `records.create_json`, at `maintenance/api/routes.py:61` and `maintenance/services/sweep.py:204-206`; persist `attempts` and `last_refusal` on the trash record in `service_kit/lakehouse/trash.py:80-91` and consume them at `maintenance/services/purge.py:442-445` so a permanent exclusion reads as permanent.

**LH-096 · Every Lance-serving path opens `lance.dataset()` per request, no process shares a `lance.Session`, and nothing bounds Lance's 1 GiB metadata + 6 GiB index + 2 GiB io-buffer defaults against the 512 Mi pod tier**
`viewer, medallion, lineage, catalog, ingest, maintenance, service-kit, chart` · **HIGH**

**RE-MEASURED 2026-09-10, AND THE COUPLING THE ROW ASSERTS DOES NOT APPLY TO THE HALF THAT MATTERS.** The row treats "stop opening per request" and "size the caches" as one change. They are two, and only the first is dangerous: a cached HANDLE pins a version, which is why the viewer's registry is a read-only trade. A bounded `lance.Session` is NOT a handle cache — its keys are `(uri, version, etag)`, so a compaction writes NEW keys and there is no freshness contract to design and no stale-read window; `lance_session.py` records that, and that it is thread-safe under 8x50 concurrent opens. **Maintenance has already shipped exactly this and it is the proof:** `shared_lance_session()` caps it at 128 MB metadata + 256 MB index and threads it through reconcile, purge and the orphan scan. **Measured today:** every lakehouse pod runs a 512 Mi limit (128 Mi request), and ONLY maintenance passes a session — catalog opens 12 bare datasets, medallion 15, lineage 6, with 6 more in shared service-kit code. Each of those mints Lance's 1 GiB metadata + 6 GiB index ceilings and discards them WITH the handle, so the cache never engages at all: ten version-opens against a shared session grow `size_bytes` 168 -> ~75k, the same opens without one leave it flat. So the safe, proven half is a bounded session per service — soft LRU bounds, no version pinning, the pattern already running in maintenance — and it needs ONE seam plus 39 call sites converged onto it, not a redesign. - *Why the rest of the row is open:* A shared handle also pins a version, so the fix needs `checkout_latest` (or a session-scoped open) plus an explicit freshness contract per service. Measured: 53 `lance.dataset()` call sites and 5 pass a session; the catalog opens ~24 bare datasets per request path. The same row carries the rest of the runtime hygiene: no `LANCE_CPU_THREADS`/`LANCE_IO_THREADS`/`LANCE_LOG` in the Ray `runtime_env`, `instrument_lance_metrics` never called by ingest/viewer/search/annotator, no branch/tag name validation at the door, blob thresholds unpinned on some create paths, `allow_http` not derived from the endpoint scheme, missing HTTPX timeouts.
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
`maintenance, service-kit, notifications` · med · **blocked:** owner decision, shared with the branch/tag control-event question — whether anyone should be TOLD a table was compacted, or whether it is an audit record only

- *Why open:* Nothing in `summarize` carries reclaimed bytes, there is no metric, and no control event is published per reclaiming sweep. The audit-log half is done (`maintenance_dataset_outcome`, one structured line per dataset per tick), but the rare destructive purge emits `table_purged`/`namespace_purged` while the hourly sweep over every dataset emits none — and there is no `table_maintained` action in `ControlAction`'s 38 values to emit even if it wanted to.
- *Closes when:* Add bytes-reclaimed to `summarize` and export it as a metric; decide with the branch/tag control-event question whether a compaction is an audit record or a notification, and if an event, add `table_maintained` across the three-file ControlAction contract (`service_kit/control_events.py:94` plus emitter/consumer, pinned by `tests/unit/test_control_action_three_file_contract.py`) and emit it from `maintenance/services/sweep.py:486-524` and `catalog/api/v1/endpoints/maintenance.py:39-56`.

**LH-100 · 49 `models/<run>/<id>/` prefixes still file as IncompleteScan coverage gaps, and the `discoveryMaxDepth` lever never reached the running pod**
`maintenance, chart` · med · **blocked:** owner decision — the depth default is a cost/coverage policy call, plus a real release to deploy it

- *Why open:* Two of three landed and were observed (`_lineage_outbox` added to `_CONTROL_PREFIXES`; a concurrent delete below the walk root tolerated), dropping incomplete 71 → 61. The walk stops at depth 3 and cannot tell 'no dataset here' from 'did not look deep enough'; the lever exists (`maintenance.discoveryMaxDepth`, bounded 1..16) but the default is still 3 and `MAINTENANCE_DISCOVERY_MAX_DEPTH` is ABSENT from the running pod because the image was rolled with `kubectl set image` rather than a release.
- *Closes when:* Owner decides whether to raise the `maintenance.discoveryMaxDepth` default above 3 — measured: depth 6 stops nothing on `lance-catalog` and finds no extra datasets, but that is 1 of 93 buckets and `_protected_roots` opens every discovered dataset in every bucket — then deploy the chart half with `make k3s-up` so the setting reaches the pod.

**LH-101 · The sweep has no per-tick budget and no rotated bucket order, so at estate scale the tail is maintained only if the tick has time left**
`maintenance` · med

- *Why open:* Nothing records which buckets a tick actually reached, so silent starvation of the last buckets is undetectable.
- *Closes when:* Give the sweep an explicit per-tick time budget and a rotated bucket order, and report which buckets a tick covered.

**LH-102 · Storage reclamation has never been run live — trash purge must go first, and it is gated on a clean drift report**
`maintenance` · med · **blocked:** a clean, complete drift report (the zero-tuple detector plus the `incomplete` rows)

- *Why open:* The order is load-bearing: a bounded delete of a RECORDED path (trash purge) beats prefix subtraction, and the reclaiming sweep may not run until the drift report is clean — which the missing zero-tuple detector and the 61 `incomplete` rows prevent.
- *Closes when:* Drive `purge_expired_trash` then the reclaiming sweep against a live estate once the drift report is clean, and record the bytes actually reclaimed.

**LH-103 · The maintenance sweep has never been run against a real S3 — its e2e is env-gated and skips**
`maintenance` · med

- *Why open:* Ranked open under Maintenance and 'confirmed open twice'; an env-gated e2e that skips reads identically to a pass.
- *Closes when:* Run the env-gated sweep e2e against a real RustFS/S3 endpoint in the harness and make the gate a hard failure rather than a skip when the endpoint is configured.

**LH-104 · No index is ever built on a governed table — search tunes `nprobes` for one that is not there and `maintenance.indexAckWait` is a 3600s placeholder**
`catalog, maintenance, search, chart` · med

- *Why open:* J7 gave the governed door (`create_index`/`create_scalar_index` publish an `IndexWorkItem` and answer with its id) but nothing actually builds an index on a governed table, so semantic search is a brute-force scan and the `nprobes` tuning is dead. The lane's only end-to-end drive is its unit tests, which is why the ack-wait value is a placeholder rather than a measurement.
- *Closes when:* Drive the queued index-build lane (the J7 worker) against a governed table end to end in-cluster, set `maintenance.indexAckWait` from that measurement, and either honour or remove search's `nprobes` tuning.

**LH-105 · There is no reindex-from-scratch operation in `services/maintenance`**
`maintenance` · med

- *Why open:* Carried as a one-phrase row, so a corrupt or mis-parameterised index can only be repaired by hand.
- *Closes when:* Add a drop-and-rebuild-index operation to `services/maintenance` with a door, a task record and a test.

**LH-106 · Resilience rows exist only inline in `RESILIENCE.md`: the chaos harness was never automated, DLQ sidecar parking was never driven live, and lineage scale-0 restart-replay was never re-verified**
`lineage, chart` · med

- *Why open:* The pull-a-service chaos rows were driven by hand and never encoded (deliberately out of default `make e2e` — they scale shared infra). Gap #2's poison-inject → Dapr `deadLetterTopic` parking has only unit tests (the #83 DLQ drive exercised the OUTBOX surface, not sidecar parking) and the runbook §6.5 it pointed at no longer exists. Honesty-note row 1 (lineage scale-0 → restart-replay under the per-app queue-group components) still awaits its one-shot re-verify on a fresh deploy.
- *Closes when:* Encode the chaos rows as an automated mutating harness kept out of default `make e2e`; drive a poison message live to observe Dapr `deadLetterTopic` parking; re-verify lineage scale-0 → restart-replay on a fresh deploy; and rewrite the dangling §6.5 runbook pointer.

**LH-107 · The catalog is correct only at `replicas=1` because `controlEmit`'s ring buffer and cursor are per-replica, and every stage runner calls it**
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

**LH-111 · No in-flight blob-byte admission budget — the catalog counts requests, not bytes**
`catalog` · med

- *Why open:* A large blob request is admitted on the same terms as a small one, so concurrent large reads can exhaust the process.
- *Closes when:* Add a byte-budget admission control on every blob door answering 503 + `Retry-After`, per the lance-context spec.

**LH-112 · Unknown whether Lance honours a tag that pins a BRANCH version during main cleanup**
`maintenance, catalog` · med

- *Why open:* Unmeasured, and it becomes load-bearing as soon as the sweep maintains branches.
- *Closes when:* A test on `tree/<branch>/` with a root tag, run through main cleanup.

**LH-113 · One 340-line catalog `Settings` class carries every domain's configuration**
`catalog` · med

- *Why open:* Listed OPEN in the Q3 carry-over table and neither re-measured nor struck.
- *Closes when:* Split the catalog `Settings` into per-domain settings blocks, the shape the eight services already share via `GovernedAuthSettings`.

**LH-114 · Multi-base (`base_paths`) is implemented but never exercised, and no test proves cleanup on a shared non-root base spares its sibling**
`maintenance, catalog` · low

- *Why open:* The `base_paths` refusal knowledge is recorded in `orphans.py` and the `rask-lance-catalog` skill, but no test or live run covers a multi-base table, so shared-base cleanup safety is assumed rather than witnessed — and the bases-as-storage-profile work ships on that assumption.
- *Closes when:* Add a multi-base fixture, drive the sweep/orphan scan over it, and add the shared-base cleanup test asserting a sibling survives.

**LH-115 · Manifest flags 16/64 are refused only by the orphan pass, not by the rest of the maintenance surface**
`maintenance` · low

- *Why open:* The refusal knowledge lives in `maintenance/services/orphans.py` and `test_orphan_files.py`, and no other operation consults it.
- *Closes when:* Apply the 16/64 flag refusal to the other maintenance operations (compaction, reclamation, the sweep), sharing one predicate with `orphans.py`.

**LH-116 · Fragment sizing is left at Lance defaults with no per-table lever**
`maintenance` · low

- *Why open:* Ranked open under Maintenance, while §H's preamble records fragment sizing as a deliberately-untouched default that is fine at current scale — a real but non-urgent row.
- *Closes when:* Expose fragment-size configuration on the compaction/maintenance path and record the sizing rationale where the default is chosen.

**LH-117 · The orphan scan has no chart toggle — `MAINTENANCE_ORPHAN_SCAN_ENABLED` is env-only**
`maintenance, chart` · low

- *Why open:* The switch exists only as an environment variable, so it cannot be set through the deploy artifact.
- *Closes when:* Expose `MAINTENANCE_ORPHAN_SCAN_ENABLED` as a `chart/values.yaml` toggle wired through the maintenance template.

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

**CP-007 · Ingest's reads of the estate-default store and of secretless registered stores have no scoped credential now that the ambient root pair is withdrawn**
`ingest, catalog, chart` · med · **blocked:** owner/operator decision — which source buckets, and which scoped identity each gets

- *Why open:* The headline is closed (2026-09-09: the root pair is gone from all five pods; ingest's governed writes and staging ledger are catalog-vended), but three read paths remain: the lineage-outbox staging write (`ingest/lineage.py:227 _outbox_storage_options` returns no keys), `objectstore._s3_prefix`'s `is_estate_default` branch, and any registered store declaring no secret (`without_credentials(options)`). Paths 2 and 3 read buckets an operator registers at runtime, so no deploy-time policy can enumerate them, and narrowing them before path 3 is closed would break ingestion from every secretless store. Latent, not live: `rask-ingest` made zero source reads in 72 hours and registers no stores.
- *Closes when:* Register the source buckets as stores that DECLARE a `secret` naming a scoped identity in the Dapr secret store, so `objectstore._own_store_for` stops falling back to `without_credentials`; the machinery landed 2026-09-08 (`06f0ab21`) and is inert until an operator aims it. The outbox prefix is a constant and can take a narrow identity today (§E1).

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
