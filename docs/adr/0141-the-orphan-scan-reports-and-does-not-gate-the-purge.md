# 0141. The orphan scan reports and does not gate the purge (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (D13, LH-227 #147).

## Context

The trash purge is blocked while the orphan scan reports a file that Lance's cleanup will not reclaim
(`reclaimable_by_lance is not True`, `services/maintenance/src/maintenance/services/reconcile.py:1347-1353`). Measured
2026-10-10: `rask-maintenance` logs `trash_purge_blocked ... 1 finding(s) across ['orphan_files']`, and the report
holds two leftovers from earlier live tests (an LH-202 probe table in `lance-catalog`, and an XC-078 `.txn` in
`acme-bucket`). One unrelated stray file therefore holds every dropped table's purge.

## Decision

- `orphan_files` leaves the purge gate (`purge.py`, ~260-275). The orphan scan stays a report and an alert.
- rask does not build a door that deletes files the format itself will not reclaim.

## Consequences

- A stray file no longer blocks purging. The orphan finding still surfaces, for a person to judge.
- LH-227 is no longer blocked on D13.
