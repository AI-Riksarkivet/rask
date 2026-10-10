# The medallion cascade — settled decisions

Migrated from `open_medallion_workflow.md` on 2026-08-22, when that working plan was retired. A root
`open_*.md` exists only while work is in progress; these three sections never were — they are rulings,
and two of them say so in their own titles. They live here because the questions
recur and the answers are expensive to re-derive.

**What the plan delivered, for anyone tracing the history.** Its slices S1–S4 (the submit/poll/verify
workflow, `continue_as_new`, the automatic quality split, and the human approval) and §9.1's review
band are implemented and pinned by tests under `services/medallion/tests`. S5 and S6 were audited and
owe no code — S5's defect was closed by reordering rather than by a saga, and S6 is a `stageRunners[]`
declaration rather than a feature; both properties are pinned by
`test_no_rows_without_a_catalog_record.py` and `test_a_same_tier_transform_is_legal.py`, which carry the
full reasoning. §9.2's `lance-ray` rename is an ops scheduling item with no design work owed: it needs
one coordinated rollout because the actor state store cannot hot-reload, and that cost is the same
whenever it happens.

**The three rulings are ADRs.** Each was raised as a defect and closed as correct, and lives in the decision log
with its reasoning:

- §10, the two cascade heads are distinct events and both fire:
  [ADR 0132](../adr/0132-the-two-cascade-heads-are-distinct-events-and-both-fire.md).
- §11, the HTTP heads' trigger rides the caller-retry contract:
  [ADR 0133](../adr/0133-a-synchronous-head-s-trigger-rides-the-caller-retry-contract.md).
- §12, the `stage_run` review and its operator surface, superseded by the plan-based stage run:
  [ADR 0134](../adr/0134-the-stage-workflow-review-and-its-operator-surface-superseded.md).
