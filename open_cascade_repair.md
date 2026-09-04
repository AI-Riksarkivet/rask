# Cascade repair — make a missed hop DETECTABLE, then REPAIRABLE

Carried out of `open_lakehouse_diff_left.md` §O2 (line 683, priority High) because a register row is
not a plan: that file gives this one line and a cross-reference, and everything below was established
by reading the tree and had nowhere to live. Same reason `open_lakehouse_lanes.md` was split out of
the cloud-native plan when it closed.

> | High | **No cascade reconciler and no re-run verb** — a missed hop is undetectable and unrepairable |

Cross-ref: `open_estate-verification.md` row 35 (D) — "Zero `bindings.cron` reconcilers on the
medallion and no re-run verb — forward-only AND unrepairable."

**Verified 2026-09-04:** `grep -rn "bindings.cron" services/medallion/src` returns nothing, and
`chart/templates/medallion.yaml` renders no cron component, while compute-prune, notifications,
catalog-control-relay, ingest and maintenance all have one. The operator surface is `GET /movers`,
`GET /movers/{m}/stages/{id}`, `POST .../terminate`, the promotions pair, and `POST /stages/{id}/terminate`
— every re-entry point (`/produce`, `/ingest-media`, `/train`) starts NEW work. Nothing repairs.

## The defect, stated precisely

**The hop that never ran.** `table_published` is published, the trigger is lost, no mover wakes, and
**no workflow instance is ever created**. Every pod is green. This shape is what makes the verb
edge-addressed: there is no instance id, no serialized input, nothing to load, so an
instance-addressed `POST /stages/{id}/rerun` is structurally unable to repair it.

## Established by reading the tree (2026-09-04, adversarially verified)

Recorded because each was expensive to establish and two of them corrected a wrong first answer.

### R1 · The same token is the correct default; a fresh one is the exception

At the pinned versions (`dapr_ext_workflow` 1.18.3, runtime 1.18.1) the instance-id guard bites on a
**LIVE** instance only. From the installed library, `dapr/ext/workflow/dapr_workflow_client.py`:

> `reuse_id_policy`: Deprecated and has no effect... **A workflow instance ID can always be reused
> once the existing instance with that ID has reached a terminal state** (e.g. COMPLETED, FAILED, or
> TERMINATED).

`transform.py` reaches `_stage_workflow_exists` only when `schedule_new_workflow` RAISES, which it
does not for a terminal instance. So re-publishing the VERBATIM trigger walks `stage_run` →
`submit_stage` → `submit_or_reattach`, which sees SUCCEEDED, returns `reattached`, and **recomputes
nothing** — the hop is repaired at zero GPU cost through the path already under test.

A fresh token changes `stage_submission_id(...)` and forces a full Ray recompute of a hop that already
succeeded, plus a second lineage run node for a run whose only defect was the wake-up. Keep it as an
explicit opt-in for the narrower "it ran and wrote garbage" case, and note that it cuts against
`bronze_arrival.py`'s standing distinction — *"the token distinguishes EVENTS, while
`stage_submission_id` distinguishes WORK, and merging the two questions is the defect"* — so it is a
deliberate exception, not the mechanism.

### R2 · The safety gate is the RAY JOB, not the workflow instance

`terminate_stage` stops the WATCH and leaves the Ray job running — its own 202 says so. So "the
instance is terminal" is satisfied by exactly the state an operator creates just before wanting a
re-run, and a re-run would then put a second Ray job on the same `to_uri`, one possibly wiping the
directory. `_write_lock` does not help: it is an unkeyed process-wide `asyncio.Lock` released the
moment pass 1 dispatches, and it never spans a job's lifetime.

**The gate must read Ray**: resolve `stage_submission_id(...)` and refuse **409** unless the job is
terminal. Where no id is resolvable there is nothing in flight to race, and the deterministic-id
reattach is the residual guard.

### R3 · Two horizons, and the verb cannot know its cost from the request

Workflow history retention bounds the instance-addressed form (168h COMPLETED / 720h FAILED+TERMINATED)
— that is how long a SPEC stays readable, not a dedupe window. The tighter and previously unrecorded
one: the no-recompute path depends on the Ray head still holding the SUCCEEDED job, and
`workflow.py` states Ray's GCS is not fault-tolerant here. **After a head restart the same-token
re-run silently becomes a full recompute.** The verb must therefore REPORT the path it took.

### R4 · Authorization is the mover's own rung, not the produce door

Gate on that mover's `fga_required_action` (`can_create_table`, or `can_promote` for silver→gold)
against `namespace:<project>-<toNamespace>`, via `authenticate_subject` plus an explicit check —
`promotions.py` states the reason verbatim: `authorize_produce`'s `can_administer` is "coarser AND
different, and would lock out exactly the non-admin validator the rung exists for." The producer
cannot derive `toNamespace`/`requiredAction` from `mover_urls` (name→URL only), so that mapping has to
be rendered.

## Ordered steps

| # | Step | State |
| --- | --- | --- |
| C1 | **One trigger shape, two producers** — extract `build_stage_trigger`; the publication head becomes its first caller, pinned by an AST anti-drift gate | ✅ **LANDED** `e1baef1c` (503 medallion tests, tests/unit 3705, ruff clean) |
| C2 | **The re-run verb** — `POST /movers/{mover}/rerun` on the producer, body `{project, from_version, to_version}`, same-token default, R2's Ray-liveness 409, R4's authz, reports the path taken per R3. No gateway row needed: `Route("/api/movers", "/movers", *medallion)` plus prefix matching already carries it | next |
| C3 | **The reconciler** — the detection half. `bindings.cron` on the producer, read-only, reports drift and repairs nothing, modelled on `maintenance/services/reconcile.py`. Must pick and record its answer to bindings.cron firing on every replica | not started |
| C4 | **Surface the silent refusals** — see below | not started |

## Found while investigating, NOT in §O2 — three silent-failure classes

1. **A DROP is an ack, and `routing_disabled` is permanent.** `_preflight` has six deterministic
   refusals, all DROPs. The counters exist (`medallion.stage.refused`, `medallion.stage.other_lane`)
   and appear in **neither** `chart/alerting/rules.yml` nor `chart/templates/perses-dashboards.yaml`.
   The code calls `routing_disabled` "a DEPLOYMENT gap, and therefore permanent: every tenant trigger
   this mover ever receives halts here" — an estate-wide halt behind a series nothing reads. This is
   C4 and is arguably higher value than C3, because it needs no new state at all.
2. **A gap the outbox does not cover.** The lineage outbox stages AFTER the Lance commit, so a pod
   kill between commit and `stage_event` loses the cascade head with the data on disk. Lineage's sweep
   back-fills the GRAPH only — it does not republish (only the drain has a publisher) — so data
   landed, graph correct, cascade never ran, nothing red.
3. **`ray_kit.submission_id` collisions.** It folds every character outside `[A-Za-z0-9_-]` to `-` and
   truncates at 200, so two distinct tokens can land on one submission id, which `submit_or_reattach`
   reads as a successful re-attach — the second stage's work silently never runs.

## Delete this file when

C2, C3 and C4 are landed AND verified in-cluster, and §O2's row is struck from
`open_lakehouse_diff_left.md` in the same commit.
