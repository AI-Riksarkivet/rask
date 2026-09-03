"""The event lane's DECISION: does this lineage event mean a dataset may need maintenance?

The sweep discovers by walking every bucket every tick — measured at 87 datasets and one manifest open
each, and measured producing `fragments_removed: 0, versions_removed: 0` on every pass since
2026-08-16. That whole-estate walk is what the event lane replaces as the PRIMARY trigger; the cron
stays as an hourly backstop because the bus is provably incomplete (ingest, Ray TRAIN and external
OpenLineage producers emit over HTTP only and never reach the topic, and the catalog's lineage lane has
no outbox, so a lost trigger is silent).

THE EVENT IS A HINT; THE PLAN IS THE DECISION. `build_write_event` carries the table id, the version
and the operation — it carries no fragment count and no row count — so this module answers only "is
this event worth opening the manifest for", and the existing planner answers whether there is work.

Two filters here are rask scar tissue, not theory, and both are silent when wrong:

* **A registration is not an arrival.** `register_table` emits a COMPLETE event indistinguishable, on
  the fields a subscriber matches, from a batch landing — measured in the cascade, where one
  `POST /produce` fired TWO cascades until `ingest_trigger` added the denylist.
* **The loop guard.** Maintenance publishes its OWN completion events onto this same topic
  (`operation=compaction`), and the catalog emits `compact_table` from `/compaction_commit`.
  Unfiltered, compaction triggers compaction, forever — and each pass would look like legitimate work.
"""

from __future__ import annotations

from typing import Any

import pytest

from maintenance.services.arrival import triggering_write


def _event(operation: str, *, name: str = "db$t", version: int | None = 7) -> dict[str, Any]:
    """A lineage event shaped like `catalog.core.lineage_emit.build_write_event` really emits."""
    output: dict[str, Any] = {"name": name, "namespace": "rask"}
    if version is not None:
        output["facets"] = {"version": {"datasetVersion": str(version)}}
    return {
        "eventType": "COMPLETE",
        "run": {"runId": "r-1", "facets": {"lance": {"operation": operation}}},
        "outputs": [output],
    }


@pytest.mark.parametrize("operation", ["insert", "merge_insert", "update", "delete", "create_table"])
def test_a_real_write_names_the_table_and_its_version(operation: str) -> None:
    hit = triggering_write(_event(operation))
    assert hit is not None
    assert (hit.table_id, hit.version) == ("db$t", 7)


@pytest.mark.parametrize("operation", ["register_table", "deregister_table", "declare_table"])
def test_a_BYTE_FREE_catalog_operation_is_not_an_arrival(operation: str) -> None:
    """No bytes landed, so there is nothing to maintain — and acting on it is a measured defect.

    The cascade fired two runs per `/produce` until this denylist existed there; the same event reaches
    this lane and would schedule a manifest open for a table that gained no data.
    """
    assert triggering_write(_event(operation)) is None


@pytest.mark.parametrize("operation", ["compaction", "compact_table"])
def test_MAINTENANCE_S_OWN_EVENTS_DO_NOT_TRIGGER_MAINTENANCE(operation: str) -> None:
    """The loop guard, and it must cover BOTH producers.

    `maintenance.core.lineage_emit` publishes `operation=compaction` on completion; the catalog
    publishes `compact_table` from `/compaction_commit`. Either one, unfiltered, is a cycle that never
    settles — and every turn of it looks like a legitimate maintenance run on the graph.
    """
    assert triggering_write(_event(operation)) is None


def test_an_event_naming_no_output_is_ignored() -> None:
    assert triggering_write({"run": {"facets": {"lance": {"operation": "insert"}}}, "outputs": []}) is None


def test_an_event_with_no_operation_facet_is_ignored() -> None:
    """Fail CLOSED on an event whose operation cannot be read.

    An unreadable operation cannot be checked against either filter, so treating it as a write would
    let exactly the events the loop guard exists to stop through — a cycle is worse than a missed
    trigger the hourly backstop will catch anyway.
    """
    assert triggering_write({"run": {"runId": "r"}, "outputs": [{"name": "db$t"}]}) is None


@pytest.mark.parametrize("malformed", [{}, {"outputs": "not-a-list"}, {"run": "not-a-dict", "outputs": [{"name": "t"}]}, {"outputs": [None]}])
def test_a_MALFORMED_event_is_ignored_rather_than_raising(malformed: dict[str, Any]) -> None:
    """Events arrive off a bus and are client-controlled. A raise here fails the subscription delivery,
    which Dapr then redelivers — turning one malformed publish into a retry loop."""
    assert triggering_write(malformed) is None


def test_a_write_with_no_version_still_triggers() -> None:
    """The version is the debounce input, not the trigger. Absent, the planner still gets to decide —
    losing a real write because a facet was missing is the expensive direction."""
    hit = triggering_write(_event("insert", version=None))
    assert hit is not None and hit.table_id == "db$t" and hit.version is None


