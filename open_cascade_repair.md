# Cascade repair — make a missed hop DETECTABLE, then REPAIRABLE

Carries `open_lakehouse_diff_left.md` §O2 line 683 (High). **Rewritten 2026-09-04** after a five-agent
audit found the first draft's central recommendation unimplementable from its own inputs; what that
audit confirmed is recorded below as the reasoning, not as prose to re-derive.

> | High | **No cascade reconciler and no re-run verb** — a missed hop is undetectable and unrepairable |

**This row is tracked in four places and all four must end together** — see *Delete this file when*.

## The defect, stated precisely

Not "the trigger is lost" in general: `d58ffaff` (2026-08-31) landed the catalog's control relay
(`api/control_relay.py` + `catalog-control-relay-cron.yaml`, `controlOutbox.enabled: true` by
default), and mover consumers are durable queue-group with a DLQ on `/publication-arrival`. The
residual loss legs, each verified:

| # | Leg | Why it is silent |
| --- | --- | --- |
| L1 | the catalog's commit → `stage_event` window | an ACCEPTED trade (`docs/DECISIONS.md`) — recorded, not new |
| L2 | `handle_publication` RETRY exhaustion | parks on the DLQ **only if** `MEDALLION_DLQ_TOPIC` is set; it defaults `""` |
| L3 | mover-side `maxDeliver` exhaustion | `transform.py`: "no DLQ is configured, so the drop is final" |
| L4 | `_preflight` DROPs | counted, alerted by nothing — **C4** |
| L5 | the not-a-lane ack | `log.debug` + SUCCESS, so a `transform_routes` typo makes every publication a silent no-op |

**Two failure SHAPES, and conflating them was the first draft's central error:**

* **the hop that NEVER RAN** — no workflow instance, therefore no Ray job. A re-run costs full compute
  whatever token it carries.
* **the hop that RAN and lost its wake-up** — the job SUCCEEDED, `publish_stage_ready` never fired.
  Here and only here can a re-run reattach and recompute nothing.

## C3 AND C4 COVER DISJOINT FAILURES — neither substitutes for the other

* **The REFUSAL class** — the trigger ARRIVED and `_preflight` dropped it. Countable by construction:
  `record_refused` fires with a bounded `reason` label. **C4 covers exactly this, and only this.**
* **The LOSS class** — the trigger never arrived, so `_preflight` never ran and **nothing increments**.
  No counter, no log, no DLQ, no lineage event. This is what O2 means by *undetectable*.

A counter cannot see a hop that never happened. **C3 is the only detector for the class O2 is about**
and is not optional; C4 is cheap, disjoint, and closes a real adjacent hole.

## Established by reading the tree

### R1 · A re-run's token is OPTIONAL, and supplying it is what makes the cheap path reachable

The token is the `table_published` event's `event_id` (`uuid4().hex`), which the control outbox drops
on ack and which no durable store retains — so **a verb whose body is `{project, from, to}` cannot
re-mint it**. The first draft made "same token" the default and was unimplementable.

* **`token` supplied** (an operator has it from the DLQ log line, which carries it) → verbatim re-mint
  through `build_stage_trigger` → same deterministic instance id → dedupes for free against a merely
  DELAYED original, and reattaches to a SUCCEEDED job in the ran-and-lost-its-wake-up shape.
* **`token` absent** → a fresh one. Full recompute, a second lineage run node. Honest, and the common
  case for the never-ran shape where nothing exists to reattach to anyway.

At the pinned versions (`dapr-ext-workflow` 1.18.3, runtime 1.18.1) a terminal instance id is freely
reusable — *"reuse_id_policy: Deprecated and has no effect... A workflow instance ID can always be
reused once the existing instance with that ID has reached a terminal state"*. **Docstring-verified,
not observed live**; a sidecar-backed test is owed.

### R2a · The listing R2 prescribes collides with a measured OOM — read before building the 409

