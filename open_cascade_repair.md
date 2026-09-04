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

### R5 · The producer authorizes; the MOVER executes

`mover_ops.py` records the shape: the producer authenticates and authorizes, then forwards to the
mover under the service token. The Ray-liveness check **cannot** run on the producer — `to_uri` is
resolved by the mover at run time and `MEDALLION_RAY_CODE_VERSION` is rendered on movers only, and the
submission id needs both. So C2 forwards, exactly as `terminate` does.

## Ordered steps

| # | Step | State |
| --- | --- | --- |
| C1 | **One trigger shape, two producers** — `build_stage_trigger`; the publication head is its first caller, pinned by an AST gate that also refuses a hand-built dict beside the call | ✅ **LANDED** `e1baef1c`, gate strengthened + mutation-proven `921a7c15` |
| C4 | **Surface the silent refusals** — two rules mirroring `MedallionStageDenied` over `medallion_stage_refused_total{lance_medallion_reason}` and `medallion_stage_other_lane_total`, with `for:`, a Perses row, a `routing_disabled` runbook section, and invariant tests binding rule↔counter. Promote L5's `log.debug` ack to a counted refusal when the namespace is tier-shaped. **Chart-only; no new state** | next |
| C3a | **Record the consumed range** — the `lance` run facet carries `from_version`/`to_version` beside the `version` it wrote, gated so the emit cannot stop passing them | ✅ **LANDED** `498b5531`, deployed `c3a-498b5531` and proven live in the mover |
| C3-core | **The lag arithmetic** — pure over injected readers; `known=False` never collapses to 0 | ✅ **LANDED** `a17cd748` |
| C3b | **Make the range QUERYABLE** — lineage emits it but does not expose it. `RunStatus` folds `operation` and `promotion_status` off the lance facet and not the range, so a reader cannot ask "what has silver consumed?". Four sites, following that exact pattern: the event model, the Cypher SET, the row model, the SELECT | **BLOCKS C3** |
| C3 | **The lag gauge** — wire C3-core to the two readers, publish `medallion_cascade_lag{edge,project}` evaluated with `for:`, on a producer cron. Never a per-tick counter or log (row 23's lesson) | after C3b |
| C2 | **The re-run verb** — edge-addressed, `token` optional per R1, forwarded per R5, authorized per R4, 409 per R2, mode PREDICTED per R3 | last |

**C4 depends on `open_alert.md`:** `alerting.enabled` is `false` in `chart/values.yaml` and `true`
only in `values-prod.yaml`, so a new rule is inert on a fresh install and live in this estate's prod.

**C3's home is the PRODUCER**, because only it holds `transform_routes` and can see a first-ever hop —
lineage infers edges from prior runs and is blind there. **Its every-replica answer is convergence**:
read-only and idempotent, so every replica firing is harmless, the same answer the control relay takes.
Recorded here rather than deferred, per the rask-dapr rule.

**C3 is BLIND TO HISTORY, and that is not a bug to fix.** C3a records the range going forward, so the
detector reports `known=False` for every edge whose last run predates its deploy — until each has run
once. An operator must be told that, or the first tick's unknowns read as an outage.

**C3 falsifies five prose sites** that currently say *nothing ever re-reads the `published` tag*
(`publication_trigger.py`, `control_relay.py`, `values.yaml`, `publication.py`). C3 **is** that reader.
Rewrite them in the same commit — falsified prose is rewritten, never annotated.

**Why C2 may precede decoupling's step 6:** it re-mints a trigger behind `build_stage_trigger`, so only
the fresh-token 409 touches the Jobs API — one call to port when `RayJobExecutor` lands.

## Not worth building

A separate DLQ-replay door. Dapr exposes no stream replay, and the optional-`token` verb already
covers the parked class without a second seam.

## Delete this file when

C4, C3 and C2 are landed AND verified in-cluster, and **in the same commit**: §O2's row struck from
`open_lakehouse_diff_left.md`, row 35 (D) struck from `open_estate-verification.md`, and
`open_compute-decoupling.md` §7.4 step 6 marked done. One row, four trackers, one truth.
