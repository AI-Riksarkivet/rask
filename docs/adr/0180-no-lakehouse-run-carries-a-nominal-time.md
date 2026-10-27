# 0180. No lakehouse run carries a nominal time (2026-10-27)

**Status:** accepted. Source: LIN-002 (#170), settled with the owner on 2026-10-27 through the multi-question tool.

## Context

OpenLineage's `NominalTimeRunFacet` (`nominalTime`, with `nominalStartTime` and an optional `nominalEndTime`) is the time
a run was scheduled for, not the time it ran. A consumer schedules and back-fills on it, so a wrong value misleads it
where an absent one does not. No run the lakehouse emits carried the facet, and nothing said whether that was a choice
or a gap.

The lakehouse emits runs from two producers, the catalog and maintenance, and lineage records and forwards runs from
outside producers through its HTTP door. Each run class is triggered by one of three things: a caller's request, a
committed write, or a `bindings.cron` tick.

## Decision

No run the lakehouse emits carries `nominalTime`. The classes, and why none of them has a schedule time:

| Run class | Producer, lane | Trigger | Facet |
| --- | --- | --- | --- |
| A catalog write emitted as a run: every operation `service_kit.openlineage.lifecycle_state` does not call DDL (insert, merge_insert, update, delete, `update_field_metadata`, `restore_table`, `publish_table`, `promote_model`), an overwrite, a create that commits rows, and a DDL change that names an input (a rename's register of its destination, a create derived from another table) | catalog, signed for its caller; `catalog.core.lineage_emit.build_write_event` | the caller's request | none: the run is the request, and nothing scheduled it |
| A DDL change that names no input and commits no rows (a create, declare or register; a drop or deregister; a column, index or schema-metadata change) | catalog, a `DatasetEvent` (LH-492), from the same builder | the caller's request | none: a `DatasetEvent` has no run to carry a run facet |
| A stock client's commit through `version/create` (an append, `compact_table`; its `create_index` is a DDL change and takes the row above) | catalog, signed for its caller (ADR 0157), from the same builder | the client's commit | none: whatever schedule the client keeps is the client's, and the catalog does not know it |
| A re-announced commit (ADR 0177) | catalog's `commit-reconcile` cron, from the same builder | a tick that finds a committed version the feed lacks | none: the tick is when a lost announcement was noticed, and the re-announcement is the original commit's run under its derived run id, so it carries what that run carried |
| `expire_versions`, COMPLETE or FAIL, from the sweep | maintenance's sweep `bindings.cron` (`maintenance.schedule`); `maintenance.core.lineage_emit` | a tick | none: see below |
| `expire_versions` from the arrival lane | maintenance's `/maintenance-arrival`, through the sweep's emitter | a table's write event | none: the run follows a write, and nothing scheduled it |

**The sweep's tick is not a schedule time of the run it emits.** `maintenance.schedule` is `@every 1800s`, an interval
between ticks rather than a wall-clock slot, so no slot exists that a tick was planned for. A
tick that finds a sweep still running, or a signing key unresolved, is skipped and emits nothing. A table gets a run
only when its pass reclaims a version or a file, so the runs of one tick are not a scheduled run per table. A
consumer back-filling on a stamped tick time would act on slots that were never planned. Moving the schedule to a
wall-clock cron and stamping its slot was considered and not taken: it adds a slot computation for a value no reader
of `expire_versions` acts on, and the skipped and no-work ticks would still read as gaps.

**An outside producer's facet is its own.** A pipeline or job posting its runs to lineage's HTTP door may carry
`nominalTime`, since it knows its own schedule. Lineage keeps the run facets it is sent in the event it records, and
the forwarder sends that recorded event (ADR 0158), so it adds no `nominalTime` and strips none.

## Consequences

- A consumer of the feed or the forwarded events reads an absent `nominalTime` on a lakehouse run as "not scheduled",
  which is what each class is.
- The absence is pinned by exact run-facet assertions on each builder's event:
  `tests/unit/test_lineage_emit.py::test_emitted_event_round_trips_into_lineage_model` (`build_write_event`, which every
  catalog run above goes through) and `tests/unit/test_maintenance_lineage.py`'s two round-trip tests (the COMPLETE and
  FAIL builders the sweep and the arrival lane share). A builder that stamps
  the facet fails them, and the change it makes is a change to this decision.
- A run class added later is added to the table above, with its trigger and whether it has a schedule time.
