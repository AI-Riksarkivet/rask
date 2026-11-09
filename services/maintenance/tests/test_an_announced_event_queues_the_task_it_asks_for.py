"""The commit lane queues the task each announced event asks for (ADR 0198 §2): a write's or a registration's pass held a
delay out, one for a whole burst, a drop's release now, and nothing for an event that moves no bytes.

THE EVENT IS A HINT; THE PASS IS THE DECISION. `build_write_event` carries the table, its uuid and its location, and no
fragment or row count, so the lane answers only which task an event is worth, and the pass decides what is due. A pass
is held rather than queued at once, so a busy table is passed at most once per delay, by a pass that reads it after every
commit it covers. Maintenance's own run is announced on the topic the lane reads, and queues nothing, so a pass never
queues itself.

THE SIGNATURE DECIDES TOO ([[XC-078]], owner ruling R4). The app token proves only that this pod's own sidecar delivered
an event, so the location a write names is the publisher's claim. The route checks the signature of an event it would
queue a task for, driven here through the registered `/maintenance-arrival` with the configuration the chart sets: the
signer's public keys come through the sidecar's secret API (respx), and the root conftest's `EventSigner` builds the
signatures from the wire format alone. Every reason series of the door's refusal counter exists at 0 from the moment the
door is registered, so the alert's `rate()` sees the first refusal as an increase. The queue is a recording fake with
`TaskQueue`'s own signatures: the bus contract is the `nats-auth` lane's.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast, get_args
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lakehouse import location_claims
from lakehouse.lineage.signing import RefusalReason
from lakehouse.lineage_events import DatasetUuids, with_dataset_uuids
from maintenance.api import arrival as route
from maintenance.api.dependencies import get_task_queue
from maintenance.core.config import MaintenanceSettings
from maintenance.services.arrival import LaneTask, lane_task
from maintenance.services.queue import QueueUnavailableError, Task, TaskQueue


URI = "s3://bucket/abc12345_db$t"
TABLE_UUID = "6f1c2c1e-5b8a-4f3e-9b1d-2a7c0e9d4b11"


def _event(operation: str, *, uri: str | None = URI, uuid: str | None = TABLE_UUID) -> dict[str, Any]:
    """A lineage event shaped as `catalog.core.lineage_emit.build_write_event` emits it: the table, its uuid and where it lives."""
    output: dict[str, Any] = {"name": "db$t", "namespace": "rask", "facets": {}}
    if uri is not None:
        output["facets"]["dataSource"] = {"name": uri, "uri": uri}
    if uuid is not None:
        output = with_dataset_uuids(output, DatasetUuids(table_uuid=uuid), producer="https://example.test/catalog")
    return {"eventType": "COMPLETE", "run": {"runId": "r-1", "facets": {"rask_lance": {"operation": operation}}}, "outputs": [output]}


class _RecordingQueue:
    """The lane's publishes, with `TaskQueue`'s signatures, recorded; ``down`` refuses each as an unreachable bus does. A
    follow-up already held for a table refuses another, as JetStream's expected-sequence guard does."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.queued: list[Task] = []
        self.followed: list[tuple[Task, datetime]] = []

    async def enqueue(self, task: Task) -> bool:
        if self.down:
            raise QueueUnavailableError("the bus did not take it")
        self.queued.append(task)
        return True

    async def follow_up(self, task: Task, at: datetime) -> bool:
        if self.down:
            raise QueueUnavailableError("the bus did not take it")
        if any(held.uuid == task.uuid for held, _ in self.followed):
            return False
        self.followed.append((task, at))
        return True


def _queue(recording: _RecordingQueue) -> TaskQueue:
    return cast(TaskQueue, recording)


# --- the decision: which task an event asks for ---------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        pytest.param("insert", LaneTask(kind="table", uuid=TABLE_UUID, location=URI), id="a-write-is-passed-now"),
        pytest.param("create_table", LaneTask(kind="table", uuid=TABLE_UUID, location=URI), id="a-create-is-a-write"),
        pytest.param("register_table", LaneTask(kind="table", uuid=TABLE_UUID, location=URI), id="a-registration-is-passed-too"),
        pytest.param("drop_table", LaneTask(kind="trash", uuid=TABLE_UUID), id="a-drop-asks-for-its-release"),
    ],
)
def test_an_event_asks_for_the_task_its_operation_needs(operation: str, expected: LaneTask) -> None:
    assert lane_task(_event(operation)) == expected


