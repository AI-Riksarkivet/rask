# open-medallion-workflow — the cascade as Dapr Workflow: Ray compute, and a quality gate a human can answer

Working plan, **2026-08-11**. Unsettled work; this file is deleted when it lands. `docs/` is for
settled architecture only.

**What this is.** A design review of where Dapr Workflow belongs in the medallion cascade, mapped
onto the patterns Dapr documents, and a concrete shape for two things the estate wants and does not
have: **bronze→silver driven by Workflow with Ray as the compute**, and **a silver→gold quality gate
that can wait for a human**.

**Evidence convention.** `path:line` — read from source this pass. `(measured 2026-08-11)` — observed
against the live k3s release or a running process. `UNVERIFIED` — inference, named inline.

**What this does NOT re-litigate.** Workflow was ruled OUT for the maintenance sweep and
§5 analysed Workflow-over-Ray. Both were re-verified this pass and both still hold; §4 is now
*stronger* (its prescribed §2.19/§2.20 fixes landed and delivered the coverage guarantee without an
engine). This document is about the cascade, which neither covered.

---

## 1. The decision, first

| | Verdict |
|---|---|
| **bronze→silver on Dapr Workflow + Ray** | **YES.** It closes a defect pub/sub structurally cannot. |
| **silver→gold quality gate with human approval** | **YES.** The gate exists; today it can only say no, permanently. |
| **silver→silver derivations** | **Not yet.** Same shape as bronze→silver; adopt after that lands. |
| **The maintenance sweep** | **NO** — unchanged, §4. |
| **Replacing pub/sub as the cascade transport** | **NO.** Workflow orchestrates *within* a stage; the bus still carries *between* services. |

---

## 2. Why bronze→silver earns it — the defect, not a preference

`docs/DECISIONS.md`'s criterion is that an engine must buy more than convenience. This buys
correctness, and the gap is measurable in source.

**`services/medallion/src/medallion/services/transform.py:333-349`** — the real-Ray lane:

```python
elif use_ray:
    await submit_stage_job(...)          # :333
    result = await run_in_threadpool(    # :344 — the NEXT statement
        measure_stage, from_uri, to_uri, settings.storage_options()
    )
# …:417-427 sets completed = True and emits COMPLETE
# …:433-448 publishes the next-stage trigger
```

`submit_stage_job` does not wait. Its own docstring
(`services/medallion/src/medallion/services/ray_submit.py:50`) is *"Submit (or re-attach to) … and
RETURN — never block"*, and the callee (`packages/ray-kit/src/ray_kit/submit.py:96-122`) POSTs
`/api/jobs/` and, on a duplicate id, GETs once to re-attach. **There is no poll, no wait, and no
completion observation anywhere on the path.**

So the mover measures the destination, emits COMPLETE, and fires the next stage **before the Ray job
has necessarily written anything**.

