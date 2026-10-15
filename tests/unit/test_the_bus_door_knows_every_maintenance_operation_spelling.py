"""The lineage bus door must recognise EVERY spelling of "this was maintenance, not a data write".

Two services emit maintenance runs and each names its own act: maintenance emits ``expire_versions`` for the versions
its sweep reclaims (`services/maintenance/src/maintenance/core/lineage_emit.py`), and the catalog emits
``compact_table`` and ``create_index`` for a bring-your-own job's compaction and index commits through
``version/create`` (`services/catalog/src/catalog/core/lineage_emit.py`). None of them changes a row.

`lineage/api/fga_deps.py` decides which RUNG may record an operation. An operation it does not know falls to the
data-write rule and demands ``can_write_data``, which the maintenance identity is deliberately never granted (owner
ruling 2026-09-08: the sweep gets ``can_maintain`` and never the writer rung).

MEASURED ON THE LIVE ESTATE 2026-09-11: **9 of 9** ``compact_table`` messages on the NATS `LINEAGE` stream were refused
``lineage_event_unauthorized reason='can_write_data required on outputs'``; none reached `lineage_events`, and each is
refused again on every restart replay. The loss is TERMINAL rather than deferred. So this asserts the bus door against
the constants the emitters export, and a spelling added tomorrow fails here instead of in a refusal nobody reads.
"""

from __future__ import annotations

from lineage.api import fga_deps


def test_the_bus_door_accepts_a_maintainer_for_every_maintenance_operation_emitted() -> None:
    """THE GATE, read off the emitters' own constants so a new spelling cannot drift past it."""
    from catalog.core.lineage_emit import COMPACT_TABLE, CREATE_INDEX
    from maintenance.core.lineage_emit import EXPIRE_VERSIONS

    for operation in (EXPIRE_VERSIONS, COMPACT_TABLE, CREATE_INDEX):
        relations = fga_deps.relations_for_operation(operation)
        assert "can_maintain" in relations, (
            f"operation {operation!r} is a maintenance emit, but the bus door offers only {relations} — "
            f"the maintenance identity holds can_maintain and is deliberately never granted can_write_data, so "
            f"this event is refused terminally and its provenance is lost"
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