R2 says the fresh-token check must "LIST jobs by `(stage, from_uri, to_uri)` rather than look one up
by id". That is right about the KEY and wrong about the cost, and the cost is not theoretical.
`ray_kit.dashboard.list_jobs` records it: Ray's `GET /api/jobs/` **accepts no parameters at all** — no
limit, no offset, no status filter — so it returns every job the cluster has ever seen. Measured on
this estate: **81,155 jobs / 164.7 MB in one response, peaking at 1179 MiB RSS against a 1536 MiB
limit, and 1488 MiB for two concurrent calls. That is an OOMKill**, and it is why `MAX_JOBS` exists.

Putting that call in a request handler is the exact pattern `open_lakehouse_lanes.md` was closed to
remove. Three consequences for C2, none of which the first draft accounts for:

* **the same-token path needs no listing at all.** The id is deterministic, so one `job_status(sub_id)`
  answers — cheap, bounded, and it is the RECOMMENDED repair anyway (R1). Only the fresh-token path
  reaches for a list;
* **the capped listing is sound for THIS question but not obviously so.** A live job is usually recent,
  but a long-running job with many newer submissions behind it falls outside `MAX_JOBS` — so the check
  can answer "no live job" wrongly, in the permissive direction;
* **the key is not available as stated.** `ray_submit` stamps `rask.stage`, `rask.project`,
  `rask.token`, `rask.transform` and `rask.originator` in Ray's `metadata` — not `from_uri`/`to_uri`.
  The reachable key is `(stage, project, transform)`, which names the same edge and is arguably the
  better one, but it is a different check from the one R2 describes.

**DECIDED 2026-09-04: the fresh-token 409 is dropped.** The stage write is `mode="overwrite"`, a Lance
commit that R2 itself calls overwrite-convergent, so two concurrent writers reach a correct final
state and waste only compute. Buying that back with an unbounded listing that has already OOM-killed
this pod — on the path the verb does not even recommend — is the wrong trade. What C2 ships instead:

* **same-token** (the recommended repair): `submit_or_reattach` already answers this for free. A
  duplicate id whose job is RUNNING returns `"reattached"`, so no second job starts and no extra call
  is made;
* **fresh-token**: no liveness check, and the verb's response SAYS SO. An operator re-running with no
  token is asking for a full recompute; being told it may duplicate a live job's work is honest, while
  a 409 derived from a capped listing would be a guarantee the listing cannot make.

### R2 · The 409 is for the FRESH-token path only

A duplicate id whose Ray job is RUNNING returns `"reattached"` (`TERMINAL_BAD = ("FAILED","STOPPED")`),
so the same-token path starts no second job — it is the ideal repair, not a hazard. Only a fresh token
(or a changed `code_version`) yields a different submission id and can race a live job, and the check
must then LIST jobs by `(stage, from_uri, to_uri)` rather than look one up by id. "Wiping the
directory" was wrong: the stage write is `mode="overwrite"`, a Lance commit, and `bronze_arrival.py`
calls it overwrite-convergent.

### R3 · Three horizons, and the verb can only PREDICT its cost

The no-recompute path needs the Ray head to still hold the SUCCEEDED job, and Ray's GCS is not
fault-tolerant here — already recorded at §O2's Owner row. Two further horizons: the Ray id folds
`code_version` while the workflow id does not, so a same-token re-run **after a deploy** is a full
recompute; and the path is decided inside `submit_stage` *after* the verb's 202, so the verb reports a
PREDICTED mode, never the taken one.

### R4 · Authorization is the mover's own rung — retained verbatim from the first draft

Gate on that mover's `fga_required_action` (`can_create_table`, or `can_promote` for silver→gold)
against `namespace:<project>-<toNamespace>`, via `authenticate_subject` plus an explicit check.
`promotions.py` states the reason: `authorize_produce`'s `can_administer` is *"coarser AND different,
and would lock out exactly the non-admin validator the rung exists for."*