**The provenance is decisive.** Commit `1a030fa2` ("delete the completion poll — three copies, and
the surviving mover re-cut") touched five files; `git show 1a030fa2 -- .../transform.py` returns
**empty**. The blocking half was deleted in the callee and the caller — which measures and cascades on
the assumption the callee blocked — was never re-cut.

**Why pub/sub cannot fix this.** The bus's unit of durability is *the message was handled*. It has no
representation of *the work the handler started is finished*. A handler that acks after submitting is
correct by the bus's contract and wrong by the cascade's. Holding the ack across the job's runtime is
explicitly outlawed (A13) and would break `ack_wait` anyway. **"I was at step 3, resume at step 4" is
exactly the line `python-infrastructure`'s reference draws for choosing Workflow over JetStream.**

---

## 3. bronze→silver — the shape

Two documented patterns composed: **task chaining** for the sequence, and the **monitor** pattern for
the wait, because the wait is unbounded and must survive a pod dying.

```python
def stage_run(ctx: DaprWorkflowContext, payload: dict[str, Any]):
    spec = StageSpec.model_validate(payload)
    yield ctx.call_activity(emit_start, input=...)          # lineage START, before anything
    try:
        job = yield ctx.call_activity(submit_stage_job_activity, input=...)   # returns a submission id
        # THE WAIT. A durable timer, not a sleep and not a held ack: the workflow unloads from memory
        # and the runtime wakes it. `poll_job_status` is an activity — it is I/O.
        while True:                                          # bounded by continue_as_new, see §6
            yield ctx.create_timer(timedelta(seconds=spec.poll_seconds))
            status = yield ctx.call_activity(poll_job_status, input=job)
            if status["terminal"]:
                break
        if not status["succeeded"]:
            raise StageJobFailed(status["reason"])
        # ONLY NOW is measuring honest.
        measured = yield ctx.call_activity(measure_stage_activity, input=...)
        yield ctx.call_activity(emit_terminal, input=...)    # lineage COMPLETE, with real facets
        yield ctx.call_activity(publish_next_stage, input=...)  # the cascade, after the fact
        return measured
    except Exception as exc:
        yield ctx.call_activity(emit_terminal, input=<FAIL>)
        raise
```

**What changes, concretely:** `measure_stage`, the COMPLETE emit and the next-stage trigger all move
*after* an observed terminal job state. The lineage graph stops recording completions that had not
happened.

**What Ray still owns** (§5, re-verified): task-level retry, actor restarts, object reconstruction,
and the job's own lifecycle. The workflow does **not** re-implement any of it — it observes the job
and sequences what happens around it. Paying twice for Ray's guarantees is the failure §5 warns about.

**The trigger still arrives on the bus.** `handle_stage` becomes thin: validate, authorize, then
`schedule_new_workflow(instance_id=<deterministic>)` and ack. The bus keeps the between-services job
it is good at; the workflow takes the within-stage job it is good at.

---

## 4. silver→gold — the quality gate, and why it needs a human

**The gate already exists and is already automatic.** `service_kit.lakehouse.quality.assert_quality`
runs `row_count_positive`, `not_null` on the key column, and `blob_resolves` per blob-v2 column. The
mover consumes it at `transform.py` and a failure becomes:

```python
_QUALITY_BLOCKED = {"status": "DROP"}   # transform.py:76
```

with the comment: *"DROP so Dapr doesn't redeliver … no DLQ is configured, so the drop is final — the
failed run in the lineage graph is the audit trail."*

**So the gate can only ever say NO, permanently, with no human able to say otherwise.** That is right
for a corrupt blob pointer. It is wrong for the case the archive actually has: a promotion that is
*unusual* rather than *broken* — a row-count delta outside the expected band, a new value in a
controlled field, a first promotion of a newly ingested volume. Today those are either auto-promoted
(if no assertion covers them) or dropped forever (if one does). There is no third answer.

**The pattern is Dapr's external-system-interaction / human-approval one**, exactly as documented:

```python
def promote_to_gold(ctx: DaprWorkflowContext, payload: dict[str, Any]):
    spec = PromotionSpec.model_validate(payload)
    assertions = yield ctx.call_activity(run_quality_assertions, input=spec)

    if assertions["hard_failed"]:
        # Corrupt, not ambiguous. No human is asked; this is the DROP that is correct today.
        yield ctx.call_activity(emit_terminal, input=<FAIL, assertions>)
        return {"status": "BLOCKED", "assertions": assertions}

    if assertions["needs_review"]:
        yield ctx.call_activity(request_approval, input=...)   # notification -> the inbox plane
        approval = ctx.wait_for_external_event("promotion_decision")
        timeout  = ctx.create_timer(timedelta(hours=spec.approval_hours))
        winner   = yield wf.when_any([approval, timeout])
        if winner is timeout:
            yield ctx.call_activity(emit_terminal, input=<FAIL, "approval timed out">)
            return {"status": "EXPIRED"}
        decision = approval.get_result()
        if not decision["approved"]:
            yield ctx.call_activity(emit_terminal, input=<FAIL, decision>)
            return {"status": "REJECTED", "by": decision["subject"]}

    yield ctx.call_activity(promote, input=spec)
    yield ctx.call_activity(emit_terminal, input=<COMPLETE, assertions, decision>)
    return {"status": "PROMOTED"}
```

**Why a workflow rather than a table and a cron.** The wait is hours-to-days and must survive every
pod restart in between; nothing else in the estate can hold a paused process that long. The
maintenance-sweep contrast applies in reverse here: the sweep re-derives its work each tick and so needs no
resumption, while an approval **is** a plan worth resuming — losing it loses a human's decision.

**Three things this design must get right, and they are where it can go wrong:**

1. **The approval route is a governed door.** `raise_workflow_event` reaching the estate un-gated makes
   "approve" world-callable. It gates on FGA exactly like every other write — the natural relation is
   the one the cascade already checks for this hop, `can_promote` (the silver→gold mover checks it as
   its own identity today). The route is `POST /api/medallion/promotions/{id}/decision`, and the same
   `guard_actor_routes`-class reasoning applies: a workflow-management route mounted outside
   `RASK_API_PREFIX` inherits none of the routers' doors.
2. **The decision must be recorded where the audit lives**, i.e. in the lineage event's facets, not
   only in workflow history — history is now retention-bounded (7d COMPLETED / 30d FAILED, landed this
   session), so it is a cache, and lineage is the durable record (`docs/DECISIONS.md`).
3. **`needs_review` must be a POLICY input, not a code branch.** A threshold compiled into the workflow
   body is a determinism hazard the moment it changes mid-run — the same class as the
   `RASK_INGEST_MAX_*` env reads §2.23 filed. It is resolved by an activity and carried, exactly as
   `resolve_limits` does.

---

## 5. What each documented pattern is worth here

| Pattern | Where it fits | Verdict |
|---|---|---|
| **Task chaining** | `submit → wait → verify → measure → emit → cascade` | **Adopt** — §3 |
| **Monitor** (durable timer + poll) | Waiting on a Ray job with no completion callback | **Adopt** — §3 |
| **External system interaction** | The silver→gold approval | **Adopt** — §4 |
| **Fan-out/fan-in** | Already in use — `ingest_run`'s chunk children | In use; see the §6 costs |
| **Async HTTP (request/poll)** | Already in use — `POST /v1/ingests` + `GET /v1/ingests/{id}` | In use |
| **Compensation / saga** | A promotion that lands rows then fails to register | **Later** — §7 |
| **Eternal / continue-as-new** | The poll loop's bound | **Required** — §6 |

---

## 6. The costs, measured rather than assumed

Everything here was established this session and bounds the design.

- **History is no longer unbounded.** `workflow.stateRetentionPolicy` now collects terminal instances
  (7d completed / 30d failed-terminated). Before it, the live store held 1367 rows with
  `count(expiredate) = 0` going back to the plane's first deploy *(measured 2026-08-11)*. A per-stage
  workflow is affordable **because** that landed; adopting this without it would reintroduce unbounded
  growth on a store nothing collected.
- **An activity result must fit ~4 MiB.** The Dapr worker keeps grpc's default and never raises it
  (reproduced against a real grpc server: `RESOURCE_EXHAUSTED … 5242880 vs 4194304`). Stage payloads
  are URIs and counts, so this is comfortable — but it is why `poll_job_status` must return a *status*,
  never a listing.
- **`terminate_workflow` cannot stop an in-flight activity** — the SDK is explicit. So a cancelled
  promotion cannot yank a running Ray job; it must ask Ray to stop it, and that is an activity.
- **Reminders live in the scheduler's etcd**, a failure domain independent of the state store, with no
  `ActorStateTTL` backstop. The approval timer is a durable timer, so it rides that domain — which is
  why `DaprSchedulerServingNoSidecars` was added this session. **A silent scheduler means an approval
  that never times out.**
- **The poll loop must `continue_as_new`.** An unbounded `while` accumulates one timer + one activity
  result per tick in history forever. At a 30 s poll a day-long job is ~2,880 events. Restart the
  workflow every N polls carrying the submission id.

---

## 7. Slices — each independently shippable, red-first

1. **S1 — close the submit/ack gap.** `stage_run` for bronze→silver: submit, poll to terminal, verify,
   then measure/emit/cascade. No approval yet. This is the defect fix and stands alone.
2. **S2 — `continue_as_new` on the poll loop** plus the history-growth assertion (§6).
3. **S3 — the quality gate, automatic half.** `run_quality_assertions` as an activity returning
   `hard_failed` / `needs_review` / `clean`, policy resolved and carried. Behaviour unchanged: clean
   promotes, hard-failed blocks. Proves the shape with no human in the loop.
4. **S4 — the approval.** `wait_for_external_event` raced with a timer, the FGA-gated decision route,
   the decision recorded in lineage facets, and the notification into the inbox plane (which is built).
5. **S5 — compensation.** The saga for a promotion that lands rows then fails to register — today that
   leaves gold rows with no catalog record.
6. **S6 — silver→silver derivations**, once S1's shape has run in anger.

---

## 8. The four questions, answered — three from precedent, one with a default

These were filed as "owner input needed". Three of them the estate has already answered, in rulings
made this session or already in the code; punting on those was wrong. The fourth needed a measurement,
not a decision. What genuinely remains for an owner is narrower and is stated at the end.

**1. What makes a promotion `needs_review`? — PROPOSED DEFAULT, configurable.**
The only part that is truly archival is the *threshold*, not the *shape*. The shape follows from what
`assert_quality` already computes:

| Outcome | Condition | Behaviour |
|---|---|---|
| **BLOCK** | any hard assertion fails (`blob_resolves`, `not_null` on the key column) | terminal FAIL — no human asked. This is today's `_QUALITY_BLOCKED` and it is correct. |
| **REVIEW** | row-count delta outside `promotionReviewBand` (default ±25%), **or** first promotion of this dataset | wait for a decision |
| **PROMOTE** | otherwise | as today |

`row_count_positive` already exists, so the delta needs only the previous version's count — which the
Lance dataset carries. **First-promotion** is the case worth having even if the band is later widened
to nothing: it is the one where nobody has ever looked, and it fires once per dataset rather than
per run. The band is a values knob so tightening it is a config change, not a deploy.

**2. Must the approver be a DIFFERENT identity from the service that would auto-promote? — YES.**
Not a preference: it is the ruling this session already made one layer down. `can_grant_owner` was
`manage_grants`, so a grant-manager could grant *itself* `owner` — the "administers access, cannot
read the data" persona was one authorized call from holding everything, and that was closed by
refusing a self-directed grant of a rung the caller does not hold. An approval gate whose approver is
the identity that would otherwise have auto-promoted is the same defect wearing a different hat: the
gate renders, audits, and changes nothing. So the decision route refuses a decision whose subject is
the run's own producer identity, exactly as `_refuse_self_elevation` does.

**3. What is the timeout, and what does expiry MEAN? — REJECT, loudly, and it must not be "hold".**
Two independent reasons, both already settled here:
- **Silence is the failure mode this estate keeps getting bitten by** — `list_ingests` rendering an
  authz outage as an empty list, an orphaned drain returning zero messages, a lost reminder that
  simply stops work. An approval that expires into nothing is that same shape. Expiry is therefore a
  terminal FAILED carrying "no decision within Nh", visible in lineage like any other failure.
- **"Hold indefinitely" is not available.** A RUNNING instance is never collected by
  `stateRetentionPolicy` (it only governs terminal states), so an indefinite hold reintroduces exactly
  the unbounded workflow-history growth that was closed this session.

Default **72h** — long enough to cross a weekend, short enough that the state store is not holding
open instances for weeks. A rejected-by-expiry promotion is re-drivable: the data is still in silver.

**4. Does rejection have to cancel the running Ray job? — NO, and the question dissolves on ordering.**
Measured rather than reasoned: `transform.py` runs `submit_stage_job` (:333) → `measure_stage` (:345)
→ `assert_quality` (:366). The gate asserts on the **written** dataset, so by the time anyone is asked
to approve, the job has already finished. There is no long-running job to cancel in the normal case —
and the SDK could not stop its in-flight activity anyway. **No Ray-side stop activity is needed for
S4.** (It would be needed only if an approval were ever placed *upstream* of the compute, which this
design does not do.)

## 9. What is actually left for an owner

1. ~~**The review band's value.**~~ **DECIDED 2026-08-15 — ±25%, plus first-promotion-of-a-dataset.
   Assumed, not measured, and flagged as such.** Nobody has looked at what a normal silver→gold delta
   is on this corpus, so the number is a defensible starting point rather than a finding. It is safe
   to assume rather than block on because of three properties the design already has:

   * the SHAPE does not depend on the value — only the interrupt rate does;
   * it is a values knob, so tightening it is a config change and not a deploy;
   * the first-promotion clause fires once per dataset, so the band's exact width does not decide
     whether anyone ever looks at a new table — which is the case that actually matters.

   **NO CODE SHIPS FOR THIS YET, deliberately.** The consumer is S3's `run_quality_assertions`, and
   S3 is not built. A `promotionReviewBand` value in `values.yaml` today would be config nothing
   reads — the dead-config defect this plane has already been bitten by twice (the orphan-scan lever
   that existed in `config.py` with no path from values, and an S1 state-store scope naming an app-id
   that does not exist). The value lands WITH its consumer, in S3, or not at all.

   Re-open this only with a measurement: the row-count deltas of a few real silver→gold promotions.
   Until then ±25% is the shipped intent.
2. **Q6 — the `lance-ray` rename.** One coordinated rollout (the state store cannot hot-reload), same
   cost whenever it happens. Purely scheduling.

   **RECOMMENDATION 2026-08-15: ride it with S3.** The rename touches component scopes, DLQ topics
   and resiliency targets, and the state store cannot hot-reload — so it needs a restart of every
   scoped app either way. S3 adds `promotionReviewBand` and the quality-gate wiring, which is already
   a chart change plus a mover restart. Doing both in one window costs one rollout instead of two,
   and there is no ordering dependency between them.
