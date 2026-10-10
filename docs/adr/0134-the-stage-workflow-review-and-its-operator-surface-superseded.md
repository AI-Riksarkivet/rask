# 0134. The stage workflow review and its operator surface (2026-08-16, superseded by 0088)

Source: `docs/architecture/medallion-cascade.md` §12 (REVIEWED 2026-08-16, corrected 2026-08-22). **Superseded on
2026-10-05 by [0088](0088-a-ray-stage-run-is-a-plan-closed-by-its-job-s-report-or-the.md):** `stage_run` and its
activities are deleted, and a Ray stage is a plan closed by its job's outcome report or a cron sweep. What remains
current is listed under Consequences; the review's verdict on the deleted workflow is kept for its reasoning.

## Context

The Diagrid `review-workflow-{determinism,activity,management}` checklists were run over `services/medallion`, whose
Ray lane then ran each stage as a Dapr workflow, `stage_run`: submit the Ray job, poll it one poll per turn with
`continue_as_new`, and report the outcome.

## Decision

- **Determinism: zero findings.** No unbounded loop (one poll per turn plus `continue_as_new`, the Monitor pattern);
  every workflow-scope log call guarded by `if not ctx.is_replaying`; no clock or env read in workflow scope, with the
  terminal-state literals test-pinned against `ray_kit`'s. State that must survive a turn (`submission_id`,
  `polls_done`) rode the spec, because each turn starts with empty history.
- **Management: no HTTP surface for the workflow**, on the grounds that the plane was trigger-driven, a deterministic
  instance id made Dapr's duplicate-instance answer the dedupe, and the watcher was bounded by `max_polls` and
  terminated itself with three distinguishable exits (`succeeded` / `abandoned` / `unnotified`).
- `DWF-ACT-009` (activities signed as `dict[str, Any]` rather than Pydantic models) was accepted as a warning, to be
  folded into a change that already touched those signatures.

## Consequences

- **The premise of the management verdict did not hold when it was written.** In every deployed estate only
  `abandoned` fired: the submission id was derived twice, with and without `code`, so the watcher polled an id the
  submitter never posted and every run abandoned at its ceiling — a fabricated FAIL over a job that was writing its
  data correctly. `519ea5c4` (2026-08-22) made the submitter return the id it posted and deleted the second
  derivation; the rule that survived is that the run key is derived in exactly one place
  (`packages/service-kit/src/service_kit/lakehouse/work_order.py:211`).
- **The medallion now has an operator surface.** The plan model gave a stage run a durable, addressable record, and
  the doors exist: `GET /stages/{instance_id}` and `POST /stages/{instance_id}/terminate`
  (`services/medallion/src/medallion/api/stage_ops.py:37,46`), the producer's forwarding door
  `/stage-runners/{stage_runner}/stages/{instance_id}[/terminate]` (`api/stage_runner_ops.py:82,111,118`), the
  edge-addressed re-run `POST /stages/rerun` (`api/rerun.py:210`), training's `GET /trains/{instance_id}` and
  `POST /trains/{instance_id}/terminate` (`api/train.py:201,214`), and the promotion decision doors
  (`api/promotions.py:249,277`). The source's "medallion exposes no workflow management endpoints" is no longer true.
- The review's "what would change this" (an operator who knows the Ray job is wrong and wants it stopped, which needs
  the watcher AND the Ray job stopped together) is what plan terminate does: it stops the job through the executor
  port, re-reads the engine's state, and resolves the plan on it
  (`services/medallion/src/medallion/services/stage_plans.py:365-400`).
