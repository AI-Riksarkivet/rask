# open_dapr-audit — the Dapr surface: workflows, activities, actors, pub/sub

Working plan, **2026-08-24**, against the working tree at `871b5e14` (+ uncommitted edits, which were
audited as they sit on disk). Unsettled work; **delete this file when the backlog is drained**.
`docs/` is for settled architecture only.

**No code was changed by this audit.** It is a read-only pass whose deliverable is this backlog.

> **PROGRESS (live).** This backlog is being drained under a `/goal` run.
> **6 of 48 closed.** Findings marked **FIXED** below carry the commit and the test that
> pins them. The file is deleted when the count reaches 48.
>
> **FIXED means fixed in HEAD, NOT running in the estate.** The Cascade Status Board
> (2026-08-25) measured the deployed images: ingest and notifications are **351 commits
> behind**, flows 494, compute/controlplane/assist 728; only the lakehouse plane is on HEAD.
> Every fix below is proven offline against HEAD source and reaches the running cluster only
> when its plane is rebuilt. Read a FIXED row as "the defect is out of the code", never as
> "the behaviour is correct in production".

## Why this exists separately from `open_python-audit.md`

That audit swept the estate by SCOPE — one auditor per service, held to a 17-rule calibration sheet
compiled from the `fastapi`, `writing-python` and `python-infrastructure` skills. It produced exactly
**four** `dapr-events` findings. This one sweeps by RULE ID, against the Diagrid `dapr-skills`
checklists that a general reviewer has no reason to know:

| Rule set | Ids | What it catches |
| --- | --- | --- |
| `DWF-DET-001..015` | determinism | code that diverges on REPLAY — wall clock, uuid, random, direct I/O, `os.environ`, unbounded `while` without `continue_as_new` |
| `DWF-ACT-001..011` | activities | retried side effects without an idempotency key, swallowed exceptions, payloads persisted into history |
| `DWF-MGT-001..015` | management | missing start/status/terminate/pause/resume/raise-event, unbounded completion waits, purge without a terminal-state check |

Actors and pub/sub have no Diagrid checklist, so those lanes carry rule `N/A` and were judged against
the Dapr actor model (turn-based concurrency, state-store scoping, the `@actormethod` wire-name trap,
timers vs reminders) and JetStream semantics (ack contract, at-least-once idempotency, DLQ, scoping).

## How this was produced (so you can trust or re-run it)

The three `dapr-skills` review checklists were read in full BEFORE any code was opened, alongside the
estate's own standing rulings (secret store fail-closed; the cascade head is the ONE bronze-write emit;
the platform knows no workload; `notifiable()` acking an untargetable event is deliberate). Nine
auditors then read the Dapr surface — 2 411 lines of workflow bodies, 4 847 lines of actors, ~12 pub/sub
subscribers and the chart's component/subscription surface. Every finding was handed to a **separate
adversarial verifier** briefed to REFUTE it: open the cited lines, check the finding is in the scope its
rule requires (a determinism rule fires only in WORKFLOW scope; the same call inside an activity is
correct), and engage with the code's own comments, which in this estate frequently record a decision.
18 agents, ~2.7 M tokens, 731 tool calls.

**Verdicts.** `CONFIRMED` = reproduced exactly as stated. `ADJUSTED` = the mechanism is real but the
severity or framing was corrected by the verifier, and the corrected form is what appears here.
`REFUTED` findings were dropped. One was refuted outright; 21 were adjusted.

## Scorecard

**48 findings survived verification** (7 critical / 16 warning / 25 info).

| Lane | Findings | Critical | Warning | Info | Rules checked clean |
| --- | ---: | ---: | ---: | ---: | ---: |
| `det-medallion` — medallion — workflow bodies (DWF-DET) | 4 | 0 | 1 | 3 | 15 |
| `det-ingest` — ingest — workflow bodies (DWF-DET) | 5 | 2 | 1 | 2 | 16 |
| `det-act-flows` — flows — workflow + activities (DWF-DET + DWF-ACT) | 3 | 0 | 2 | 1 | 23 |
| `act-medallion` — medallion — activities (DWF-ACT) | 8 | 1 | 2 | 5 | 6 |
| `act-ingest` — ingest — activities (DWF-ACT) | 5 | 1 | 1 | 3 | 6 |
| `mgt` — management endpoints, all three apps (DWF-MGT) | 10 | 0 | 5 | 5 | 10 |
| `actors-annotator` — annotator — virtual actors | 5 | 1 | 1 | 3 | 9 |
| `actors-notifications` — notifications — virtual actors | 4 | 1 | 1 | 2 | 9 |
| `pubsub` — pub/sub + Dapr components | 4 | 1 | 2 | 1 | 15 |
| **total** | **48** | **7** | **16** | **25** | |

## What this audit did NOT find (read this before the backlog)

The Dapr surface is in better shape than the volume above suggests, and the determinism discipline in
particular holds up:

- **The workflow bodies are replay-clean.** Across the three workflow modules the lanes checked and
  cleared 15, 16 and 23 determinism rules respectively. No wall-clock read, no `uuid4()`, no `random`,
  no direct network or file I/O, no `asyncio.gather` in workflow scope.
- **The code is already rule-aware.** `services/medallion/src/medallion/workflow.py` cites
  **`DWF-DET-013`** by name in its own module docstring when explaining why its poll loop is bounded and
  why it reaches for `ctx.continue_as_new` rather than `while True`. `ctx.is_replaying` guards log lines;
  deadlines derive from `ctx.current_utc_datetime`; timers are `ctx.create_timer`, never `sleep`.
- **`DWF-MGT-006` is satisfied.** Exactly one `ctx.wait_for_external_event` exists in the estate (the
  medallion promotion review) and a reachable `raise_workflow_event` route wakes it with a matching
  event name — the failure mode that hangs an instance forever is not present.
- **The `WorkflowRuntime` is constructed lazily on purpose**, and `flows/runtime.py` documents why:
  building it eagerly opens a gRPC channel to a sidecar that may not exist.

The problems concentrate in four places: **the return leg of the ingest fan-in** (unbounded payloads
through workflow history), **terminate paths that skip their own cleanup**, **actor turn re-entry**, and
**the cascade head's fail-open publish**.

---

## The findings

### Critical — 7

<details><summary><b>The fan-in fragment list is unbounded — the 4 MiB gRPC ceiling is measured on dispatch only</b> <i>(act-ingest, rule DWF-ACT-004, CONFIRMED)</i></summary>

**Sites:** `services/ingest/src/ingest/workflow.py:665`, `services/ingest/src/ingest/workflow.py:692`, `services/ingest/src/ingest/workflow.py:793`, `services/ingest/src/ingest/runtime.py:377`

**Why it matters.** `enumerate_chunks` is guarded by a measured budget (`CHUNK_DISPATCH_BUDGET_BYTES`, 3 MiB under the 4 MiB grpc default that `GRPC_MAX_MESSAGE_BYTES`:846 names) because one activity payload crosses the sidecar as one gRPC message. The RETURN direction has no such guard. Every chunk's `ChunkResult.fragments` carries the full serialized `FragmentMetadata` JSON for that chunk (workflow.py:793), the parent flattens all of them (workflow.py:665) and passes the whole list as `finalize`'s INPUT (workflow.py:692) — so the list is persisted in history at least twice and re-delivered to the worker on every parent replay. I measured the real record: one fragment manifest against BRONZE_SCHEMA on pylance 9.0.0 serializes to 413 bytes (~445 after the list re-escapes it), so the ceiling is ~9,400 fragments. Fragments per run = units / rows-per-fragment, and rows-per-fragment is min(fragment_rows=1024, fragment_bytes=256 MiB / payload size) — for the 20 MB scans this schema is built for (`blob_field`, dedicated tier &gt;= 2 MiB) that is 12 rows, so a 113,000-unit media harvest already exceeds 4 MiB, a 2 MB-page harvest crosses at ~1.2M units, and even the small-record extreme crosses at ~9.6M against an advertised scale of 10M (the same 10M `CHUNK_SIZE` was raised to 10000 for on 2026-08-24). The worker's own channel is where it breaks: verified against the installed SDK, `_durabletask/internal/shared.py:129` merges only `DEFAULT_GRPC_KEEPALIVE_OPTIONS` and `WorkflowRuntime.__init__` never passes `channel_options`, so grpc's 4 MiB `max_receive_message_length` stands. The run therefore fetches, validates and stages every byte and only then wedges at the fan-in with RESOURCE_EXHAUSTED from inside the SDK, burning four ACTIVITY_RETRY attempts with nothing naming a knob — the exact failure `_refuse_oversized_dispatch` exists to make impossible in the other direction. `test_dispatch_payload_scales_by_chunk.py`, `test_dispatch_ceiling_at_real_scale.py` and `test_enumeration_dispatch_ceiling.py` all measure descriptors; none measures fragments, and the first file's headline claim ('the dispatch payload is O(chunks), not O(units)') is true of descriptors and false of the fragment list travelling the other way.

**Fix.** Stop carrying fragment JSON through workflow history. `finalize_run` already declares `discover_staged` to be 'STORAGE TRUTH, and it is the ONLY truth' (runtime.py:404-413) and the worker already writes each batch's fragment list into the run's staging manifest before acking — so `drain_chunk` can return counts (or a manifest pointer, exactly the §2.13 move that took the unit list out of the descriptors) and let `finalize` read the prefix. If the carried list is kept as the unreadable-staging fallback, measure it at the point of use the way `_refuse_oversized_dispatch` does and refuse before the payload exists, and add the fan-in direction to the dispatch-ceiling suite so a growing fragment record fails there instead of in production.

**Evidence.**