@pytest.mark.parametrize("operation", ["declare_table", "deregister_table", "rename_table", "expire_versions"])
def test_an_event_that_moves_no_bytes_asks_for_nothing(operation: str) -> None:
    """A declaration holds no data yet, a deregistration and a rename keep the uuid and location a task is keyed by, and
    ``expire_versions`` is maintenance's own run: queueing on it, the pass would queue itself after every reclaim."""
    assert lane_task(_event(operation)) is None


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_event("insert", uri=None), id="a-write-naming-no-location"),
        pytest.param(_event("drop_table", uri=None, uuid=None), id="a-drop-naming-no-uuid"),
        pytest.param({**_event("insert"), "outputs": []}, id="no-output"),
        pytest.param({**_event("insert"), "run": {"runId": "r-1", "facets": {}}}, id="no-operation-facet"),
        pytest.param({"run": "not-a-dict", "outputs": [{"name": "t"}]}, id="malformed"),
        pytest.param("a CloudEvent's data that is not an object", id="data-that-is-not-an-object"),
    ],
)
def test_an_event_the_lane_cannot_act_on_is_acknowledged_untouched(event: object) -> None:
    """Fails closed: a pass must never be pointed at a location nobody confirmed, and a raise would turn one bad publish
    into a redelivery loop."""
    assert lane_task(event) is None


# --- the handler: what reaches the queue -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_burst_of_commits_holds_one_pass_a_delay_out() -> None:
    """Two writes and a registration hold one pass, ``MAINTENANCE_COMMIT_PASS_DELAY_SECONDS`` out, and queue nothing: the
    held one covers the rest, since its pass reads the table after them. A registration waits as a write does, which an
    undrop needs, since it announces its table before it clears the trash record."""
    queue = _RecordingQueue()
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    delay = timedelta(seconds=settings.commit_pass_delay_seconds)
    before = datetime.now(UTC)

    answers = [await route.handle_arrival({"data": _event(operation)}, settings, _queue(queue)) for operation in ("insert", "insert", "register_table")]

    after = datetime.now(UTC)
    assert (answers, queue.queued) == ([{"status": route.SUCCESS}] * 3, [])
    assert [task for task, _ in queue.followed] == [Task(kind="table", uuid=TABLE_UUID, location=URI)]
    assert before + delay <= queue.followed[0][1] <= after + delay, "the pass was not held the commit delay"


@pytest.mark.asyncio
async def test_a_drop_queues_the_release_of_the_uuid_it_names() -> None:
    queue = _RecordingQueue()

    answer = await route.handle_arrival({"data": _event("drop_table", uri=None)}, MaintenanceSettings.model_validate({"s3_bucket": "b"}), _queue(queue))

    assert (answer, queue.queued) == ({"status": route.SUCCESS}, [Task(kind="trash", uuid=TABLE_UUID)])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uri", "passed"), [pytest.param(URI, True, id="a-location-a-claim-holds"), pytest.param("s3://bucket/held-by-no-claim", False, id="a-location-none-holds")]
)
async def test_a_write_naming_no_uuid_is_passed_as_its_locations_claim_names_it(tmp_path: Path, uri: str, passed: bool) -> None:
    """A producer outside the catalog may name no uuid: the claim at the location names the table, and a location no
    claim holds names no table to pass, which no redelivery changes."""
    queue = _RecordingQueue()
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"}).model_copy(update={"control_root": str(tmp_path / "control")})
    claims = location_claims.ClaimStore(control_root=settings.resolved_control_root, storage_options=settings.storage_options())
    claimed = str(uuid4())
    location_claims.take(claims, URI, "db$t", ["db", "t"], is_gone=lambda _claim: False, uuid=claimed)

    answer = await route.handle_arrival({"data": _event("insert", uri=uri, uuid=None)}, settings, _queue(queue))

    expected = [Task(kind="table", uuid=claimed, location=URI)][: int(passed)]
    assert (answer, [task for task, _ in queue.followed]) == ({"status": route.SUCCESS}, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("queue", "status"),
    [
        pytest.param(lambda: _queue(_RecordingQueue(down=True)), route.RETRY, id="the-bus-did-not-take-it"),
        pytest.param(lambda: None, route.SUCCESS, id="a-stack-that-runs-no-worker"),
    ],
)
async def test_an_announcement_is_delivered_again_only_when_that_can_queue_it(queue: Callable[[], TaskQueue | None], status: str) -> None:
    """A bus that did not take the task is the one failure a redelivery can fix; a stack that runs no worker has nothing to
    queue on and decides the same way on every delivery."""
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})

    answer = await route.handle_arrival({"data": _event("insert")}, settings, queue())

    assert answer == {"status": status}


