# 0139. Training stays in the lakehouse producer and names no engine (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (CP-044 (2), CP-029, CP-056 #105; `docs/audits/2026-09-30/lakehouse-dataflow.md` weak point 19).

## Context

The training head (`/train`), its trigger consumer and the training plan live in the medallion producer: about 1,047
lines across `api/train.py`, `api/train_outcomes.py`, `services/train.py` and `services/train_plans.py`, plus
`scripts/ray_train_job.py`. The gateway routes `/api/train` and `/api/trains` to medallion
(`services/gateway/src/gateway/__init__.py:238-241`), and the models zone calls them. Training already runs through the
executor port (ADR 0105, `executor_for` at `train_plans.py:45,262`), but `train_plans.py` still names Ray
directly: the `RAY_ENGINE` import (`:44`) and `engine=RAY_ENGINE` (`:250`), and the `ray_executor` import (`:46`) and
its calls (`:317,359,383`). CP-056 (approved 2026-10-05)
makes the lakehouse know no engine.

## Decision

- Training stays in the producer. ADR 0010's single provenance, authorization and storage model for training holds.
- CP-056 step 3 removes the direct engine reference: the training head is enabled when a configured engine registers
  the task `train`, and the plan reaches it only through the executor port.

## Consequences

- No training code moves to the compute plane, and the gateway rows stay.
- The models zone (LOW) keeps working against the same doors.