```
workflow.py:665  `fragments = [f for r in parsed for f in r.fragments]`
workflow.py:688-694  `outcome: dict[str, Any] = yield ctx.call_activity(\n            finalize,\n            input={\n                "spec": spec.model_dump(),\n                "fragments": fragments,`
workflow.py:793  `fragments=[str(fragment) for fragment in (drained.get("fragments") or ())],`
```

**Verifier (CONFIRMED).** Reproduced, and the real ceiling is TIGHTER than claimed.

The asymmetry is real at HEAD. Dispatch is guarded: `workflow.py:863 _refuse_oversized_dispatch` measures `len(json.dumps(chunks).encode())` against `CHUNK_DISPATCH_BUDGET_BYTES: int = 3 * 1024 * 1024` (workflow.py:856) with the named reason at 846-850 ("`WorkflowRuntime` exposes no `channel_options` … so this is what an activity result is measured against on the way back to the sidecar"). The return direction has NO equivalent: `workflow.py:665  fragments = [f for r in parsed for f in r.fragments]`, then `workflow.py:692  "fragments": fragments,` as `finalize`'s input. Note the contrast three lines below 665 — the ERROR dict is explicitly bounded at the merge ("BOUNDED at the merge, not merely at each child … N * MAX_REPORTED_ERRORS in the parent's history") and the fragment list beside it is not. That is the author bounding one of the two lists that ride the same history and missing the other; there is no comment, test or plan doc anywhere in services/ingest recording the fragment direction as considered.

SDK claim verified in the installed 1.18.3 tree, not from memory: `dapr/ext/workflow/_durabletask/internal/shared.py:129  merged = dict(DEFAULT_GRPC_KEEPALIVE_OPTIONS)` (line 90 shows it is four keepalive tuples, no size keys), and `dapr/ext/workflow/workflow_runtime.py:141-152` constructs `worker.TaskHubGrpcWorker(...)` passing host/metadata/secure_channel/log/interceptors/concurrency_options and NO `channel_options` — although `_durabletask/worker.py:322` accepts one and forwards it at 454-458. So grpc's 4 MiB `max_receive_message_length` stands on the worker's `GetWorkItems` stream, and `_durabletask/worker.py:901` shows the orchestrator work item carries `req.pastEvents` — the FULL history, every time.

I measured the record myself rather than accepting the figure: built the real bronze batch through `ingest.worker.units_to_table` and `lance.fragment.write_fragments(**CREATION_FLAGS)` on the installed pylance (10.0.0, not the 9.0.0 the finding names) — one fragment manifest serializes to 410 bytes, 446 inside a JSON list. The finding said 413/~445; close enough that its arithmetic holds.

Where I DISAGREE is that the finding is too generous. The parent's history carries the list twice: once inside each child's `SubOrchestrationInstanceCompleted` output (`workflow.py:793  fragments=[str(fragment) for fragment in (drained.get("fragments") or ())]`) and again in the `finalize` TaskScheduled input at 692. At ~446 bytes per fragment per copy the parent's own work item crosses 4 MiB at roughly 4,700 fragments, not 9,400. Fragments per run = units / min(fragment_rows, fragment_bytes / payload size), and `sizing.py:53-58` gives the real defaults — `RASK_INGEST_FRAGMENT_ROWS` 1024, `RASK_INGEST_FRAGMENT_BYTES` 256 MiB — so the 20 MB scans `sizing.py:36` says the byte ceiling exists for give 13 rows/fragment and the wedge lands near 61,000 units, and even the small-record extreme (1024 rows/fragment) crosses at ~4.8M against docstrings that advertise million-unit harvests (workflow.py:903-905, 779-785).

Failure sequence: every chunk fetches, validates, stages and acks; the last child completes; the sidecar tries to hand the parent a work item whose `pastEvents` exceed 4 MiB; the worker's channel raises RESOURCE_EXHAUSTED, the stream reconnects (`_durabletask/worker.py:524-526`), the sidecar redelivers the same oversized item after lock expiry, forever. A permanently stuck instance after the whole harvest's bytes have been paid for, with nothing naming a knob — exactly what `_refuse_oversized_dispatch` exists to make impossible in the other direction. Severity critical is right by the rubric (stuck instance). The three named tests do measure only descriptors — `test_dispatch_payload_scales_by_chunk.py` builds `ChunkSpec` dicts and its only mention of fragments is the `sizing` fixture at lines 42-44.


**FIXED 2026-08-25 — same fix.** This is the same defect the `det-ingest` lane filed; one bound
closes both. Recorded on both rows rather than merged, so the count stays honest about what was
found and what was fixed.

</details>

<details><summary><b>An approved promotion on a TERMINAL tier never moves the tag — and lineage says PROMOTED</b> <i>(act-medallion, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:904`, `services/medallion/src/medallion/workflow.py:1036`, `services/medallion/src/medallion/workflow.py:824`, `services/medallion/src/medallion/services/promotion_hold.py:66`, `chart/values.yaml:1082`, `chart/values.yaml:1089`

**Why it matters.** The uncommitted edit at workflow.py:1036 dropped `and settings.cascade_via_publish`, making `publish_promotion` unconditionally the CATALOG TAG MOVE when the spec carries a version. But `promotion_review` still calls that activity only inside `if spec.pub_topic:` — a gate written for the deleted trigger-driven world, where `pub_topic` WAS the promotion mechanism. `hold_spec` fills `pub_topic` from `settings.pub_topic` (promotion_hold.py:66), and the chart ships `pubTopic: ""` on BOTH terminal movers: `silver-to-gold` (values.yaml:1082, `toDataset: gold$catalog`, `requiredAction: can_promote`) and `media-to-silver` (values.yaml:1089). Concrete sequence: silver→gold writes gold$catalog v7; the band breaches (`review_reasons` non-empty) so `gate_decision` returns HOLD before it ever reaches PUBLISH; `publish_hold` ships a PromotionSpec with `version=7, pub_topic=""`; the producer opens `promotion-&lt;token&gt;`; a validator POSTs `/promotions/{id}/decision` with approved=true; `resolve_review_policy` says `review`, the external event arrives, `decided_by` is set, approval passes every refusal check — and then `if spec.pub_topic:` is FALSE, so `_resume_publish` is never called. The workflow proceeds straight to `emit_promotion_outcome` with `{"status": "PROMOTED"}`, which builds a COMPLETE RunEvent stamped `lance.promotion_status = PROMOTED` and `promotion_decided_by = &lt;the validator&gt;`. The `published` tag on gold$catalog still points at the pre-hold version, forever; every consumer reading `published` keeps serving stale data; and the durable audit record asserts a promotion that physically never happened. `PromotionSpec.pub_topic`'s own comment (workflow.py:824) still says an empty topic means "an approval then records the decision and promotes nothing, which is the honest outcome for the last tier" — true while promotion was a trigger, a lie now that promotion is a tag move. This is invisible to the suite: `tests/test_promotion_resume.py:42` fixes `"pub_topic": ""` with `to_dataset: "gold$catalog"` and calls `workflow.publish_promotion(...)` DIRECTLY, so it proves the activity works on exactly the spec the orchestrator will never hand it — the same unreachable-branch shape `gate_decision.py`'s module docstring records the band `elif` having had.

**Fix.** Stop gating the resume on `pub_topic`. Call `publish_promotion` whenever the approval is granted (`yield ctx.call_activity(publish_promotion, ...)` unconditionally on the approved path) and let the activity's own `if spec.version:` decide tag-move vs. legacy-trigger — `publish_promotion` already returns after the tag move without touching `pub_topic`, and its trigger fallback already reads `spec.pub_topic`, so it is the correct place for that branch. Then rewrite the `PromotionSpec.pub_topic` comment: empty now means "terminal — nothing to WAKE", not "nothing to promote". Add a `promotion_review` test with `pub_topic=""` and `version=7` that asserts `publish_promotion` is among the scheduled activities, so the guard cannot come back.

**Evidence.**

```
workflow.py:904-911
    if spec.pub_topic:
        yield ctx.call_activity(publish_promotion, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
    yield ctx.call_activity(
        emit_promotion_outcome,
        input={"spec": spec.model_dump(), "outcome": {"status": "PROMOTED", "decided_by": decided_by}},
        retry_policy=ACTIVITY_RETRY,
    )
    return {"status": "PROMOTED", "decided_by": decided_by, "reasons": spec.reasons}

workflow.py:1034-1036 (uncommitted)
    spec = PromotionSpec.model_validate(payload)
    settings = get_settings()
    if spec.version:

chart/values.yaml:1082
    - { name: silver-to-gold, ... toDataset: gold$catalog, ... pubTopic: "", requiredAction: can_promote, ... }
```

**Verifier (CONFIRMED).** Reproduced end to end.

1. The orchestrator gate is still there, unchanged — workflow.py:904-911:
```
    if spec.pub_topic:
        yield ctx.call_activity(publish_promotion, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
    yield ctx.call_activity(
        emit_promotion_outcome,
        input={"spec": spec.model_dump(), "outcome": {"status": "PROMOTED", "decided_by": decided_by}},
```
2. The uncommitted edit is exactly as quoted (`git diff services/medallion/src/medallion/workflow.py`): `- if spec.version and settings.cascade_via_publish:` → `+ if spec.version:`, and `cascade_via_publish` is deleted from config.py, so `publish_promotion` is now unconditionally the tag move whenever a version is present.
3. The spec really does carry an empty topic and a real version on a terminal mover. transform.py:1057-1069 calls `promotion_hold.hold_spec(..., version=result.version if result else 0)`; promotion_hold.py:66 sets `pub_topic=settings.pub_topic`; chart/values.yaml:1082 `silver-to-gold ... toDataset: gold$catalog ... pubTopic: "", requiredAction: can_promote` and :1089 `media-to-silver ... pubTopic: ""`.
4. The HOLD is reachable on a terminal mover: `gate_decision` returns HOLD on `band_reasons` BEFORE any terminal/pub_topic consideration (gate_decision.py: `if failed_assertions: BLOCK` / `if band_reasons: HOLD`), so a terminal stage with a band breach holds with `version=N, pub_topic=""`.
5. Consequence: approval → `if spec.pub_topic:` False → no `_resume_publish` → `published` tag on gold$catalog stays at the pre-hold version, while `emit_promotion_outcome` builds a COMPLETE run event with `lance["promotion_status"] = "PROMOTED"` and `lance["promotion_decided_by"]` (workflow.py:1100-1104). The tag IS the readiness boundary — catalog_register.py: "A commit makes the output readable; this is what makes it READY" — so consumers keep serving the pre-hold version forever.

The repo documents this exact remaining gap itself. services/medallion/tests/test_promotion_review_has_a_live_path.py:25-28: "an approved hold resumes via `workflow.publish_promotion` -&gt; `spec.pub_topic`, and under `cascadeViaPublish` there is no `pub_topic` for it to publish to — the resume must call that publish door with the accepted assertion names instead." The ACTIVITY was ported; the CALLER's gate was not.

The comment at workflow.py:824 ("empty means terminal — an approval then records the decision and promotes nothing") is a recorded decision from the trigger world, and it is contradicted by test_promotion_resume.py's own module docstring ("An approved promotion resumes by MOVING THE TAG, not by firing a trigger nothing listens for") — so it is stale prose, not a standing ruling that refutes the finding.

Invisibility confirmed: test_promotion_resume.py:42 uses `"pub_topic": ""` with `to_dataset: "gold$catalog"` and calls `workflow.publish_promotion(cast("Any", None), _spec().model_dump())` directly; test_promotion_review.py:86 drives the orchestrator with `"pub_topic": "medallion.gold"` — a topic the chart no longer renders. No test drives promotion_review with an empty topic.


**Re-checked at `b4d9ba3a` (after the audit's base commit).** Still reproduces, and it is no longer an uncommitted edit — the change landed. The line numbers in the evidence below are from the working tree as audited; at `b4d9ba3a` the gate is `services/medallion/src/medallion/workflow.py:904` `if spec.pub_topic:` guarding `:905` `yield ctx.call_activity(publish_promotion, ...)`. The chart still ships `pubTopic: ""` on both terminal movers, so the finding stands unchanged.


**FIXED 2026-08-25.** The `if spec.pub_topic:` gate is gone; `publish_promotion` is called on
every approved path and picks tag-move vs legacy trigger on `spec.version`, which it already did
internally. The gate was a condition that outlived its world: it was written when the topic WAS
the promotion mechanism, and under a tag-driven cascade the tag move is.

**This needed an owner ruling, because the code documented the bug as intentional.**
`PromotionSpec.pub_topic` said "empty means terminal — an approval then records the decision and
promotes nothing, which is the honest outcome for the last tier", while the field directly below
it said `version` exists so a resume can publish the reviewed version. The chart settles it:
`silver-to-gold` ships `toDataset: gold$catalog`, `requiredAction: can_promote`, `pubTopic: ""` —
the tier gated on *can promote* is the one that promoted nothing. Ruling (owner, 2026-08-25):
the finding is right; the docstring is stale and is corrected here.

**A second-order edge is closed with it.** Removing the caller's filter makes
`publish_promotion` reachable for a version-0 hold on a terminal tier — a pre-migration hold with
no tag to move and no topic to fire — which would have published to `topic_name=""`. That is not
a promotion, just a malformed publish nothing subscribes to. It now logs
`medallion_promotion_has_no_resume_path` and returns.

**No replay hazard, unlike the ingest terminate fix.** Removing an `if` around a call does not
change the action sequence, and `tests/unit/test_workflow_action_order.py` passes unchanged —
so this deploys without draining in-flight instances.

Two RED tests: `test_an_approved_promotion_on_a_TERMINAL_tier_STILL_MOVES_THE_TAG` (the
workflow reaches the resume) and `test_it_does_not_fire_a_trigger_at_an_EMPTY_topic` (the edge
the removal opens).

</details>

<details><summary><b>Saving a draft does NOT renew the lease — the renewal branch is on a path no client uses</b> <i>(actors-annotator, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/annotator/src/annotator/projects/actor.py:271`, `services/annotator/src/annotator/projects/actor.py:339`, `services/annotator/src/annotator/projects/actor.py:400`, `services/annotator/src/annotator/api/v1/endpoints/tasks.py:379`, `frontend/microfrontends/annotator/src/lib/projects/draft-sync.ts:46`, `tests/unit/test_annotation_task_actor.py:253`

**Why it matters.** There are two `save_draft`s. The one that renews the lease is the `fire()` branch (`elif event == "save_draft": ... await self._arm_lease(seconds)  # a save RENEWS the lease`, actor.py:271-274). The one that actually writes a draft is `AnnotationTaskActor.save_draft` (actor.py:339-402), which validates state/holder/etag, writes DRAFT_KEY, saves, and returns — it never touches `task.lease_expires_at`, never calls `_arm_lease`, and never writes TASK_KEY at all. The only production draft-write path reaches the second one: the canvas calls `syncTaskDraft` -&gt; `saveDraft` -&gt; `PUT /tasks/{id}/draft` (tasks.remote.ts:104) -&gt; `tasks.py:379 await actor.save_draft(...)`. Nothing in the frontend ever POSTs `event: "save_draft"` to `/tasks/{id}/events` (`bulk-events.ts:24` explicitly excludes it: "save_draft belongs to the canvas"). Concrete failure: gina fires `claim` at 12:00 on a project with `lease_seconds=1800`; `_arm_lease(1800)` registers the one-shot `lease` reminder. She annotates continuously, saving every 60 s through `PUT /draft` — 30 successful writes, revision 1..30, none of which re-arms anything. At 12:30 the reminder fires; `receive_reminder` loads the task, sees `TaskState.CLAIMED`, and fires `lease_expired`, which nulls `assignee` and `lease_expires_at` and returns the task to the pool. Her 12:31 save now hits `if task.state is not TaskState.CLAIMED: raise IllegalTransition("draft", task.state.value, "save_draft")` -&gt; 409, and the queue has been rendering the task as EXPIRED since 12:30 (`lease.test.ts:31`, "a lapsed lease is EXPIRED, never held"). Another annotator can `claim` it out from under her. The estate's own unit test asserts the opposite property but drives the wrong door: `test_saving_a_draft_renews_the_lease` calls `actor.fire({"event": "save_draft", ...})` and asserts `len(actor.reminders) == 2` — exactly the mocks-stay-green shape the module docstring warns about elsewhere. Three docstrings state the false invariant as fact (actor.py:14-15 "re-registered on each claim/save", actor.py:274, actor.py:444 "`save_draft` renews the lease").

**Fix.** Renew inside `AnnotationTaskActor.save_draft`: after the etag check passes and before returning, set `task.lease_expires_at = now + timedelta(seconds=int(payload.get("lease_seconds") or task.lease_seconds))`, `await self._store(task)` and `await self._arm_lease(seconds)` — arming before the store, mirroring `fire`'s arm-early rule. Add a RED test that drives `actor.save_draft(...)` (not `actor.fire`) and asserts the lease reminder was re-armed and `lease_expires_at` moved. Either keep the `fire` branch for a heartbeat caller or delete it, but the two must not disagree.

**Evidence.**

```
actor.py:271-274 —
        elif event == "save_draft":
            seconds = int(payload.get("lease_seconds") or task.lease_seconds)
            task.lease_expires_at = now + timedelta(seconds=seconds)
            await self._arm_lease(seconds)  # a save RENEWS the lease

actor.py:400-402 (the end of the real `save_draft` — no lease, no TASK_KEY write) —
        await self._state_manager.set_state(DRAFT_KEY, draft.model_dump_json())
        await self._state_manager.save_state()
        return draft.model_dump(mode="json")

tests/unit/test_annotation_task_actor.py:253-257 —
async def test_saving_a_draft_renews_the_lease() -> None:
    await actor.fire(_verified({"event": "claim", "actor": "gina", "lease_seconds": 60}))
    await actor.fire(_verified({"event": "save_draft", "actor": "gina", "lease_seconds": 60}))
    assert len(actor.reminders) == 2, "save_draft must re-arm, not let the lease run down"
```

**Verifier (CONFIRMED).** Two doors exist and only the unused one renews. `actor.py:271-274`:
```
        elif event == "save_draft":
            seconds = int(payload.get("lease_seconds") or task.lease_seconds)
            task.lease_expires_at = now + timedelta(seconds=seconds)
            await self._arm_lease(seconds)  # a save RENEWS the lease
```
The real draft writer `AnnotationTaskActor.save_draft` (actor.py:339-402) ends at
```
        await self._state_manager.set_state(DRAFT_KEY, draft.model_dump_json())
        await self._state_manager.save_state()
        return draft.model_dump(mode="json")
```
— it never touches `task.lease_expires_at`, never calls `_arm_lease`, never writes TASK_KEY. The production path is confirmed: `annotator.svelte.ts:1731 _syncTaskDraft` -&gt; `draft-sync.ts:46 saveDraft` -&gt; `tasks.remote.ts:104 PUT /tasks/{id}/draft` -&gt; `tasks.py:379 await actor.save_draft(...)` (and `tasks.py:455` for import, same). I grepped the whole annotator zone for a client that POSTs `event: "save_draft"` to `/tasks/{id}/events`: there is none — every occurrence is an EXCLUSION (`bulk-events.ts:24 NOT_BULK = ['assign','request_changes','save_draft']`, `TaskQueue.svelte:391`, `QuickView.svelte:56`). No renewal/heartbeat exists anywhere: grep for `renew|heartbeat|keepalive` across `services/annotator/src` and the zone returns only the three prose comments that assert the false invariant (machines.py:53, actor.py:74, actor.py:444). Defaults make it reachable in ordinary use: `Task.lease_seconds: int = 1800` (models.py:238) and autosave fires on `AUTOSAVE_IDLE_MS = 4000` — 30 minutes of continuous saving re-arms nothing, `receive_reminder` (actor.py:424-437) then loads a still-CLAIMED task and fires `lease_expired`, nulling `assignee`/`lease_expires_at`. The next save hits `if task.state is not TaskState.CLAIMED: raise IllegalTransition("draft", task.state.value, "save_draft")` -&gt; 409, and the task is back in the pool where another annotator can claim it and whole-shape-set-replace the draft — the exact loss the module docstrings promise against. `tests/unit/test_annotation_task_actor.py:253-257` asserts the property through `fire()`, the door no client uses, so the suite stays green over the defect. Severity critical stands: a documented invariant is false on the only live path and in-progress work is taken from an active annotator.


**FIXED 2026-08-25.** `AnnotationTaskActor.save_draft` now renews: it sets
`lease_expires_at` from `payload.lease_seconds or task.lease_seconds`, calls `_arm_lease`
BEFORE persisting (the same arm-early rule `fire` follows — if the store then fails the reminder
is armed against a still-CLAIMED task, which is safe, while the reverse order strands a claimed
task with no self-expiry), and commits the draft and the renewed task in one `_store`.

The RED test was the EXISTING one, corrected rather than added:
`test_saving_a_draft_renews_the_lease` drove `actor.fire({'event': 'save_draft'})` — a door
`bulk-events.ts` explicitly excludes — so it asserted the right property through a path the
product never takes. Pointed at `actor.save_draft(...)` it failed with
`AssertionError: save_draft must re-arm the lease reminder — assert 1 == (1 + 1)`, the claim's
reminder being the only one armed. It now asserts on the STORED task, because `save_draft`
returns the draft and a renewal that never reached `TASK_KEY` would satisfy any assertion made
on the return value.

`test_the_events_door_and_the_canvas_door_agree_about_renewal` is new and pins the two doors to
one answer, since the finding's real shape was two implementations disagreeing. The two
docstrings stating the false invariant as fact were corrected in the same change.

</details>

<details><summary><b>The digest tick re-enters its own actor: `_send_digest` calls back into InboxActor/&lt;self&gt; while holding that actor's turn, with reentrancy disabled everywhere</b> <i>(actors-notifications, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/notifications/src/notifications/inbox_actor.py:612`, `services/notifications/src/notifications/api/channels.py:233`, `services/notifications/src/notifications/api/channels.py:249`, `services/notifications/src/notifications/proxies.py:225`

**Why it matters.** The `digest` reminder is delivered by daprd as `PUT /actors/InboxActor/&lt;id&gt;/method/remind/digest`, and daprd holds that actor's turn lock for the whole callback. Inside the callback `_send_digest` obtains `digest_push()` — which is `make_push(table, open_inbox=inbox_for, defer=False)` (proxies.py:225) — and calls `await push(self._subject(), payload)`. `self._subject()` is `decode_subject(self.id.id)`, so `inbox_for(subject)` builds `inbox_actor_id(subject) == encode_subject(subject) == self.id.id`: the SAME actor type and the SAME actor id. `push` then issues `await inbox.get_prefs()` (channels.py:233) — a fresh sidecar invocation of `GetPrefs` on the actor whose turn is already held by the reminder — and, past that, `claim_channel` (channels.py:249) does it again. Actor reentrancy is off: `grep -rn 'reentran|ActorRuntimeConfig|set_actor_config' services/ packages/ chart/` returns nothing, so `ActorRuntimeConfig.__init__(reentrancy=None)` omits the `reentrancy` key from `/dapr/config` (config.py:146 `if self._reentrancy:`) and daprd's default is disabled ('If not provided, reentrancy is diabled'). Concrete sequence on any deployment with `RASK_NOTIFICATIONS_CHANNELS` non-empty (the chart wires email+slack in `templates/notifications-channels.yaml`) and a subject whose prefs carry `digest_seconds`: a non-failure notification arrives → `make_push` defers and calls `arm_digest({'seconds': N})` → N seconds later the one-shot fires → `drain_digest()` runs in-turn and writes `DIGEST_KEY={'pending': False}`, CLOSING the window and returning the batch → the first `await push(...)` blocks on the actor lock held by this very callback → it hangs until `DAPR_HTTP_TIMEOUT_SECONDS` (60, dapr/conf/global_settings.py:37) → the exception is swallowed by `except Exception: logger.exception('inbox_digest_send_failed')`. Outcome: every digested notification is drained and never sent — permanently, because the window is already marked not-pending and only a NEW arrival re-arms it — and for those 60 seconds every other call for that subject (the badge poll, `GET /inbox`, a bus `Deliver`) queues behind the stuck turn. The file's own comment reasons at length about deliberately holding the turn for an SMTP conversation, but nothing in it notices that the send path's first act is an invocation of this same actor. No test can see it: `tests/test_channels.py` drives `make_push` with `open_inbox=lambda _s: inbox` returning a plain in-process `_DigestInbox`, which cannot express the sidecar's per-actor lock, and `_send_digest` itself is never exercised end-to-end.

**Fix.** Do not invoke this actor from inside its own turn. `drain_digest()` already returns the batch precisely so the caller can send outside the turn — apply the same rule to the prefs read and the claim: have `_send_digest` read `PREFS_KEY` directly off `self._state_manager` and claim channels by mutating its own pointer rows in-turn, then hand the fully-resolved (destination, rendered message) list to a sender that touches no actor proxy. Equivalently, move the whole digest send out of `receive_reminder` (e.g. the reminder only marks the window due; the cron route or a channel worker drains and sends from outside any turn). Enabling `ActorReentrancyConfig(enabled=True)` would also unblock it, but it is the wrong lever here — it would license the pattern generally and this file explicitly rests on turn-based concurrency being the lock. Whichever is chosen, add a test that drives `_send_digest` with an `open_inbox` that raises if it is handed this actor's own id.

**Evidence.**

```
inbox_actor.py:604-614
        from notifications.proxies import digest_push

        push = digest_push()
        if push is None:
            return
        try:
            drained = await self.drain_digest()
            for payload in drained["pointers"]:
                await push(self._subject(), payload)
        except Exception:
            logger.exception("inbox_digest_send_failed")

channels.py:231-233
    async def push(subject: str, payload: dict[str, Any]) -> None:
        inbox = open_inbox(subject)
        prefs = await inbox.get_prefs()

proxies.py:225
    return None if table is None else make_push(table, open_inbox=inbox_for, defer=False)
```

**Verifier (CONFIRMED).** Every link verified at HEAD.

(1) The reminder runs in-turn: `inbox_actor.py:395-406` — `async def receive_reminder(...)` / `if name == DIGEST_REMINDER: await self._send_digest(); return`. daprd delivers a reminder as an actor method invocation and takes the actor lock for it.

(2) `_send_digest` (inbox_actor.py:580-614) does `from notifications.proxies import digest_push; push = digest_push(); ... drained = await self.drain_digest(); for payload in drained["pointers"]: await push(self._subject(), payload)`.

(3) Same actor id: `_subject()` (inbox_actor.py:204-212) is `decode_subject(self.id.id)`; `digest_push()` (proxies.py:225) is `make_push(table, open_inbox=inbox_for, defer=False)`; `inbox_for` (proxies.py:121-127) returns `typed_proxy(INBOX_ACTOR_TYPE, inbox_actor_id(subject), ...)` and `inbox_actor_id` is `encode_subject(subject)` — i.e. exactly `self.id.id`. `TypedActorProxy` wraps a real `ActorProxy`, so the call crosses the sidecar; there is no in-process short-circuit.

(4) First act of the pusher is a re-entrant invocation: channels.py:231-233 `async def push(subject, payload): inbox = open_inbox(subject); prefs = await inbox.get_prefs()` — `GetPrefs` on the actor whose turn is already held.

(5) Reentrancy is off: `grep -rn reentran services/ packages/ chart/` returns nothing and `grep -rn 'ActorRuntimeConfig|set_actor_config'` returns nothing, so the SDK default applies — `dapr/actor/runtime/config.py` `ActorRuntimeConfig.__init__(..., reentrancy: Optional[ActorReentrancyConfig] = None)` and `if self._reentrancy: configDict.update({'reentrancy': ...})`, so the key never reaches `/dapr/config` and daprd's default (disabled) stands. The inner call therefore blocks on the held lock until `DAPR_HTTP_TIMEOUT_SECONDS = 60` (dapr/conf/global_settings.py:37), and the exception is swallowed by `except Exception: logger.exception("inbox_digest_send_failed")`.

(6) The loss is permanent, as claimed: `drain_digest` (inbox_actor.py:571-578) writes `DIGEST_KEY={"pending": False}` and returns the rows BEFORE any send, and `arm_digest` only re-arms on a NEW deferred arrival (`if await self._digest_pending(): return {"armed": False}`). The drained rows are never re-offered.

(7) No test can see it — `services/notifications/tests/test_channels.py:550-588` drives `make_push` against an in-process `_DigestInbox`; grep for `_send_digest` across the suite returns nothing.

Two corrections that do not change the verdict: the surrounding docstring (inbox_actor.py:581-601) is a recorded decision about holding the turn for an SMTP send, and it genuinely never considers that the send path's first act re-enters this actor — so it is not a sanctioning comment for this defect. And the finding's parenthetical is wrong about the chart: `chart/templates/notifications-channels.yaml` renders each binding only `{{- if (($ch.email).enabled) }}` / `slack.enabled`, and `IngressSettings.enabled_channels` defaults to `""` (api/settings.py:128) — so the trigger condition is an opt-in deployment with channels enabled AND a subject with `digest_seconds`, not every deployment. Within that condition the failure is deterministic: every digested notification is drained and never sent, and the actor's turn is stalled 60s per pointer.


**FIXED 2026-08-25.** `digest_push()` is replaced by `digest_push_into(inbox)`, and
`_send_digest` hands it `self`: `make_push`'s `open_inbox` now returns THIS actor instead of a
`TypedActorProxy` to it, so `get_prefs` and `claim_channel` are ordinary in-turn method calls
with identical semantics and no sidecar hop. A `DigestInbox` Protocol names the shape both
implementations satisfy. Pinned by
`services/notifications/tests/test_inbox_actor.py::test_the_digest_drain_does_not_re_enter_its_own_actor`,
which asserts both halves — no proxy is opened for this actor's own subject, AND the digest
still reaches the channel exactly once. Before the fix that test failed with
`AssertionError: re-entrant actor call: the digest opened a proxy to its own inbox`, behind the
swallowed `inbox_digest_send_failed`. The test double also gained `set_state`, absent until the
digest paths were driven end to end.

</details>

<details><summary><b>The fan-in carries an unbounded FragmentMetadata list past the module's own 4 MiB gRPC ceiling — the wedge `enumerate_chunks` was hardened against, one activity later</b> <i>(det-ingest, rule DWF-ACT-004, CONFIRMED)</i></summary>

**Sites:** `services/ingest/src/ingest/workflow.py:665`, `services/ingest/src/ingest/workflow.py:692`, `services/ingest/src/ingest/workflow.py:793`, `services/ingest/src/ingest/workflow.py:850`, `services/ingest/src/ingest/workflow.py:856`

**Why it matters.** `GRPC_MAX_MESSAGE_BYTES` (:850) is declared as "what an activity result is measured against on the way back to the sidecar", and `_refuse_oversized_dispatch` enforces a 3 MiB budget against it — but ONLY for `enumerate_chunks`. The return leg is unguarded. Each `chunk_run` returns every fragment blob its drain produced (:793), the parent flattens every child's list (:665) and hands the whole thing to `finalize` as one activity input (:692). I measured a real `FragmentMetadata.to_json()` blob for a 5-column bronze table at 395 bytes (pylance, CREATION_FLAGS from lander.py). Two concrete sequences: (a) TEXT-shaped, all defaults — `fragment_rows=1024` (sizing.py:60) over the owner's stated "over 10 million images" scale (workflow.py:78-79) gives 9,766 fragments x 395 B = 3.86 MB, already past CHUNK_DISPATCH_BUDGET_BYTES and 92% of the 4 MiB ceiling; 11M units crosses it. (b) MEDIA-shaped, which sizing.py names as the normal path for this estate ("1024 twenty-megabyte pages is a 20 GB fragment ... so `fragment_bytes` closes the batch first on anything image-shaped") — `fragment_bytes=256 MiB` over 20 MB pages closes at ~13 rows/fragment, so 10M units gives ~769,000 fragments = ~304 MB in ONE `finalize` input, 72x the ceiling. The orchestrator's work-item completion carrying that scheduleTask action is rejected by daprd, the work item is not completed, the sidecar redelivers it, the body replays and builds the same oversized response: a permanent wedge AFTER every byte was fetched, validated and staged, with nothing naming a knob — verbatim the failure `enumerate_chunks`'s docstring says it exists to make impossible. The same arithmetic sinks the parent's history first (1,000 child results x ~304 KB each). It is also dead weight: `runtime.py:413` makes `discover_staged` the authoritative fragment list and uses the carried one only as an unreadable-staging fallback, so 304 MB crosses the wire to feed a fallback branch.

**Fix.** Stop carrying fragment blobs through workflow history. `ChunkResult` should return a COUNT (and, if the fallback must survive, a staging-prefix pointer) the way §2.13 already turned unit keys into `offset`/`count` pointers into `staging.unit_manifest_uri`. `finalize` already reads storage truth via `discover_staged(uri, run_id)`; the workflow only needs to tell it how many fragments the children believed they staged so the `not all_fragments and fragments` warning at runtime.py:414 can still fire. If the carried list is kept at all, run it through the same `_refuse_oversized_dispatch` measurement before line 692 so the run fails with a named knob instead of wedging.

**Evidence.**

```
workflow.py:665  `fragments = [f for r in parsed for f in r.fragments]`
workflow.py:692  `"fragments": fragments,`
workflow.py:793  `fragments=[str(fragment) for fragment in (drained.get("fragments") or ())],`
workflow.py:850  `GRPC_MAX_MESSAGE_BYTES: int = 4 * 1024 * 1024`
workflow.py:856  `CHUNK_DISPATCH_BUDGET_BYTES: int = 3 * 1024 * 1024`
```

**Verifier (CONFIRMED).** Every cited line is verbatim at HEAD. workflow.py:665 `fragments = [f for r in parsed for f in r.fragments]`; :692 `"fragments": fragments,` inside the `finalize` `call_activity` input; :793 `fragments=[str(fragment) for fragment in (drained.get("fragments") or ())],` on `ChunkResult`. The asymmetry is real: `grep -rn CHUNK_DISPATCH_BUDGET_BYTES` finds exactly one enforcement site, workflow.py:1023 `refusal = _refuse_oversized_dispatch(chunks, units=len(pairs), max_units=...)` inside `enumerate_chunks` — nothing measures the return leg. The neighbouring bound exists only for errors (`bound_errors`, :674/:790, capped at MAX_REPORTED_ERRORS=100); `fragments` is declared at :333 `fragments: list[str] = Field(default_factory=list, description="FragmentMetadata JSON blobs")` with no cap anywhere.

I reproduced the 395-byte measurement independently with pylance against a 5-column bronze-shaped table: `json.dumps(write_fragments(...)[0].to_json())` -&gt; 395 bytes. The arithmetic holds at HEAD, and CHUNK_SIZE is now 10000 (:96), so 10M units is exactly the ~1,000 children the finding assumes. Text default `fragment_rows=1024` (sizing.py:60) over the owner-stated scale (workflow.py:78-79, `this estate holds "over 10 million images" (owner, 2026-08-24)`) -&gt; 9,766 fragments x 395 B = 3.86 MB, already over CHUNK_DISPATCH_BUDGET_BYTES (:856) and 92% of GRPC_MAX_MESSAGE_BYTES (:850). The media path is not hypothetical — sizing.py's own docstring says "1024 twenty-megabyte pages is a 20 GB fragment ... so `fragment_bytes` closes the batch first on anything image-shaped", and `default_fragment_bytes() = 256 MiB` over 20 MB pages closes at ~13 rows/fragment, giving ~769k fragments in one `finalize` input.

The dead-weight half is confirmed by the code's own comment at runtime.py:405-413: "STORAGE TRUTH, and it is the ONLY truth" — `all_fragments = discover_staged(uri, spec.run_id)` is authoritative and the carried list is consumed only by `if not all_fragments and fragments:` (the unreadable-staging fallback that logs `ingest_staging_unreadable_using_carried_fragments`). No test covers the return leg: test_dispatch_ceiling_at_real_scale.py, test_dispatch_payload_scales_by_chunk.py and test_enumeration_dispatch_ceiling.py all measure the dispatch direction only. Critical stands: the failure lands after every byte was fetched, validated and staged, and the redelivered work item rebuilds the same oversized response.


**FIXED 2026-08-25.** The return leg is now measured against `FANIN_RETURN_BUDGET_BYTES`, defined
as `= CHUNK_DISPATCH_BUDGET_BYTES` so the two legs cannot drift apart — both cross the sidecar as
one gRPC message, so both answer to one number. `_bound_carried_fragments` measures the SERIALISED
list and drops ALL or NOTHING: a half list would be worse than none, because the fallback commits
what it is handed and a partial fallback is a partial commit presented as a whole one.

**The finding's suggested fix was wrong, and reading `finalize_run` is what showed it.** The carried
list is not what gets committed — `discover_staged` is ("STORAGE TRUTH, and it is the ONLY truth"),
an exact-cover selection that deselects superseded fragments; unioning the two caused the
"four units in, six rows out" duplication `test_partial_ack_duplication.py` closed. The carried list
is reached ONLY when staging returns nothing, so the suggested staging-prefix POINTER is worthless
in exactly the case the fallback exists for. Owner ruling (2026-08-25): bound and keep the fallback.

A second-order consequence had to be closed with it: a dropped fallback meeting unreadable staging
would have fallen into `finalize_run`'s ordinary "nothing to commit" no-op and read as an empty
run. `fallback_dropped` now rides into `finalize` and that case logs
`ingest_staging_unreadable_and_fallback_dropped` at ERROR.

Pinned by `services/ingest/tests/test_fanin_return_ceiling.py` (4 tests, all RED first):
carries-while-it-fits, drops-past-the-budget, one-ceiling-for-both-legs, and the dropped-fallback
report. Measured ~395 B per fragment manifest at `fragment_rows=1024`, so the budget lands near 8M
rows — inside the estate's stated 10M-image scale.

</details>

<details><summary><b>`POST /v1/ingests/{id}/terminate` kills the parent before `emit_terminal`, so the run's JetStream units are stranded forever and no FAIL record ever reaches lineage</b> <i>(det-ingest, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/ingest/src/ingest/__init__.py:340`, `services/ingest/src/ingest/api.py:599`, `services/ingest/src/ingest/workflow.py:1122`, `services/ingest/src/ingest/queue.py:316`

**Why it matters.** `emit_terminal` is the ONLY caller of `release_run_units` and the file says so at :1104-1119 ("RELEASE WHAT THIS RUN LEFT QUEUED, on every terminal path ... The live estate sat at `messages: 1, consumers: 0` for hours with every other signal green"). `terminate_workflow(run_id)` sets the instance to TERMINATED; the generator is never resumed, so `emit_terminal` never runs on this path. Sequence: an operator sees a run pointed at the wrong prefix, POSTs terminate at the moment a chunk has published its units but its `drain_chunk` has not created the durable consumer. The parent and (recursively) every `chunk_run` stop. Nothing purges `ingest.tasks.&lt;run_id&gt;` and nothing deletes the `ingest-&lt;run_id&gt;` durable. queue.py:316 states the consequence: "WORK_QUEUE retention is what makes it permanent: a message leaves only when it is ACKED, no consumer will ever be created for that run id again, and nothing sweeps the stream. Every other signal stays green." `tests/test_no_unread_publishes.py` exists precisely because this estate has already been burned by unacked messages accumulating on this stream. Three further losses ride along: no `_lineage().terminal(...)` emit, so the START emitted at accept is orphaned in the graph forever and the ORIGINATOR is never told their run ended; `purge_staged` (runtime.py:452) never runs, so the run's staged fragments become orphans; and `merge_workflow_state` maps TERMINATED -&gt; FAILED (runs.py:213) with `_failure_detail` finding nothing, so the door reports FAILED with an empty `errors` dict. `test_terminate_stops_a_live_run.py` asserts routing, method, auth, 202 and threading — nothing about the release.

**Fix.** Make terminate a terminal PATH rather than a kill. Either (a) raise an external event / set a cancellation flag the parent selects on (`wf.when_any([fanout, cancel_event])`) so it falls through the existing deadline-shaped branch — terminate_chunks, then emit_terminal, then return FAILED with an operator-supplied reason; or (b) if the hard kill must stay, have `terminate_ingest` invoke the release and the FAIL lineage emit itself before calling `terminate_workflow`, and say in `TerminateAccepted.detail` what was reclaimed. Option (a) reuses the deadline path verbatim and keeps `emit_terminal` the single terminal seam the module claims it is.

**Evidence.**

```
__init__.py:340  `wf.DaprWorkflowClient().terminate_workflow(run_id)`
api.py:599  `await asyncio.to_thread(terminator.terminate, run_id)`
workflow.py:1122  `_run_async(release_run_units(spec.run_id))`  (inside `emit_terminal`, which a terminated instance never reaches)
```

**Verifier (CONFIRMED).** `grep -rn release_run_units` over services/ingest/src returns exactly one call site: workflow.py:1122 `_run_async(release_run_units(spec.run_id))`, inside `emit_terminal`. The terminate path does not reach it — __init__.py:339-340 is the whole implementation, `wf.DaprWorkflowClient().terminate_workflow(run_id)` / `return True`, and api.py:598 `await asyncio.to_thread(terminator.terminate, run_id)` is the only thing the route does after authorizing. Dapr's terminate marks the instance TERMINATED in the engine; it does not resume the generator, so neither the `except` boundary nor `emit_terminal` runs.

The consequence is stated by the code itself. workflow.py:1104-1119: "RELEASE WHAT THIS RUN LEFT QUEUED, on every terminal path ... The live estate sat at `messages: 1, consumers: 0` for hours with every other signal green." queue.py:316-322: "WORK_QUEUE retention is what makes it permanent: a message leaves only when it is ACKED, no consumer will ever be created for that run id again, and nothing sweeps the stream. Every other signal stays green." queue_health.py's `_derive_stranded` says the same and is explicitly a report, not a sweep ("This REPORTS; it never gates"). I found no reconciler, cron or sweep that releases a run's subject — cron.py is the incremental-trigger scheduler, not a queue reaper.

The three riders check out: `_lineage().terminal(...)` exists only inside `emit_terminal` (workflow.py:1153); `purge_staged` is called only from the two `finalize` paths (runtime.py:452, 507); and runs.py:214 maps `"TERMINATED": "FAILED"` while `_failure_detail` has no engine failure field to read for a terminated instance, so the door answers FAILED with an empty `errors`. test_terminate_stops_a_live_run.py asserts only route registration, POST-only, 404, 202, the "not immediate" body, `authorize_ingest` in the source and the `to_thread` hop — nothing about release, lineage or staging. Critical stands: permanently stranded queue work plus a dropped terminal lineage event, leaving the accept-time START orphaned.


**FIXED 2026-08-25.** Terminate is a terminal PATH now, not a kill. The route raises a `cancel`
external event and the fan-in races it beside the fan-out and the deadline; both early exits
then share ONE terminal block — `terminate_chunks`, `terminal_emitted = True`, `emit_terminal`,
return FAILED. They share the code rather than agreeing by inspection, because the whole finding
was that a second exit skipped the cleanup the first one does.

Written first as a delegated generator, which was wrong: `terminal_emitted` must be set BETWEEN
the two calls (a failing `terminate_chunks` should still reach the outer boundary's FAIL emit,
while a failing `emit_terminal` must not be answered with a second contradicting record), and a
`yield from` cannot set the caller's local at that point.

**DEPLOY NOTE — this one needs a drain.** The change inserts exactly one action,
`wait_for_external_event(CANCEL_EVENT)`, at index 9: immediately after the fan-out dispatch,
which is where a run SITS while its chunks drain — the longest phase of an ingest run. An
in-flight instance there at deploy time replays into `_get_wrong_action_name_error`, raised
outside the generator where no error boundary catches it, and goes terminal FAILED. **Drain
in-flight ingest runs before rolling this out.** Accepted knowingly (owner, 2026-08-25);
`tests/unit/workflow_action_order.json` is regenerated and the gate at
`tests/unit/test_workflow_action_order.py` is what forced the question. The estate still has no
versioning seam (`is_patched`, named workflow versions), which that gate names as the real fix.

Accepted cost of the design: terminate is ASYNCHRONOUS — it asks the run to stop at its next
select rather than stopping it, so a parent wedged before that point will not honour it.

Pinned by `test_OPERATOR_CANCELLATION_stops_the_children_and_leaves_a_FAIL_record`, which
asserts the ORDER (children stopped before the queue is reclaimed) and that the operator's
reason survives into the FAIL record. 12 tests across 5 files were updated: racing cancellation
means the fan-in reads `fanout.get_result()` rather than the value sent into the yield, so every
harness encoding the old protocol changed. The AST gate in `test_run_deadline` was WIDENED
rather than repointed — it looked for `if winner is deadline:`, which now only decides the
outcome, so left alone it would have silently covered nothing.

</details>

<details><summary><b>The cascade head's real production trigger — the catalog's write announcement — is a fail-open publish with no outbox, and no recovery path can re-fire it</b> <i>(pubsub, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/catalog/src/catalog/core/lineage_emit.py:663`, `services/catalog/src/catalog/core/lineage_emit.py:23`, `services/ingest/src/ingest/lineage.py:156`, `services/lineage/src/lineage/api/reconcile_cron.py:201`, `services/medallion/src/medallion/api/bronze_arrival.py:38`

**Why it matters.** Every mechanical claim reproduces, but the framing presents an undiscovered fail-open with a merely-stale excuse; it is in fact the estate's own recorded gap #1, and the remedy the finding gestures at is explicitly ruled out. Restated: the catalog-announced cascade lane is the ONE path with neither a caller-visible signal nor any recovery. docs/RESILIENCE.md:66 already names it — "**The catalog outbox gap (the #1 weakness).** The catalog emits lineage **inline-awaited + best-effort** *after* the Lance write commits" — and RESILIENCE.md:122 records the consequence ("catalog emits swallow, the cascade halts"), with the shipped mitigation (the B4 storage-&gt;graph reconcile) restoring PROVENANCE only. Adding an outbox to the catalog would NOT close it: docs/architecture/medallion-cascade.md §11 records the ruling "the outbox re-ingests lineage, it never re-fires triggers — trigger loss is the documented idempotency-token caller-retry contract", which is why the finding's own reading of reconcile_cron._drain_outbox is correct AND is the intended behaviour. The genuinely un-recorded defect is the asymmetry the finding identifies last: §11's caller-retry contract is what makes trigger loss survivable on /produce, /ingest-media and /train (produce.py:97-104 returns 503 + Retry-After: 5), and the catalog-announced ingest lane has no such contract — the catalog answers 2xx and no caller ever learns the bronze-&gt;silver-&gt;gold run never started. Severity stays critical (a silently dropped cascade trigger), but it should be filed as "the caller-retry contract has no counterpart on the catalog lane", not as "the catalog lacks an outbox".

**Fix.** Route the catalog's lineage emit through service_kit.lakehouse.outbox.publish_lineage_with_outbox the way medallion/services/produce.py:140 already does (the catalog writes to the same object store, so the 'no DB' objection no longer holds), AND make the relay able to restore a cascade: _drain_outbox should re-PUBLISH the drained event to LINEAGE_DAPR_TOPIC rather than only ingesting it — the head is idempotent on the cascade token, so a re-published head converges. Failing that, give the medallion plane a reconciler that compares published bronze versions against silver's and re-fires. At minimum correct the counter description at lineage_emit.py:61 so it names the consequence ('a lost event AND, for a governed-tier write, a silently cancelled cascade').

**Evidence.**

```
        except Exception as exc:
            _emit_failed.add(1, {"lance.catalog.transport": "dapr"})
            log.warning("lineage_publish_failed", extra={"operation": operation, "table": table_id, "error": str(exc)})
```

**Verifier (ADJUSTED).** services/catalog/src/catalog/core/lineage_emit.py:663-665 is exactly as quoted: `except Exception as exc: _emit_failed.add(1, {"lance.catalog.transport": "dapr"}); log.warning("lineage_publish_failed", ...)` — no re-raise, and make_emitter/chart wire this transport in production (chart/templates/services.yaml:114 `- { name: LANCE_LINEAGE_TRANSPORT, value: "dapr" }`). The module docstring lines 23-24 do say "the catalog has no DB for a transactional outbox", and services/catalog/src/catalog/services/warehouses.py:9-10 does cite the object-store shape ("Stateless-over-object-store, the same shape as ``service_kit/lakehouse/outbox.py``"), so the stated reason is indeed stale — but immaterial, per medallion-cascade.md §11. services/ingest/src/ingest/lineage.py:157-160 confirms the production chain: "It was not needed. The CATALOG announces the write — `lance-catalog/insert.bronze$events` ... which is exactly what the medallion's `/bronze-arrival` filters on". services/lineage/src/lineage/api/reconcile_cron.py:159-207 confirms the drain calls `await repository.ingest_event(event)` and `await record_event_best_effort(repository, event)` then `outbox.drop_event(...)` — no publish to lineage.events.v1. No medallion cron exists: `grep -rln bindings.cron chart/` returns only notifications-cron.yaml, compute-prune-cron.yaml, ingest-cron.yaml, maintenance.yaml and services.yaml (lineage).

</details>

### Warning — 16

<details><summary><b>finalize purges the staging record before its own result is durable, so a replay cannot reach the catalog's run_id dedupe</b> <i>(act-ingest, rule DWF-ACT-002, CONFIRMED)</i></summary>

**Sites:** `services/ingest/src/ingest/runtime.py:507`, `services/ingest/src/ingest/runtime.py:413`, `services/ingest/src/ingest/runtime.py:427`, `services/ingest/src/ingest/runtime.py:452`, `services/ingest/src/ingest/workflow.py:1070`

**Why it matters.** The commit itself IS idempotency-keyed — `(run_id, read_version)` reaches `dataplane.py:607 _find_run_commit`, which recognizes the run's own earlier version by its `rask.ingest.run_id=` transaction property. But that door is only reachable while the retry still has fragments to present, and `purge_staged` (runtime.py:507) destroys the run's record of what it wrote as soon as the commit lands — before Dapr has durably recorded the activity result. Concrete sequence, all of it ordinary crash recovery this plane already designs for: (1) a chunk's `drain_chunk` acks and stages its fragments and the pod dies before the result is recorded; the retry re-drains an empty queue, `reconcile_chunk` sees `num_pending == 0`, and the child returns `fragments=[]` — the A3 case `discover_staged` exists for. (2) `finalize` attempt 1 discovers those staged fragments, commits version N+1 through the catalog, purges staging, and loses its pod before the result is recorded. (3) The retry finds `discover_staged` empty AND the carried list empty, so the `if not all_fragments and fragments` fallback (runtime.py:414) does not fire and it takes the nothing-to-commit branch: it returns `committed_version: None, rows: 0, status COMPLETE, published: None, publish_reason: 'nothing to commit'`. The rows are committed and durable, but the run record, the API status and the lineage COMPLETE all say it wrote nothing (`_output_datasets` emits `rowCount=0` and, with `version=None`, no dataset-version facet at all), and because `_publish` is never reached the committed version is never gated — bronze holds rows nothing marks READY. The cascade is unaffected (the catalog announces the write, not this emit), which is exactly why the wrong report would go unnoticed. No test covers it: `test_replay_hygiene.py:307` stubs `discover_staged` to return `["{}"]` on both attempts and `test_partial_ack_duplication.py:292` replaces `purge_staged` with a no-op, so the one replay in which the dedupe is unreachable is the one nothing drives.

**Fix.** Either defer the purge past the point the workflow has recorded the terminal outcome (a sweep keyed on the run's terminal state, or `emit_terminal`), or make the nothing-to-commit branch ASK before it answers: resolve the run's own commit through the marker the catalog already stamps (a describe plus a `_find_run_commit`-equivalent lookup keyed on the carried `read_version`) and report that version instead of `None` — never report 'nothing to commit' from the absence of evidence this activity's previous attempt deleted.

**Evidence.**

```
runtime.py:505-507  `# Only after the commit lands. Purging earlier would delete the record a retried finalize needs,\n    # turning a recoverable failure into exactly the data loss staging exists to prevent.\n    purge_staged(uri, spec.run_id)`
runtime.py:413-414  `all_fragments = discover_staged(uri, spec.run_id)\n    if not all_fragments and fragments:`
runtime.py:445-455  `result = Lander(catalog).commit_fragments(uri, all_fragments, run_id=spec.run_id)\n        ...\n        purge_staged(uri, spec.run_id)\n        return {\n            "committed_version": None,\n            "rows": 0,`
```

**Verifier (CONFIRMED).** Every link verified at HEAD.

The idempotency key exists and is real: `catalog/services/dataplane.py:676-678` calls `_find_run_commit(location, so, run_id, read_version)` before the Append, and 607-613 states the exact replay it exists for. But it is only reachable with fragments in hand — `dataplane.py:675  if run_id:` sits inside `commit_fragments`, past the point `runtime.py:411` decides there is nothing to commit.

`runtime.py:505-507` purges on the success path with a comment defending the ORDER ("Purging earlier would delete the record a retried finalize needs") — correct as far as it goes, and it does not address the window AFTER the commit and before Dapr records the result, which is the one the finding names.

The three-step sequence reproduces on inspection: (1) `chunk_run` (workflow.py:788-798) returns `fragments=[str(f) for f in (drained.get("fragments") or ())]`, and a drain retry over an already-acked chunk returns none — `runtime.py:352-372 reconcile_from_queue` returns a hardcoded `"fragments": []` when `num_pending == 0`, which is precisely the A3 case `discover_staged` is documented for at runtime.py:404-412. (2) `finalize` attempt 1 takes `runtime.py:445-455`, commits, `purge_staged`, loses the pod. (3) The retry: `all_fragments = discover_staged(...)` is now empty, `if not all_fragments and fragments:` (runtime.py:414) does not fire on an empty carried list, and control reaches the nothing-to-commit branch — `Lander.commit_fragments` with an empty list is a documented no-op (`lander.py:146-148  if not fragments_json: … return CommitResult(..., version=ds.version, rows=ds.count_rows())`), so nothing raises and the run returns `"committed_version": None, "rows": 0`, `status` COMPLETE, `"publish_reason": "nothing to commit"`.

The consequences check out too. `lineage.py:117-118` says outright that `version=None` writes no dataset-version facet at all, and `_output_datasets(project, dataset, None, 0)` therefore announces a COMPLETE run that wrote nothing. `_publish` (runtime.py:552) is never reached, and runtime.py:509-517 is explicit that a commit is not a publication — so the committed version is never gated and bronze holds rows nothing marks READY. The finding is also right that the cascade survives (lineage.py:89-96: the trigger is the write record, matched on the bronze namespace pair), which is what makes the wrong report quiet.

The test claim is accurate: `test_replay_hygiene.py:307  monkeypatch.setattr(staging, "discover_staged", lambda uri, run_id: ["{}"])` — staging answers non-empty on BOTH attempts, so the drive never reaches the branch; `test_partial_ack_duplication.py:292  monkeypatch.setattr(runtime, "purge_staged", lambda *a, **k: None, raising=False)` removes the purge entirely.

Severity: warning is correct. It needs two independent crash events (a drain whose result was never recorded plus a finalize that dies post-commit), nothing is duplicated or lost, and the damage is a false terminal report plus an ungated version — not a stuck instance or a double side effect.

</details>

<details><summary><b>`publish_promotion` has no error boundary, so a refused catalog destroys the human decision with no record at all</b> <i>(act-medallion, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:904`, `services/medallion/src/medallion/workflow.py:1037`, `services/medallion/src/medallion/services/catalog_register.py:243`, `services/catalog/src/catalog/services/publication.py:216`

**Why it matters.** `publish_promotion` calls `_resume_publish` → `publish_stage_output`, which RAISES `RegisterError` on any catalog 4xx/5xx (catalog_register.py:243-244). The call site at workflow.py:904-905 is bare — no `try:` — unlike the structurally identical `publish_stage_ready` call in `stage_run` (workflow.py:258-267), which IS wrapped precisely because "an exhausted retry policy used to raise into the workflow, take the instance terminal FAILED, and skip the report entirely". Concrete non-transient trigger: the approval window is 72 hours by default (`approval_hours: int = 72`) and `PromotionSpec.version`'s own comment anticipates "a later commit may have landed while the approver was deciding". If a later version was also PUBLISHED in that window, `publication.publish` hits `if previous is not None and version &lt; previous:` and raises `InvalidTableStateError` — "refusing to move 'published' backwards" — which the endpoint returns as a 4xx, `publish_stage_output` turns into `RegisterError`, and ACTIVITY_RETRY re-raises identically on all five attempts (backwards is deterministic; retrying cannot fix it). The exception then propagates out of the generator: the instance goes terminal FAILED, `emit_promotion_outcome` at :906 NEVER runs, so `record_promotion_outcome` never fires and no lineage event is written. The validator got a 202, the tag did not move, the metric shows nothing, the graph shows nothing, and `GET /promotions/{id}` afterwards answers `_live_spec`'s 404 ("no longer under review (FAILED)"). A human's governance decision is destroyed leaving only a daprd-side activity-failed counter. The same path is reached by an unreachable catalog or a 403 on the service identity.

**Fix.** Wrap the `publish_promotion` call the way `stage_run` wraps `publish_stage_ready`: catch the activity failure, and emit `emit_promotion_outcome` with a distinct status (e.g. `PROMOTION_FAILED`, alongside the existing PROMOTED|REJECTED|BLOCKED|EXPIRED) carrying the reason, so the decision and its failure both reach lineage and the counter. Do not swallow it into PROMOTED — the tag did not move.

**Evidence.**

```
workflow.py:904-906
    if spec.pub_topic:
        yield ctx.call_activity(publish_promotion, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
    yield ctx.call_activity(
        emit_promotion_outcome,

contrast — workflow.py:258-267
        try:
            yield ctx.call_activity(publish_stage_ready, input={...}, retry_policy=ACTIVITY_RETRY)
        except Exception:
            outcome = outcome.model_copy(update={"verdict": "unnotified"})
```

**Verifier (CONFIRMED).** The asymmetry is real and deliberate on one side only.

Bare call, workflow.py:904-905:
```
    if spec.pub_topic:
        yield ctx.call_activity(publish_promotion, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
```
The structurally identical publish in `stage_run` IS wrapped, workflow.py:258-267, with the reason written out at :252-257: "AN ERROR BOUNDARY, because pass 1 already acked the trigger... an exhausted retry policy used to raise into the workflow, take the instance terminal FAILED, and skip the report entirely".

The raise path exists: catalog_register.py:229-244 — `raise RegisterError(f"catalog unreachable publishing {table_id!r}: {exc}")` and `if response.status_code &gt;= 400: raise RegisterError(...)`; its docstring says "Raises on an unreachable or refusing catalog". ACTIVITY_RETRY is `max_number_of_attempts=5` (workflow.py:75-80).

The deterministic trigger checks out: publication.py:216-222
```
    previous = _tag_version(ns, storage_options, table_id, tag)
    if previous is not None and version &lt; previous:
        raise InvalidTableStateError(
            f"refusing to move {tag!r} backwards from {previous} to {version}; ...")
```
with `approval_hours: int = 72` (workflow.py:829) and PromotionSpec.version's own comment anticipating "a later commit may have landed while the approver was deciding". Retrying a backwards publish cannot succeed, so all five attempts fail identically and the exception leaves the generator: `emit_promotion_outcome` at :906 never runs, `record_promotion_outcome` never fires (workflow.py:1085), no lineage event is written, and `_live_spec` then answers `TableNotFoundError(f"promotion {instance_id!r} is no longer under review ({name})")` (api/promotions.py:112-114). Reachable today on bronze-to-silver (pubTopic `medallion.silver`, values.yaml:1081), which is the one mover whose `pub_topic` gate lets `publish_promotion` run at all. Warning is the right severity: the decision is lost, but the tag is not wrongly moved.

</details>

<details><summary><b>`request_approval` mints a fresh dedupe key per execution, so a re-executed activity double-notifies the approver</b> <i>(act-medallion, rule DWF-ACT-002, CONFIRMED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:942`, `services/medallion/src/medallion/workflow.py:961`, `packages/service-kit/src/service_kit/control_events.py:151`, `services/notifications/src/notifications/api/control_events.py:135`

**Why it matters.** `request_approval` is an activity whose entire purpose is an external side effect: a Dapr publish onto `catalog.control.v1`, which the notifications plane turns into a durable InboxActor row for `user:&lt;approver&gt;`. Dapr Workflow activity execution is at-least-once — a worker crash (or sidecar disconnect) after the publish lands but before the ActivityCompleted event is durably appended re-executes the activity on recovery — and this activity reads NOTHING from its `ctx`: `WorkflowActivityContext` exposes `workflow_id` and `task_id` (the taskExecutionId pair) and neither is touched. Worse, the one dedupe handle the consumer relies on is generated INSIDE the activity body: `CatalogControlEvent.event_id` defaults to `uuid4().hex` (control_events.py:151, whose own module docstring says "`event_id` is the client-side dedupe key"), and the notifications projection builds `notification_id = f"{event.event_id}@{event.action.upper()}"` (notifications/api/control_events.py:135). So the re-execution publishes a control event that is byte-identical except for a NEW `event_id`, the inbox writes a SECOND pointer under a second notification_id, and the validator sees two identical "promotion review requested" rows for one held promotion — both linking to the same `promotion-&lt;token&gt;` instance, each needing separate dismissal. The dedupe mechanism exists and is defeated by where the id is minted.

**Fix.** Derive the id from something stable across re-execution: `CatalogControlEvent(event_id=f"{ctx.workflow_id}-{ctx.task_id}", ...)`, or from the workflow's own deterministic input (`f"promotion-review-{spec.token}"`, which is already the instance id's basis). Either makes `notification_id` stable so the inbox collapses the duplicate. The same audit applies to `occurred_at`, though a drifting timestamp only affects ordering.

**Evidence.**

```
workflow.py:942, 961-967
def request_approval(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> bool:
    ...
    event = CatalogControlEvent(
        action="promotion_review_requested",
        object_type="table",
        object_id=f"table:{_qualified(spec.project, spec.to_dataset)}",
        actor=None,
        extra={"subject": f"user:{spec.approver}", "reasons": spec.reasons, "project": spec.project, "token": spec.token},
    )

control_events.py:150-151
    #: Client-side dedupe key (a redelivery carries the same id).
    event_id: str = Field(default_factory=lambda: uuid4().hex)
```

**Verifier (CONFIRMED).** Every link in the chain is as described.

workflow.py:942 `def request_approval(ctx: WorkflowActivityContext, payload: dict[str, Any]) -&gt; bool:` — `ctx` is never read in the body; the event is constructed inline at :961-967 with no id field, so the default fires.

control_events.py:150-151:
```
    #: Client-side dedupe key (a redelivery carries the same id).
    event_id: str = Field(default_factory=lambda: uuid4().hex)
```

notifications/api/control_events.py:127-135: "`notification_id` is `&lt;event_id&gt;@&lt;ACTION&gt;`" → `notification_id=f"{event.event_id}@{event.action.upper()}"`.

And that id is the ONLY dedupe: inbox_actor.py:322-332 — "Land one pointer. Idempotent on `notification_id`, which is the dedupe." / `if any(pointer.notification_id == delivery.notification_id for pointer in pointers)`. A random id per execution defeats it exactly as claimed, so an at-least-once re-execution writes a second pointer under a second id and the validator sees two identical review rows for one held promotion.

The activity's docstring (:944-948) explains only why it publishes directly rather than through `process_control_emitter()` — it records nothing about duplicate suppression, so there is no countervailing decision here. Note the rule catalog rates DWF-ACT-002 critical; warning is the correct downgrade, since the duplicated side effect is a notification row, not a data or tag mutation.

</details>

<details><summary><b>`TaskStateChanged` resurrects a deliberately dropped task, re-wedging the publish</b> <i>(actors-annotator, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/annotator/src/annotator/projects/project_actor.py:300`, `services/annotator/src/annotator/projects/project_actor.py:311`, `services/annotator/src/annotator/projects/project_actor.py:345`, `services/annotator/src/annotator/projects/actor.py:307`, `services/annotator/src/annotator/projects/actor.py:326`

**Why it matters.** `drop_task` removes an index entry and deliberately leaves the task's own actor alone, on the stated ground that "an orphaned task document is inert". It is not inert: it still holds an armed `lease` reminder and still calls `_report_state` on every transition, and `task_state_changed` writes `index[task_id] = str(state)` with no membership check — by design, to cover a half-completed `send`. The two decisions collide. Concrete sequence: project P is `labeling`; task T names a media dataset that was renamed, so it can never be submitted or skipped; gina claimed it before the rename, so T is CLAIMED with a 1800 s lease reminder armed. The manager runs `DELETE /projects/P/tasks/T` (project_events.py:337) — index entry gone, `may_publish` true, exactly the wedge `drop_task` exists to clear. Thirty minutes later T's reminder fires on the task actor: `receive_reminder` -&gt; `fire({"event": "lease_expired"})` (a system edge, so `_refuse_if_frozen` returns immediately) -&gt; `_store` -&gt; `_report_state` -&gt; `proxy.task_state_changed({"task_id": "T", "state": "unassigned"})` -&gt; `index["T"] = "unassigned"`. `may_publish` is false again and `fire("publish")` raises "publish (tasks are not all terminal)", with nothing anywhere naming T as the cause. Two sharper variants: (a) if the manager froze P after dropping T, `drop_task` is now refused (`DROPPABLE_STATES = {draft, labeling}`, project_events.py:297) and the only escape is `open` -&gt; drop -&gt; `freeze` again; (b) if the resurrection tick lands during PUBLISHING, `saga.collect` enumerates T from `listing["tasks"]`, `_refuse_if_not_terminal` raises `PublishRefusal`, and a legal publish is failed by a task the manager removed.

**Fix.** Give the project actor a durable tombstone: keep a `dropped` set (a third state key, or a reserved sentinel in the index) written by `drop_task`, and have `task_state_changed` return a no-op for a task id in it instead of re-inserting. Alternatively make `drop_task` disarm the orphan by invoking a `Retire`/`Drop` method on the task actor that unregisters its lease reminder and marks the task retired so `_report_state` stands down — but that reintroduces the half-failing second write the docstring rejects, so the tombstone is the smaller change. Either way, `task_state_changed`'s "unknown ids are recorded rather than rejected" must distinguish "never indexed" from "indexed and removed".

**Evidence.**

```
project_actor.py:307-311 —
        task_id = str(payload["task_id"])
        state = TaskState(payload["state"])
        project = await self._require()
        index = await self._load_index()
        index[task_id] = str(state)

project_actor.py:329-333 (drop_task's stated assumption) —
        The task's own actor is left alone deliberately. This index is what the precondition reads and
        what the publish enumerates; an orphaned task document is inert, whereas reaching across to
        delete it would be a second write that can half-fail

actor.py:307 / 326 (the unconditional report on every edge, lease_expired included) —
        await self._report_state(task)
            await proxy.task_state_changed({"task_id": task.task_id, "state": str(task.state)})
```

**Verifier (CONFIRMED).** Both halves are exactly as quoted and they do collide. `project_actor.py:299-311` records unconditionally:
```
        task_id = str(payload["task_id"])
        state = TaskState(payload["state"])
        project = await self._require()
        index = await self._load_index()
        index[task_id] = str(state)
```
with the docstring "Unknown task ids are recorded rather than rejected". `drop_task` (project_actor.py:328-337) deletes only the index entry, stating "an orphaned task document is inert". It is not: the DELETE route (`project_events.py:300-337`) gates only on `DROPPABLE_STATES = {draft, labeling}` (line 297) and on `can_manage` — it never checks the TASK's state, and it reads `holder = task.get('assignee')` right before dropping, i.e. dropping a CLAIMED task is the expected case. That task actor still holds its armed one-shot `lease` reminder; nothing disarms it. On fire, `receive_reminder` -&gt; `fire({"event": "lease_expired", "actor": None})`; `lease_expired` carries permission `None` in `TASK_EDGES`, so `self._refuse_if_frozen(task, event, payload, principal=permission is not None)` returns immediately (actor.py:196), the store lands, and actor.py:307 runs unconditionally:
```
        await self._report_state(task)
...
            await proxy.task_state_changed({"task_id": task.task_id, "state": str(task.state)})
```
writing `index['T'] = 'unassigned'`. `unassigned` is non-terminal, so `may_publish` flips false and `fire("publish")` raises "publish (tasks are not all terminal)" (project_actor.py:227) naming nothing. Variant (b) also holds: `saga.collect` enumerates `listing["tasks"]` and `_refuse_if_not_terminal` (saga.py:273-275) raises `PublishRefusal` on the resurrected id. Variant (a) holds too — a frozen project can no longer be dropped from (DROPPABLE_STATES), so recovery is open -&gt; drop -&gt; freeze. Warning is the right severity: recovery exists but is undiscoverable.

</details>

<details><summary><b>A control-lane row whose reason degrades to UNKNOWN falls back into the render gate it was exempted from — the unclearable badge, re-created by the rollback-tolerance path</b> <i>(actors-notifications, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/notifications/src/notifications/models.py:181`, `services/notifications/src/notifications/api/inbox.py:93`, `services/notifications/src/notifications/api/inbox.py:46`

**Why it matters.** `_CONTROL_REASONS` is derived from `NAMED_ACTIONS`, and `get_inbox` exempts exactly those rows from `can_get_metadata` because the control lane delivered them without a visibility check — being NAMED is the targeting. But `InboxPointer._tolerate_a_newer_vocabulary` rewrites any reason this build cannot name to `NotificationReason.UNKNOWN`, and `UNKNOWN` is not in `_CONTROL_REASONS`, so the row is put back into `governed` and checked against its `object_id`. Concrete sequence, and it is the exact scenario models.py's own docstring records as having happened on 2026-08-16: a build adds a named action (say `task_review_ready`) to `ControlAction`/`NAMED_ACTIONS`/`NotificationReason`; rows carrying it land in durable actor state; the deployment rolls back. The older build reads those rows, degrades the reason to `unknown`, and `get_inbox` asks FGA for `can_get_metadata` on `annotation_task:…` — an object the subject holds no grant on, which is precisely why the control lane skips the check. The row is dropped from `notifications`, while `page.unread` (feed.paginate, counted over ALL pointers) and `GET /inbox/unread` (straight off `InboxMeta.unread`) still count it. The badge reads one higher than the rows the panel can render, and the panel cannot clear it because `mark_seen` only names ids it actually rendered — an unclearable badge until compaction, which at the default `inbox_ttl_seconds` is 30 days. That is verbatim the failure inbox.py:79-85 says this exemption exists to end.

**Fix.** Make the render exemption survive an unnameable reason. The lanes are already distinguishable without adding a stored field (which `extra="forbid"` rightly forbids): the control lane stamps a CANONICAL id carrying its type (`table:db1$t`, `annotation_task:…`) while the lineage lane stamps a bare dataset name — the same discriminator `visibility._as_object` (visibility.py:89) already relies on. Extend the exempt set to include any pointer whose reason is `NotificationReason.UNKNOWN` and whose `object_id` already carries a type prefix, and pin it with a test that stores a row under an unknown reason with a colon-qualified object id and asserts it still renders.

**Evidence.**

```
models.py:181-183
        if isinstance(value, str) and value not in set(NotificationReason):
            return NotificationReason.UNKNOWN
        return value

inbox.py:93-94
    governed = {pointer.object_id for pointer in page.pointers if pointer.reason not in _CONTROL_REASONS}
    allowed = await visibility.visible(subject, governed) | {pointer.object_id for pointer in page.pointers if pointer.reason in _CONTROL_REASONS}
```

**Verifier (CONFIRMED).** Every step holds at HEAD.

models.py:174-183: `@field_validator("reason", mode="before") ... if isinstance(value, str) and value not in set(NotificationReason): return NotificationReason.UNKNOWN`.

models.py:60-63 pins that `UNKNOWN` is "reachable only when reading back state a NEWER build stored" — i.e. the rollback path the docstring at models.py:146-152 records as a live 2026-08-16 outage.

api/inbox.py:46 `_CONTROL_REASONS: Final[frozenset[NotificationReason]] = frozenset(NotificationReason(action) for action in NAMED_ACTIONS)` — derived from control_events.py:42-53 (`grant_added`, `grant_revoked`, `task_assigned`, `task_unassigned`, `task_changes_requested`, `task_dropped`, `task_lease_expired`, `promotion_review_requested`). `UNKNOWN` is in neither set, so a degraded row is NOT exempt.

api/inbox.py:93-94 `governed = {pointer.object_id for pointer in page.pointers if pointer.reason not in _CONTROL_REASONS}` / `allowed = await visibility.visible(subject, governed) | {...reason in _CONTROL_REASONS}` — the degraded control row is checked with `can_get_metadata` (visibility.py:129-135) against e.g. `annotation_task:…`, which is exactly the check control-lane delivery skipped, and is dropped from `notifications`.

The badge is unaffected by the drop: `unread=page.unread` is returned straight from the actor page, and `GET /inbox/unread` (inbox.py:105-119) answers off `InboxMeta` alone and is "Deliberately NOT visibility-filtered".

Unclearable is right too: `mark_seen` (inbox.py:121-131) takes `InboxMark.notification_ids` (models.py:241-247) — ids "the panel actually rendered" — and `dismiss` takes one id, so a row that never renders can be neither seen nor dismissed. That is verbatim the failure inbox.py:79-85 says the exemption exists to end ("a badge that cannot be cleared by reading, because what it counts is never shown").

No comment anywhere addresses the UNKNOWN×`_CONTROL_REASONS` interaction, so this is not a recorded decision. Severity `warning` is right: it needs a rollback across a build that added a named action (a scenario the code itself documents as having occurred), and the blast radius is the rows carrying the new action, until dismissal is impossible and compaction/TTL clears them.

</details>

<details><summary><b>re.error from compiled.sub() escapes the NodeError boundary and fails the WHOLE run — the durable lane retries it 3x, then fails the workflow with no node states</b> <i>(det-act-flows, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/flows/src/flows/executor.py:268`, `services/flows/src/flows/executor.py:263`, `services/flows/src/flows/executor.py:290`, `services/flows/src/flows/activities.py:48`, `services/flows/src/flows/workflow.py:95`

**Why it matters.** `_regex` wraps only `re.compile(pattern)` in its try/except; the replacement string is expanded by `sub()` OUTSIDE it. A `regex` node configured `{"regexMode": "replace", "regexPattern": "(a)", "regexReplace": "\\9"}` raises `re.error: invalid group reference 9 at position 1` from line 268 (reproduced: `re.compile(r'(a)').sub(r'\9','aaa')`). It is not a NodeError, so `executor.run_node`'s `except NodeError` at :290 does not catch it. DURABLE LANE: it propagates out of `asyncio.run` at activities.py:48, so the activity RAISES — which is exactly what workflow.py:35-37 says must not happen for a user-input refusal ("a refused node is not retried four times"). NODE_RETRY burns 3 attempts at 2s/4s, then `yield wf.when_all(tasks)` at workflow.py:95 raises into the workflow body, which catches nothing, so the orchestration goes FAILED. `routes._state_from_engine` maps FAILED to `RunState(status="failed", error=&lt;raw engine failure text&gt;)` with an EMPTY `nodes` map — so the builder cannot paint the node that broke, defeating the stated invariant at workflow.py:70 ("The builder must paint the node that broke"). Every sibling node in the same wave that succeeded is discarded with it. INLINE LANE: it propagates through `asyncio.gather` (executor.py:347) and out of `create_run` (routes.py:213, no try) as a 500 for the whole run. tests/test_executor.py:433 covers only the bad-COMPILE path, so nothing catches this.

**Fix.** Move `compiled.sub(...)` inside the try (or add a second try around the sub/finditer call) and convert `re.error` to `NodeError(f"bad replacement: {exc}")`. Independently, widen `executor.run_node`'s boundary at :290 to `except Exception as exc` — logging the traceback and returning `NodeResult(state=NodeRunState(status='failed', ...))` — so that the module's stated contract (a node fails as STATE, never as a raise) holds for every arm, not only the arms that remembered to raise NodeError.

**Evidence.**

```
executor.py:260-270
    if len(payload.text) > _REGEX_MAX_SUBJECT:
        raise NodeError(f"regex input too large: ...")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise NodeError(f"bad pattern: {exc}") from exc
    if node.config.get("regexMode") == "replace":
        replacement = node.config.get("regexReplace")
        return Payload(text=compiled.sub(replacement if isinstance(replacement, str) else "", payload.text))

executor.py:290-291
    except NodeError as exc:
        return NodeResult(state=NodeRunState(status="failed", ms=_elapsed_ms(started), error=str(exc)))
```

**Verifier (CONFIRMED).** Reproduced end-to-end through the exact function the activity calls. `uv run python` driving `flows.executor.run_node` with `config={'regexMode':'replace','regexPattern':'(a)','regexReplace':'\\9'}` printed `ESCAPED: PatternError invalid group reference 9 at position 1` — it did NOT return a failed NodeResult.

The try/except boundary is exactly as described. /home/blackwell/Desktop/rask/services/flows/src/flows/executor.py:262-268:
```
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise NodeError(f"bad pattern: {exc}") from exc
    if node.config.get("regexMode") == "replace":
        replacement = node.config.get("regexReplace")
        return Payload(text=compiled.sub(replacement if isinstance(replacement, str) else "", payload.text))
```
`sub()` at :268 is outside the try; only `re.compile` at :263 is inside. executor.py:290 catches `except NodeError as exc:` only, and `NodeError(Exception)` at :49 is not a superclass of `re.PatternError` (verified: `isinstance(PatternError, re.error) == True`, unrelated to NodeError).

Reachability is unguarded: `FlowNode.config: dict[str, object]` (models.py:86) with `extra="ignore"`, and `graph.validate_graph` checks only ids/kinds/edges/fan-in/cycles — it never inspects `config`. The studio zone ships `regexReplace` as a free `v.fallback(v.string(), '')` (frontend/microfrontends/studio/src/lib/flows/persistence.ts:34).

Inline lane: executor.py:344 is `results = await asyncio.gather(*(run_node(job, client=client) for job in jobs))` — no `return_exceptions`, so it propagates out of `execute` into `create_run` (routes.py:213, `state = await executor.execute(...)`, no try) → 500 for the whole run, siblings in the wave discarded.

Durable lane: activities.py:48 `result = asyncio.run(_run(job))` is untried, so the activity RAISES — the precise thing workflow.py:35-37 says must not happen ("a refused node is not retried four times"). NODE_RETRY burns 3 attempts, then `results = yield wf.when_all(tasks)` (workflow.py:95) raises into a body with no handler → orchestration FAILED. routes.py:_state_from_engine then returns `RunState(run_id=run_id, status="failed", error=str(detail)...)` with `nodes` unset — defeating workflow.py:70 ("The builder must paint the node that broke").

Coverage gap confirmed: services/flows/tests/test_executor.py:432-433 is `with pytest.raises(NodeError, match="bad pattern")` on `{"regexPattern": "([unclosed"}` — the bad-COMPILE path only. No test exercises a bad replacement.

Note for the record: this is already a filed audit item, `FLOWS-NODE-ESCAPE` (open_python-audit.md:1456, rated HIGH — "`run_node` catches only `NodeError`, so a bad `regexReplace` 500s the entire run and orphans its sibling nodes"). It is filed and still open at HEAD, not fixed, so the finding stands as written. Severity `warning` is right: the workflow goes FAILED, not stuck, and no side effect is duplicated.

</details>

<details><summary><b>FLOWS-REDOS-ON-LOOP is only half fixed: the subject-length cap cannot bound nested-quantifier backtracking, so one node poisons the workflow history and survives every pod restart</b> <i>(det-act-flows, rule DWF-ACT-006, ADJUSTED)</i></summary>

**Sites:** `services/flows/src/flows/executor.py:270`, `services/flows/src/flows/executor.py:246`, `services/flows/src/flows/executor.py:250`, `services/flows/src/flows/executor.py:175`, `services/flows/src/flows/activities.py:48`

**Why it matters.** The `regex` node has no bound on pattern-driven backtracking, so one caller-supplied nested-quantifier pattern stalls the whole flows process (the GIL is held by `re`, so `asyncio.to_thread` does not help — executor.py:167 says so). This is the un-implemented half of the recorded FLOWS-REDOS-ON-LOOP remediation (open_python-audit.md:108 asked for the subject cap AND either a nested-quantifier rejection or an `asyncio.wait_for`; only the cap at executor.py:250 landed, and its own comment's O(2^N)-in-the-subject reasoning is wrong — measured 4x per added character, so 256 KiB is irrelevant). The defect lives in the SHARED `_regex`, so it hits the inline lane (routes.py:213) as well as the durable one; rule id is N/A, not DWF-ACT-006 (which is the `time.sleep(N&gt;=300)` heuristic). On a governed stack the door is the estate `writer` tier (routes.py:150-155); it is open to `anon` only in the default auth-off posture (security.py:20-21). In the durable lane the stalled activity is already persisted in workflow history, so the redelivery-after-kill loop is a plausible poison pill — inferred from Dapr redelivery semantics, not reproduced here.

**Fix.** Bound the WORK, not the input: run the regex arms in a `concurrent.futures.ProcessPoolExecutor` with a wall-clock budget (a separate process can actually be killed, unlike a GIL-holding thread) and raise `NodeError("regex exceeded its CPU budget")` on timeout — that turns the stall into a normal per-node failure the builder can paint. Alternatively vendor a linear-time engine (`re2`/`regex` with a timeout) for the `regex`/`alto`/`compare` arms. Either way, correct the comment at :246-249: the subject cap is a memory bound, not the ReDoS defence it claims to be.

**Evidence.**

```
executor.py:246-250
#: The regex arm's subject-length cap. Python's `re` is backtracking: a nested-quantifier pattern
#: over N chars is O(2^N)-ish, and the pattern AND the subject are both caller-influenced. Running
#: off the loop does not stop a GIL stall, so bounding the input is the load-bearing defence
#: (open_python-audit FLOWS-REDOS-ON-LOOP). 256 KiB dwarfs any real ALTO/transcription payload.
_REGEX_MAX_SUBJECT = 256 * 1024

executor.py:270
    return Payload(text="\n".join(m.group(1) if m.lastindex else m.group(0) for m in compiled.finditer(payload.text)))
```

**Verifier (ADJUSTED).** The mechanism is real and the arithmetic checks out, but the rule id and the scope framing are wrong.

Measured on this machine, `re.compile(r'^(a+)+$').search('a'*n + 'b')`: n=20 → 0.036 s, n=22 → 0.149 s, n=24 → 0.588 s, n=26 → 2.441 s. That is ~4x per added character, so ~40 s at n=30 and unsurvivable well before n=100 — four orders of magnitude below the cap. executor.py:246-250 states the mitigation:
```
#: The regex arm's subject-length cap. Python's `re` is backtracking: a nested-quantifier pattern
#: over N chars is O(2^N)-ish, and the pattern AND the subject are both caller-influenced. Running
#: off the loop does not stop a GIL stall, so bounding the input is the load-bearing defence
#: (open_python-audit FLOWS-REDOS-ON-LOOP). 256 KiB dwarfs any real ALTO/transcription payload.
_REGEX_MAX_SUBJECT = 256 * 1024
```
The comment's own reasoning is what fails: catastrophic backtracking is driven by the PATTERN, so a 256 KiB subject ceiling bounds nothing. Confirmed against the recorded remediation — open_python-audit.md:108 prescribed "cap the subject length, **and either** reject nested-quantifier patterns **or** move the match behind an `asyncio.wait_for` on a thread… `google-re2` is the durable fix". Only the cap landed, so "half fixed" is literally accurate, not rhetorical. Grepped `services/flows/src/flows/config.py` in full — the only budget is `serve_timeout` (the httpx read bound); there is no per-node CPU budget anywhere.

The off-loop half is genuinely done (executor.py:173-175, `case "regex": return await asyncio.to_thread(_regex, node, inputs)`), and the code itself concedes a thread does not help (executor.py:167).

What I am correcting:
(1) **Rule id.** DWF-ACT-006 is `time.sleep(N)` with `N &gt;= 300` inside an activity (diagrid-labs/skills/shared/review-activity-python.md:14). There is no `time.sleep` here and the rule's detection pattern does not cover CPU-bound regex. This is rule `N/A`.
(2) **Scope.** This is not an activity-scope defect. `_regex` is in the SHARED executor called by both lanes; the inline lane (routes.py:213) hangs the pod identically with no Dapr involved. Framing it as an activity finding narrows a defect that is broader.
(3) **Reachability caveat the finding omits.** `POST /flows/runs` is FGA-gated on the estate `writer` tier (routes.py:150-155, `security.EXECUTE = "writer"`), so on a governed stack this is an authenticated-writer DoS, not an open door. The audit's "`/api/*` has no auth" premise is stale. It IS open in the default posture — flows/security.py:20-21: "With auth off (every knob defaults off) the subject is ``anon`` and the checker is permissive".
(4) **The poison-pill escalation is inferred, not reproduced.** The liveness probe exists (chart/templates/fleet.yaml:231-235, `/api/health`, period 20 s, timeout 5 s) so the kill is real, and Dapr activity work items are redelivered after a worker dies — but I did not exercise a redelivery cycle, so "permanent crashloop" should be stated as a consequence of Dapr redelivery semantics rather than as a measured outcome.

</details>

<details><summary><b>The child's progress `set_custom_status` is read by nothing, so `units_done` is absent for the entire fan-out — the exact window the comment says it fixes</b> <i>(det-ingest, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/ingest/src/ingest/workflow.py:788`, `services/ingest/src/ingest/workflow.py:550`, `services/ingest/src/ingest/workflow.py:677`, `services/ingest/src/ingest/runs.py:363`, `services/ingest/src/ingest/__init__.py:357`

**Why it matters.** `chunk_run` publishes per-chunk progress on the CHILD instance (:788) under the heading "THE FAN-OUT'S ONLY PROGRESS SIGNAL, and it has to come from the child" (:776-781), and runs.py:357-360 promises "a run says '320 of 500' while it is still going". Nothing reads it. `_DaprWorkflowReader.state` (__init__.py:357) calls `get_workflow_state(run_id)` for the PARENT only; a repo-wide grep for `serialized_custom_status` returns exactly runs.py:355 and runs.py:363, both against that parent state, and no code anywhere reconstructs the `{run_id}-c{i}` child ids to query them. The parent's status during the fan-out is whatever :550 set BEFORE it — `{"units_total": N, "chunks": M}`, with no `units_done` key — and `units_done` only appears at :677, after `when_all` has already returned. Sequence: start a 10M-unit harvest with `RASK_INGEST_MAX_RUN_HOURS=24`; for the whole multi-hour fan-out `GET /v1/ingests/{id}` falls through `output.get("rows")` (absent) and `serialized_custom_status["units_done"]` (absent) to `record.units_done`, which the accept path set to 0 and nothing writes. The operator reads "0 of 10,000,000" for hours on a run that is landing rows the whole time — indistinguishable from a wedged run, which is the state this plane's terminate door exists for.

**Fix.** Either aggregate in the parent (have each child return progress incrementally, or set the parent's status from a periodic `ctx.create_timer` racing the fan-out via `when_any`, re-arming until `fanout` wins), or make the read side fan out: have `_DaprWorkflowReader` also fetch `{run_id}-c{i}` for `i in range(chunks)` — the count is already in the parent's custom status at :550 — and sum their `units_done`. If neither is wanted, delete :788 and the comment block above it rather than leaving a signal nothing consumes; runs.py:357-360's "320 of 500" claim must change with it.

**Evidence.**

```
workflow.py:788  `ctx.set_custom_status(json.dumps({"chunk_id": chunk.chunk_id, "units_done": units_done, "units_expected": chunk.expected_units}))`
workflow.py:550  `ctx.set_custom_status(json.dumps({"units_total": units_total, "chunks": len(chunks)}))`
runs.py:363  `done = _as_mapping(state.get("serialized_custom_status")).get("units_done")`
__init__.py:357  `state = wf.DaprWorkflowClient().get_workflow_state(run_id, fetch_payloads=True)`
```

**Verifier (CONFIRMED).** A repo-wide grep for `serialized_custom_status` over services/ingest/src returns exactly two hits, both in runs.py and both against the state handed in for the PARENT run id: :355 `total = _as_mapping(state.get("serialized_custom_status")).get("units_total")` and :363 `done = _as_mapping(state.get("serialized_custom_status")).get("units_done")`. The only producer of that state is __init__.py:357 `state = wf.DaprWorkflowClient().get_workflow_state(run_id, fetch_payloads=True)` — one instance id, the run id. Nothing in the service reconstructs the `f"{spec.run_id}-c{i}"` child ids (workflow.py:641) for a status read; that list exists solely so `terminate_chunks` can name them.

So the child's `ctx.set_custom_status(json.dumps({"chunk_id": chunk.chunk_id, "units_done": units_done, "units_expected": chunk.expected_units}))` at :788 lands on the child instance and no code path in this estate reads it, despite the heading above it at :776-781 calling it "THE FAN-OUT'S ONLY PROGRESS SIGNAL". The parent's status for the whole fan-out is what :550 set — `json.dumps({"units_total": units_total, "chunks": len(chunks)})`, no `units_done` key — and `units_done` first appears at :677, after `when_all` has already returned.

The fallback chain confirms the operator-visible outcome: runs.py:362-363 tries `output.get("rows")` (no output until finalize returns), then the parent custom status (no key), and runs.py:376 falls through to `record.units_done`, whose only definition is `units_done: int = 0` (runs.py:92) with no writer anywhere — `grep -n units_done` over src shows no store update between accept and terminal. api.py:538 renders `units_done=record.units_done`, i.e. 0, for the entire multi-hour fan-out. Warning is the right grade: an observability gap that makes a working run read as a wedged one, not data loss.

</details>

<details><summary><b>A `None` poll answer (Ray dashboard 404) abandons the watch on the FIRST poll and silently halts the cascade — the exact race two docstrings claim is survivable</b> <i>(det-medallion, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:226`, `services/medallion/src/medallion/workflow.py:216-222`, `services/medallion/src/medallion/workflow.py:677`, `packages/ray-kit/src/ray_kit/submit.py:157-158`

**Why it matters.** The conflation is real and reproducible: `workflow.py:226` reads `if not _is_terminal(status) and status is not None and polls &lt; spec.max_polls:`, so the single `status = None` sentinel carries two meanings — "the poll activity exhausted ACTIVITY_RETRY" (set in the `except` at :222) and "`job_status` saw a 404" (`ray_kit/submit.py:157-158` returns `None` by design). A returned `None` therefore skips `continue_as_new` and ends the watch on poll 1, so `publish_stage_ready` never runs and the mover is never woken. That contradicts the file's own comment at :206-208 ("`job_status` answers None for an unknown id precisely so that race is not fatal") and `job_status`'s docstring. I reproduced it against the repo's own harness: driving `stage_run` with `poll_stage` scripted `[None, 'SUCCEEDED']` gave actions `['call_activity(submit_stage)', 'create_timer(30s)', 'call_activity(poll_stage)', 'call_activity(report_stage_outcome)']` and outcome `{'submission_id': 'sub-1', 'status': None, 'polls': 1, 'verdict': 'abandoned'}`; the second answer was never requested. But three parts of the framing do not survive: (1) it is NOT silent — the abandoned branch at :230-240 unconditionally yields `report_stage_outcome`, which records the `abandoned` metric, sets an ERROR span status and best-effort-publishes a FAIL RunEvent to the graph, so an operator and the notifications plane do get a record (with `status` printed as UNKNOWN, which is the actual diagnosability defect); (2) the `status is not None` clause is not gratuitous — on the exception path `polls` is NOT incremented (:221 only runs on success), so removing it would make a persistently unreachable dashboard loop `continue_as_new` forever without ever reaching `max_polls`; any fix has to distinguish the two Nones, not just drop the clause; (3) the claimed production path is thin — the timer fires BEFORE the first poll (:214) precisely to close the not-yet-registered race, and `submit_stage` returns the id the submitter POSTed (:277-282) rather than re-deriving it, so a 404 30 s after an ACKed submit needs the job record itself to be gone (head restart / RayService head switch / job deletion), i.e. cases where the job is generally dead too. "The Ray job runs to SUCCEEDED and writes the destination dataset" while the dashboard 404s is asserted, not demonstrated. Real defect worth fixing (the watch should treat a 404 as "not yet terminal" and hand forward, bounded by `max_polls`, exactly as both docstrings promise), but a warning, not a critical.

**Fix.** Stop overloading `status`. Carry the failure as its own local — deterministic, since it is recomputed identically on every replay from the same history: set `watch_lost = False` in the `try` after the assignment and `watch_lost = True` in the `except`, then make the continuation guard `if not watch_lost and not _is_terminal(status) and polls &lt; spec.max_polls:`. That lets a 404 burn one poll of the existing `max_polls` budget (which is exactly what the ceiling is for) instead of ending the watch, and keeps an exhausted activity retry falling through to `abandoned` as documented. Apply the same change at :677 in `train_run`. Then pin it: a `poll_stage` sequence of `[None, None, "SUCCEEDED"]` must still reach `publish_stage_ready`.

**Evidence.**

```
workflow.py:216-226 —
    try:
        status = yield ctx.call_activity(poll_stage, input={"submission_id": submission_id}, retry_policy=ACTIVITY_RETRY)
        polls = spec.polls_done + 1
    except Exception:
        if not ctx.is_replaying:
            log.error("medallion_stage_watch_lost", extra={"submission_id": submission_id, "polls": polls})
        status = None

    # STILL RUNNING and budget left: hand the rest to a fresh turn. ...
    if not _is_terminal(status) and status is not None and polls < spec.max_polls:

against ray_kit/submit.py:157-158 —
    if response.status_code == 404:
        return None
```

**Verifier (ADJUSTED).** workflow.py:226 `if not _is_terminal(status) and status is not None and polls &lt; spec.max_polls:`; :222 `status = None` in the `except`; :206-208 `# ... and `job_status` answers None for an unknown id precisely so that race is not fatal.`; ray_kit/submit.py:157-158 `if response.status_code == 404: return None`. Reproduced with the repo harness (`services/medallion/tests/test_stage_workflow.py::_Ctx`/`_drive`, poll_stage=[None,'SUCCEEDED']): actions `submit_stage, create_timer(30s), poll_stage, report_stage_outcome`, outcome verdict `abandoned`, polls 1. Counter-evidence to the 'silent' claim: :239 `yield ctx.call_activity(report_stage_outcome, ...)` on that same branch, and :423 `record_stage_outcome(outcome.verdict, ...)` inside it. Counter-evidence to the reachability claim: :214 `yield ctx.create_timer(...)` before the first poll, and submit_stage's comment at :277-282 ('RETURN WHAT THE SUBMITTER POSTED — never re-derive it').

</details>

<details><summary><b>train_run is scheduled into the producer, which starts no WorkflowRuntime under the default chart — the training watcher never runs</b> <i>(mgt, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/producer.py:108`, `services/medallion/src/medallion/producer.py:172`, `services/medallion/src/medallion/services/train.py:301`, `services/medallion/src/medallion/services/train.py:329`, `chart/values.yaml:967`, `chart/values.yaml:1000`, `chart/templates/dapr-statestore.yaml:109`

**Why it matters.** The flag coupling is real and the watcher IS dead on a stock deployment, but the finding's second mechanism is wrong. `medallion/workflow.py:1140` reads `WORKFLOWS = (stage_run, train_run, promotion_review)` and `register()` (workflow.py:597-602) loops `for w in WORKFLOWS: runtime.register_workflow(w)` — so when `qualityReview` IS on, the producer's runtime registers `train_run` and the watcher works. The defect is exactly one thing: the producer's runtime and its actor-state-store scope are BOTH gated on `medallion.qualityReview` (producer.py:108; dapr-statestore.yaml `{{- if .Values.medallion.qualityReview }}` → `append $scopes .Values.medallion.producer.daprAppId`) while the train head is gated on `medallion.ray` (medallion.yaml:111 `- { name: MEDALLION_RAY_ENABLED, value: "true" }` under `{{- if .Values.medallion.ray }}`). With the shipped defaults (`ray: true`, `qualityReview: false`) `schedule_train_watch` runs in a process with no runtime and an unscoped app-id, the schedule raises, and train.py:332-334 logs `medallion_train_watch_not_scheduled` and acks. Severity warning rather than critical: nothing is stuck, nothing is duplicated, the training job still runs and emits its own OpenLineage lifecycle; what is lost is the notification for the narrow case the watcher exists for (a job that dies before emitting anything), and that loss is explicitly declared tolerable by the function's own docstring ("losing the watcher costs the notification if it dies, never the training run … Logged loudly and acked").

**Fix.** Gate the producer's WorkflowRuntime on `quality_review_enabled OR ray_enabled` (it already registers every workflow via `medallion.workflow.register`), and widen the dapr-statestore scope for `medallion.producer.daprAppId` to the same disjunction. Alternatively move `train_run` to a mover, which starts its runtime on `ray_enabled` and is already scoped under `.Values.medallion.ray`. Either way, `schedule_train_watch`'s swallow should log at ERROR and be covered by a test that asserts the instance exists, not merely that no exception escaped.

**Evidence.**

```
producer.py:108 `if get_settings().quality_review_enabled:` … producer.py:172 `register_train_trigger_route(app, _dapr_app)`; train.py:329-334 `try:\n        client = wf.DaprWorkflowClient()\n        client.schedule_new_workflow(workflow=train_run, input=spec.model_dump(), instance_id=instance_id)\n    except Exception:\n        log.warning("medallion_train_watch_not_scheduled", …)\n        return None`; values.yaml:1000 `qualityReview: false`; values.yaml:967 `ray: true`
```

**Verifier (ADJUSTED).** producer.py:108 `if get_settings().quality_review_enabled:` … producer.py:119 `app.state.workflow_client = wf.DaprWorkflowClient()`; producer.py:172 `register_train_trigger_route(app, _dapr_app)` (unconditional). train.py:301 `schedule_train_watch(settings, token=token, model=model, …)`; train.py:329-334 `client = wf.DaprWorkflowClient()\n        client.schedule_new_workflow(workflow=train_run, input=spec.model_dump(), instance_id=instance_id)\n    except Exception:\n        log.warning("medallion_train_watch_not_scheduled", …)\n        return None`. chart/values.yaml:967 `ray: true`, :1000 `qualityReview: false`; chart/templates/dapr-statestore.yaml:108-109 `{{- if .Values.medallion.qualityReview }}\n{{-   $scopes = append $scopes .Values.medallion.producer.daprAppId }}` with the comment "It starts a runtime exactly when `medallion.qualityReview` is on (producer.py's lifespan), so that is the gate here — NOT `medallion.ray`"; values.yaml:1328-1395 base `stateStore.scopes` contains annotator/catalog/ingest/flows/notifications and no producer id. REFUTED sub-claim: workflow.py:1140 `WORKFLOWS = (stage_run, train_run, promotion_review)` — `train_run` IS registered whenever the producer's runtime starts.

</details>

<details><summary><b>No terminate route for the cascade: an in-flight stage_run cannot be stopped by any HTTP means</b> <i>(mgt, rule DWF-MGT-003, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/mover.py:140`, `services/medallion/src/medallion/mover.py:143`, `services/medallion/src/medallion/workflow.py:90`, `services/compute/src/compute/routes.py:29`

**Why it matters.** The route inventory is accurate — the mover mounts only `app.include_router(health_router)` (mover.py:140) and `register_stage_route(app)` (mover.py:143), `services/compute/routes.py` is eight `@router.get` rows and nothing else, and `grep -rn terminate_workflow services/` finds it only in ingest (`__init__.py:340`) and ingest's own child-terminate (`workflow.py:1231`). But two load-bearing parts of the harm story do not hold. (1) `stage_run` is not an unbounded hang: workflow.py:86-90 documents the ceiling and says a job still running at it "is NOT killed — the workflow gives up WATCHING it and says so; the job's own registered commit remains the source of truth, and the lineage reconciler still catches a job that dies" — the same boundedness the finder used to grade the flows twin as a warning, applied inconsistently here. (2) Terminating `stage_run` would not stop the GPU burn the finding leads with: the workflow only submits and polls a Ray job, so `terminate_workflow` stops the WATCH and the wake-up publish, never the job. The real, narrower gap is the operator lever to stop a wrongly-dispatched stage from publishing the next tier's trigger — DWF-MGT-003, and a warning, not a critical.

**Fix.** Give the movers (or the producer, which already has a gateway row) a `POST /stages/{instance_id}/terminate` that resolves the instance, authorizes on the destination namespace the way `promotion_object` does, and calls `terminate_workflow`. Note the SDK limit `ingest.workflow.terminate_chunks` already documents — it stops further scheduling, not an in-flight activity — so the route must also be able to stop the underlying Ray job, which means `services/compute` needs a guarded stop endpoint over `ray_kit`.

**Evidence.**

```
mover.py:140-143 `app.include_router(health_router)\n# The DaprApp wrapper serves GET /dapr/subscribe (read by the sidecar at startup) and routes deliveries\n# of `sub_topic` to /medallion-event. …\nregister_stage_route(app)`; workflow.py:90 `MAX_POLLS: Final = 2880`; compute/routes.py:29 `@router.get("/jobs")`
```

**Verifier (ADJUSTED).** mover.py:140 `app.include_router(health_router)`; mover.py:143 `register_stage_route(app)` — the complete non-health surface. workflow.py:86-90 `#: Hard ceiling on poll iterations, so history cannot grow without bound (DWF-DET-013). At the default\n#: interval this is 24h of waiting. A job still running at the ceiling is NOT killed …\nMAX_POLLS: Final = 2880`. services/compute/src/compute/routes.py:24-59 — `@router.get("/health")`, `/jobs`, `/jobs/{submission_id}/logs`, `/cluster`, `/actors`, `/tasks`, `/overview`, `/logs`; no POST/DELETE.

</details>

<details><summary><b>No status route for stage_run or train_run — the cascade's workflow instances are unobservable over HTTP</b> <i>(mgt, rule DWF-MGT-002, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/mover.py:143`, `services/medallion/src/medallion/services/transform.py:111`, `services/medallion/src/medallion/services/train.py:327`

**Why it matters.** The headline holds — no route in medallion reads back a `stage-…` or `train-…` instance — but the supporting claim is a misread. `get_workflow_state` is called in THREE places in medallion, and one of them is not an existence probe: promotions.py:108 `state = client.get_workflow_state(instance_id, fetch_payloads=True)` inside `_live_spec`, which is exactly the read that backs the public `GET /promotions/{instance_id}` (promotions.py:251). So medallion does have a workflow status seam over HTTP; it covers `promotion_review` only. The other two are probes: transform.py:158 and promotions.py:167. Corrected framing: the promotion lane got a status read and the two Ray lanes did not, so a stalled stage or train instance has no HTTP answer to "does it exist and where is it" — which is also why the terminate gap above bites twice.

**Fix.** Add `GET /stages/{instance_id}` on the producer (which already hosts a gateway row and a `_live_spec`-shaped reader for promotions), returning a typed model built from `state.to_json()` — the same `to_json()`-not-attributes rule both siblings already pin against dapr-ext-workflow 1.18.3. Gate it on the destination namespace like `promotion_object` does.

**Evidence.**

```
mover.py:143 `register_stage_route(app)` — the mover's only non-health mount; transform.py:111 `instance_id = f"stage-{stage_submission_id(stage, token, from_uri, to_uri)}"`; train.py:327 `instance_id = f"train-{submission_id}"`
```

**Verifier (ADJUSTED).** transform.py:111 `instance_id = f"stage-{stage_submission_id(stage, token, from_uri, to_uri)}"`; train.py:327 `instance_id = f"train-{submission_id}"`; mover.py:143 `register_stage_route(app)` is the mover's only non-health mount. promotions.py:108 `state = client.get_workflow_state(instance_id, fetch_payloads=True)` (in `_live_spec`, called by `show` at promotions.py:261), promotions.py:167 `return client.get_workflow_state(instance_id) is not None` (`_exists`), transform.py:158 `return client.get_workflow_state(instance_id) is not None` (`_stage_workflow_exists`).

</details>

<details><summary><b>GET /promotions/{instance_id} skips its authorization gate entirely for an unauthenticated caller</b> <i>(mgt, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/medallion/src/medallion/api/promotions.py:251`, `services/medallion/src/medallion/api/promotions.py:262`, `services/medallion/src/medallion/api/produce_auth.py:157`

**Why it matters.** `authenticate_subject` returns `None` when no `Authorization` header is present (produce_auth.py:157-158) — it does not raise. `show` then evaluates `if gate is not None and subject:`, so a caller with NO credential falls straight past the `can_promote` check and gets a 200 carrying the promotion's `project`, `from_dataset`, `to_dataset`, `reasons` (the failed quality assertions) and `approval_hours`. The route is public: the gateway row `("/api/promotions", "/promotions", *medallion)` (gateway/__init__.py:161) is root-mounted, and the router declares no dependencies. Concretely: an anonymous GET to /api/promotions/promotion-&lt;token&gt; either returns a tenant's dataset names and quality-failure reasons, or 404s — which also makes the route an unauthenticated oracle for which reviews are live. Its own sibling on the same router refuses exactly this caller ('a promotion decision must name the person who made it'), and both `ingest.get_ingest` and `flows.get_run` authorize their status reads with explicit rationale ('reading it is not public'; 'anyone could read the product of the compute they were refused permission to spend').

**Fix.** Refuse an unresolved subject the way `decide_promotion` does — raise `PermissionDeniedError` when `subject` is falsy and FGA is on — and make the gate unconditional: `if gate is not None: await gate(subject=subject, obj=promotion_object(spec))`. Leaving it dev-open when `app.state.fga is None` is consistent with the rest of the estate; skipping it because the CALLER sent no token is not.

**Evidence.**

```
promotions.py:262-264 `gate = _fga_gate(request)\n    if gate is not None and subject:\n        await gate(subject=subject, obj=promotion_object(spec))`; produce_auth.py:157-158 `if not authorization:\n        return None`
```

**Verifier (CONFIRMED).** produce_auth.py:157-158 `if not authorization:\n        return None` — the dependency returns `None` for a caller with no header, before the OIDC-enabled-but-unwired 503 branch at :159 and before any verification. promotions.py:251-263 `@router.get("/promotions/{instance_id}")\nasync def show(… subject: Annotated[str | None, Depends(authenticate_subject)]) -&gt; PromotionUnderReview:\n    wf_client = _client(getattr(request.app.state, "workflow_client", None))\n    spec = await run_in_threadpool(_live_spec, wf_client, instance_id)\n    gate = _fga_gate(request)\n    if gate is not None and subject:` — with FGA on and no credential, `subject` is None, the `and subject` short-circuits, and the 200 body carries `project`, `from_dataset`, `to_dataset`, `reasons`, `approval_hours`. The route is reachable: gateway/__init__.py:162 `("/api/promotions", "/promotions", *medallion)`; `router = APIRouter(tags=["promotions"])` declares no dependencies and producer.py adds no auth middleware (`grep -n "dependencies=\|add_middleware" producer.py` returns nothing). The sibling on the same router refuses the same caller — promotions.py:189 `raise PermissionDeniedError("a promotion decision must name the person who made it; sign in and retry")` — so this is an asymmetry, not a documented dev-open path (the documented dev-open path is `_fga_gate` returning None when FGA is off, which the `gate is not None` half already covers). Warning is the right grade: read-only metadata for one promotion, and the caller must already know the `promotion-&lt;token&gt;` id.

</details>

<details><summary><b>flows exposes start and status but no terminate — a durable run cannot be cancelled</b> <i>(mgt, rule DWF-MGT-003, CONFIRMED)</i></summary>

**Sites:** `services/flows/src/flows/routes.py:96`, `services/flows/src/flows/routes.py:226`, `services/flows/src/flows/workflow.py:45`

**Why it matters.** `flows/routes.py` declares exactly two run routes — `POST /flows/runs` and `GET /flows/runs/{run_id}` — and nothing in the service calls `terminate_workflow`, `pause_workflow` or `resume_workflow`. `flow_run_workflow` fans out `run_node` activities across topological waves against LIVE Ray Serve endpoints, which security.py's own docstring says 'spends the estate's GPU compute on whatever the caller drew'. A wide graph submitted against a wedged Serve deployment therefore occupies Serve replicas for up to `serve_timeout` (default 180 s, config.py:40) x `NODE_RETRY` 3 attempts per node, per wave, and the only way to stop it is to wait — a pod restart does not help, because the instance is durable and resumes. Graded warning rather than critical because the run genuinely IS bounded: every activity carries a read timeout, retries are capped at 3, and the wave count is finite. What is missing is the operator lever, not an unbounded hang.

**Fix.** Add `POST /flows/runs/{run_id}/terminate` behind the same `security.EXECUTE` check `get_run` already uses, calling `terminate_workflow` through `asyncio.to_thread`, and answer 202 with the SDK's real semantics stated in the body the way `ingest.api.TerminateAccepted` does ('further scheduling stops, but work already in flight may still complete').

**Evidence.**

```
routes.py:96 `@router.post(\n    "/runs",` and routes.py:226 `@router.get("/runs/{run_id}", response_model=RunState)` — the complete run surface; workflow.py:45 `@wfr.workflow(name="flow_run")`
```

**Verifier (CONFIRMED).** `grep -n "@router\." services/flows/src/flows/routes.py` returns exactly four rows: `:82 @router.get("/catalog")`, `:88 @router.post("/validate")`, `:96 @router.post(` (`/runs`), `:226 @router.get("/runs/{run_id}", response_model=RunState)`. `grep -rn "terminate_workflow|pause_workflow|resume_workflow" services/` finds no hit anywhere under `services/flows`. The durable lane is real (routes.py:186-201, the `create_workflow_instance` branch) and `workflow.py:45 @wfr.workflow(name="flow_run")` fans out `run_node` with `retry_policy=NODE_RETRY` (workflow.py:38, 3 attempts) against `serve_timeout` (config.py:40 `serve_timeout: float = Field(default=180.0, alias="RASK_FLOWS_SERVE_TIMEOUT")`), so the run is bounded and the finder's own warning grade is right: the missing lever, not an unbounded hang. DWF-MGT-003 as cited.

</details>

<details><summary><b>Under cascadeViaPublish the whole downstream cascade rides `table_published`, which is emitted fail-open and documented as "loses nothing"</b> <i>(pubsub, rule N/A, CONFIRMED)</i></summary>

**Sites:** `packages/service-kit/src/service_kit/control_emit.py:109`, `services/catalog/src/catalog/api/v1/endpoints/publication.py:155`, `services/medallion/src/medallion/services/gate_decision.py:62`, `services/medallion/src/medallion/api/bronze_arrival.py:96`

**Why it matters.** With medallion.cascadeViaPublish true, gate_decision returns GateOutcome.PUBLISH and the mover deliberately does NOT fire pub_topic — the next hop happens ONLY when /publication-arrival receives the catalog's table_published control event. That event is emitted by DaprControlEmitter.emit, which swallows every publish failure into a counter whose own description reads 'fail-open: the change itself still happened and is audited, only the live-refresh hint is lost', and publication.py:155-157 states 'A consumer that MISSES this event loses nothing: the published tag still answers what is ready?'. Both claims are true for the catalog's console ring buffer and for a polling consumer; both are false for the cascade, which does not poll the tag — the medallion plane runs no cron and no reconcile binding. Sequence: silver$features is written, registered and gate-passed; the catalog advances the published tag (result.advanced true, so the data IS now consumable); the emit at publication.py:166 hits the 5s publish timeout during a NATS blip; emit catches, bumps catalog.control_emit.failed, logs at WARNING; the publish route returns 200 with a successful PublishResult. The silver-&gt;gold hop never runs, every pod is green, and the only signal is a counter documented as meaning nothing was lost. Unlike the lineage lane, the control lane has no outbox at all — publish_lineage_with_outbox is used for lineage.events.v1 and there is no equivalent on catalog.control.v1.

**Fix.** Either stage table_published through the object-store outbox (and teach the relay to re-publish, per the previous finding), or gate cascadeViaPublish at boot on a recovery mechanism existing. At minimum stop asserting the loss is free: amend publication.py:155-157 and the control_emit.failed counter description at control_emit.py:92-95 to say that under cascadeViaPublish a dropped table_published cancels the downstream cascade with no second chance.

**Evidence.**

```
        except Exception as exc:
            self._emit_failed.add(1, {f"lance.{self._service}.action": event.action})
            log.warning(
                "control_publish_failed",
                extra={"action": event.action, "object_id": event.object_id, "error": str(exc)},
            )
```

**Verifier (CONFIRMED).** Reproduced at HEAD. packages/service-kit/src/service_kit/control_emit.py:107-114 swallows every publish failure — `except Exception as exc: self._emit_failed.add(1, {...}); log.warning("control_publish_failed", ...)` — under a counter whose own description reads "fail-open: the change itself still happened and is audited, only the live-refresh hint is lost" (control_emit.py:96-99), and emit_control's docstring (control_emit.py:143) repeats "best-effort — the emitter swallows every error". services/catalog/src/catalog/api/v1/endpoints/publication.py:163-164 carries the premise the cascade falsifies: "A consumer that MISSES this event loses nothing: the `published` tag still answers 'what is ready?'". The mover really does stop at the catalog: HEAD transform.py's `elif decision is GateOutcome.PUBLISH and result is not None:` branch calls only `catalog_register.publish_stage_output` and never `settings.pub_topic`, and gate_decision.py:20-27 states the intent ("PUBLISH = 'let the catalog's gate rule, and its tag move is the trigger'"). The only consumer is services/medallion/src/medallion/api/bronze_arrival.py:96-113 (/publication-arrival), and the medallion plane has no cron/reconcile binding anywhere in chart/templates, so nothing polls the tag. There is no outbox on the control lane — control_emit calls dapr_publish.publish_event directly, while every lineage emit in medallion goes through outbox.publish_lineage_with_outbox. Two notes that strengthen rather than weaken it: (a) the finding's precondition is correct at HEAD (chart/values.yaml:1034 `cascadeViaPublish: false`), and (b) the working tree has DELETED the flag from the app (config.py:370 "that flag chose between two enforcement points and is gone"; gate_decision returns PUBLISH whenever `has_target and has_catalog`, and chart/templates/medallion.yaml renders MEDALLION_CATALOG_URL unconditionally for every mover) — so on the working tree this becomes the DEFAULT and only door. Warning is the right severity: the published data stays correct and consumable, only the next tier silently never runs.

</details>

<details><summary><b>The publication trigger's {from_version, to_version} range is published, silently discarded by the consumer model, and never reaches the job that reads it — the CDF delta read is dead config</b> <i>(pubsub, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/services/publication_trigger.py:147`, `services/medallion/src/medallion/services/trigger_guards.py:102`, `services/medallion/src/medallion/services/ray_submit.py:105`, `runners/dummy/src/dummy_runner/job.py:74`

**Why it matters.** The mechanism is exact — the range is published, dropped at parse, and never exported as BASE_VERSION — but the impact statement is too broad. It is dead config on an OPT-IN lane, not an active linear-cost regression in the shipped estate: BASE_VERSION is only read by a Ray stage job, and the Ray stage-job entrypoint is per-mover opt-in (`Optional per-mover stageJob:` at chart/values.yaml:1072) which NO mover row in chart/values.yaml declares. So today nothing pays the O(tier) cost; what is broken is that D1's advertised "O(delta), not a tier rescan" property cannot be obtained by any lane that turns the Ray stage job on, and both the job docstring and publication_trigger's docstring assert a wiring that does not exist. Same severity, narrower blast radius.

**Fix.** Add from_version: int | None and to_version: int | None to StageTrigger (they are already the additive-evolution example its own docstring cites), thread them through _dispatch_stage_workflow into submit_stage_job, and export BASE_VERSION in ray_submit.py's env_vars (empty string when from_version is None, which the runner already reads as 'everything'). Add a test asserting that a publication trigger carrying a range produces a BASE_VERSION in the submitted runtime_env — the gap sits between three files that each look correct alone.

**Evidence.**

```
    token: str | None = None
    dataset: str | None = None
    namespace: str | None = None
    project: str | None = None
    ...
    model_config = ConfigDict(extra="ignore")   # from_version/to_version, published at publication_trigger.py:147-148, are dropped here
```

**Verifier (ADJUSTED).** services/medallion/src/medallion/services/publication_trigger.py:147-148 stamps `"from_version": extra.get("from_version"), "to_version": extra.get("to_version")` onto the trigger. services/medallion/src/medallion/services/trigger_guards.py:102 is `model_config = ConfigDict(extra="ignore")` and neither field is declared — and the model's own docstring at line 90 names them as an example of what is tolerated-but-undeclared ("``from_version``/``to_version`` already ride this payload"), so they are dropped at `StageTrigger.model_validate(data)` (trigger_guards.py:194). `grep -rn from_version services/medallion/src` returns only publication_trigger, two docstrings, and catalog_register.py:248-249 (which parses the catalog's PUBLISH RESPONSE, not the trigger) — no consumer of the trigger's range exists. services/medallion/src/medallion/services/ray_submit.py:105-140's env_vars dict has FROM_URI/TO_URI/STAGE/LINEAGE_JSON/S3_*/OTEL_*/RASK_PARAM_* and no BASE_VERSION; the workload prefix is applied locally (`**{f"RASK_PARAM_{key}": value ...}`) so no lane can smuggle it in. Estate-wide, BASE_VERSION appears only at runners/dummy/src/dummy_runner/job.py:7,9,73-75, scripts/ray_dummy_job.py:11 and one runner test. runners/dummy/src/dummy_runner/job.py:42-44 therefore always takes `if base_version is None: return ds.to_table(with_row_id=True)` — the whole upstream tier — and the redelivery no-op at job.py:94-95 (`if delta.num_rows == 0`) is indeed unreachable for a non-empty source.

</details>

### Info — 25

<details><summary><b>DaprWorkflowClient is constructed and used inside the terminate_chunks activity</b> <i>(act-ingest, rule DWF-ACT-001, CONFIRMED)</i></summary>

**Sites:** `services/ingest/src/ingest/workflow.py:1228`, `services/ingest/src/ingest/workflow.py:1231`

**Why it matters.** A literal hit on DWF-ACT-001, and it stands: the rule's hazard is an activity ORCHESTRATING (starting a child, or waiting on one) and deadlocking against its own history, and this neither starts nor waits — it enqueues a terminate for children the parent has already abandoned, then returns a count. The SDK offers no orchestrator-side alternative (`DaprWorkflowContext` has no terminate-child verb; `terminate_workflow` is `recursive=True` by default, confirmed in the installed 1.18 SDK), the docstring's residual is accurate and quoted from the SDK ('terminating a workflow has no effect on any in-flight activity function executions'), and the app-id constraint is respected because the activity worker and the chunk workflows are registered in the same runtime in the same process (`__init__.py:203-206`). The swallowed exception on 1233 is the normal already-terminal race and is logged, not `pass`. The one actionable residual is hygiene: `DaprWorkflowClient` exposes `close()` and this never calls it, so each invocation leaves a TaskHubGrpcClient channel to be reclaimed by refcounting rather than closed deterministically.

**Fix.** Keep the call — record the DWF-ACT-001 exemption in the docstring beside the SDK quotation that already justifies it, and wrap the client so it is closed deterministically (`with contextlib.closing(wf_client.DaprWorkflowClient()) as client:`).

**Evidence.**

```
workflow.py:1228-1233  `client = wf_client.DaprWorkflowClient()\n    for child_id in child_ids:\n        try:\n            client.terminate_workflow(child_id)\n            terminated += 1\n        except Exception:`
```

**Verifier (CONFIRMED).** The finding reports a literal DWF-ACT-001 hit and then downgrades it from the rule table's `critical` to info on the merits; that downgrade is correct and I could not break it.

The code is as quoted — `workflow.py:1228  client = wf_client.DaprWorkflowClient()` inside `terminate_chunks`, a registered activity (`ACTIVITIES`, workflow.py:1239-1250). The rule's stated hazard is "activities must not orchestrate other workflows directly; this can deadlock and corrupt history", and this neither starts a child nor waits on one: it calls `terminate_workflow` in a loop and returns a count. The installed SDK backs the docstring verbatim — `dapr/ext/workflow/dapr_workflow_client.py:252-255`: "terminating a workflow has no effect on any in-flight activity function executions … there is no way to terminate an in-flight activity execution" — and 240-241 confirms `recursive: bool = True` by default. `DaprWorkflowContext` offers no terminate verb, so there is no orchestrator-side alternative to move this to. The exception at 1233 is not `pass`: it logs at debug with `exc_info=True`, and the docstring at 1205-1215 records the whole decision (the residual is BOUNDED, not eliminated; best-effort by construction).

The residual the finding keeps is real: `dapr_workflow_client.py:291-293  def close(self): """Closes the gRPC connection used by the client."""` exists and `terminate_chunks` never calls it, so each invocation leaves a TaskHubGrpcClient channel to refcounting. Bounded in practice — this activity runs at most once or twice per run (the deadline branch at workflow.py:657 and the error boundary at 715), under ACTIVITY_RETRY — so info is the right severity, not warning.

</details>

<details><summary><b>Activity envelopes are untyped dicts, so a missing key becomes a plausible default instead of a validation error</b> <i>(act-ingest, rule DWF-ACT-009, ADJUSTED)</i></summary>

**Sites:** `services/ingest/src/ingest/workflow.py:1070`, `services/ingest/src/ingest/workflow.py:1086`, `services/ingest/src/ingest/workflow.py:1096`, `services/ingest/src/ingest/workflow.py:803`

**Why it matters.** The observation holds — `finalize` (workflow.py:1070) and `emit_terminal` (workflow.py:1096) take a bare `dict[str, Any]` envelope and pick it apart by key, with `read_version=int(payload.get("read_version") or 0)` at workflow.py:1085 coercing a missing key to a default rather than validating a model. But the failure the finding attaches to it does not occur: read_version=0 is accepted by Lance, not refused. So this is a style/DWF-ACT-009 gap with no demonstrated failure, not a latent data path.

**Fix.** Declare the envelopes as models next to the payload models they wrap (a `FinalizeInput` with a required `read_version` and a `TerminalInput{spec, outcome}`) and validate them the way the bodies already validate `RunSpec`; drop the `or 0` so an absent required field raises where it can be read as a rollout problem rather than as a Lance error.

**Evidence.**

```
workflow.py:1070  `def finalize(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> dict[str, Any]:`
workflow.py:1081-1086  `spec = RunSpec.model_validate(payload["spec"])\n    outcome = finalize_run(\n        spec,\n        payload.get("fragments") or [],\n        payload.get("errors") or {},\n        read_version=int(payload.get("read_version") or 0),\n    )`
```

**Verifier (ADJUSTED).** The rule-conformance half is accurate and I confirm it: `RunSpec`/`ChunkSpec`/`ChunkResult`/`RunOutcome` are Pydantic models validated on the first line of each activity (e.g. workflow.py:1081, 1102-1103), and only the multi-field envelopes are bare dicts.

The failure claim is REFUTED by measurement. The finding asserts "a Lance Append against a version that does not exist fails as a manifest lookup after every byte has been fetched". I ran it on the installed pylance 10.0.0 against a real bronze dataset built through `ingest.worker.units_to_table` + `CREATION_FLAGS`: `lance.LanceDataset.commit(uri, LanceOperation.Append(frags), read_version=0)` SUCCEEDED, producing version 2 with 6 rows. That matches `lander.py:124-131`, which already records the measurement — "an Append does NOT get conflict detection from `read_version`: appends COMMUTE, so Lance rebases them" — and lander.py:143-144, which says version 0 is a legal Lance version.

The dedupe is not lost either: `dataplane.py:625-628` skips versions `&lt;= read_version`, so presenting 0 makes `_find_run_commit` scan MORE versions, not fewer — strictly more likely to recognise the run's own commit. And `lander.py:146` short-circuits an empty list before `read_version` is used at all.

The cross-deploy premise is also weaker than stated. runtime.py:236-240 documents a rollout path for the CHUNK descriptor shape (`if chunk.keys:` inline branch), not for the finalize envelope, and no comment or test anywhere claims an older build's finalize input existed without `read_version`. Severity info is right; the framing needs the failure sentence removed.

</details>

<details><summary><b>No activity uses the &lt;verb&gt;_activity naming convention</b> <i>(act-ingest, rule DWF-ACT-008, CONFIRMED)</i></summary>

**Sites:** `services/ingest/src/ingest/workflow.py:1239`

**Why it matters.** All ten registered activities are named for the verb alone (`emit_start`, `resolve_limits`, `ensure_dataset`, `enumerate_chunks`, `publish_units`, `drain_chunk`, `reconcile_chunk`, `finalize`, `emit_terminal`, `terminate_chunks`). The runtime registers by `__name__`, so these are the wire names another language's caller would have to use, and two of them (`finalize`, `drain_chunk`) read as plain functions rather than as durable steps at a call site. Nothing is broken today — the registry is single-sourced through `register()` and no cross-language caller exists — so this is a convention note, not a defect.

**Fix.** If cross-language invocation is ever wanted, rename to `&lt;verb&gt;_activity` in `ACTIVITIES` and the `ctx.call_activity` call sites together; otherwise record the deviation once beside `ACTIVITIES` so the next sweep does not re-raise it.

**Evidence.**

```
workflow.py:1239-1250  `ACTIVITIES = (\n    emit_start,\n    resolve_limits,\n    ensure_dataset,\n    enumerate_chunks,\n    publish_units,\n    drain_chunk,\n    reconcile_chunk,\n    finalize,\n    emit_terminal,\n    terminate_chunks,\n)`
```

**Verifier (CONFIRMED).** Accurate as stated. `workflow.py:1239-1250` lists all ten registered activities — `emit_start, resolve_limits, ensure_dataset, enumerate_chunks, publish_units, drain_chunk, reconcile_chunk, finalize, emit_terminal, terminate_chunks` — and none carries the `_activity` suffix DWF-ACT-008 names (the rule is `info` severity in the checklist, matching). Registration is by `__name__`: `register()` at workflow.py:1253-1258 loops `runtime.register_activity(a)` with no explicit name, and `workflow_runtime.py` resolves `effective_name = name or fn.__name__`, so these are the wire names a cross-language caller would use.

I checked for a recorded decision that would refute this and found none — no comment near `ACTIVITIES` or `register()` addresses naming. The finding's own hedge is correct: the registry is single-sourced through `register()` (its docstring: "one place, so nothing is silently unregistered") and no cross-language caller exists, so nothing is broken. Convention note, info, no action forced.

</details>

<details><summary><b>`StageJobSpec.lineage_json` is an uncapped `str` re-persisted into workflow state on every one of up to 2880 turns</b> <i>(act-medallion, rule DWF-ACT-004, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:110`, `services/medallion/src/medallion/workflow.py:227`, `services/medallion/src/medallion/services/transform.py:550`, `packages/lineage-kit/src/lineage_kit/consume.py:175`

**Why it matters.** `StageJobSpec.lineage_json` is an uncapped `str` on the workflow input, so it is re-persisted to the actor state store on every `continue_as_new` turn and re-serialized into three activity envelopes — a literal DWF-ACT-004 miss. In practice the document is bounded by cascade depth (one LineageEdge per hop, bronze→silver→gold), so it is a few KB, not an unbounded upstream-controlled payload; no oversize failure is demonstrated. It is only needed on the FIRST turn (submit_stage passes it into the Ray runtime_env), so the cheap correct fix is to clear it on `continue_as_new` and declare a cap.

**Fix.** Either bound it at the seam the way the FAIL message is bounded (a `_LINEAGE_JSON_CAP`, refusing/truncating with a log rather than letting an upstream cell size a state-store write), or make it a pointer: stage the document to the object store once in a dedicated activity and carry only its key on the spec, resolving it inside `submit_stage` where the Ray `runtime_env` actually needs the bytes. The pointer form also removes it from the `continue_as_new` payload entirely.

**Evidence.**

```
workflow.py:106-115
    from_uri: str
    to_uri: str
    stage: str
    token: str | None = None
    lineage_json: str = ""

workflow.py:227
        ctx.continue_as_new(spec.model_copy(update={"submission_id": submission_id, "polls_done": polls, "started_at": started_at}).model_dump())
```

**Verifier (ADJUSTED).** The literal rule match is real, the stated magnitude is not.

True as quoted: workflow.py:110 `lineage_json: str = ""` on the workflow INPUT model; workflow.py:227 `ctx.continue_as_new(spec.model_copy(update={...}).model_dump())`; `MAX_POLLS: Final = 2880` (workflow.py:89); transform.py:550 `lineage_json=lineage_doc.to_json()`; and it is re-serialized into the `{"spec": spec.model_dump()}` envelopes at :259/:267/:269. No cap exists on the path, and `_STAGE_FAIL_MESSAGE_CAP = 800` (workflow.py:463) / `_MAX_CONFIG_BYTES = 8192` (train.py:40) show the estate caps strings elsewhere. DWF-ACT-004 fires literally.

What does not hold is "the bound on a workflow-state write is whatever an arbitrary upstream producer put in a JSONB column". consume.py:158-171 builds `derived_from = [*hops, *parent_chain]` — ONE `LineageEdge` per input per hop — and the governed cascade is bronze→silver→gold (chart/values.yaml:1081-1089, three movers, one terminal). That is a handful of small edges, i.e. low single-digit KB, not an attacker-shaped payload; `inherited_chain` (consume.py:174-186) returns `[]` on anything unparseable. The finding names no measured size, no state-store limit, and no reachable writer that inflates the chain. The field is also load-bearing on the first turn — ray_submit.py:109 `"LINEAGE_JSON": lineage_json` — so it cannot simply be replaced by a pointer; the honest fix is to drop it from the spec after `submission_id` is set, or cap it.

</details>

<details><summary><b>No activity types its input or output with a Pydantic model — all ten take `dict[str, Any]`</b> <i>(act-medallion, rule DWF-ACT-009, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:278`, `services/medallion/src/medallion/workflow.py:310`, `services/medallion/src/medallion/workflow.py:327`, `services/medallion/src/medallion/workflow.py:378`, `services/medallion/src/medallion/workflow.py:699`, `services/medallion/src/medallion/workflow.py:716`, `services/medallion/src/medallion/workflow.py:923`, `services/medallion/src/medallion/workflow.py:942`, `services/medallion/src/medallion/workflow.py:1017`, `services/medallion/src/medallion/workflow.py:1071`

**Why it matters.** All ten registered activities annotate their payload as `dict[str, Any]` and return primitives or dicts rather than declared models, which misses DWF-ACT-009 on a 1.18.3 runtime. The gap is real for the OUTPUT and ENVELOPE shapes (`resolve_review_policy`'s verdict dict and `emit_promotion_outcome`'s five hand-built `outcome` literals), but every body already re-validates its input through the matching Pydantic spec, and neither cited failure is reachable in current code: `resolve_review_policy` returns `verdict` on every branch, and every `emit_promotion_outcome` call site supplies the one key read unconditionally.

**Fix.** Declare the missing models next to the existing ones — a `ReviewPolicy(verdict: Literal[...], reasons: list[str])` and a `PromotionOutcome(status: Literal[...], decided_by: str | None = None, reasons: list[str] = [])` — and type the activity signatures and returns with them, letting dapr-ext-workflow 1.18's first-class Pydantic support do the (de)serialization instead of the hand-rolled `model_validate` at the top of each body. The five inline outcome literals then become one constructor and the key-set drift becomes a type error.

**Evidence.**

```
workflow.py:923-939
def resolve_review_policy(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> dict[str, Any]:
    ...
    if not spec.reasons:
        return {"verdict": "promote", "reasons": []}

workflow.py:855-856
    policy = yield ctx.call_activity(resolve_review_policy, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
    verdict = policy.get("verdict", "block")
```

**Verifier (ADJUSTED).** The count is exact — all ten registered activities are `(ctx: WorkflowActivityContext, payload: dict[str, Any])` (workflow.py:278, 310, 327, 378, 699, 716, 923, 942, 1017, 1071) and dapr-ext-workflow is 1.18.3 (uv.lock:729-730), the version at which Pydantic payloads are first-class. So DWF-ACT-009 fires literally.

But both concrete failures are unreachable as written:
- `resolve_review_policy` (workflow.py:923-939) returns a `verdict` key on EVERY branch (`{"verdict": "promote"...}`, `{"verdict": "block"...}`, `{"verdict": "review" if ... else "block"...}`), so `policy.get("verdict", "block")` at :856 can never fall through to its default. The "every held promotion silently refused" scenario requires a future rename, not current code.
- `emit_promotion_outcome` reads exactly one key unconditionally — `record_promotion_outcome(str(outcome["status"]))` at :1085 — and every one of the five call sites (:874, :886, :893, :899, :908) supplies `"status"`. `reasons` and `decided_by` are read with `.get()` (:1101, :1103), which is the correct handling of genuinely optional fields, not an accident.

Also, the input side is not merely "defended by hand": each body does `Spec.model_validate(payload)` — the same validation a typed signature would give, with the SDK's deserialized dict typed honestly. This is a convention/clarity gap on the output and envelope shapes, with no demonstrated failure.

</details>

<details><summary><b>The lost-audit log line in `emit_promotion_outcome` names nothing — no token, no dataset, no decider</b> <i>(act-medallion, rule DWF-ACT-003, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:1122`, `services/medallion/src/medallion/core/best_effort.py:34`

**Why it matters.** `emit_promotion_outcome` is the only `best_effort` call site in the medallion that passes no `**context`, so a dropped promotion-outcome emit logs `medallion_best_effort_emit_failed_promotion_outcome` with a traceback but no token, dataset, status or decider — the operator cannot tell WHICH approvals lost their durable record during a lineage/NATS outage. Diagnosability only: the failure is named and stack-traced, no behaviour changes, and the fix is passing token/dataset/status the way all seven sibling sites do.

**Fix.** Pass the identity the sibling sites pass: `with best_effort("promotion_outcome", token=spec.token, dataset=spec.to_dataset, status=outcome["status"], decided_by=outcome.get("decided_by")):`. The values are already in scope on the line above.

**Evidence.**

```
workflow.py:1120-1123
    # emit_promotion_outcome logged NOTHING at all on this path, while its docstring calls workflow
    # history "a cache; lineage is the durable record" — a dropped publish silently emptied the record.
    with best_effort("promotion_outcome"):
        _run_async(_publish())
```

**Verifier (ADJUSTED).** The inconsistency is exactly as reported. best_effort.py:34 `def best_effort(what: str, **context: object)` → `log.exception(f"medallion_best_effort_emit_failed_{what}", extra=dict(context))`, and workflow.py:1122 `with best_effort("promotion_outcome"):` is the ONLY one of eight call sites passing no context — the others are `best_effort("stage_fail_event", token=..., submission_id=...)` (:442), `("read_stage_failure", submission_id=...)` (:491), `("train_fail_event", token=..., submission_id=...)` (:739), and four in transform.py all carrying `transition=`/`token=`.

What the finding overstates is the consequence. The swallow is sanctioned and the finding says so; DWF-ACT-003 ("except Exception: pass") does not actually fire, because the failure IS logged by name with a full traceback via `log.exception`. What is missing is only the identity fields, so this is a diagnosability defect with no behavioural component: nothing is delivered wrongly, no state changes, and an operator sees N named-and-stacked failures rather than N anonymous ones. Under the stated calibration ("anything cosmetic is info") a one-line, log-only consistency fix is info, not warning. The loss of the durable record itself is a property of the sanctioned best-effort design, not of the missing kwargs.

</details>

<details><summary><b>A retried `publish_stage_ready` double-counts the cascade's rows and bytes counters</b> <i>(act-medallion, rule DWF-ACT-002, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:327`, `services/medallion/src/medallion/workflow.py:259`, `services/medallion/src/medallion/services/transform.py:1078`, `services/medallion/src/medallion/core/metrics.py:146`

**Why it matters.** A duplicated `publish_stage_ready` (activity retry, or ordinary at-least-once redelivery of `sub_topic`) re-runs pass 2 to completion — the same-version re-publish is accepted by `publication.publish`, so `record_transition` and `record_stage_completion` fire twice and `medallion.stage.rows` / `medallion.stage.bytes` over-report by a whole stage's output. The activity is not the sole cause and an idempotency key on the publish would not fix it: nothing on this path dedupes by key, so the correction belongs at the consumer (guard the completion record on the token-derived run id) where it also covers plain redelivery.

**Fix.** Stamp a stable execution key on the re-published trigger (`trigger["wake_id"] = f"{ctx.workflow_id}-{ctx.task_id}"`) and have `handle_stage` skip the volume counters when it has already recorded that key — or, cheaper and sufficient, move `record_stage_completion`'s rows/bytes adds behind the same de-dup the lineage emit already gets from its deterministic run_id.

**Evidence.**

```
workflow.py:327
def publish_stage_ready(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:

metrics.py:146-152
    attrs = {"lance.medallion.transition": transition}
    _stage_duration.record(duration_seconds, attrs)
    if rows is not None:
        _stage_rows.add(rows, attrs)
    if size_bytes is not None:
        _stage_bytes.add(size_bytes, attrs)
```

**Verifier (ADJUSTED).** The over-count is real and I traced pass 2 end to end.

`publish_stage_ready` (workflow.py:327-374) publishes the trigger onto `settings.sub_topic` with no message id and never touches `ctx`; it is scheduled with ACTIVITY_RETRY (5 attempts) at :259. A duplicated wake-up re-enters `handle_stage` on the `ray_job_done` branch, and the tail is genuinely non-idempotent: the re-publish of the same version is accepted — publication.py:216-262 only refuses `version &lt; previous`, so `previous == version` falls through to `_set_tag(...)` and returns `published=True` — so control reaches transform.py:1078 `record_stage_completion(transition, duration_seconds=..., rows=..., size_bytes=...)` and :1071 `record_transition(transition)` a second time, and metrics.py:146-152 `_stage_rows.add(rows, attrs)` / `_stage_bytes.add(size_bytes, attrs)` are OTel counters.

What is wrong is the remedy framing. `ctx.task_id`/`workflow_id` is not "the handle sitting unused": Dapr pub/sub over NATS here does no consumer-side dedupe on any key the activity could stamp (service_kit/dapr_publish.py forwards `publish_event(**kwargs)` and guards only payload size), so adding an id to the trigger would dedupe nothing unless `handle_stage` also tracked it. The fix belongs at the consumer — an idempotent completion record keyed on the token/run id — which is also what fixes the ordinary at-least-once redelivery the finding already concedes is an independent cause. Severity info is right.

</details>

<details><summary><b>None of the ten registered activities follows the `&lt;verb&gt;_activity` naming convention</b> <i>(act-medallion, rule DWF-ACT-008, CONFIRMED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:1142`

**Why it matters.** The runtime registers activities by function name (`runtime.register_activity(a)` at :602 over the `ACTIVITIES` tuple), so the wire names are `submit_stage`, `poll_stage`, `publish_stage_ready`, `report_stage_outcome`, `poll_train`, `report_train_outcome`, `resolve_review_policy`, `request_approval`, `publish_promotion`, `emit_promotion_outcome` — zero of ten carry the `_activity` suffix the checklist expects. The practical cost here is small and worth stating honestly: these names already appear in daprd's `activity||&lt;name&gt;` spans (workflow.py:425-427 relies on that), and nothing cross-language calls them today. The cost is that a reader of a trace or of `ACTIVITIES` cannot tell an activity from an ordinary helper in the same module — `_publish_fail_event`, `_resume_publish` and `_read_stage_failure` sit beside them and are NOT registered, and the only thing distinguishing the two groups is membership in a tuple 700 lines away.

**Fix.** Either rename to the convention (`submit_stage_activity`, …) — noting that this changes the registered wire name, so in-flight instances must drain first — or, if the estate deliberately keeps these names, record that decision in `register`'s docstring so the next reviewer does not re-raise it. Given the trace-attribute coupling, the second is defensible; the silence is not.

**Evidence.**

```
workflow.py:1142-1153
ACTIVITIES = (
    submit_stage,
    poll_stage,
    publish_stage_ready,
    report_stage_outcome,
    poll_train,
    report_train_outcome,
    resolve_review_policy,
    request_approval,
    publish_promotion,
    emit_promotion_outcome,
)
```

**Verifier (CONFIRMED).** Factually exact. `ACTIVITIES` at workflow.py:1144-1153 lists `submit_stage, poll_stage, publish_stage_ready, report_stage_outcome, poll_train, report_train_outcome, resolve_review_policy, request_approval, publish_promotion, emit_promotion_outcome` — zero carry the suffix — and registration is by function name, so those are the wire names. The rule catalog rates DWF-ACT-008 info ("Activity decorator name does not match the convention `&lt;thing&gt;_activity`"), matching the reported severity, and the finding correctly discounts the cross-language cost since nothing outside this module calls them. The stated cost (a trace or `ACTIVITIES` reader cannot separate activities from same-module helpers like `_publish_fail_event`, `_resume_publish`, `_read_stage_failure`) holds — though those three are `_`-prefixed and the registered ten are not, which is a weaker but real distinguishing convention already in place. Purely cosmetic; nothing to change unless the estate adopts the suffix.

</details>

<details><summary><b>The publish saga is spawned as an unreferenced asyncio task; a collected task leaks `_RUNNING` and strands the project in `publishing`</b> <i>(actors-annotator, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/annotator/src/annotator/projects/lakehouse.py:294`, `services/annotator/src/annotator/projects/lakehouse.py:299`, `services/annotator/src/annotator/projects/lakehouse.py:321`, `services/annotator/src/annotator/projects/lakehouse.py:324`, `services/annotator/src/annotator/projects/project_actor.py:451`

**Why it matters.** `spawn_publish` discards the task handle (`project_actor.py:451 lakehouse.spawn_publish(project.project_id)` against `lakehouse.py:324 return asyncio.get_running_loop().create_task(_drive())`), which is a deviation from asyncio's keep-a-reference guidance — but the stated failure (finally never runs, `_RUNNING` leaks forever, project stranded in `publishing`) does not follow and is not reachable as described.

**Fix.** Hold a strong reference for the task's lifetime — keep a module-level `_TASKS: set[asyncio.Task]`, `task.add_done_callback(_TASKS.discard)`, and add the task to it in `spawn_publish` — or, better, key `_RUNNING` on the task object itself (`_RUNNING: dict[str, asyncio.Task]`) and have the guard stand down only when `not task.done()`, so a destroyed or finished task cannot pin the id. Also raise the stand-down log from `debug` to `info` so a permanently-standing-down watchdog is visible.

**Evidence.**

```
lakehouse.py:297-302, 321-324 —
def spawn_publish(project_id: str) -> asyncio.Task[None] | None:
    """Schedule the saga for one project, unless it is already running here."""
    if project_id in _RUNNING:
        logger.debug("publish for %s already running — the tick stands down", project_id)
        return None
    _RUNNING.add(project_id)
...
        finally:
            _RUNNING.discard(project_id)

    return asyncio.get_running_loop().create_task(_drive())

project_actor.py:451 (the return value is dropped) —
        lakehouse.spawn_publish(project.project_id)
```

**Verifier (ADJUSTED).** The code is as quoted (`lakehouse.py:294-324`, `project_actor.py:451`), but the failure chain breaks at two points. (1) A pending task is not collectable while it is awaiting real work: every await in `_drive` -&gt; `run_publish_for` -&gt; `run_publish` is anchored — `await asyncio.to_thread(publish_token, settings)` (lakehouse.py:375) is held by the executor's callback chain, and every `project_handle.get()/list_tasks()/fire()/record_publish()` and `publisher.create_table()` await goes through the Dapr proxy's HTTP transport, whose futures hold `Task.__step` in their done-callbacks; between `create_task` and the first step the loop's ready-queue handle holds it. The finding offers no sequence in which the only reference is the task itself. (2) Even granting collection, the `finally` DOES run: `Task.__del__` only logs "Task was destroyed but it is pending!"; the suspended coroutine is then deallocated, which calls `coro.close()`, throwing GeneratorExit at the await point and executing the non-awaiting `finally: _RUNNING.discard(project_id)` (lakehouse.py:321-322). So `_RUNNING` is cleared and the 60 s watchdog re-drives, which the saga is explicitly built for ("Safe to call again after any crash: every step converges on the same `pending_publish_id`", saga.py:158). The residual is hygiene, not a stuck instance — info, not warning.

</details>

<details><summary><b>The "publishing but carries no publish token" refusal is raised outside the try, so no `publish_failed` is ever recorded</b> <i>(actors-annotator, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/annotator/src/annotator/projects/saga.py:190`, `services/annotator/src/annotator/projects/saga.py:196`, `services/annotator/src/annotator/projects/saga.py:198`, `services/annotator/src/annotator/projects/saga.py:250`

**Why it matters.** The placement observation is literally true — saga.py:190-198 raises above the try whose handler fires `publish_failed` — but the state it guards is unreachable at HEAD, and moving the raise inside the try would not create an operator escape either, so this is a defensive-code note rather than a defect.

**Fix.** Move the token/instant check inside the try (or wrap it in the same `_converge({"event": "publish_failed", ...})` the handler uses), so a project that cannot be published stops at `publish_failed` with the reason on `publish_error` instead of ticking in `publishing` with no exit. The check itself is right — do not invent a token — only its placement is wrong.

**Evidence.**

```
saga.py:190-198 —
    publish_id = project.pending_publish_id
    published_at = project.pending_publish_at
    if not publish_id or published_at is None:
        # The actor mints this at the `publish` transition. Its absence means the state machine and
        # this saga disagree about what happened, and guessing a token would defeat the whole
        # idempotency argument — so this stops rather than inventing one.
        raise PublishRefusal(f"project {project.project_id} is publishing but carries no publish token or instant — refusing to mint one here")

    try:
```

**Verifier (ADJUSTED).** Confirmed placement: `publish_id = project.pending_publish_id` / `published_at = project.pending_publish_at` / `if not publish_id or published_at is None: raise PublishRefusal(...)` all precede the `try:` at saga.py:198, and the except at saga.py:250 is the only thing that fires `publish_failed`. But the two fields have exactly one writer in the whole service — I grepped `pending_publish_at|pending_publish_id` across `services/annotator/src`: only `project_actor.py:255-257`
```
            if project.pending_publish_id is None:
                project.pending_publish_id = new_id()
                project.pending_publish_at = datetime.now(UTC)
```
both minted in one branch of one actor turn and persisted by a single `_store`; nothing anywhere clears either. So id-without-instant cannot be produced by any code path — the finding concedes this and rests on a July-2026 state row written in a six-minute window between two commits. Further, the proposed correction is inert: because `pending_publish_id is not None`, a post-`publish_failed` retry re-enters PUBLISHING with the same half-token and re-raises, so the project would oscillate rather than recover. Note also that the two sibling raises above the try (saga.py:162 "project does not exist", saga.py:189 "is X, not publishing") are correctly unrecorded, which is the family this one belongs to.

</details>

<details><summary><b>The typed proxy has no guard that every `@actormethod` carries a non-None wire name</b> <i>(actors-annotator, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/annotator/src/annotator/projects/proxies.py:36`, `tests/unit/test_actor_proxy_names.py:71`

**Why it matters.** `actormethod(name=None)` in the Dapr SDK sets `funcobj.__actormethod__ = name`, i.e. literally `None` (dapr/actor/actor_interface.py). `TypedActorProxy.__getattr__` reads `getattr(declared, "__actormethod__", name)` — the attribute EXISTS with value `None`, so the default never applies and `wire` becomes `None`, then `getattr(self._proxy, None)` raises `TypeError: attribute name must be string, not 'NoneType'`. Concrete sequence: someone adds `@actormethod()` (no name) for a new method on `AnnotationProjectActorInterface`, unit tests pass because the fakes implement the Python name, and the first in-cluster call raises TypeError from inside `_report_state`, where it is swallowed by design (actor.py:327) — a silently stale index, which is precisely the class of failure the proxies module was written to end. The annotator's guard test pins eight named methods and sweeps for raw `ActorProxy.create`, but never asserts the invariant itself; the notifications service, which shares this proxy design, does (services/notifications/tests/test_actor_proxies.py:70-74, "A bare `@actormethod()` stores `None`, and the proxy then computes a null wire name — a routing id that cannot exist").

**Fix.** Port the notifications assertion into tests/unit/test_actor_proxy_names.py, over all three interfaces: for each member with `__actormethod__`, assert `isinstance(v.__actormethod__, str) and v.__actormethod__`. Optionally also make `TypedActorProxy.__getattr__` fail loudly — `wire = getattr(declared, "__actormethod__", None) or name` still resolves the sane case while turning the null into a named error rather than a TypeError from the SDK.

**Evidence.**

```
proxies.py:32-37 —
    def __getattr__(self, name: str) -> Any:
        declared = getattr(self._interface, name, None)
        if declared is None:
            raise AttributeError(f"{self._interface.__name__} declares no method {name!r}")
        wire = getattr(declared, "__actormethod__", name)
        return _translating(getattr(self._proxy, wire))

dapr/actor/actor_interface.py —
    def wrapper(funcobj):
        funcobj.__actormethod__ = name
```

**Verifier (CONFIRMED).** The SDK behaviour is as claimed — `.venv/lib/python3.13/site-packages/dapr/actor/actor_interface.py`:
```
def actormethod(name: Optional[str] = None):
    def wrapper(funcobj):
        funcobj.__actormethod__ = name
```
so a bare `@actormethod()` stores literal `None`. `proxies.py:32-37` then reads
```
        wire = getattr(declared, "__actormethod__", name)
        return _translating(getattr(self._proxy, wire))
```
— the attribute exists, so the `name` default never applies and `getattr(self._proxy, None)` raises `TypeError: attribute name must be string`. The blast radius is as stated: such a call from `_report_state` is swallowed by the deliberate `except Exception` at actor.py:327-333, leaving a silently stale index — the exact failure class proxies.py's own docstring says the module exists to end. The coverage gap is real: `tests/unit/test_actor_proxy_names.py` pins eight (interface, python, wire) triples and sweeps for raw `ActorProxy.create`, but has no equivalent of `services/notifications/tests/test_actor_proxies.py:69-74 test_every_interface_method_declares_an_explicit_wire_name` (`assert all(isinstance(v.__actormethod__, str) and v.__actormethod__ for v in declared)`). I verified every current annotator declaration names its wire id (tenant_actor 2, actor.py 5, project_actor 10), so there is no live defect today — info is the correct severity for a missing invariant test.

</details>

<details><summary><b>`watchers_of` absorbs an actor-plane fault into an empty audience, so a WATCH delivery is lost permanently behind a SUCCESS ack</b> <i>(actors-notifications, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/notifications/src/notifications/proxies.py:159`, `services/notifications/src/notifications/api/fanout.py:93`, `services/notifications/src/notifications/api/ingest.py:116`

**Why it matters.** `watchers_of` (proxies.py:150-161) does swallow every fault and return `[]`, and `ingest_run_event` (api/ingest.py:116-121) then acks `DAPR_SUCCESS` because `result.needs_retry` is False — that part is exact. But the load-bearing claim, "there is no second chance, because the reconciler lane will also skip it once the bus lane advanced nothing that it re-walks", is false: the reconciler is an INDEPENDENT lane with its own cursor over lineage's durable feed and processes every row regardless of what the bus did. `reconcile()` (api/reconciler.py:314-395) walks from the stored mark (`after = stored.resume_from`), filters `fresh = [record for record in page.events if record.seq &gt; walk_floor]` with `walk_floor = max(stored.seq - FEED_OVERLAP, settled_floor)`, and forwards the SAME `watchers=watchers_of` (api/reconcile_cron.py:110-116). Delivery is idempotent on the notification's natural key, so the re-offer costs a counted duplicate for the author and delivers the watchers. For exactly the fault class the finding names — sidecar restart, actor rebalance, state-store failover, all healing in seconds — the next cron tick (30s) re-resolves the watchers and the notification lands. Residual, real but much narrower: because a pass where `watchers_of` returned `[]` is a CLEAN pass, the mark advances, so only a watch-index outage that spans both the bus delivery and every reconciler pass covering that seq (beyond the 64-seq `FEED_OVERLAP` re-offer window) loses the watch delivery — and even then it is on an ERROR line (`watch_index_unreadable`). The behaviour is a recorded decision (proxies.py:151-156) whose reasoning survives this correction.

**Fix.** Distinguish 'this project has no watchers' from 'the index could not be read'. Let `watchers_of` re-raise (or return a sentinel) on a transport/actor fault so `ingest_run_event` answers RETRY, exactly as an inbox-actor fault already does — the fan-out is idempotent, so the author's already-landed pointer costs a counted duplicate on the retry and nothing more. Keep the swallow only for the genuinely permanent cases it also covers today (`ValueError` from `watch_index_for` on an unusable project id), and update the docstring, which currently asserts the redelivery claim the change would falsify.

**Evidence.**

```
proxies.py:159-164
    try:
        result = await watch_index_for(project_id).list_watchers()
    except Exception:
        logger.exception("watch_index_unreadable", extra={"project_id": project_id})
        return []
    return [str(s) for s in (result.get("subjects") or [])]
```

**Verifier (ADJUSTED).** proxies.py:158-163 `try: result = await watch_index_for(project_id).list_watchers()  except Exception: logger.exception("watch_index_unreadable", ...); return []`.

api/ingest.py:116-121 `audience = await audience_for(notice, watchers=watchers); result = await fan_out(...); if result.needs_retry: return DAPR_RETRY; return DAPR_SUCCESS` — confirms the SUCCESS ack.

Refuting the permanence: api/reconciler.py:1-12 ("**The bus is provably incomplete** ... The one place both paths converge is this feed") and 327-331 ("**The cursor advances only when the whole pass succeeded.** ... the next tick re-offers everything above it — including the rows that already landed, which is free: delivery is idempotent on the notification's natural key"), plus reconciler.py:370-395 which re-runs `ingest_run_event` with `watchers` forwarded — the comment at 388-394 records that dropping `watchers`/`push` on THIS lane was itself the bug that was fixed. api/reconcile_cron.py:110-116 passes `watchers=watchers_of` on every tick. So the bus's degraded audience is re-attempted by the second lane, contradicting "there is no second chance".

</details>

<details><summary><b>`project#member` is NOT re-checked at delivery, though `watch_actor.py` and `watches.py` both state it as the safeguard that makes a stale watch harmless</b> <i>(actors-notifications, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/notifications/src/notifications/watch_actor.py:13`, `services/notifications/src/notifications/api/watches.py:9`, `services/notifications/src/notifications/api/fanout.py:93`

**Why it matters.** The literal claim is correct — no `member` check runs at delivery — but the consequence is much narrower than stated, because the FGA model routes project membership into the gate that DOES run. `WATCH_RELATION = "member"` (watches.py:42) is used only by `_require_member` at the create door; `audience_for` (fanout.py:93-96) adds every subject the index returns unconditionally, and the only re-derivation is `visibility.sees_all(subject, notice.outputs)` asking `can_be_notified` (fanout.py:163-165, visibility.py:137-150). But `model.fga:98` defines `warehouse#reader: [...] or writer or member from project`, and `namespace#reader`/`table#reader` inherit `reader from parent` (model.fga:259, 331) with `table#can_be_notified: reader` (model.fga:347) — so for a subject whose only path to the run's outputs was project membership, revoking membership DOES drop them from the audience (Outcome.HIDDEN). The residue is the case the finding constructs: a subject holding an INDEPENDENT `[user, role#assignee]` reader grant on the output table/namespace/warehouse keeps receiving `reason: watch` rows for a project they were offboarded from, indefinitely, since only they can call `DELETE /notifications/watches/{id}` (watches.py:115-127, `CurrentSubject`). Nothing is disclosed beyond what that grant already lets them read, so this is an audience-hygiene and documentation defect — three docstrings (watch_actor.py:13-14, watches.py:8-11 and :83-85, :102-104) assert a membership re-check that does not exist and should say `can_be_notified` on the run's outputs — not an open door.

**Fix.** Either implement the re-check or delete the claim, and do it in one change. Implementing it is the direction the docstrings already commit to and it is cheap: after `watchers(notice.project)` returns, run one `fga.batch_check(user=subject, relation="member", objects=[f"project:{notice.project}"])` per candidate — or a single `list_users` on `project:&lt;id&gt;#member` intersected with the index — and drop watchers who no longer hold it, keeping the existing fail-closed rule (an FGA outage is RETRY, never a delivery). If instead the intended contract is 'the visibility gate is the only re-check', correct watch_actor.py:13 and watches.py:9 to say that, and say what happens to a watch left behind by an offboarded member.

**Evidence.**

```
watch_actor.py:12-14
`project#member` — is checked at watch-create, against the project, which is the object that exists
(create-on-parent doctrine). It is re-checked at DELIVERY, because membership can be revoked between
the watch and the run.

fanout.py:93-96
    if watchers is not None and notice.project:
        for subject in await watchers(notice.project):
            if subject not in audience:
                audience.append(subject)
```

**Verifier (ADJUSTED).** watches.py:41-42 `#: The relation a watch requires, on `project:&lt;id&gt;`. ... WATCH_RELATION = "member"` — its only reader is `_require_member` (watches.py:67-76), called from `watch_project` (watches.py:100) alone; grep for `WATCH_RELATION` finds no other site.

fanout.py:93-96 `if watchers is not None and notice.project: for subject in await watchers(notice.project): if subject not in audience: audience.append(subject)` — no permission question.

fanout.py:163-165 `if not await visibility.sees_all(subject, notice.outputs): return Outcome.HIDDEN` and visibility.py:145-150 "Asks `can_be_notified`, NOT the render's question" — the only re-derivation, and it is over the run's OUTPUT objects, not `project:&lt;id&gt;`.

Why the practical effect is narrow: packages/service-kit/src/service_kit/governed/auth/model.fga:98 `define reader: [...] or writer or member from project` (warehouse), :259 `or reader from parent` (namespace), :331 same (table), :347 `define can_be_notified: reader`. Membership therefore feeds the gate that does run; only an independent grant survives an offboarding.

</details>

<details><summary><b>The retried activity's outbound Serve POST carries no idempotency key, and `ctx` — which holds the workflow_id/task_id pair that would be one — is accepted and never read</b> <i>(det-act-flows, rule DWF-ACT-002, ADJUSTED)</i></summary>

**Sites:** `services/flows/src/flows/activities.py:28`, `services/flows/src/flows/executor.py:379`, `services/flows/src/flows/workflow.py:38`

**Why it matters.** `ctx` is an unused parameter (activities.py:28) that already carries a free idempotency key (`workflow_id`/`task_id`, dapr/ext/workflow/workflow_activity_context.py:34-41), and the Serve POST (executor.py:379-386) sends no dedupe header. But there is NO defect at HEAD: every node-level fault is returned rather than raised (activities.py:74-79, executor.py:290), so NODE_RETRY cannot fire on any Serve error — only on worker death or lost result delivery — and the one arm that calls out is stateless inference (`dataset` and `mcp` refuse at executor.py:211/219). This is a hardening note for the first write-shaped kind added through the catalog seam (catalog.py:3-6), not an at-least-once exposure that exists today.

**Fix.** Thread `f"{ctx.workflow_id}:{ctx.task_id}"` from activities.run_node through `NodeJob` (or as an explicit argument to `executor.run_node`/`_call_serve`) and send it as an `Idempotency-Key` header on the Serve POST at executor.py:379. The inline lane can pass the derived `run_id` plus the node id for the same effect.

**Evidence.**

```
activities.py:28
def run_node(ctx: WorkflowActivityContext, activity_input: dict[str, object]) -> dict[str, object]:

executor.py:379-387
        resp = await client.post(
            url,
            content=payload.as_bytes(),
            params=params,
            headers={"content-type": content_type},
            timeout=serve_timeout,
        )
```

**Verifier (ADJUSTED).** The two mechanical claims check out, but the retry path is narrower than stated and there is no non-idempotent side effect at HEAD, so this is a hardening note rather than a defect.

Verified: `ctx` is genuinely unused. activities.py:28 is `def run_node(ctx: WorkflowActivityContext, activity_input: dict[str, object]) -&gt; dict[str, object]:` and no line in the body references `ctx` (the span work uses `trace.get_current_span()`, not `ctx`). The proposed key does exist — .venv/lib/python3.13/site-packages/dapr/ext/workflow/workflow_activity_context.py:34-41 exposes `workflow_id` (`self.__obj.orchestration_id`) and `task_id` (`self.__obj.task_id`). No idempotency header is sent: executor.py:379-386 posts with `headers={"content-type": content_type}` only.

What narrows it — the activity almost never raises. activities.py deliberately RETURNS a failed NodeResult ("Logged, not raised. Raising would make Dapr retry the activity…"), and every node-level fault is a `NodeError` caught at executor.py:290, including `httpx.RequestError` (:394), 405 (:399) and any `&gt;= 400` (:401). So NODE_RETRY cannot fire on a Serve error at all. The only paths that reach a retry are a worker death mid-call, a lost result delivery, or the `re.error` escape from finding 1 — and that last one is a `regex` node, which issues no Serve POST, so it cannot duplicate one.

What removes the harm — no arm is write-shaped. `dataset` refuses at executor.py:211 (`raise NodeError(f"dataset source is a scaffold — reading rows{named} needs the Arrow lane")`), `mcp` refuses at :219, and `model` (:381) is stateless inference; the `?name=` param is stamped into the ALTO the call RETURNS, not into any store. Re-issuing it is safe. Under the estate's own test — a duplicate-side-effect claim needs a side effect that is actually not idempotent — that condition is not met, so DWF-ACT-002's `critical` grading does not apply here and `info` is correct.

The forward-looking argument is fair (catalog.py:3-6 does call the catalog "the SEAM" for server-side kinds), but it should be stated as such rather than as a live at-least-once-without-dedupe exposure.

</details>

<details><summary><b>`terminate_chunks` is dispatched un-guarded on the only path to the terminal record, so a tidy-up failure defeats the error boundary it was added to protect</b> <i>(det-ingest, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/ingest/src/ingest/workflow.py:656`, `services/ingest/src/ingest/workflow.py:723`, `services/ingest/src/ingest/workflow.py:1212`, `services/ingest/src/ingest/workflow.py:1228`

**Why it matters.** The structural gap is real but the stated trigger is unreachable, so this is a hardening note rather than a live defect. workflow.py:1228 `client = wf_client.DaprWorkflowClient()` does sit outside the per-child `try` that opens on the next line, and neither call site (:656 inside the deadline try, :723 in the error boundary) guards the dispatch — so if that constructor ever raised, ACTIVITY_RETRY would burn four attempts on a deterministic failure and the exception would propagate out of the `except` block with no handler above it, skipping `emit_terminal`. But the only way the constructor raises is the malformed-address branch, and `WorkflowRuntime.__init__` validates the identical address through the identical three lines at startup — so a process whose workflows are executing has already proved the address parses. Worth moving the constructor inside the guard (or wrapping both dispatch sites) so the docstring's 'best-effort by construction' is literally true, but no input sequence reaches the described loss today.

**Fix.** Move `DaprWorkflowClient()` construction inside a try in `terminate_chunks` and return `{"terminated": 0, "requested": N, "error": ...}` rather than raising, so the activity is best-effort in fact as well as in its docstring. Belt-and-braces at the workflow level: dispatch it with a retry policy of one attempt, and in the `except` handler order it AFTER `emit_terminal` is guaranteed — or set `terminal_emitted` semantics so a terminate failure can never preempt the terminal emit.

**Evidence.**

```
workflow.py:723  `yield ctx.call_activity(terminate_chunks, input={"child_ids": child_ids}, retry_policy=ACTIVITY_RETRY)`
workflow.py:724  `yield ctx.call_activity(emit_terminal, input={"spec": spec.model_dump(), "outcome": failed}, retry_policy=ACTIVITY_RETRY)`
workflow.py:1228 `    client = wf_client.DaprWorkflowClient()`   (outside the per-child `try` that begins on the next line)
```

**Verifier (ADJUSTED).** workflow.py:1226-1236 at HEAD: `import dapr.ext.workflow as wf_client` ... `client = wf_client.DaprWorkflowClient()` then `for child_id in child_ids:` / `try:` / `client.terminate_workflow(child_id)` / `except Exception: log.debug(...)`. So the constructor is indeed the one unguarded line, and the docstring at :1212-1214 does claim "Best-effort by construction ... a tidy-up that fails must not turn a run that recorded its outcome into one that died."

The trigger fails verification. dapr/ext/workflow/dapr_workflow_client.py:56-62: `address = getAddress(host, port)` / `try: uri = GrpcEndpoint(address)` / `except ValueError as error: raise DaprInternalError(f'{error}') from error`. dapr/ext/workflow/workflow_runtime.py:128-133 is the same three lines — `address = getAddress(host, port)`, `GrpcEndpoint(address)`, `raise DaprInternalError` — and both resolve through the same `util.getAddress` (DAPR_GRPC_ENDPOINT, or DAPR_RUNTIME_HOST:DAPR_GRPC_PORT), read from process env at construction. A malformed address therefore kills `WorkflowRuntime` at worker startup and no workflow body ever executes, so `terminate_chunks` cannot be reached in the state the finding requires. Nothing else in that constructor is a plausible raiser: `TaskHubGrpcClient` builds a grpc channel, which does not connect eagerly, so an unreachable or down sidecar yields a working client object and the failure lands inside the per-child `try` that is already caught.

</details>

<details><summary><b>The bespoke DWF-DET-009 gate does not follow helpers, contradicting the checklist's own Cross-reference rule and the header's "nothing in workflow scope reads env"</b> <i>(det-ingest, rule DWF-DET-009, CONFIRMED)</i></summary>

**Sites:** `services/ingest/src/ingest/replay_guard.py:48`, `services/ingest/src/ingest/workflow.py:219`

**Why it matters.** Stated as a GATE defect, not a live divergence — I checked and today's workflow scope is clean. `env_reads_in_workflow_bodies` walks the AST and keeps only `FunctionDef` nodes whose `node.name in workflow_names`, where `workflow_names` comes from the `WORKFLOWS` tuple, i.e. exactly `ingest_run` and `chunk_run`. The determinism checklist's Cross-reference says the scope of every rule is "the body of any function decorated with @wfr.workflow ... (or any helper called from such a function)". `bound_errors` (:219) is called from BOTH generator bodies (:670 and :790) and is not covered; neither would any future helper be. An `os.getenv` added to `bound_errors` — plausible, since it caps a payload and `MAX_REPORTED_ERRORS` is exactly the kind of number an operator asks to tune — would be an env read on the branch that builds `finalize`'s input, and this module's own gate would answer clean. That is the same class of hole the gate's docstring criticises in `test_replay_hygiene.py` ("the estate had a gate that would have let the very defect it was written about come straight back").

**Fix.** Resolve the call graph: from each named workflow body, collect `ast.Call` targets that resolve to module-level `FunctionDef`s in the same source, transitively, and scan those too. Exclude names in the `ACTIVITIES` tuple, since an activity reading env is the sanctioned asymmetry the module header depends on.

**Evidence.**

```
replay_guard.py:48  `node.name in workflow_names and _reads_env(node)`
workflow.py:219  `def bound_errors(errors: Mapping[str, str], total: int | None = None) -> tuple[dict[str, str], int]:`   — called at workflow.py:670 and workflow.py:790, invisible to the gate
```

**Verifier (CONFIRMED).** replay_guard.py:47-50 filters exactly as described: `[node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in workflow_names and _reads_env(node)]`, and test_workflow_bodies_read_no_env.py:36-38 supplies `workflow_names` as `{fn.__name__ for fn in wf_module.WORKFLOWS}` — with `WORKFLOWS = (ingest_run, chunk_run)` at workflow.py:1177. Coverage is those two names and nothing else; a helper is invisible whether or not a workflow calls it.

The checklist claim verifies. shared/review-determinism-python.md:3: "The 'scope' of every rule is the body of any function decorated with `@wfr.workflow(...)` (or any helper called from such a function)." Its Cross-reference section (:33) repeats it: "If a rule trips inside a helper function, confirm the helper is reachable from a `@wfr.workflow`-decorated function before reporting it." DWF-DET-009 is the `os.environ` / `os.getenv(` / `Settings()` row (:17).

The named helper is genuinely in workflow scope: `def bound_errors(...)` at workflow.py:221, called at :674 (`errors, errors_total = bound_errors(merged, sum(r.errors_total for r in parsed))`, inside `ingest_run`) and :790 (`listed, total = bound_errors(errors, errors_total)`, inside `chunk_run`) — and :674's result is what builds `finalize`'s input. I confirmed the finding's own caveat: today's workflow scope is clean, the module's single `os.getenv` cluster is in `RunLimits.from_env`, which `resolve_limits` calls from activity scope. Info is correct — a gate-coverage hole, not a live divergence, and the same class the guard's own docstring criticises ("the estate had a gate that would have let the very defect it was written about come straight back").

</details>

<details><summary><b>`train_run`'s abandoned exit yields no activity at all — a lost or unknown train watch is recorded nowhere</b> <i>(det-medallion, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:681-687`, `services/medallion/src/medallion/workflow.py:694-695`

**Why it matters.** The code reads as described — `workflow.py:681-687` builds a `TrainJobOutcome(verdict='abandoned')`, logs `log.warning('medallion_train_watch_abandoned', ...)` and `return`s with no `yield ctx.call_activity(...)`, unlike `stage_run:239` — but 'recorded nowhere' and the warning severity do not hold. Emitting nothing on this branch is a RECORDED, TEST-PINNED decision, not an oversight: the comment at :682-683 says 'The ceiling, or a lost watch. NOT a failure: a training job still running at the ceiling is alive and may yet land, and reporting it as dead sends somebody hunting a healthy run', and two tests pin it — `test_the_watch_is_BOUNDED_and_says_so_rather_than_reporting_a_failure` (:210) and `test_a_LOST_watch_is_not_reported_as_a_dead_job` (:222, which drives the exact `raise_on='poll_train'` path the finding calls unrecorded and asserts only `verdict == 'abandoned'`). The watcher's stated purpose — a job that dies before emitting anything — is served by the `failed` branch (:694-695), which Ray answers with FAILED for a bad image, an OOM or an `exit 2`; the abandoned branch is by construction the case where Ray has NOT said the job is dead. What genuinely remains is narrower and cosmetic: `record_train_outcome` is called only inside `report_train_outcome` (:737), so an abandoned train watch increments no counter at all, whereas the stage lane records `abandoned` (:423). So a train watch lost to a 404 or an unreachable dashboard leaves a log line and nothing in metrics — an observability asymmetry between the two lanes, worth closing with a metric (not a lineage FAIL), at info severity.

**Fix.** Report the abandoned/lost train watch the way the stage lane does. Give `report_train_outcome` a non-`failed` branch (its `reason` string at :736 already reads correctly for it once the verdict is threaded, mirroring `report_stage_outcome`'s two-armed `reason` at :398-402), and `yield ctx.call_activity(report_train_outcome, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)` before returning at :687 so `record_train_outcome("abandoned")` fires and the person named in `spec.originator` hears that their run stopped being watched. Keep `succeeded` silent — that part of the docstring's contract is correct and deliberate (a second COMPLETE would fork the run).

**Evidence.**

```
workflow.py:681-696 —
    if not _is_terminal(status):
        # The ceiling, or a lost watch. NOT a failure: ...
        outcome = TrainJobOutcome(submission_id=spec.submission_id, status=status, polls=polls, verdict="abandoned")
        if not ctx.is_replaying:
            log.warning("medallion_train_watch_abandoned", extra={...})
        return outcome.model_dump()

    verdict = "succeeded" if status == _TERMINAL_OK else "failed"
    ...
    if verdict == "failed":
        yield ctx.call_activity(report_train_outcome, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)

(contrast stage_run:239, which DOES report the abandoned branch:)
        yield ctx.call_activity(report_stage_outcome, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)
```

**Verifier (ADJUSTED).** workflow.py:681-687 — `outcome = TrainJobOutcome(..., verdict="abandoned")` / `log.warning("medallion_train_watch_abandoned", ...)` / `return outcome.model_dump()` (no call_activity). Recorded decision at :682-683: `# The ceiling, or a lost watch. NOT a failure: a training job still running at the ceiling is alive and may yet land, and reporting it as dead sends somebody hunting a healthy run.` Pinned by services/medallion/tests/test_train_workflow.py:222 `test_a_LOST_watch_is_not_reported_as_a_dead_job` (ctx.raise_on = 'poll_train'; asserts out['verdict'] == 'abandoned'). Residual gap: grep shows `record_train_outcome` called only at workflow.py:737 inside `report_train_outcome`, versus `record_stage_outcome` at :423 which the stage abandoned branch does reach.

</details>

<details><summary><b>A permanently failed submit reports with an empty submission id, sending the failure reporter at Ray's job LIST endpoint</b> <i>(det-medallion, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:197`, `services/medallion/src/medallion/workflow.py:412-417`, `services/medallion/src/medallion/workflow.py:478-492`

**Why it matters.** When `submit_stage` exhausts `ACTIVITY_RETRY`, the workflow body builds the outcome with `submission_id=""` and `verdict="failed"`, then hands it to `report_stage_outcome`. That activity's enrichment guard fires on `verdict == "failed"`, so `_read_stage_failure("")` runs and `job_failure` issues `client.get(f"/api/jobs/{sub_id}")` with an empty id — i.e. `GET /api/jobs/`, Ray's job *list* endpoint, which answers 200 with a JSON array. `RayJobFailure.model_validate(&lt;list&gt;)` then raises a `ValidationError` that is neither an `httpx.HTTPError` nor a 4xx, so it escapes `job_failure` and is swallowed by `best_effort("read_stage_failure", ...)`. Net effect on every permanently-failed submit: one wasted request against the wrong endpoint, a spurious `read_stage_failure` warning that reads like a Ray outage, and a FAIL event whose text is "the Ray stage job  ended UNKNOWN after 0 poll(s)" — an empty id where an operator expects one, with nothing saying the submission itself never landed. Not a replay hazard; a diagnosability one.

**Fix.** Guard the enrichment on a non-empty id — `outcome.verdict == "failed" and outcome.submission_id and (cause := ...)` at :412-414 — and make the submit-failure branch say what it is, e.g. by giving `StageJobOutcome` a `verdict="unsubmitted"` (or by shaping `reason` at :398-402 to read "the stage job was never submitted" when `submission_id` is empty), so the FAIL event distinguishes "Ray refused the submission" from "Ray ran it and it died".

**Evidence.**

```
workflow.py:197 —
            failed = StageJobOutcome(submission_id="", status=None, polls=0, verdict="failed")

workflow.py:412-414 —
    if (
        outcome.verdict == "failed"
        and (cause := _read_stage_failure(outcome.submission_id)) is not None
```

**Verifier (CONFIRMED).** workflow.py:197 `failed = StageJobOutcome(submission_id="", status=None, polls=0, verdict="failed")`, handed straight to `report_stage_outcome` at :198. That activity's enrichment guard at :412-414 fires on the verdict: `if (outcome.verdict == "failed" and (cause := _read_stage_failure(outcome.submission_id)) is not None ...)`, so `_read_stage_failure("")` runs and `job_failure` issues `await client.get(f"/api/jobs/{sub_id}")` (ray_kit/submit.py) — i.e. `GET /api/jobs/`, the job LIST endpoint. I confirmed the validation half directly: `RayJobFailure.model_validate([{'status':'FAILED'}])` raises `ValidationError: Input should be a valid dictionary or instance of RayJobFailure`, which is neither an `httpx.HTTPError` nor a &gt;=400 status, so it escapes `job_failure` and is swallowed by `best_effort("read_stage_failure", ...)` (core/best_effort.py logs `medallion_best_effort_emit_failed_read_stage_failure` and control flow continues to `return None`). Net effect is exactly as claimed: one request at the wrong endpoint, a log.exception that reads like a Ray outage, and a FAIL event whose reason string is `the Ray stage job  ended UNKNOWN after 0 poll(s)` with an empty id. Diagnosability only — no replay hazard, no duplicated side effect. Info is the right severity.

</details>

<details><summary><b>`_watch_seconds` re-imports `datetime` inside the one workflow-scope helper whose sibling docstring forbids import-time behaviour in the body</b> <i>(det-medallion, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/workflow.py:164`, `services/medallion/src/medallion/workflow.py:51`, `services/medallion/src/medallion/workflow.py:576-583`

**Why it matters.** The redundancy is real: `from datetime import UTC, datetime, timedelta` at workflow.py:51 already binds `datetime` at module scope, and `_watch_seconds` re-binds the identical name locally at :164, which changes nothing. It is dead code and should go. But the stated justification is wrong on two counts. First, `_is_terminal`'s docstring (:577-582) is about not importing `ray_kit` INTO the workflow module — 'keeping this a pure comparison over two literals means the body has no import-time behaviour to be non-deterministic about' — it says nothing against a function-local import, and a function-local `from datetime import datetime` has no import-time behaviour at all (the module is already imported at :51). Second, deferred function-scope imports are this file's deliberate house style: every activity does it (`poll_stage` imports httpx and ray_kit inside the body, `submit_stage` imports get_settings/submit_stage_job inside the body), so a reader finding one here has no reason to think it is load-bearing determinism machinery. Correct framing: a no-op duplicate import to delete on the next touch of the function — tidiness, not a rule violation and not a replay hazard.

**Fix.** Delete line 164; the module-level import at :51 already binds `datetime` in this scope.

**Evidence.**

```
workflow.py:163-167 —
    """
    from datetime import datetime

    try:
        return max(0.0, (ctx.current_utc_datetime - datetime.fromisoformat(started_at)).total_seconds())
```

**Verifier (ADJUSTED).** workflow.py:51 `from datetime import UTC, datetime, timedelta`; workflow.py:164 `    from datetime import datetime` inside `_watch_seconds`, whose only use is :167 `return max(0.0, (ctx.current_utc_datetime - datetime.fromisoformat(started_at)).total_seconds())` — identical binding, no behaviour change. The 'rule' cited is workflow.py:578-582, which is scoped to module-level imports of ray_kit: 'The workflow module is imported by the replay path; keeping this a pure comparison over two literals means the body has no import-time behaviour to be non-deterministic about.' Contrast the sanctioned in-body imports at :309-312 (`poll_stage`: `import httpx` / `from ray_kit.submit import job_status`) and :285-286 (`submit_stage`).

</details>

<details><summary><b>The promotion routes drive the synchronous workflow client with no timeout, unlike both sibling services</b> <i>(mgt, rule DWF-MGT-011, ADJUSTED)</i></summary>

**Sites:** `services/medallion/src/medallion/api/promotions.py:194`, `services/medallion/src/medallion/api/promotions.py:203`, `services/medallion/src/medallion/api/promotions.py:145`, `services/medallion/src/medallion/api/promotions.py:261`

**Why it matters.** The absence of a bound is real (promotions.py:145, :194, :203, :261 are all bare `run_in_threadpool` over synchronous gRPC), but the comparison that carries the severity is wrong. ingest bounds only its SCHEDULE call — api.py:483/:590 `await asyncio.to_thread(reader.state, run_id)` and api.py:599 `await asyncio.to_thread(terminator.terminate, run_id)` are both unbounded, i.e. ingest's status and terminate doors have exactly the gap being reported here. The one place the estate bounds is the schedule path, where a stated caller contract demanded it (`__init__.py:304 await asyncio.wait_for(asyncio.to_thread(_schedule), timeout=SCHEDULE_TIMEOUT_SECONDS)`, flows/lifespan.py:176/:222). The threadpool-exhaustion chain is also conditional on a sidecar that accepts and never answers, on a low-volume human-driven approval door; and `run_in_threadpool` around a blocking SDK call is the estate's sanctioned pattern. Real, worth a bound for symmetry with the schedule path — but info, not a warning about taking down the cascade head.

**Fix.** Bound every workflow-client call on these routes the way ingest does — `await asyncio.wait_for(asyncio.to_thread(...), timeout=…)` — and map the timeout to 503 + Retry-After, which is honest here because `instance_for(token)` is deterministic so a retried decision converges on the same instance rather than forking one.

**Evidence.**

```
promotions.py:194 `spec = await run_in_threadpool(_live_spec, wf_client, instance_id)`; promotions.py:203 `await run_in_threadpool(lambda: wf_client.raise_workflow_event(instance_id, "promotion_decision", data={"approved": approved, "subject": subject}))`
```

**Verifier (ADJUSTED).** promotions.py:194 `spec = await run_in_threadpool(_live_spec, wf_client, instance_id)`; promotions.py:203 `await run_in_threadpool(lambda: wf_client.raise_workflow_event(instance_id, "promotion_decision", data={"approved": approved, "subject": subject}))`; promotions.py:145 `await run_in_threadpool(lambda: wf_client.schedule_new_workflow(workflow=promotion_review, …))`. Counter-evidence: services/ingest/src/ingest/api.py:599 `await asyncio.to_thread(terminator.terminate, run_id)` and :483 `engine_state = await asyncio.to_thread(reader.state, run_id) if reader is not None else None` — no `wait_for`; the only bounded call is `__init__.py:304`.

</details>

<details><summary><b>No pause route anywhere in the estate</b> <i>(mgt, rule DWF-MGT-004, ADJUSTED)</i></summary>

**Sites:** `services/ingest/src/ingest/api.py:554`, `services/flows/src/flows/routes.py:226`, `services/medallion/src/medallion/api/promotions.py:230`

**Why it matters.** The fact is right — `grep -rn "pause_workflow" services/` returns nothing, and ingest's `POST /ingests/{run_id}/terminate` (api.py:554) is the estate's whole lifecycle-control surface. But nothing in this finding adds harm that is not already carried by the terminate findings above: for ingest the finder concedes pause is a convenience over an existing terminate; for `promotion_review` the finder concedes it is moot (the review is already parked on `wait_for_external_event` and `POST /promotions/{id}/decision` with `approved: false` is the cancel); and for the cascade the harm IS the missing terminate, already reported as its own item. Reporting the same cascade gap twice inflates it. DWF-MGT-004 fires on the letter of the checklist; the residual harm unique to this finding is a nice-to-have, i.e. info.

**Fix.** Lower priority than the missing medallion terminate. If added, `POST /v1/ingests/{run_id}/pause` on ingest is the one with a real use (hold the fan-out while a credential is rotated) and it must be paired with resume, since a paused instance with no resume route is strictly worse than a terminated one.

**Evidence.**

```
`grep -rn "pause_workflow" services/` returns nothing; the estate's entire lifecycle-control surface is ingest's `@router.post("/ingests/{run_id}/terminate", status_code=status.HTTP_202_ACCEPTED, response_model=TerminateAccepted)` at api.py:554
```

**Verifier (ADJUSTED).** `grep -rn "pause_workflow" services/` → no matches. services/ingest/src/ingest/api.py:554 `@router.post("/ingests/{run_id}/terminate", status_code=status.HTTP_202_ACCEPTED, response_model=TerminateAccepted)` — the sole lifecycle-control door, whose docstring names DWF-MGT-003 and states "BOUNDED, NOT INSTANT … it stops further SCHEDULING and does not stop an in-flight activity". promotions.py:230 `@router.post("/promotions/{instance_id}/decision", status_code=202)` is the working cancel for a held promotion.

</details>

<details><summary><b>No resume route anywhere in the estate</b> <i>(mgt, rule DWF-MGT-005, CONFIRMED)</i></summary>

**Sites:** `services/ingest/src/ingest/api.py:554`, `services/flows/src/flows/routes.py:226`, `services/medallion/src/medallion/api/promotions.py:230`

**Why it matters.** `resume_workflow` appears nowhere in `services/`. Downgraded to info because it is strictly consequent on the pause finding: nothing in this estate can put an instance into SUSPENDED, so no instance can be stranded there. The one place SUSPENDED is even acknowledged is `promotions._LIVE`, which deliberately treats it as answerable so a decision raised against a suspended review is not refused as terminal. Adding resume without pause would be dead code; adding pause without resume would create the stranded-instance failure this rule names.

**Fix.** Add only as the pair of a pause route, never alone.

**Evidence.**

```
promotions.py:46 `_LIVE = (WorkflowStatus.RUNNING, WorkflowStatus.PENDING, WorkflowStatus.SUSPENDED)` — the only mention of SUSPENDED in the estate; no `resume_workflow` call exists
```

**Verifier (CONFIRMED).** `grep -rn "resume_workflow" services/` returns nothing. promotions.py:44-46 `#: A workflow instance is never terminal-and-answerable: the engine accepts an event for a completed\n#: instance and discards it, which is the silent-success this door exists to refuse.\n_LIVE = (WorkflowStatus.RUNNING, WorkflowStatus.PENDING, WorkflowStatus.SUSPENDED)` is indeed the only mention of SUSPENDED under `services/medallion`, and since nothing can pause an instance, nothing can strand one. Info grade is correct and the reasoning (resume without pause is dead code) holds.

</details>

<details><summary><b>ingest's terminate answers 202 TERMINATING for a run that is already terminal</b> <i>(mgt, rule N/A, ADJUSTED)</i></summary>

**Sites:** `services/ingest/src/ingest/__init__.py:337`, `services/ingest/src/ingest/runs.py:222`, `services/ingest/src/ingest/api.py:599`

**Why it matters.** The mechanism reproduces exactly: the Protocol and the implementation both declare `-&gt; bool` with the docstring "True when the engine accepted the termination; False when it had nothing to stop", the body hardcodes `return True`, the route discards the value, and nothing on the path filters a terminal instance — `record_from_workflow_state` (runs.py:232-259) rebuilds a record from `serialized_input` with no runtime-status check, so a COMPLETED or already-TERMINATED run reaches the same 202. But the outcome is a misreported STATUS on a control door, not a workflow-management defect: no state is changed wrongly, nothing keeps running, nothing is duplicated, and the caller can immediately read the true state from `GET /v1/ingests/{run_id}`, which maps `COMPLETED`/`TERMINATED` honestly (runs.py:207-214 `_RUNTIME_STATUS`). A dead `bool` contract plus an over-confident 202 body is an honesty/cosmetic defect — info, not a warning.

**Fix.** Read the engine state before terminating (the route already holds a `reader`) and 409 or 404 when `runtime_status` is not RUNNING/PENDING/SUSPENDED — the same `_LIVE` check promotions.py:46 already implements. Then either honour the declared `bool` (return False when nothing was stopped, and reflect it in the response) or narrow the Protocol to `-&gt; None` so the contract and the implementation agree.

**Evidence.**

```
__init__.py:337-341 `def terminate(self, run_id: str) -> bool:\n        """True when the engine accepted the termination; False when it had nothing to stop."""\n        import dapr.ext.workflow as wf\n\n        wf.DaprWorkflowClient().terminate_workflow(run_id)\n        return True`; api.py:599 `await asyncio.to_thread(terminator.terminate, run_id)`
```

**Verifier (ADJUSTED).** services/ingest/src/ingest/__init__.py:337-341 `def terminate(self, run_id: str) -&gt; bool:\n        """True when the engine accepted the termination; False when it had nothing to stop."""\n        import dapr.ext.workflow as wf\n\n        wf.DaprWorkflowClient().terminate_workflow(run_id)\n        return True`; runs.py:218-222 `class WorkflowTerminator(Protocol):` … `def terminate(self, run_id: str) -&gt; bool: ...`; api.py:599-601 `await asyncio.to_thread(terminator.terminate, run_id)\n    logger.info("ingest_run_termination_requested", …)\n    return TerminateAccepted(run_id=run_id)`; api.py:590 `engine_state = await asyncio.to_thread(reader.state, run_id) if reader is not None else None` feeding `record_from_workflow_state`, which has no terminal filter.

</details>

<details><summary><b>decide() builds a per-request DaprWorkflowClient while its sibling show() reads the lifespan one and documents why</b> <i>(mgt, rule N/A, CONFIRMED)</i></summary>

**Sites:** `services/medallion/src/medallion/api/promotions.py:247`, `services/medallion/src/medallion/api/promotions.py:260`, `services/medallion/src/medallion/producer.py:119`

**Why it matters.** The producer's lifespan builds exactly one client for the app — `app.state.workflow_client = wf.DaprWorkflowClient()` (producer.py:119) — and `show` reads it under an explicit rule: 'From `app.state`, built once in the lifespan. Constructing a client per request re-opens its connection to the sidecar on every call — the "build it in lifespan, inject it" rule.' `decide`, on the same router 13 lines above, calls `decide_promotion(...)` with no `client=` argument, so `_client(None)` constructs a fresh `DaprWorkflowClient` — and therefore a fresh gRPC channel to the sidecar — on every approval, twice per request (`_live_spec` then `raise_workflow_event` share the one object, but each request builds a new one). Not a correctness bug — app-id resolution is still the producer's, which is what DWF-MGT-006 depends on — but it is the same rule violated on the route the rule was written next to, and it doubles the connection cost of the estate's one raise-event path.

**Fix.** `outcome = await decide_promotion(instance_id, approved=body.approved, subject=subject or "", client=getattr(request.app.state, "workflow_client", None), authorize=_fga_gate(request))` — `_client` already falls back correctly when that is None.

**Evidence.**

```
promotions.py:247 `outcome = await decide_promotion(instance_id, approved=body.approved, subject=subject or "", authorize=_fga_gate(request))` vs promotions.py:260 `wf_client = _client(getattr(request.app.state, "workflow_client", None))`
```

**Verifier (CONFIRMED).** promotions.py:247 `outcome = await decide_promotion(instance_id, approved=body.approved, subject=subject or "", authorize=_fga_gate(request))` — no `client=`, so promotions.py:191 `wf_client = _client(client)` falls into promotions.py:96-100 `def _client(client: _Client | None) -&gt; _Client:\n    if client is not None:\n        return client\n    import dapr.ext.workflow as wf\n\n    return wf.DaprWorkflowClient()` and constructs a fresh client (and gRPC channel) per approval. Thirteen lines below, promotions.py:258-260 `# From `app.state`, built once in the lifespan. Constructing a client per request re-opens its\n    # connection to the sidecar on every call — the "build it in lifespan, inject it" rule.\n    wf_client = _client(getattr(request.app.state, "workflow_client", None))`, and producer.py:119 `app.state.workflow_client = wf.DaprWorkflowClient()` builds the one client. App-id resolution is unaffected (both clients live in the producer process), so info is the correct grade.

</details>

<details><summary><b>`retry_when_draining` — the estate's own B6 admission guard for sidecar-delivered routes — is applied to 1 of the 8 subscription handlers</b> <i>(pubsub, rule N/A, ADJUSTED)</i></summary>

**Sites:** `packages/service-kit/src/service_kit/draining.py:19`, `services/medallion/src/medallion/api/events.py:45`, `services/medallion/src/medallion/api/bronze_arrival.py:96`, `services/medallion/src/medallion/api/train.py:113`, `services/medallion/src/medallion/api/promotions.py:280`, `services/lineage/src/lineage/api/dapr.py:79`, `services/notifications/src/notifications/api/subscriptions.py:59`

**Why it matters.** The coverage fact is right (one non-test call site) but the stated failure is not reachable, and the scope is partly a recorded decision. `app.state.shutting_down` is set ONLY inside the lifespan `finally` in every app that has it (services/medallion/src/medallion/mover.py:109, producer.py:134, lineage/main.py:112, notifications/lifespan.py:166, viewer, search, annotator) — i.e. after uvicorn has stopped accepting connections and drained in-flight requests. A delivery that is already being served passed the dependency before the flag flipped, and a delivery arriving after it flipped never reaches the app at all. So the claim that a draining mover "starts and abandons a full stage transform instead of handing the trigger straight back" describes a window the current lifespan wiring does not open — and adding the dependency to /medallion-event would change no observable behaviour today. Restate it as: the drain gate's coverage is one route, its vacuity guard cannot see subscription doors, and the flag it reads is set too late for any of them to matter — the fix worth naming is flipping `shutting_down` at SIGTERM/preStop, not sprinkling the dependency. Severity stays info.

**Fix.** Add `drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None` plus the two-line early return to the other seven subscription handlers, mirroring bronze_arrival.py:49-56 verbatim. If the guard is judged not worth it on the cheap handlers (/lineage-events, /publication-arrival), say so in draining.py's docstring rather than leaving a module that claims to cover a class it covers once.

**Evidence.**

```
* :func:`retry_when_draining` — a sidecar-delivered route gets **RETRY**, never DROP and never
  SUCCESS. DROP is final and these topics carry no DLQ, so dropping a trigger because this replica
  happened to be draining silently cancels a cascade
```

**Verifier (ADJUSTED).** packages/service-kit/src/service_kit/draining.py:19-23 is quoted correctly, and `grep -rn retry_when_draining services packages --include=*.py` outside tests returns only the module itself and services/medallion/src/medallion/api/bronze_arrival.py:22,49. The other subscription handlers confirm the gap: events.py:45-63 (`on_stage`), train.py:113-124 (`on_train_trigger`), promotions.py:280-292 (`on_promotion_held`), bronze_arrival.py:96-113 (`on_publication`), lineage/api/dapr.py:79,91, notifications/api/subscriptions.py:59,103, plus catalog/api/dapr.py:84 — so the real non-DLQ count is ~10, not 8. Part of the scope IS recorded: services/medallion/tests/test_run_doors_refuse_while_draining.py:26-37 fixes the list ("The doors that CREATE work" / `SUBSCRIPTION_DOORS = {("bronze_arrival.py", "on_bronze_arrival")}`) and even records why the promotion decision is excluded; what it does NOT do is defend excluding the mover — its anti-vacuity test only scans `@router.post` decorators (`d.func.attr == "post"`), so a `@dapr_app.subscribe` door added ungated is invisible to it. Reachability is refuted by mover.py:106-113: `try: yield finally: app.state.shutting_down = True` — the flag flips only in lifespan teardown.

</details>

---

## Appendix A — what each lane found the code to be

The surface notes, so a reader can judge whether the sweep understood what it was reading.

### `det-medallion` — medallion — workflow bodies (DWF-DET)

`services/medallion/src/medallion/workflow.py` registers exactly three workflows (`WORKFLOWS = (stage_run, train_run, promotion_review)` at :1140, wired by `register()` at :597) and ten activities, all in one module with a hard "everything non-deterministic lives below this line" divider at :273. On the determinism checklist proper the bodies are genuinely clean, and unusually so: the only clock read is `ctx.current_utc_datetime` (:189, :167), waiting is `ctx.create_timer` (:211, :664, :880), the poll loop was rewritten as one-poll-per-turn with `ctx.continue_as_new` (:227, :678) so there is no `while` loop at all, every `log.*` call in workflow scope is guarded by `if not ctx.is_replaying:` (:195, :220, :237, :244, :265, :673, :685, :691), and the three helpers the bodies call synchronously — `_watch_seconds` (:152), `_is_terminal` (:576), `settings_author_marker` (:914) — are pure. `get_settings()`, `httpx`, `asyncio.run`, `DaprClient` and `datetime.now(UTC)` appear only inside activities, and `promotion_review` deliberately routes its threshold read through an activity (`resolve_review_policy`, :855) with a comment saying why. I verified `wf.when_any` semantics against the vendored SDK (`dapr/ext/workflow/_durabletask/task.py:574-579`): it completes with the child `Task` object itself and `ctx.create_timer` is a pass-through, so the `winner is deadline` identity test at :883 is correct, not a latent bug. What I did find is not a replay hazard but a control-flow one that the file's own comments assert is handled and is not: `status is None` is used as the sentinel for "the poll activity raised", while `job_status` also legitimately *returns* `None` for a 404, so the two collapse and the watch aborts after a single poll. I drove `stage_run` and `train_run` through the repo's own fake contexts to confirm it rather than reasoning about it.

*Checked clean:* `DWF-DET-001`, `DWF-DET-002`, `DWF-DET-003`, `DWF-DET-004`, `DWF-DET-005`, `DWF-DET-006`, `DWF-DET-007`, `DWF-DET-008`, `DWF-DET-009`, `DWF-DET-010`, `DWF-DET-011`, `DWF-DET-012`, `DWF-DET-013`, `DWF-DET-014`, `DWF-DET-015`

### `det-ingest` — ingest — workflow bodies (DWF-DET)

This is an unusually determinism-literate file. Both registered workflows (`ingest_run`, `chunk_run`, registered at workflow.py:1253) are sync generators; every clock, random source, env read, network call, file read and DB touch is already behind an activity, and the module header (:29-45) states the rule explicitly. I ran all fifteen DWF-DET patterns over workflow scope — the two generator bodies plus the only helper they call synchronously (`bound_errors`, :219) and the Pydantic models they validate — and every one is clean: no `datetime.now`/`time.*`, no `uuid`/`random`, no `sleep`, no `asyncio`/`threading`, no HTTP/file/DB client, and no `os.getenv` (the three env reads at :179-181 are inside the `resolve_limits` activity; :990 and :1226 are `logging` inside activities; :1180 `asyncio.run` is the activity-only `_run_async`). The estate went further than the checklist asks: `RunLimits`/`ChunkSpec`/`RunSpec`/`ResolvedSizing` deliberately carry no env-reading `default_factory`, because both bodies call `model_validate` at their first line, and `replay_guard.py` is a bespoke AST gate for exactly that. `bound_errors` sorts before truncating so the survivors replay identically; `child_ids` (:619) derives from position over an activity result; `finalize`'s `read_version` rides the input rather than being re-read; the deadline branch's `when_any`/`is deadline` identity test resolves from history order and `fanout.get_result()` (:660) correctly re-raises a recorded child failure into the boundary. There is no `while` loop and no `continue_as_new`, and none is needed for this shape. `DaprWorkflowClient` does appear inside an activity (:1228, the DWF-ACT-001 pattern) but it terminates children rather than orchestrating them, and there is no `ctx` API for a parent to stop its own fan-out, so it is the only mechanism available — the deadlock the rule warns about does not arise. What the file has NOT solved is payload size on the fan-IN leg: it wrote a measured gRPC budget for `enumerate_chunks` (:850-:888) and then carried an unbounded FragmentMetadata list back through the children into `finalize`'s input, which is the same wedge one activity later. The other three findings are on the terminate/abandonment path the prompt flagged.

*Checked clean:* `DWF-DET-001`, `DWF-DET-002`, `DWF-DET-003`, `DWF-DET-004`, `DWF-DET-005`, `DWF-DET-006`, `DWF-DET-007`, `DWF-DET-008`, `DWF-DET-010`, `DWF-DET-011`, `DWF-DET-012`, `DWF-DET-013`, `DWF-DET-014`, `DWF-DET-015`, `DWF-ACT-001`, `DWF-MGT-003`

### `det-act-flows` — flows — workflow + activities (DWF-DET + DWF-ACT)

`services/flows` is the studio flow-builder's backend: a small FastAPI service that takes a drawn node graph, validates it purely, and executes it in one of two lanes decided at startup — inline (`executor.execute`, asyncio.gather per topological wave) when no sidecar is present, or as a Dapr Workflow (`flow_run` in workflow.py, one `run_node` activity per node, fanned out with `wf.when_all` per wave) when `DAPR_GRPC_PORT` says one is. The workflow body is genuinely clean on determinism: it is a plain `def` generator whose only inputs are the validated `RunJob` payload and the results of `yield`ed activities; the plan is recomputed each replay from `graph.topo_waves`, which is I/O-free and sorts each wave so replay derives a byte-identical plan; the run id is `ctx.instance_id` rather than a `uuid4`; timing lives in the activity; there is no clock, no env read, no HTTP, no logging and no unbounded loop in workflow scope. All fifteen determinism rules pass, and several of them pass because the module docstring names them and the code was written against them. The activity side is where the residue is. `activities.run_node` is a sync `def` that runs `asyncio.run` on the Dapr worker thread (correct, and documented), types its wire boundary as `dict` on a stated history-compatibility argument (a recorded deviation from DWF-ACT-009, not a defect), and delegates to the shared `executor.run_node` so both lanes behave identically. That sharing is also how the two real findings reach activity scope: `executor.run_node` catches only `NodeError`, so a caller-supplied `regexReplace` like `\9` raises `re.error` out of `compiled.sub` — outside the one `try` in `_regex` — and kills the entire run instead of one node; and the FLOWS-REDOS-ON-LOOP audit item is only half fixed — the regex arms are off the event loop via `asyncio.to_thread` and a 256 KiB subject cap exists, but a subject cap does not bound nested-quantifier backtracking, which blows up at ~30 characters (measured: 9.7 s at 28 chars, 4x per added char). Everything else I checked — the lazy `WorkflowRuntime` in runtime.py, the three-outcome `DaprFlowScheduler`, the uuid5 run-id derivation with a `\x00` separator, the two payload ceilings and their 4 MiB grpc arithmetic — is deliberate, documented and correct as written.

*Checked clean:* `DWF-DET-001`, `DWF-DET-002`, `DWF-DET-003`, `DWF-DET-004`, `DWF-DET-005`, `DWF-DET-006`, `DWF-DET-007`, `DWF-DET-008`, `DWF-DET-009`, `DWF-DET-010`, `DWF-DET-011`, `DWF-DET-012`, `DWF-DET-013`, `DWF-DET-014`, `DWF-DET-015`, `DWF-ACT-001`, `DWF-ACT-003`, `DWF-ACT-004`, `DWF-ACT-005`, `DWF-ACT-007`, `DWF-ACT-009`, `DWF-ACT-010`, `DWF-ACT-011`

### `act-medallion` — medallion — activities (DWF-ACT)

`medallion/workflow.py` hosts three workflows (`stage_run`, `train_run`, `promotion_review`) and exactly ten activities, all registered by name through `register()` at :597-602. The activities are thin, correctly-shaped shells: every one is a SYNC `def` that opens its own event loop through `_run_async` (`asyncio.run`) on the workflow worker thread, so there is no sync-I/O-in-`async def` anywhere (DWF-ACT-005 is clean by construction, and the `_run_async` docstring says why). No activity touches `DaprWorkflowClient`, no activity sleeps, none prints, none writes a module global, and no two share a registered name. Every activity that swallows an exception does so through `core/best_effort.py`, a purpose-built compensating-emit guard whose own docstring argues the case — that is the estate's sanctioned pattern, not a `except: pass`. Idempotency is genuinely thought about on the Ray lane: `submit_stage` returns the id the submitter POSTED rather than re-deriving it, `ray_kit.submit.submission_id` folds `work` and the build digest into a deterministic key, `submit_or_reattach` re-attaches, promotion instances are keyed `promotion-&lt;token&gt;`, and every lineage emit rides a deterministic `run_id` so a duplicate MERGEs. What is missing is the other half: **not one of the ten activities reads its `WorkflowActivityContext`** — `ctx` appears only in the ten signatures, never in a body — so the `workflow_id`/`task_id` pair the SDK exposes as the taskExecutionId is available and unused, and the two activities whose side effect is a bus publish mint a fresh dedupe key per execution. The sharpest defect is not a checklist rule at all: the uncommitted edit that made the catalog's tag move the ONLY promotion door left `promotion_review` still gating `publish_promotion` on `spec.pub_topic`, which the chart sets to `""` on both terminal movers — so an approved gold promotion emits a PROMOTED lineage COMPLETE and never advances the tag. NOTE: the working tree moved under this audit — `gate_decision.py` and `transform.py` were both rewritten by a concurrent editor between my first and second reads (`TRIGGER`→`UNGOVERNED`/`MISCONFIGURED`, `if False:`→`if settings.quality_enabled:`). Every line cited below was re-verified against the file as it sits on disk after that rewrite; `workflow.py` (md5 bfc5133b…) did not change during the sweep.

*Checked clean:* `DWF-ACT-001`, `DWF-ACT-005`, `DWF-ACT-006`, `DWF-ACT-007`, `DWF-ACT-010`, `DWF-ACT-011`

### `act-ingest` — ingest — activities (DWF-ACT)

The ten registered activities (workflow.py:1239-1258) are all sync `def` bodies that validate a Pydantic model on the first line and delegate every network call to `ingest/runtime.py`, which is split out precisely so a reader can see the workflow bodies contain no I/O. The plane is unusually self-aware about this checklist: `tests/test_replay_hygiene.py` is an explicit "DWF-ACT sweep" naming F12a/F12b/F12d, the error map is capped (`bound_errors`, MAX_REPORTED_ERRORS=100), the lineage in-process mirror is a bounded deque with DWF-ACT-007 cited by id in its comment, `publish_units` really does stamp `Nats-Msg-Id` (and `test_unit_dedupe_and_namespace_refusal.py` pins the id's stability AND that the publisher attaches it, by source inspection), and `finalize`'s at-least-once semantics are closed at the catalog with a `rask.ingest.run_id=` transaction-property marker plus a carried `read_version` (`dataplane.py:607` `_find_run_commit`, pinned by `test_a_RETRIED_finalize_presents_the_SAME_read_version_and_never_re_reads_it`). So most of DWF-ACT is already answered, and answered with measurements rather than assertions. What the tests do NOT pin is the two things I report: (a) the 4 MiB gRPC budget is measured on the DISPATCH direction only — three test files compute descriptor bytes at 10M units and none computes the FAN-IN fragment bytes, which are O(units / rows-per-fragment) and cross the same channel; and (b) every replay test stubs `discover_staged` to keep returning fragments (`test_replay_hygiene.py:307`) or disables the purge outright (`test_partial_ack_duplication.py:292`), so no test drives `finalize` twice with staging actually purged between attempts — which is the one replay in which the catalog's dedupe is unreachable. I verified the SDK claims the code makes rather than trusting them: `_durabletask/internal/shared.py:89-131` merges only four keepalive options and `WorkflowRuntime` exposes no `channel_options`, so grpc's 4 MiB default receive limit does stand; `terminate_workflow` is indeed `recursive=True` by default and its own docstring says it cannot stop an in-flight activity. I also measured a real fragment manifest against BRONZE_SCHEMA on pylance 9.0.0 (413 bytes) rather than estimating it.

*Checked clean:* `DWF-ACT-003`, `DWF-ACT-005`, `DWF-ACT-006`, `DWF-ACT-007`, `DWF-ACT-010`, `DWF-ACT-011`

### `mgt` — management endpoints, all three apps (DWF-MGT)

Three apps host Dapr workflows and they have three very different management surfaces. `services/ingest` is the mature one: `POST /v1/ingests` (start), `GET /v1/ingests/{run_id}` (status, with a rebuild-from-`serialized_input` fallback so a pod restart does not 404 a live run), and `POST /v1/ingests/{run_id}/terminate` — all three behind `authorize_ingest`'s dual-auth, all three driving the synchronous gRPC client through `asyncio.to_thread` with an explicit bound. `services/flows` has start + status on `/api/flows/runs[/{id}]`, both behind the estate `writer` tier, with a genuinely careful three-outcome scheduler (`DaprFlowScheduler`) that refuses rather than double-runs — but no terminate. `services/medallion` is the split one: the workflows that actually move the cascade (`stage_run`, `train_run`) execute in the bus-only movers and the producer, and NEITHER app exposes any status or terminate route for them — the mover mounts only `/healthz`, `/medallion-event` and `/dlq-event`. The one management surface medallion does have is the promotion review (`GET /promotions/{id}`, `POST /promotions/{id}/decision`), and DWF-MGT-006 is genuinely CLEAN there: the event name `promotion_decision` matches on both sides (`workflow.py:879` waits, `promotions.py:203` raises), the workflow and the raise-event route are hosted in the same process for the documented app-id reason, the gateway carries `/api/promotions` → `/promotions` root-mounted, and both halves are gated by the same `medallion.qualityReview` flag so a mover cannot publish a hold into an app that hosts no reviewer. No `purge_workflow` call exists anywhere in the estate (DWF-MGT-012 vacuously clean) and no route awaits `wait_for_workflow_completion` (DWF-MGT-011's headline shape clean); every long wait is a durable `ctx.create_timer` with a `max_polls` ceiling and `continue_as_new`. Every mutating route is guarded (DWF-MGT-010 clean). What the sweep found instead is a hosting defect — the `train_run` watcher is scheduled into an app that starts no workflow runtime under the default chart — and a management hole: an operator has no HTTP lever of any kind to inspect or stop an in-flight cascade stage, because `services/compute` proxies Ray read-only.

*Checked clean:* `DWF-MGT-001`, `DWF-MGT-006`, `DWF-MGT-007`, `DWF-MGT-008`, `DWF-MGT-009`, `DWF-MGT-010`, `DWF-MGT-012`, `DWF-MGT-013`, `DWF-MGT-014`, `DWF-MGT-015`

### `actors-annotator` — annotator — virtual actors

This is a genuinely well-built virtual-actor plane, not a first draft. Three actor types (`AnnotationTaskActor` per task, `AnnotationProjectActor` per project holding the task index, `TenantProjectsActor` per tenant) are registered in `annotator/main.py`'s lifespan (never at import), behind `DaprActor(app)` mounted at import with `guard_actor_routes(app)` closing the sidecar-only `/actors/...` surface, plus `probe_actor_state_store()` to catch the "Workflow engine started, then nil-deref" trap the estate already recorded. Every actor is stateless in memory: each method re-reads its keys through `_state_manager.try_get_state`, re-parses JSON into a fresh Pydantic model, mutates, and `set_state` + `save_state` in the same turn — so nothing survives deactivation by assumption, and read-modify-write never straddles a cross-actor await. Every proxy in the service goes through `annotator.projects.proxies.typed_proxy`, which reads `__actormethod__` off the interface, and `tests/unit/test_actor_proxy_names.py` both pins the translation and sweeps for raw `ActorProxy.create` — the wire-name trap is closed. Durability is reminders only (no `register_timer` anywhere): a one-shot `lease` reminder with a bounded retry policy, and a repeating `publish-run` watchdog with a drop policy, and the arm-before-persist / disarm-after-persist ordering is deliberate and tested. Reentrancy is not configured anywhere, and it does not need to be: the only cross-actor call made inside a turn is task→project `TaskStateChanged`, and the project actor never calls a task actor from inside a turn — its watchdog explicitly `spawn_publish`es the saga onto the loop instead of awaiting it. The chart scopes `lance-statestore` to app-id `annotator` with `actorStateStore: "true"`. What I found wrong is narrower than the architecture: the lease-renewal branch lives on a code path no client uses, the project index can resurrect a deliberately-dropped task, and the fire-and-forget saga task is unreferenced with a process-local guard that leaks if it is ever collected.

*Checked clean:* `N/A — turn-based concurrency: no re-entrant cycle exists. The only cross-actor call inside a turn is AnnotationTaskActor.fire -> _report_state -> AnnotationProjectActor.TaskStateChanged (actor.py:326); the project actor never invokes a task actor from inside a turn, and its watchdog explicitly refuses to await the saga (project_actor.py:435-451, `spawn_publish` onto the loop). receive_reminder's `await self.fire(...)` (actor.py:437) is an in-process call on self, not a sidecar round-trip. Reentrancy is correctly left unconfigured (grep for reentrancy/ActorRuntimeConfig across .py/.yaml: no hits).`, `N/A — state consistency: every actor method reads its keys, mutates a freshly re-parsed model, and set_state+save_state inside one turn; no value is read in one method and written in another after an await. `_store` writes PROJECT_KEY and INDEX_KEY through one save_state, which the SDK sends as a single transactional op set (state_manager.py:242). Success is returned only after the store: fire() stores at actor.py:297 before disarming and reporting, save_draft stores at actor.py:400-401 before returning the draft.`, `N/A — proxy wire-name trap: every @actormethod name (Seed/Get/Fire/SaveDraft/GetDraft; Create/Get/Fire/Send/TaskStateChanged/ListTasks/RecordPublish/NoteProgress/SetOntology/DropTask/Adjudicate; Register/ListProjects) matches its call sites in actor.py, saga.py, lakehouse.py, projects.py, project_events.py and tasks.py. Every call site goes through typed_proxy, and tests/unit/test_actor_proxy_names.py:71 sweeps the whole package for raw ActorProxy.create.`, `N/A — actor state store: chart/templates/dapr-statestore.yaml:62 sets actorStateStore "true", and chart/values.yaml:1328-1329 scopes `lance-statestore` to app-id `annotator`, which is the annotator's daprAppId (chart/values.yaml:1275). annotator/main.py:143 additionally calls probe_actor_state_store() after registration, precisely so the "registered locally, sidecar hosting disabled" silence is caught at startup.`, `N/A — timers vs reminders: register_timer appears nowhere in the service. Both durable mechanisms are reminders (LEASE_REMINDER actor.py:414, PUBLISH_REMINDER project_actor.py:275), each with an explicitly chosen ActorReminderFailurePolicy (bounded retry for the one-shot lease, drop for the repeating watchdog) rather than the runtime default.`, `N/A — lifecycle/deactivation: no actor carries an instance field across turns. _load/_load_index re-read through the state manager on every call and re-parse from JSON, and _store writes a JSON string back, so mutations never leak into the SDK's state-change tracker. No _on_activate is overridden and none is needed.`, `N/A — saga forward recovery: run_publish is token-keyed idempotent as documented (create is exist_ok, tag converges via _tag_points_at, record_publish is set-once, publish_succeeded converges through _converge's IllegalTransition catch), and the crash-between-record_publish-and-publish_succeeded hole is explicitly closed at saga.py:174-175. The failure handler logs before recording and guards the recording so the original exception survives (saga.py:258-263).`, `N/A — idempotency at the actor boundary: seed (actor.py:139), create (project_actor.py:165), register (tenant_actor.py:140), send (project_actor.py:293), drop_task (project_actor.py:341), record_publish (project_actor.py:359) and adjudicate (PUT semantics) are all explicitly idempotent; fire() is protected by the closed-world transition table, so a retried edge lands as a 409 rather than a second application. send_items checks seed's return before indexing (project_events.py:517).`, `N/A — notifications producer contract: the lease-lapse emit names the real audience on extra.subject read BEFORE the transition nulls assignee (actor.py:437-438, 472), and services/notifications/src/notifications/api/control_events.py:49-50,107 registers task_lease_expired/task_dropped and targets on extra['subject'] — so actor="system:annotator" is not a role literal in a targeting field. drop_task's emit is gated on `removed` and on holder != subject (project_events.py:346).`

### `actors-notifications` — notifications — virtual actors

This is a genuinely careful virtual-actor implementation, and most of the classic traps are already closed by design rather than by accident. `InboxActor` is one actor per verified subject, id = `encode_subject(token.sub)` decoded back out of `self.id.id` so no method ever takes a subject; state is split into four partitions (meta/rows/watches/prefs) with the badge answered from the small one; the unread count is re-derived from the rows on every mutation so there is no second truth; `_require_owner` is a redundant second lock against a mis-keyed row; and both mutating turns persist through one `save_state()` transaction before they report success, so a crash between fan-out and save yields a RETRY that lands as a counted duplicate rather than a wrong badge. `proxies.TypedActorProxy` reads the interface's own `@actormethod` metadata, and I verified every call site (`deliver/page/unread/mark_seen/dismiss/get_watches/set_watch/get_prefs/set_prefs/claim_channel/arm_digest` and `watch/unwatch/list_watchers`) resolves to a declared wire name against the vendored SDK's `get_dispatchable_attrs_from_interface` — the wire-name trap is closed. Idempotency is on `notification_id` (`run_id@STATE` / `&lt;event_id&gt;@&lt;ACTION&gt;`), identical on the bus and the reconciler because both run `ingest_run_event`, so the cron re-offer and the FEED_OVERLAP window cost duplicates, never double counts. The two FGA gates are correctly split (`can_be_notified` at delivery in `_deliver_one`, `can_get_metadata` at render in `get_inbox`, control rows exempt on both halves), and `chart/templates/dapr-statestore.yaml` carries `actorStateStore: "true"` with `notifications` in `stateStore.scopes`. What it does NOT get right is the one place an actor turn reaches back through the sidecar into itself: the digest tick. Reentrancy is nowhere configured in this repo (no `ActorRuntimeConfig`, no `ActorReentrancyConfig`, nothing in the chart), so the SDK omits it from `/dapr/config` and daprd disables it — and `_send_digest` calls a pusher that opens a proxy to its own actor id. Two smaller issues follow the same shape: a fault absorbed into a SUCCESS ack that permanently drops the WATCH audience, and a read-tolerance path that re-creates the unclearable badge the render exemption was written to end.

*Checked clean:* `N/A-turn-concurrency-fanout: fan_out iterates recipients sequentially over DISTINCT actor ids (fanout.py:133-146); no cross-actor cycle, no shared lock, no re-entrance on the delivery path. `deliver`, `mark_seen`, `dismiss`, `claim_channel` are single-turn read-modify-write with no etag and no OCC, which is correct under single activation.`, `N/A-proxy-wire-names: verified every proxy call site against the decorated interface names using the vendored SDK's own dispatch (`_type_utils.get_dispatchable_attrs_from_interface` keys on `__actormethod__`). InboxActorInterface: Deliver/Page/Unread/MarkSeen/Dismiss/GetWatches/SetWatch/GetPrefs/SetPrefs/ClaimChannel/ArmDigest/DrainDigest; WatchIndexActorInterface: Watch/Unwatch/ListWatchers. All 17 call sites (inbox.py, prefs.py, watches.py, channels.py, fanout.py, control_events.py, proxies.py) use Python names declared on the interface; `TypedActorProxy.__getattr__` raises AttributeError for anything else. Every concrete actor overrides all 12 (resp. 3) abstract members, so registration cannot fail on an unimplemented wire name.`, `N/A-durable-read-state: `mark_seen`/`dismiss`/`deliver` all reach `_persist` -> `_save` -> `await save_state()` BEFORE returning their counts; ROWS and META ride one transaction so the derived unread count cannot half-apply. A crash after save but before the response yields a RETRY that the `notification_id` guard turns into `delivered: False`. No path reports success on unsaved read state.`, `N/A-at-least-once-idempotency: `notification_id` is `run_id@STATE` on the lineage lane and `<event_id>@<ACTION>` on the control lane, minted in the shared projection both ingresses run (`ingest_run_event`). `InboxActor.deliver` short-circuits on a matching id and returns `delivered: False` -> counted DUPLICATE, never a badge increment. FEED_OVERLAP's deliberate 64-seq re-offer and JetStream redelivery are therefore both free; `event_seq` is stamped after `notifiable()` and does not participate in the key.`, `N/A-claim-check-pointers: rows store only (notification_id, reason, object_id, source_run_id, event_seq, occurred_at, seen, dismissed, sent); no payload copy. A pointer whose object is gone or ungranted is filtered by `visibility.visible` per page and dropped from the list — one row, not the page — and `InboxRow.of` is a declared field projection that cannot raise per row. `InboxPointer`'s reason degrade prevents the all-or-nothing list-validation 503 for the lineage lane (see the control-lane exception filed above).`, `N/A-ack-contract: `notifiable()` returning None -> SUCCESS is deliberate and NOT reported. Every genuinely transient failure is routed to RETRY: `_deliver_one` catches per recipient -> Outcome.RETRIED -> `needs_retry` -> DAPR_RETRY; a userset-expansion (FGA) failure and an inbox-actor failure on the control lane both answer RETRY; an unparseable payload answers DROP with the payload redacted (`_faults`). The reconciler holds its mark on any retry and only steps over after FEED_MAX_STALLS with an ERROR. The one ack that does swallow a real fault is filed above (watchers_of).`, `N/A-gating-split: delivery asks `can_be_notified` via `Visibility.sees_all` with a SUBSET test over all outputs (fanout.py:165); render asks `can_get_metadata` via `Visibility.visible` (inbox.py:94). Neither is missing and neither is on the wrong half. FGA-on-with-no-client raises rather than answering open (visibility.py:116-119). The control lane's skip of the delivery gate, and `/inbox/unread` not being filtered, are both documented decisions with stated bounds.`, `N/A-state-store-scoping: chart/templates/dapr-statestore.yaml:41-62 renders `state.postgresql` with `actorStateStore: "true"`, its DSN resolved through the `lance-secrets` Dapr secret store (no env fallback), and chart/values.yaml:1377 lists `notifications` in `stateStore.scopes` — so the component is loadable by this app-id. The reconciler's cursor uses the same store by name through the plain state API (`RASK_NOTIFICATIONS_STATE_STORE` default `lance-statestore`), and the cron binding component name matches `RASK_NOTIFICATIONS_BINDING_NAME` and is scoped to `notifications`.`, `N/A-reminder-policies: `_DROP_THE_TICK` on the repeating compaction reminder and `_RETRY_THE_ONE_SHOT` on the `period=0` digest reminder are both supported by the vendored SDK (`register_reminder(..., failure_policy=...)`, failure_policy.py) and both match their reminder's cardinality. Arm-before-persist / disarm-after-persist ordering across the Scheduler-etcd vs Postgres split is correct, and the read-path repair (`_repair_compaction`) is conditional so ordinary reads stay reads.`

### `pubsub` — pub/sub + Dapr components

This is one of the most deliberately-audited pub/sub surfaces I have reviewed, and most of the brief is already closed with its reasoning recorded inline. Ack semantics are uniform and correct across all nine subscription handlers: malformed payloads DROP (never raise, never RETRY — the poison-message rule is stated by name in lineage/services/consumer.py, notifications/api/ingest.py and transform.py), transient outages RETRY, and anything acted-on-but-not-actionable SUCCESS-acks. Dead-lettering is wired as a SET: dapr.resiliency.enabled renders the Resiliency CRD, the per-app *_DLQ_TOPIC envs and the long crash-recovery broker backOff together, so a deadLetterTopic can never exist without a retry policy behind it. Component-level durableName collisions are avoided because every co-hosted topic lives on a DIFFERENT JetStream stream (LINEAGE/MEDALLION/TRAINING/DLQ/CATALOG_CONTROL), which nats-stream-job.yaml creates, asserts the retention of, and reconciles for durable drift. Scoping is derived rather than restated (secret-store scopes from stateStore.scopes; annotator/maintenance control-publisher scopes gated by the same expression as their CONTROL_EMIT envs). Topic names are single-sourced, CloudEvent handling is uniform, trace context genuinely propagates (the lance-tracing NullExporter was fixed 2026-08-22 and pinned by a test, and TRACEPARENT rides into the Ray job), and ordering assumptions hold at moverReplicas=1 with a single-flight overwrite-convergent write. What survives all of this is one class of defect: the cascade's CONTINUATION rides best-effort, fail-open emits that have no outbox, no reconciler and no poller behind them, while the surrounding prose asserts that losing those events costs only a refresh hint.

*Checked clean:* `N/A-ack-semantics: every handler DROPs on unparseable input and RETRYs only on transient failure; none returns 200 on an unhandled exception and none raises into the sidecar (lineage/services/consumer.py:74-85, notifications/api/ingest.py:9-19, medallion/services/transform.py:293-297, publication_trigger.py:114-123, promotions.py:134-155)`, `N/A-dlq-wiring: medallion/api/dlq.py subscribes a real per-app dlq.<appId> topic that every subscription declares as deadLetterTopic; the DLQ stream is created at nats-stream-job.yaml:151; the parking route acks unconditionally with no auto-replay; every *_DLQ_TOPIC env is gated on dapr.resiliency.enabled so a deadLetterTopic never ships without a retry policy (medallion.yaml:146-149 and 353-360, services.yaml:320-327, configmap.yaml:59-69)`, `N/A-dlq-durable-collision: the per-app durableName is reused by each app's main and DLQ subscriptions, which is sound because NATS scopes consumer names per stream and dlq.* lives on its own DLQ stream (stated at nats-stream-job.yaml:145-151 and values.yaml:1876-1878); lineage additionally gets a dedicated -dlq component because its main one is ephemeral deliverPolicy=all`, `N/A-idempotency: AGE MERGEs on run_id and the events feed dedupes on (run_id, event_type, event_time); notifications dedupes on a natural key derived from the event rather than the route; the mover holds a process-wide single-flight lock over an overwrite-convergent write; Ray submissions collapse on a deterministic stage_submission_id; promotion reviews collapse on a deterministic instance_id; deliverPolicy=new + durable for every trigger consumer, deliverPolicy=all only for the graph rebuild`, `N/A-cascade-head-chain: POST /produce -> seed_bronze -> ONE build_run_event -> publish_lineage_with_outbox on lineage.events.v1 -> /bronze-arrival filters COMPLETE + bronze namespace -> publishes medallion.bronze; the emit is outbox-staged, a publish failure propagates instead of being swallowed, and the route turns it into 503 + Retry-After with an Idempotency-Key retry contract (produce.py:130-152, api/produce.py:97-109, ingest_trigger.py:161-198)`, `N/A-loop-guard: /bronze-arrival acks and ignores the movers' own silver/gold writes because their output namespace is never the bronze one, project-qualified or not (ingest_trigger.py:37-86)`, `N/A-component-scoping: every publisher and subscriber app-id appears in the scopes of the component it uses — catalog+maintenance on the publish-only lineage component, catalog/maintenance/annotator on the control broadcast, per-subscriber components scoped to their own app-id, secret-store scopes derived from stateStore.scopes; the maintenance and annotator CONTROL_EMIT envs are gated by the same expression as their scope entries so the two cannot drift`, `N/A-topic-agreement: CONTROL_TOPIC is imported into medallion config rather than re-typed; lane_routes is derived in-chart from medallion.movers[] so no lane can exist the publication head cannot route; MEDALLION_CONTROL_PUBSUB renders only when catalog.controlEmit is on and the code gates the subscription on it being non-empty; the five topic literals are pinned by tests/unit/test_invariants.py`, `N/A-cloudevent-shape: every publisher sets data_content_type='application/json' and every handler reads event.get('data') behind an isinstance guard; the DLQ handlers correctly treat the re-published envelope's data as the original payload; every handler types the body dict[str, Any] rather than Any, with the 422-on-Any trap documented at three sites`, `N/A-trace-context: the lance-tracing Dapr Configuration carries a real OTLP exporter (corrected 2026-08-22 from a silent NullExporter, pinned by test_dapr_sidecar_spans_actually_have_an_exporter), samplingRate is 1, and ray_submit hands TRACEPARENT plus the OTLP env into the Ray job so the cascade stays one trace across the bus and across job submission`, `N/A-resiliency: a pubsubDeliveryRetry policy targets every subscriber component including the two control-topic ones lance.subPubsub does not reach; the CRD uses only fields the schema declares (the exponential/duration mismatch was fixed 2026-07-28) and jitter is pinned to 0 so 450s stays under the 720s broker ackWait; invocation resiliency carries a timeout and a status-matched retry, and the circuit breaker was deliberately removed because Dapr's breaker has no status matching and 4xx-counting made it a remotely-triggerable DoS`, `N/A-ordering: moverReplicas defaults to 1, queueGroupName makes replicas a competing-consumer group rather than a fan-out, and the stage write is overwrite-convergent under a process-wide single-flight lock; no subscriber branches on the relative order of two events`, `N/A-secrets: credentials come only from the Dapr secret store (apply_dapr_secrets, fail-closed) and every consuming app-id including all movers is in the lance-secrets scopes; no env fallback for a credential exists on any pub/sub path`, `N/A-raise-workflow-event-appid: promotion_review is hosted in medallion-producer specifically because raise_workflow_event resolves the instance through the calling app-id and only the producer has a reachable door (producer.py:98-105); handle_promotion_held schedules into that same process, and the constraint is respected`, `N/A-notifications-producer-contract: promotion_review_requested is in NAMED_ACTIONS, carries extra.subject as a real user: principal (_publisher refuses usersets and wildcards), and lance.project/lance.originator are stamped through the cascade from the head; an unsendable approval ask is treated as a refusal and emitted to lineage as BLOCKED 'no reachable approver' rather than parking silently`

---

## Cross-references

- `open_python-audit.md` — the estate-wide Python audit this one complements. Findings there that touch
  the same files are cited by id rather than restated.
- `open_fastapi-audit.md` — the HTTP-surface sweep; the management-endpoint lane here and the
  authn/authz lane there both touch the workflow doors, and each defers to the other.
- `.claude/skills/rask-notifications` — the producer contract the notifications-actor lane is judged against.
- `docs/RESILIENCE.md` — records the catalog outbox gap as the estate's **#1 weakness**; the `pubsub`
  critical here is that gap, re-measured, with the observation that the shipped B4 reconcile restores
  provenance but cannot re-fire a halted cascade.