# --- the door's half: a task is queued only when its signature lets the door act -----------------


SIGNER = "service-catalog"
OUTSIDER = "service-outsider"
AUTHOR = "alice"
TOKEN = "the-estate-app-token"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"
REFUSED = "maintenance.signature.refused"
WOULD_REFUSE = "maintenance.signature.would_refuse"

#: What the signature counters hold: metric name -> (door, reason) -> count.
type Counts = dict[str, dict[tuple[str, str], int]]

#: Every series of the refusal counter, at the 0 it is created with: the door, every reason the kit can refuse for.
ZERO_REFUSALS: dict[tuple[str, str], int] = {("maintenance-arrival", reason): 0 for reason in get_args(RefusalReason.__value__)}


type Door = Callable[[str], tuple[TestClient, _RecordingQueue]]


@pytest.fixture
def signature_counts(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], Counts]]:
    """What the door's signature counters hold, read through a real in-memory reader.

    The counters are made when `maintenance.core.metrics` is imported. Its code is run again into a module object of its
    own under a meter of this reader's provider, and only the two signature counters are swapped onto the imported module,
    so the names the alert reads are the module's own and nothing else is replaced: a reload would hand every importer's
    references a second copy of the module's objects.
    """
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

    from maintenance.core import metrics

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    spec = importlib.util.find_spec(metrics.__name__)
    assert spec is not None and spec.loader is not None, "the metrics module has no loader to run it again with"
    measured = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(otel_metrics, "get_meter", lambda name, *_args, **_kwargs: provider.get_meter(name))
        spec.loader.exec_module(measured)
    for counter in ("_signature_refused", "_signature_would_refuse"):
        monkeypatch.setattr(metrics, counter, getattr(measured, counter))

    def counts() -> Counts:
        found: Counts = {}
        data = reader.get_metrics_data()
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if not metric.name.startswith("maintenance.signature."):
                        continue
                    # A counter only ever yields `NumberDataPoint`, the member of the reader's point union that has a value.
                    for point in cast(Sequence[NumberDataPoint], metric.data.data_points):
                        attributes = point.attributes or {}
                        found.setdefault(metric.name, {})[(str(attributes.get("door")), str(attributes.get("reason")))] = int(point.value)
        return found

    yield counts
    provider.shutdown()


@pytest.fixture
def arrival_door(monkeypatch: pytest.MonkeyPatch, signature_counts: Callable[[], Counts]) -> Iterator[Door]:
    """`arrival_door(mode)`: the registered route, configured through the environment as the chart configures it, and the
    queue it puts tasks on.

    Built after `signature_counts`, as a pod registers its door after its MeterProvider is installed.
    """
    from maintenance.core.config import get_settings

    queue = _RecordingQueue()

    def _door(mode: str) -> tuple[TestClient, _RecordingQueue]:
        for key, value in {
            "APP_API_TOKEN": TOKEN,
            "DAPR_HTTP_PORT": "3500",
            "MAINTENANCE_ARRIVAL_ENABLED": "true",
            "RASK_SIGNATURE_DOORS": mode,
            "RASK_EVENT_SIGNERS": json.dumps([SIGNER, "service-bronze-to-silver"]),
            "RASK_EVENT_DELEGATORS": json.dumps([SIGNER]),
        }.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        app = FastAPI()
        app.dependency_overrides[get_task_queue] = lambda: _queue(queue)
        route.register_arrival_route(app, get_settings())
        return TestClient(app), queue

    yield _door
    get_settings.cache_clear()


