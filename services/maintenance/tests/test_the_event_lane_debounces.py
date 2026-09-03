"""An event lane with no debounce re-plans on every commit, and a plan is not cheap.

`plan_one` reads two registries and then calls `sibling_base_refs`, which opens EVERY sibling dataset's
manifest in the warehouse to read its `base_paths` — that is the whole-warehouse cost, paid per event.
A table taking a burst of writes therefore drives one full sibling sweep per write, which is the
event-storm failure the design named and did not close.

The debounce is Lakekeeper's `min-snapshots-to-expire` in rask's own registry: record the version a
dataset was last PLANNED at, and do not plan again until enough versions have accumulated. It must
short-circuit BEFORE the expensive work, or it saves nothing.

Absence maintains. A dataset with no stamp has never been planned by this lane, and refusing it would
mean a table is never maintained until something else writes a stamp — the direction that fails silent.
"""

from __future__ import annotations

import pytest

from maintenance.services.arrival import should_replan


def test_a_first_event_for_an_unseen_dataset_plans() -> None:
    """No stamp means never planned. Maintaining is the only safe direction."""
    assert should_replan(last_planned=None, event_version=1, min_versions=10) is True


def test_a_redelivery_of_the_same_version_does_not_replan() -> None:
    """Delivery is at-least-once, so the identical event arrives again. Re-planning it would enqueue a
    duplicate unit for work already queued."""
    assert should_replan(last_planned=7, event_version=7, min_versions=1) is False


def test_an_out_of_order_older_event_does_not_replan() -> None:
    """The bus does not order events. An event describing a version we have already passed says nothing
    new, and acting on it would undo the debounce every time one arrived late."""
    assert should_replan(last_planned=7, event_version=3, min_versions=1) is False


@pytest.mark.parametrize(
    ("last", "now", "minv", "expected"),
    [
        (10, 11, 10, False),  # one write since — far short of the threshold
        (10, 19, 10, False),  # nine writes since — still short
        (10, 20, 10, True),  # ten writes since — the threshold is reached
        (10, 25, 10, True),  # past it
        (10, 11, 1, True),  # threshold of 1 means every write plans
    ],
)
def test_the_threshold_decides_how_many_writes_are_worth_a_plan(last: int, now: int, minv: int, expected: bool) -> None:
    assert should_replan(last_planned=last, event_version=now, min_versions=minv) is expected


def test_an_event_with_no_version_plans() -> None:
    """A producer outside the catalog may stamp no version facet. Without one there is nothing to
    compare, and dropping the event would lose maintenance for every such producer — so it plans, and
    the stamp written afterwards debounces the next one."""
    assert should_replan(last_planned=99, event_version=None, min_versions=10) is True


def test_a_threshold_below_one_is_treated_as_one() -> None:
    """A misconfigured 0 or negative must not mean "never plan" (silently disabling the lane) nor divide
    anything — it means the smallest real threshold, which is every write."""
    assert should_replan(last_planned=5, event_version=6, min_versions=0) is True
    assert should_replan(last_planned=5, event_version=5, min_versions=0) is False


# --- the wiring: the predicate is only worth having if the handler actually short-circuits on it ---


@pytest.mark.asyncio
async def test_a_debounced_event_never_reaches_the_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point is to skip BEFORE `plan_one`, because that is what opens every sibling manifest.
    A debounce that still planned would cost exactly as much as no debounce at all."""
    from maintenance.api import arrival as route
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.work_queue import SUCCESS

    def never(*a: object, **k: object) -> object:
        raise AssertionError("plan_one ran for a debounced event — the short-circuit is in the wrong place")

    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda *a: 100)
    monkeypatch.setattr(route, "plan_one", never)
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b", "MAINTENANCE_EVENT_MIN_VERSIONS": 10})
    event = {
        "data": {
            "run": {"facets": {"lance": {"operation": "insert"}}},
            "outputs": [{"name": "db$t", "facets": {"version": {"datasetVersion": "101"}, "dataSource": {"uri": "s3://b/t.lance"}}}],
        }
    }
    assert await route.handle_arrival(event, settings, object()) == {"status": SUCCESS}


@pytest.mark.asyncio
async def test_the_stamp_is_written_ONLY_when_the_unit_actually_reached_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stamping a unit that failed to publish is the one combination that loses maintenance silently:
    the dataset would be debounced against a plan that never happened, so the next events skip too."""
    from maintenance.api import arrival as route
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.sweep import DatasetPlan, DatasetWorkItem

    stamped: list[Any] = []

    async def failed_publish(*a: object, **k: object) -> tuple[int, list[str]]:
        return 0, ["s3://b/t.lance"]

    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda *a: None)
    monkeypatch.setattr(route.maintenance_policies, "write_planned_version", lambda *a: stamped.append(a))
    monkeypatch.setattr(route, "plan_one", lambda uri, s: DatasetWorkItem(uri=uri, plan=DatasetPlan()))
    monkeypatch.setattr(route, "enqueue_units", failed_publish)

    event = {
        "data": {
            "run": {"facets": {"lance": {"operation": "insert"}}},
            "outputs": [{"name": "db$t", "facets": {"version": {"datasetVersion": "5"}, "dataSource": {"uri": "s3://b/t.lance"}}}],
        }
    }
    await route.handle_arrival(event, MaintenanceSettings.model_validate({"s3_bucket": "b"}), object())
    assert stamped == [], "a failed publish must not debounce the next event"