def test_the_physical_uri_rides_the_event() -> None:
    """The lane cannot act on a table id alone — this service holds no catalog client by design.

    The catalog stamps the standard `dataSource` facet on every write (`emit_measured_write` derives it
    from the same readback that supplies the version), and that URI is what makes the id openable here
    without a bucket walk.
    """
    event = _event("insert")
    event["outputs"][0]["facets"]["dataSource"] = {"uri": "s3://bucket/abc12345_db$t"}
    hit = triggering_write(event)
    assert hit is not None and hit.location == "s3://bucket/abc12345_db$t"


def test_a_write_from_a_producer_that_stamps_no_uri_still_triggers_but_carries_none() -> None:
    """A producer outside the catalog may emit no `dataSource`. That is a real case, not a defect:
    the hourly backstop reaches those tables by discovery. What must never happen is a GUESSED path —
    a URI nobody confirmed would point maintenance at the wrong object."""
    hit = triggering_write(_event("insert"))
    assert hit is not None and hit.location is None


# --- the route's half: what the subscription does with a decision ------------------------------


def _write_event(uri: str | None = "s3://bucket/abc12345_db$t") -> dict[str, Any]:
    event = _event("insert")
    if uri:
        event["outputs"][0]["facets"]["dataSource"] = {"uri": uri}
    return {"data": event}


@pytest.mark.asyncio
async def test_an_event_this_lane_declines_is_ACKED_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every decline here is a decision, not a shrug — and none of them is retryable.

    A byte-free operation, a loop-guard hit, an event with no URI, or a dataset the planner refuses
    (trashed, policy-disabled, already at target) will all decide the same way on redelivery. The
    hourly backstop re-reaches anything declined in error.
    """
    from maintenance.api import arrival as route
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.work_queue import SUCCESS

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda *a: None)
    monkeypatch.setattr(route, "plan_one", lambda uri, s: None)

    # no dataSource facet -> nothing this service can open (it holds no catalog client)
    assert await route.handle_arrival(_write_event(uri=None), settings, object()) == {"status": SUCCESS}
    # the planner refused (trash / policy / nothing to do)
    assert await route.handle_arrival(_write_event(), settings, object()) == {"status": SUCCESS}
    # the loop guard
    assert await route.handle_arrival({"data": _event("compaction")}, settings, object()) == {"status": SUCCESS}


@pytest.mark.asyncio
async def test_a_publishable_unit_is_enqueued_and_acked(monkeypatch: pytest.MonkeyPatch) -> None:
    from maintenance.api import arrival as route
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.sweep import DatasetPlan, DatasetWorkItem
    from maintenance.services.work_queue import SUCCESS

    published: list[str] = []

    async def fake_enqueue(dapr: object, items: list[Any], **kwargs: object) -> tuple[int, list[str]]:
        published.extend(i.uri for i in items)
        return len(items), []

    # The debounce reads a stamp before planning; stub it so these stay about the ACK decision.
    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda *a: None)
    monkeypatch.setattr(route.maintenance_policies, "write_planned_version", lambda *a: None)
    monkeypatch.setattr(route, "plan_one", lambda uri, s: DatasetWorkItem(uri=uri, plan=DatasetPlan()))
    monkeypatch.setattr(route, "enqueue_units", fake_enqueue)

    answer = await route.handle_arrival(_write_event(), MaintenanceSettings.model_validate({"s3_bucket": "b"}), object())

    assert answer == {"status": SUCCESS}
    assert published == ["s3://bucket/abc12345_db$t"]


@pytest.mark.asyncio
async def test_a_unit_that_could_NOT_be_published_is_RETRIED(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one failure redelivery can fix.

    Acking here would drop a dataset that genuinely needs maintenance and make a sidecar outage look
    identical to an estate with nothing to do — the same reason `enqueue_units` counts its failures
    rather than swallowing them.
    """
    from maintenance.api import arrival as route
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.sweep import DatasetPlan, DatasetWorkItem
    from maintenance.services.work_queue import RETRY

    async def fake_enqueue(dapr: object, items: list[Any], **kwargs: object) -> tuple[int, list[str]]:
        return 0, [i.uri for i in items]

    # The debounce reads a stamp before planning; stub it so these stay about the ACK decision.
    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda *a: None)
    monkeypatch.setattr(route.maintenance_policies, "write_planned_version", lambda *a: None)
    monkeypatch.setattr(route, "plan_one", lambda uri, s: DatasetWorkItem(uri=uri, plan=DatasetPlan()))
    monkeypatch.setattr(route, "enqueue_units", fake_enqueue)

    answer = await route.handle_arrival(_write_event(), MaintenanceSettings.model_validate({"s3_bucket": "b"}), object())
    assert answer == {"status": RETRY}