**And say so out loud:** the sibling verb `terminate` is gated on `authorize_produce` ("whoever may
start this tenant's pipeline may stop it"). Two verbs on one router will sit on two rungs. That is
defensible — stopping is not re-driving — but it must be written down, not discovered.

### R5 · The producer authorizes; the MOVER executes — SUPERSEDED by R2a

Retained because the reasoning is still right about `terminate` and about why the split exists at all.
It is wrong about C2 for one reason: **its whole case for forwarding was the Ray-liveness check**, and
R2a drops that. With no liveness check the verb needs neither `to_uri` nor `MEDALLION_RAY_CODE_VERSION`
and therefore nothing the mover holds — while the producer already mints stage triggers in its
`table_published` subscription. C2 publishes from the producer.

`mover_ops.py` records the shape: the producer authenticates and authorizes, then forwards to the
mover under the service token. The Ray-liveness check **cannot** run on the producer — `to_uri` is
resolved by the mover at run time and `MEDALLION_RAY_CODE_VERSION` is rendered on movers only, and the
submission id needs both. So C2 forwards, exactly as `terminate` does.

## Ordered steps

| # | Step | State |
| --- | --- | --- |
| C1 | **One trigger shape, two producers** — `build_stage_trigger`; the publication head is its first caller, pinned by an AST gate that also refuses a hand-built dict beside the call | ✅ **LANDED** `e1baef1c`, gate strengthened + mutation-proven `921a7c15` |
| C4 | **Surface the silent refusals** — two rules mirroring `MedallionStageDenied` over `medallion_stage_refused_total{lance_medallion_reason}` and `medallion_stage_other_lane_total`, with `for:`, a Perses row, a `routing_disabled` runbook section, and invariant tests binding rule↔counter. Promote L5's `log.debug` ack to a counted refusal when the namespace is tier-shaped. **Chart-only; no new state** | ✅ **LANDED** `35164269` |
| C3a | **Record the consumed range** — the `lance` run facet carries `from_version`/`to_version` beside the `version` it wrote, gated so the emit cannot stop passing them | ✅ **LANDED** `498b5531`, deployed `c3a-498b5531` and proven live in the mover |
| C3-core | **The lag arithmetic** — pure over injected readers; `known=False` never collapses to 0 | ✅ **LANDED** `a17cd748` |
| C3b | **Make the range QUERYABLE** — lineage emits it but does not expose it. `RunStatus` folds `operation` and `promotion_status` off the lance facet and not the range, so a reader cannot ask "what has silver consumed?". Four sites, following that exact pattern: the event model, the Cypher SET, the row model, the SELECT | ✅ **LANDED** `838a33c5` |
| C3 | **The lag gauge** — wire C3-core to the two readers, publish `medallion_cascade_lag{edge,project}` evaluated with `for:`, on a producer cron. Never a per-tick counter or log (row 23's lesson) | ✅ **LANDED** `b0dec4f0`; the `for:`-evaluated rule + its two promtool cases landed 2026-09-04. **DRIVEN IN-CLUSTER 2026-09-04 and it was NON-FUNCTIONAL** — see below |
| C2 | **The re-run verb** — edge-addressed, `token` optional per R1, authorized per R4 on the edge's own rung, mode PREDICTED per R3 | ✅ **LANDED** — `POST /api/movers/stages/rerun`. Two of the draft's rules did NOT survive contact: the 409 is dropped (R2a), and with it the forward — **R5's only reason to forward was the Ray-liveness check**, so the producer mints the trigger itself, exactly as its `table_published` subscription already does. `build_stage_trigger` now has the two callers its docstring named. |

**C4 depends on `open_alert.md`:** `alerting.enabled` is `false` in `chart/values.yaml` and `true`
only in `values-prod.yaml`, so a new rule is inert on a fresh install and live in this estate's prod.

**C3's home is the PRODUCER**, because only it holds `transform_routes` and can see a first-ever hop —
lineage infers edges from prior runs and is blind there. **Its every-replica answer is convergence**:
read-only and idempotent, so every replica firing is harmless, the same answer the control relay takes.
Recorded here rather than deferred, per the rask-dapr rule.

**C3 is BLIND TO HISTORY, and that is not a bug to fix.** C3a records the range going forward, so the
detector reports `known=False` for every edge whose last run predates its deploy — until each has run
once. An operator must be told that, or the first tick's unknowns read as an outage.

**C3 falsifies TWO prose sites, not five** — counted 2026-09-04 rather than taken on report:
`chart/values.yaml:943` and `services/catalog/src/catalog/api/control_relay.py:10`. Both say *"the
medallion plane runs no cron, so nothing ever re-reads the `published` tag"*.

**And only their PREMISE is falsified.** Each sentence continues *"`/publication-arrival` receiving
this event is the ONLY thing that wakes silver→gold"*, and that stays TRUE: C3 is read-only and
reports lag without repairing it. The durability argument both comments support therefore survives
intact. Rewrite the premise when the cron lands — in that commit, not before, because until then the
claim is still true — and do not delete the conclusion with it.

**Why C2 may precede decoupling's step 6:** it re-mints a trigger behind `build_stage_trigger`, so only
the fresh-token 409 touches the Jobs API — one call to port when `RayJobExecutor` lands.

## Not worth building

A separate DLQ-replay door. Dapr exposes no stream replay, and the optional-`token` verb already
covers the parked class without a second seam.

## Delete this file when

C4, C3 and C2 are landed AND verified in-cluster, and **in the same commit**: §O2's row struck from
`open_lakehouse_diff_left.md`, row 35 (D) struck from `open_estate-verification.md`, and
`open_compute-decoupling.md` §7.4 step 6 marked done. One row, four trackers, one truth.


## C3, driven in-cluster 2026-09-04 — it had never worked

The lag cron ticked cleanly and reported `{"edges":3,"published_points":0,"unknown":0,"failed":3}`.
Three defects, peeled one at a time by calling the readers directly, each hidden by the one in front:

1. **Every edge was named `<source>->?` with an empty project.** A stale deployment, not code: the
   chart renders `MEDALLION_LANE_DESTINATIONS` correctly and rev 99 predates it.
2. **`consumed_reader` asked for a route lineage does not serve.** `/api/v1/runs` → 404. Probed all
   three spellings against the deployed service: `/v1/runs` 404, `/api/v1/runs` 404, `/runs` 401.
   This is the SECOND route-that-does-not-exist in this file's short history — a route is not a thing
   to derive from a prefix convention.
3. **Neither reader sent any credential.** A bare `httpx.get` → **401 on every edge**. Adding the
   obvious pair still 401'd: `service-medallion-producer` is a PRIVILEGED subject, and the door binds
   it to `service-token-<identity>` rather than the estate's shared token. `catalog_register` has
   resolved that since 2026-08-26; the readers were a second hand-written copy of a credential rule.

All three are fixed and both readers now reach **403** — authenticated, and refused on authorization.

**The chain, peeled to the bottom.** Each layer was hidden by the one in front, and every step was
measured against the deployed estate rather than reasoned about:

| # | symptom | cause | state |
| --- | --- | --- | --- |
| 1 | every edge `<source>->?`, empty project | `MEDALLION_LANE_DESTINATIONS` absent | stale deployment; the chart renders it |
| 2 | consumed read 404 | `/api/v1/runs` — lineage serves `/runs` | **fixed** |
| 3 | published read 401 | no credential sent at all | **fixed** |
| 4 | still 401 | shared token where the door demands `service-token-<identity>` | **fixed** — delegates to `catalog_register.credential` |
| 5 | catalog 403 | `can_get_metadata required on table:bronze` — the root-warehouse `reader` grant does not reach the medallion tiers' warehouse | **chart**: the producer joins the bootstrap `readers` list |
| 6 | lineage 403 | `service identity not allowed` — an ALLOWLIST, not FGA | **chart**: the producer joins `LINEAGE_SERVICE_SUBJECTS` |
| 7 | lineage 401 | the dedicated token for this subject does not exist in the store | **BLOCKED** |

**Row 7 is not this file's to close.** `dedicatedServiceCredentials: false` is the estate's current
posture and `open_estate-verification.md` row 35 (B) already tracks it — the same switch that leaves
`LANCE_PRIVILEGED_SUBJECTS` unrendered. C3's last inch waits on that decision, not on this work.

And note how six layers survived: a reader that cannot read publishes NOTHING and reports nothing
wrong, so an empty series reads as a healthy cascade. That is the same failure shape the whole file is
about, committed inside the detector written to catch it.
