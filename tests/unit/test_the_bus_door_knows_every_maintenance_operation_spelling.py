"""The lineage bus door must recognise EVERY spelling of "this was maintenance, not a data write".

Two services emit a compaction and they spell it differently, because each names its own door:
`services/maintenance/src/maintenance/core/lineage_emit.py:56` emits ``compaction`` (the sweep) and
`services/catalog/src/catalog/core/lineage_emit.py:103` emits ``compact_table`` (the on-demand
``/compaction_commit`` door). Both are compactions; neither changes a row.

`lineage/api/fga_deps.py` decides which RUNG may record an operation, and it knew only one of the two.
So a catalog-emitted ``compact_table`` fell through to the data-write rule and demanded
``can_write_data`` — which `service-maintenance` is deliberately never granted, because the owner's
2026-09-08 zero-trust ruling gives the sweep ``can_maintain`` and never the writer rung.

MEASURED ON THE LIVE ESTATE 2026-09-11: **9 of 9** post-gate ``compact_table`` messages on the NATS
`LINEAGE` stream were refused ``lineage_event_unauthorized reason='can_write_data required on
outputs'``; none reached `lineage_events`, and each is refused again on every restart replay. The loss
is TERMINAL rather than deferred — the outbox drops its staged object once the sidecar accepts the
publish, this path does not dead-letter a refusal, and the reconciler excludes `Rewrite` from
provenance holes, so nothing downstream ever notices the gap.

THE DRIFT WAS ALREADY VISIBLE IN THE ESTATE, which is why this gate reads the emitters rather than a
hand-kept list: `maintenance/services/arrival.py:44` names BOTH spellings and has for as long as the
catalog door existed. Two lists, one updated and one not — so this asserts the bus door against the
constants the emitters actually export, and a third emitter added tomorrow fails here instead of in a
refusal nobody reads.
"""

from __future__ import annotations

from lineage.api import fga_deps


def test_the_bus_door_accepts_a_maintainer_for_every_maintenance_operation_emitted() -> None:
    """THE GATE, read off the emitters' own constants so a new spelling cannot drift past it."""
    from catalog.core.lineage_emit import COMPACT_TABLE
    from maintenance.core.lineage_emit import COMPACTION, CREATE_INDEX

    for operation in (COMPACTION, CREATE_INDEX, COMPACT_TABLE):
        relations = fga_deps.relations_for_operation(operation)
        assert "can_maintain" in relations, (
            f"operation {operation!r} is a maintenance emit, but the bus door offers only {relations} — "
            f"the sweep holds can_maintain and is deliberately never granted can_write_data, so this event "
            f"is refused terminally and its provenance is lost"
        )


def test_a_data_write_still_demands_the_writer_rung() -> None:
    """The other half, or the fix above would be a hole rather than a correction.

    Widening the maintenance set must not let a real data write be recorded by a maintainer: a
    maintainer rewrites HOW a dataset is stored, a writer changes WHAT it says, and `model.fga` keeps
    the two apart in both directions.
    """
    from catalog.core.lineage_emit import DELETE, INSERT, MERGE_INSERT, UPDATE

    for operation in (INSERT, UPDATE, DELETE, MERGE_INSERT):
        relations = fga_deps.relations_for_operation(operation)
        assert "can_maintain" not in relations, f"{operation!r} changes rows — a maintainer must not be able to record it as provenance"
        assert "can_write_data" in relations


def test_the_two_maintenance_lists_in_the_estate_agree() -> None:
    """`maintenance/services/arrival.py` filters the SAME class for a different reason (cycle-breaking).

    It named both spellings while the bus door named one, and that divergence is precisely what let the
    catalog's compaction event be refused nine times out of nine. They answer different questions and
    must still agree on which operations are maintenance.
    """
    from maintenance.services.arrival import _MAINTENANCE_OPERATIONS as arrival_ops

    for operation in arrival_ops:
        assert "can_maintain" in fga_deps.relations_for_operation(operation), (
            f"{operation!r} is maintenance to the cascade's arrival filter but a data write to the bus door"
        )
