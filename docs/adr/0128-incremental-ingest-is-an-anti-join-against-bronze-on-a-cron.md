# 0128. Incremental ingest is an anti-join against bronze, on a cron (ingest 1c, 2026-08-07)

Source: `docs/architecture/ingest-and-tier-movement.md` (decision 1c and "What landed"), ruled in
`open_ingest_design.md` on 2026-08-07, migrated 2026-08-22.

## Context

Incremental ingest (CDC) has to answer "which source units has bronze not seen yet". The usual answer is a second
store of bookmarks or a change log, which then has to be kept consistent with the table it describes.

## Decision

- At enumerate, ingest anti-joins the source's units against **bronze itself**. No new store: the answer is computed
  from the artifact that must be correct anyway.
- The trigger is a cron at the outer edge: a Dapr cron binding re-runs a configured source; the schedule is chart
  component config, and there is no scheduler thread in the service (`services/ingest/src/ingest/cron.py:1-21`).
- The anti-join's cost is O(existing rows) per tick, bounded by `RASK_INGEST_INCREMENTAL_MAX_ROWS`
  (`services/ingest/src/ingest/config.py:106`). Past the ceiling the run is **refused, never sampled**
  (`services/ingest/src/ingest/workflow.py:158-171`, enforced before the read at `:1123-1133`): truncating an anti-join does not degrade it, it inverts it — a
  partial "already have" set makes the run re-land rows bronze already holds.

## Consequences

- Mechanism and cron both shipped (the cron in `e629e2cc`).
- This is one instance of a rule that recurs: **delta bookkeeping is data, not state.** The anti-join here, the
  `published` tag ([0131](0131-annotations-are-derived-and-readiness-is-the-published-tag.md)) and the publication
  `{from_version, to_version}` range are the same ruling applied three times.
- The default ceiling is `0`, which reads as unbounded (`workflow.py:190-194`; `chart/values.yaml:166`): a live
  default would kill the long harvest the ceiling protects, so an operator sizes it per deployment.
