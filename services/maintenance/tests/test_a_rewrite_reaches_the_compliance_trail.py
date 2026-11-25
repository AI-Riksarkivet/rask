"""Version expiry reaches the compliance trail, naming its table as the rest of the trail names it.

Expiry deletes superseded manifests and the files only they reference, so a pass that reclaims anything leaves an
`expire_versions` record on the `lance.audit` stream (`governed/audit.py`), keyed on the object the table's grants are
held by (`table:<uuid>`, the one spelling a compliance query joins on) with the table's name beside it. A pass that
reclaimed nothing leaves none: the sweep walks every dataset each tick and most are converged, so a record each would
drown the stream; `_did_material_work` is the rule the lineage emit is gated on too, so the trail and the graph agree
about what counted as work.

The record is operator-global and lives on the trail's retention: it answers "did the estate reclaim anything, when, and
from what", not a tenant's question about its own table's failures, which needs a durable record kind.
"""

import logging

import pytest

from lakehouse.events.lineage_events import DatasetUuids
from lakehouse.governed.audit import AUDIT_LOGGER, configure_audit
from maintenance.services.expiry import DatasetResult
from maintenance.services.table_pass import _record_dataset_outcome


_TABLE_UUID = "5f0c3b1e-9d2a-4c6b-8e7f-1a2b3c4d5e6f"
#: A pass that reclaimed five versions of one table, as the sweep's outcome funnel receives it.
_RECLAIMED = DatasetResult(
    uri="s3://lance-catalog/medallion/bronze",
    table_id="bronze$events",
    uuids=DatasetUuids(table_uuid=_TABLE_UUID),
    old_versions_removed=5,
    bytes_removed=7567,
    duration_seconds=4.25,
)


def _arm(caplog: pytest.LogCaptureFixture) -> None:
    """Arm the trail as a service does. `caplog.set_level` raises the root handler's level and leaves the audit logger's
    own to `configure_audit`, which decides whether a record is made: `caplog.at_level(..., logger=AUDIT_LOGGER)` would
    set that logger to INFO itself and hide a disarmed trail (measured: disabled leaves it at level 51)."""
    caplog.set_level(logging.INFO)
    configure_audit(enabled=True)


@pytest.mark.parametrize(
    ("result", "recorded"),
    [
        pytest.param(_RECLAIMED, [("expire_versions", f"table:{_TABLE_UUID}", "bronze$events")], id="a-pass-that-reclaimed-versions"),
        pytest.param(_RECLAIMED.model_copy(update={"old_versions_removed": 0}), [], id="a-converged-dataset"),
    ],
)
def test_a_pass_reaches_the_trail_by_its_tables_object_only_when_it_reclaimed_something(
    caplog: pytest.LogCaptureFixture, result: DatasetResult, recorded: list[tuple[str, str, str]]
) -> None:
    """Driven through the outcome funnel both of the sweep's callers go through, so the record is the sweep's own."""
    _arm(caplog)

    _record_dataset_outcome(result, subject="service-maintenance")

    trail = [r for r in caplog.records if r.name == AUDIT_LOGGER]
    assert [(getattr(r, "audit.action"), getattr(r, "audit.resource"), getattr(r, "audit.table", None)) for r in trail] == recorded
