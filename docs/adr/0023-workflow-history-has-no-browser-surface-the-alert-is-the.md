# 0023. Workflow history has no browser surface — the ALERT is the surface (2026-08-26, owner ruling)

**Decision.** No zone shows how much Dapr workflow history exists, how old it is, or what the retention
policy is, and **none will**. The operator concern is carried by
`DaprWorkflowHistoryNotCollected` + `DaprWorkflowStateMetricsMissing` in `chart/alerting/rules.yml`.
Extends the 2026-07-23 UI-operability boundaries above; same reasoning, later question.

**Why.** The estate's answer to an operator concern is an alert, not a page — a dashboard rendering a
gauge nobody acts on is a surface to maintain, not a feature. The question "is retention working?" is
answered continuously by a rule; a page would answer it only when someone remembered to look, which is
precisely the state this work replaced (both measurements the estate had of workflow-history volume —
1367 rows on 2026-08-10, 7239 on 2026-08-26 — happened because a person went looking).

**What was considered and also dropped.** A per-run line on `compute/ingest/[run_id]` stating how long
THAT run's history survives given its terminal state. Defensible — it is a per-run fact on a page that
already exists, not a new observability surface — and dropped as a nice-to-have nothing depends on. If
it is ever wanted, the values are `dapr.workflowRetention` in `chart/values.yaml` (168h completed,
720h failed/terminated) and the page already renders that run's terminal state.

**Where the reasoning lives now.** `open_workflow_retention.md` was deleted with this ruling; nothing
was lost, because each durable part had already been written to where it is enforced:
`chart/templates/observability.yaml` (the policy, why there is no application-side purge, and what
actually enforces it — a per-app `retentioner` actor on a scheduler reminder, NOT the scheduler);
`chart/templates/otel-collector.yaml` (the measurement, and why the documented `wf-history-` key prefix
matches **zero** rows in the Postgres state store); `chart/alerting/rules.yml` (why the rule is on AGE
rather than volume, and why the `absent()` companion is not decoration);
`docs/runbooks/RUNBOOK-oncall.md#workflow-history-not-collected` (diagnose + purge procedure); and four
gates in `tests/unit/test_invariants.py` binding rule ↔ receiver ↔ threshold ↔ key shape.

**The lifecycle controls are now verified on BOTH paths (2026-08-26).** They shipped in `4e44584c`
proven only on the DISABLED path, because no live ingest run existed to click Terminate on. Closed by
starting one: `acme/u2verify` over `acme-bucket`, run `c146ea3b`, started through the ETL form as a
signed-in user rather than by curl.

Every link exercised in the browser: Terminate and Pause rendered ENABLED on a RUNNING run (Resume
disabled, with its reason) → click → the door's own 202 wording appeared verbatim ("further scheduling
stops, but work already in flight may still complete, so this is not immediate") → the state flipped
RUNNING → FAILED **with no manual reload**, which is the single-flight `.refresh()` doing its job → all
three buttons re-rendered disabled with correct new reasons. The run recorded `terminated by operator
with 6636 units enumerated`. Zero console messages.

**No provisioning or grant was needed, and the earlier attempt failed for a reason worth recording.**
It targeted project `demo` — which is `RASK_INGEST_SERVICE_PROJECT`, the SERVICE-token project, not a
tenant. Its bronze namespace does not exist and the signed-in user is not its admin, so the run died on
`namespace 'demo-bronze' is not provisioned` and the UI could not read it. The estate already had five
projects the user administers with provisioned bronze namespaces. The lesson is the diagnosis: two
distinct 403s (cross-project service token, and a user lacking `can_administer`) plus a missing
namespace, all reachable from one wrong project name.

**One legibility finding, not fixed.** An operator-terminated run lands in `FAILED`; the ingest model
has no `TERMINATED` terminal state (`TERMINAL = ['COMPLETE', 'COMPLETE_WITH_ERRORS', 'FAILED']`). The
REASON is honest and on screen — `terminated by operator with …` — but in a run LIST a deliberate stop
is indistinguishable from a crash without opening it. Worth a distinct state; not changed here.
