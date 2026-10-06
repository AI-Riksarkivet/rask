# Batch processing — the settled invariants

What a Ray-backed batch plane in this estate carries, and where each property is pinned. Everything
on this page is IMPLEMENTED and covered by a test; a property with no test is not an invariant, it is
an intention.

**What is deliberately NOT here.** This file records INVARIANTS and what they settled. Work in
progress, with its disposition, lives in the root `open_batch_process.md`. They sat on this page for part of
2026-08-22 and were moved back the same day: `docs/` asserts settled, so unfinished work placed here
reads as decided regardless of what the prose around it says.

---

## Landed, and what pins each

| | Invariant | Where it is pinned |
| --- | --- | --- |
| **B1** | History and events carry identifiers and counts — never payloads | No test. Ingest's runtime budgets (`_refuse_oversized_dispatch`, `FANIN_RETURN_BUDGET_BYTES`) refuse a dispatch or fan-in large enough for a payload to matter |
| **B2** | Never pre-gate a Ray submission on capacity or resource labels | No test: it is a rule about code not yet written, and no linter rule expresses it |
| **B3** | The submission id carries the full work identity | `packages/ray-kit/tests/test_submission_id.py` (both axes) **plus** a rendered-chart test — the deploy axis is fed by ONE chart line, and `test_an_unset_code_version_reproduces_the_previous_id_exactly` blesses an empty code on purpose, so deleting that line was indistinguishable from the compatibility path |
| **B6** | Refuse new runs while draining | `service_kit.draining`. No test pins a run door's gate: its behavioural home is a `TestClient` on the producer with `app.state.shutting_down = True` asserting 503 on `POST /produce`, and that test does not exist yet |
| **B10** | Monotonic clocks; the same number lands in the lineage facet | No test. Its behavioural home is a `handle_stage` test on the Ray wake-up trigger that asserts the emitted COMPLETE's duration, and that test does not exist yet |
| **B12** | Randomise iteration order | `tests/unit/test_batch_invariants_are_actually_guarded.py` — both the dataset list and the failure-retry list |
| **B13** | Single-flight claims get chart gates | No test: `stageRunnerReplicas: 1` in both values files is the only guard of the stage runner's process-local `asyncio.Lock`. Its home is a `maximum: 1` in a chart values schema, and the chart ships no schema yet |

**B10 was the only live defect among them.** On the Ray lane the stage runner runs twice — submit, then a
wake-up hours later to measure and emit — and the metric used the watcher's measured span while the
lineage facet used the wake-up's own wall time. The graph recorded seconds for stages that ran hours,
and the wrong number was the one in the durable audit trail. The correct value already existed; it
was computed after the event that needed it.

**B12, B13 and B3 are the more interesting class**: all three WORKED, and nothing would have noticed
losing them. Deleting `random.shuffle(uris)` passed every test, which would silently restore the
starvation the shuffle was written for. `stageRunnerReplicas` could be scaled past 1 with nothing
objecting, though single-flight there is an `asyncio.Lock` and therefore process-local. A guard that
guards nothing is this estate's signature defect, and closing it is worth more than the features
filed beside it — a silently-lost invariant costs the incident it was written to prevent, twice.

---
---

## B11 — the two columns

The distinction that decides whether a value may be read at boot or must ride the spec. Getting it
wrong in the BOOT direction wedges a durable workflow: a value the body branches on, changed between
a run's first execution and its replay, produces an action stream the history does not match.

**BOOT-ENV — worker-derivable, a restart to change.** Read once at process start; identical for every
run the process serves; changing it is a deployment.

* object-store endpoint and credentials (`RASK_S3_*`), which are a property of where the pod runs
* Lance cache caps and reader tuning
* the OTLP endpoint and resource attributes
* FGA store/model ids and the FGA API URL
* topic and pubsub NAMES, and the Dapr app-api-token
* the catalog URL and the service identity a pod claims at it

**LIVE-SPEC — a property of the RUN, resolved once and carried.** Anything a workflow body may branch
on, or that two pods executing one run could disagree about.

* the target table, namespace and tier
* batch size, fragment sizing, actor sizing and concurrency
* every ceiling: `max_units`, `max_run_hours`, `incremental_max_rows`
* enablement flags a run's shape depends on (`quality_review_enabled`, `cascade_via_publish`)
* the promotion review band and the approver
* `code_version` — the deploy axis of the submission id

**The test.** If two pods could execute the same run and read different values, it is LIVE-SPEC and
must be resolved in an activity and pinned in history. `resolve_limits` is the worked example; the
gate that keeps the workflow bodies honest is
`services/ingest/tests/test_workflow_bodies_read_no_env.py`, which refuses an env read inside a
registered workflow body while leaving activities free to read — an activity's result is recorded, so
every replay sees what the first execution saw.
