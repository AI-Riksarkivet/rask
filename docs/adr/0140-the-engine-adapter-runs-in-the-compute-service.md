# 0140. The engine adapter runs in the compute service (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (CP-054 #363, CP-056 #105 and its reviewed plan, amendment A1).

## Context

CP-056's done condition requires the Ray consumer to deploy from outside the lakehouse image. Its reviewed plan
names app-id `compute`, reached by Dapr service invocation. The worker half of CP-054 no longer exists: stage_run and
train_run are not Dapr workflows (`stage_runner.py`'s lifespan runs no workflow runtime), and medallion's only
workflow is `promotion_review` (`workflow.py:1-5`), which is lakehouse governance.

## Decision

- The Ray engine adapter runs in the existing `compute` service, which already has a Dapr sidecar, an app token, a
  cron binding and a ServiceAccount. Medallion reaches it by Dapr service invocation.
- No dedicated engine deployable is added.

## Consequences

- No new image or deployment, so no added release size.
- CP-054 is answered and closes.