def _arrived_write() -> dict[str, Any]:
    """A catalog write as it reaches the lane: the table, its uuid, its physical URI, and the person it was made for."""
    event = _event("insert")
    event["run"]["facets"]["rask_author"] = {"name": AUTHOR, "sub": AUTHOR}
    return event


@respx.mock
@pytest.mark.parametrize(
    ("mode", "signed_by", "tampered", "queued", "counted"),
    [
        pytest.param("enforce", None, False, [], [(REFUSED, "unsigned")], id="enforce-refuses-an-unsigned-write"),
        pytest.param("enforce", OUTSIDER, False, [], [(REFUSED, "signer")], id="enforce-refuses-a-signer-the-chart-does-not-list"),
        pytest.param("enforce", SIGNER, True, [], [(REFUSED, "signature")], id="enforce-refuses-a-location-changed-after-signing"),
        pytest.param("enforce", SIGNER, False, [URI], [], id="enforce-queues-a-write-a-listed-delegator-signed"),
        pytest.param("observe", None, False, [URI], [(WOULD_REFUSE, "unsigned")], id="observe-queues-an-unsigned-write-and-counts-it"),
    ],
)
def test_a_task_is_queued_only_when_the_door_admits_its_signature(
    *,
    arrival_door: Door,
    signature_counts: Callable[[], Counts],
    event_signer: Any,
    respx_allows_unused_routes: None,
    mode: str,
    signed_by: str | None,
    tampered: bool,
    queued: list[str],
    counted: list[tuple[str, str]],
) -> None:
    """ENFORCE acks a refusal and queues nothing, since no redelivery can sign published bytes; OBSERVE queues as before
    and counts what enforcing would refuse."""
    delegator = event_signer(SIGNER)
    respx.get(f"{SECRETS}/signing-public-{SIGNER}").mock(return_value=httpx.Response(200, json={"keys": delegator.public}))
    arrived = _arrived_write()
    if signed_by is not None:
        arrived = (delegator if signed_by == SIGNER else event_signer(signed_by)).sign(arrived, on_behalf_of=AUTHOR)
    if tampered:
        arrived["outputs"][0]["facets"]["dataSource"]["uri"] = "s3://another-tenant/abc12345_db$t"
    client, queue = arrival_door(mode)
    assert signature_counts() == {REFUSED: ZERO_REFUSALS}, "registering the door must create every refusal series at 0, or a first refusal is no increase"

    answered = client.post(
        "/maintenance-arrival",
        json={"id": "ce-1", "specversion": "1.0", "type": "com.dapr.event.sent", "datacontenttype": "application/json", "data": arrived},
        headers={"dapr-api-token": TOKEN},
    )

    expected: Counts = {REFUSED: dict(ZERO_REFUSALS)}
    for name, reason in counted:
        expected.setdefault(name, {})[("maintenance-arrival", reason)] = 1
    reached = [task.location for task, _ in queue.followed]
    assert (answered.status_code, answered.json(), reached, signature_counts()) == (200, {"status": "SUCCESS"}, queued, expected)


@respx.mock
def test_a_door_that_cannot_read_the_signers_keys_asks_for_the_event_again(*, arrival_door: Door, event_signer: Any) -> None:
    """Enforcing, a door that cannot read a signer's keys can neither admit nor refuse the event, so it asks for it again,
    since the read can succeed on a later delivery, and queues nothing meanwhile. Off reads no key: the shared judge
    answers off before it verifies anything (`lakehouse.lineage.door.judge`)."""
    delegator = event_signer(SIGNER)
    keys = respx.get(f"{SECRETS}/signing-public-{SIGNER}").mock(return_value=httpx.Response(503))
    client, queue = arrival_door("enforce")

    answered = client.post(
        "/maintenance-arrival",
        json={
            "id": "ce-2",
            "specversion": "1.0",
            "type": "com.dapr.event.sent",
            "datacontenttype": "application/json",
            "data": delegator.sign(_arrived_write(), on_behalf_of=AUTHOR),
        },
        headers={"dapr-api-token": TOKEN},
    )

    assert (answered.status_code, answered.json(), queue.followed, keys.called) == (200, {"status": "RETRY"}, [], True)
