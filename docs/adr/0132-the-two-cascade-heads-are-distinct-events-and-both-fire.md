# 0132. The two cascade heads are distinct events, and both fire (2026-08-15)

Source: `docs/architecture/medallion-cascade.md` §10 (DECIDED 2026-08-15 in `open_medallion_workflow.md`, commit
`9af803ce`; migrated 2026-08-22). Raised as a defect ("a table emitting both signals cascades twice") and closed as
correct behaviour. The source's line citations were stale; the ones below are read from the code at this commit, and
the dedupe mechanism is restated for the plan-based stage runs that replaced the Dapr workflow
([0088](0088-a-ray-stage-run-is-a-plan-closed-by-its-job-s-report-or-the.md)).

## Context

Two subscriptions publish the `medallion.bronze` stage trigger, and they derive its correlation token from different
sources:

- `/bronze-arrival` (`services/medallion/src/medallion/services/ingest_trigger.py`) takes it from the bronze-write
  run's `lance.token` facet, falling back to the run id (`_cascade_token`, `ingest_trigger.py:201-214`);
- `/publication-arrival` (`services/medallion/src/medallion/services/publication_trigger.py`) takes it from the
  control event's `event_id` (`build_stage_trigger`, `publication_trigger.py:112-139`).

Different tokens give different run keys, so the two cascades never dedupe against each other. Both run.

## Decision

Both heads fire, because they do not describe the same work:

| | `/bronze-arrival` | `/publication-arrival` |
| --- | --- | --- |
| fires on | a bronze WRITE reaching COMPLETE | a table being PUBLISHED (`table_published`) |
| `dataset` | the dataset actually written, project-qualified | `<tier>$<table>`, the published table |
| range | none; the arrival is the unit | carries `from_version` / `to_version` |

A version range is a concept the ingest head does not have, and the datasets differ. Unifying the token would collide
two legitimate cascades onto one run key, and the second would be answered as a duplicate: one of two pieces of work
that must both happen, silently dropped. It would also conflate two cascades in tracing, which is what the token
exists to keep apart.

**The token distinguishes EVENTS; the run key distinguishes WORK.** The run key is the order's `idempotency_key`,
derived in one place from stage, token, source URI, destination URI and code version
(`packages/service-kit/src/service_kit/lakehouse/work_order.py:211`, called at
`services/medallion/src/medallion/services/stage_submit.py:148`), and it is the plan's action id: a redelivery finds
the plan it already wrote (`services/medallion/src/medallion/services/stage_plans.py:195-222`).

## Consequences

- **Not the fix: token de-duplication at the stage runners.** An adversarial review found that key is not unique per
  legitimate message on a stage runner's own topic: deploying it halts every distributed cascade, because a stage
  runner deliberately receives more than one message per token.
- **What would change this:** a head that publishes a trigger whose `dataset` AND range are identical to another's.
  That IS a duplicate, and the run key correctly dedupes it.
- Not every head goes through a write event: the media head publishes its media-chain trigger directly after the
  bronze landing (`services/medallion/src/medallion/services/media_produce.py:250-253`), so it is a third trigger
  source with its own topic, outside the two-head comparison above.
