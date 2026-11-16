"""The stream Job declares the consumer each service binds to, and its retire phase removes only this release's own ([[XC-199]]).

A service takes its deliveries from a durable consumer it never creates: it binds by name to the one the stream Job
declared (`lakehouse.bus.Bus.consume`, `maintenance.services.queue.Worker`), and a name the Job does not declare is a
lane that pulls nothing while its pod stays Ready. So the names the chart declares are held here to the names the
services' own code binds to, in every render that switches a lane on or off, and lineage's lane to the numbers its
settings are given: a consumer whose ack wait is shorter than the one lineage cuts its handling by hands an event to a
second replica beneath a handling still running.

THE RETIRE PHASE SPARES WHAT THE DECLARE PHASE DECLARES, AND KNOWS ITS OWN BY A MARK ([[LH-127]]). It removes a consumer
the chart declares no longer, and it must never remove one the chart still declares, nor one another subscriber made on
the same streams. Both phases therefore take their consumers from one derivation, and every declared consumer carries
the mark the retire phase removes by: a consumer added to one phase and not the other, or declared without the mark,
fails here rather than on the estate. What the two scripts do on a real bus is the `nats-auth` lane's.
"""

from __future__ import annotations

import pytest

from lineage.api import ingest_lane
from lineage.core.config import LineageSettings
from maintenance.services import queue
from tests.unit.chart_render import DEFAULT_ARGS, bus_consumers, containers, env_of, render, retired_consumers


LINEAGE = (ingest_lane.STREAM, ingest_lane.CONSUMER)
QUEUE = {(queue.TASKS_STREAM, queue.TASK_CONSUMER), (queue.SCHEDULES_STREAM, queue.DUE_CONSUMER), (queue.STORE_EVENTS_STREAM, queue.STORE_EVENTS_CONSUMER)}
ARRIVALS = (queue.ARRIVALS_STREAM, queue.ARRIVALS_CONSUMER)


@pytest.mark.parametrize(
    ("flags", "bound"),
    [
        pytest.param((), {LINEAGE, ARRIVALS, *QUEUE}, id="every-lane"),
        pytest.param(("--set", "maintenance.arrivalEnabled=false"), {LINEAGE, *QUEUE}, id="no-write-event-lane"),
        pytest.param(("--set", "maintenance.enabled=false"), {LINEAGE}, id="no-maintenance"),
    ],
)
def test_the_job_declares_the_consumers_the_services_bind_to_and_its_retire_phase_spares_exactly_those(
    flags: tuple[str, ...], bound: set[tuple[str, str]]
) -> None:
    docs = render(*DEFAULT_ARGS, *flags)

    declared = bus_consumers(docs)
    spared, mark = retired_consumers(docs)

    assert declared.keys() == bound
    assert spared == bound
    assert (mark, {key: config["metadata"] for key, config in declared.items()}) == (
        '"rask.dev/release":"rask"',
        {key: {"rask.dev/release": "rask"} for key in bound},
    )


_EVERY_NUMBER_MOVED = (
    *("--set", "services.lineage.delivery.attempts=7"),
    *("--set", "services.lineage.delivery.retrySeconds=60"),
    *("--set", "services.lineage.delivery.ackWaitSeconds=120"),
    *("--set", "services.lineage.deliveriesInFlight=32"),
)


@pytest.mark.parametrize(
    ("flags", "attempts", "retry_seconds", "ack_wait_seconds", "in_flight"),
    [
        pytest.param((), 5, 120, 300, 64, id="the-shipped-numbers"),
        pytest.param(_EVERY_NUMBER_MOVED, 7, 60, 120, 32, id="every-number-moved"),
    ],
)
def test_lineages_consumer_is_declared_with_the_numbers_lineages_lane_is_given(
    flags: tuple[str, ...], attempts: int, retry_seconds: int, ack_wait_seconds: int, in_flight: int
) -> None:
    docs = render(*DEFAULT_ARGS, *flags)
    [rendered] = [env_of(container) for workload, name, container in containers(docs) if workload.endswith("-lineage") and name == "lineage"]
    given = {name: value for name, value in rendered.items() if name.startswith(("LINEAGE_DELIVER", "LINEAGE_EVENTS_TOPIC"))}

    lane = ingest_lane.lane(LineageSettings.model_validate(given))
    consumer = bus_consumers(docs)[lane.stream, lane.consumer]

    assert (lane.attempts, lane.retry_seconds, lane.ack_wait_seconds, lane.in_flight) == (attempts, retry_seconds, ack_wait_seconds, in_flight)
    assert {key: consumer[key] for key in ("ack_wait", "max_ack_pending", "max_deliver", "filter_subject", "deliver_policy")} == {
        "ack_wait": ack_wait_seconds * 1_000_000_000,
        "max_ack_pending": in_flight,
        # No bound: lineage's lane parks a delivery itself, and a park its store did not take is delivered again.
        "max_deliver": -1,
        "filter_subject": given["LINEAGE_EVENTS_TOPIC"],
        "deliver_policy": "by_start_sequence",
    }


def test_each_consumer_begins_where_its_first_declare_must_leave_it() -> None:
    """Where a consumer begins is fixed when it is first declared. The commit lane's begins at the events published from
    then on: a hint for every commit the stream still holds would hold a pass for every table at once. The work queue's
    begin at their streams' first message, since each holds work nobody has taken, and lineage's at a sequence the Job
    reads off the bus."""
    begins = {key: config["deliver_policy"] for key, config in bus_consumers(render(*DEFAULT_ARGS)).items()}

    assert begins == {LINEAGE: "by_start_sequence", ARRIVALS: "new", **dict.fromkeys(QUEUE, "all")}


def test_the_retire_phase_runs_once_the_sync_is_applied_under_either_reconciler() -> None:
    """A consumer removed while the revision before still serves stops a lane there with its pod Ready, so the retire
    phase waits for the sync: a post-upgrade hook to Helm and `PostSync` to Argo CD, where the declare phase is a
    manifest of the sync itself."""
    phases = {
        "retire" if "-nats-stream-retire-" in doc["metadata"]["name"] else "declare": doc["metadata"].get("annotations") or {}
        for doc in render(*DEFAULT_ARGS)
        if doc.get("kind") == "Job" and "-nats-stream-" in doc["metadata"]["name"]
    }

    assert {phase: (annotations.get("helm.sh/hook"), annotations.get("argocd.argoproj.io/hook")) for phase, annotations in phases.items()} == {
        "declare": (None, "Sync"),
        "retire": ("post-install,post-upgrade", "PostSync"),
    }


def test_a_store_the_release_runs_has_a_stream_to_publish_its_events_to_with_maintenance_off() -> None:
    """The development store publishes each object write on `store.events` as the bus user the release issues it whenever
    the store is on. With no stream behind the subject its publishes are refused and its own outbound queue fills, so the
    Job declares STORE_EVENTS (bounded by `maintenance.queue.storeEventsMaxBytes` and a week's age) for a store as well as
    for maintenance's lane, which alone binds a consumer to it."""
    scripts = " ".join(
        " ".join(str(arg) for arg in container.get("command") or [])
        for workload, _, container in containers(render(*DEFAULT_ARGS, "--set", "maintenance.enabled=false"))
        if workload.startswith("Job/rask-nats-stream-r")
    )

    assert "converge_stream STORE_EVENTS" in scripts
