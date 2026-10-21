"""An event lane with no debounce re-evaluates on every commit, and an evaluation is not cheap.

Refreshing a table's due record reads its bucket's `__manifest`, the policy registry and the clone list and opens the
dataset, so a table taking a burst of writes would pay that per write. The debounce is Lakekeeper's
`min-snapshots-to-expire` in rask's own registry: record the version a table was last evaluated at, under the table uuid
the event names (ADR 0173 §8), and do not evaluate again until enough versions have accumulated. It must short-circuit
BEFORE the expensive work, or it saves nothing. A table with no stamp has never been evaluated by this lane, and is.
"""

from __future__ import annotations

import pytest

from maintenance.services.arrival import should_replan
from service_kit.openlineage import DatasetUuids, with_dataset_uuids


TABLE_UUID = "0a6f3c8e-5b1d-4e2a-9c7f-3d8b2e1a4f60"


@pytest.mark.parametrize(
    ("last", "now", "minv", "expected"),
    [
        (10, 19, 10, False),  # nine writes since — still short
        (10, 20, 10, True),  # ten writes since — the threshold is reached
    ],
)
def test_the_threshold_decides_how_many_writes_are_worth_an_evaluation(*, last: int, now: int, minv: int, expected: bool) -> None:
    assert should_replan(last_planned=last, event_version=now, min_versions=minv) is expected


# --- the wiring: the predicate is only worth having if the handler actually short-circuits on it ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "evaluated"),
    [
        ("insert", False),  # one version past the stamp: debounced
        ("compact_table", True),  # a bring-your-own compaction: the refresh is what takes it off the due list
        ("create_index", True),  # an index build or optimize, likewise
    ],
)
async def test_a_debounced_insert_is_never_evaluated_and_a_compaction_always_is(monkeypatch: pytest.MonkeyPatch, *, operation: str, evaluated: bool) -> None:
    """The debounce skips BEFORE the evaluation, or it costs as much as none; a compaction's or an index's commit is the
    one write it never skips, because the table stays on the due list until the next evaluation."""
    from maintenance.api import arrival as route
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter
    from maintenance.services.arrival import Acted

    acted_on: list[str] = []

    def act(location: str, _settings: object) -> Acted:
        acted_on.append(location)
        return Acted(verdict="clear")

    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda _root, _options, table_uuid: 100 if table_uuid == TABLE_UUID else None)
    monkeypatch.setattr(route.maintenance_policies, "write_planned_version", lambda *a: None)
    monkeypatch.setattr(route, "act_on_write", act)
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b", "MAINTENANCE_EVENT_MIN_VERSIONS": 10})
    event = {
        "data": {
            "run": {"facets": {"lance": {"operation": operation}}},
            "outputs": [
                with_dataset_uuids(
                    {"name": "db$t", "facets": {"version": {"datasetVersion": "101"}, "dataSource": {"uri": "s3://b/t.lance"}}},
                    DatasetUuids(table_uuid=TABLE_UUID),
                    producer="test",
                )
            ],
        }
    }
    assert await route.handle_arrival(event, settings, NoopEmitter()) == {"status": route.SUCCESS}
    assert acted_on == (["s3://b/t.lance"] if evaluated else [])
