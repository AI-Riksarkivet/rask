"""The commit lane queues the task each announced event asks for (ADR 0198 §2): a write's or a registration's pass held a
delay out (the bus keeping one for a whole burst), a drop's release now, and nothing for an event that moves no bytes.

THE EVENT IS A HINT; THE PASS IS THE DECISION. `build_write_event` carries the table, its uuid and its location, and no
fragment or row count, so the lane answers only which task an event is worth, and the pass decides what is due. A pass
is held rather than queued at once, so a busy table is passed at most once per delay, by a pass that reads it after every
commit it covers. Maintenance's own run is announced on the topic the lane reads, and queues nothing, so a pass never
queues itself.

THE SIGNATURE DECIDES TOO ([[XC-078]], owner ruling R4). The bus's permission table says who may publish on the topic,
not who wrote a given event, so the location a write names is the publisher's claim. The lane checks the signature of an
event it would queue a task for, driven here through the worker's handling of one delivery's body
(`maintenance.services.arrival.Arrivals.hold_task`) with the configuration the chart sets: the signer's public keys come
from the secret store (the suite's double), and the root conftest's `EventSigner` builds the signatures from the wire
format alone. The handling answers how the delivery is acknowledged, or raises what the worker settles it by
(`maintenance.services.queue.Worker.run_arrivals`, driven on a real broker by the queue's own lane). The queue is a
recording fake with `TaskQueue`'s own signatures: the bus contract is the `nats-auth` lane's.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from tests.secret_store_double import SecretStoreDouble

from lakehouse import location_claims
from lakehouse.cloud_events import lineage_envelope
from lakehouse.lineage_events import DatasetUuids, with_dataset_uuids
from maintenance.core.config import MaintenanceSettings
from maintenance.services.arrival import Arrivals, LaneTask, lane_task
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


def _body(event: dict[str, Any]) -> bytes:
    """What a delivery of ``event`` carries: its envelope on the lineage topic, as the bus holds it."""
    return json.dumps(lineage_envelope(event).as_wire()).encode()


class _RecordingQueue:
    """The lane's publishes, with `TaskQueue`'s signatures, recorded; ``down`` refuses each as an unreachable bus does.
    A follow-up asked for a task that already has one is answered as the bus answers it, not held, and one asked for
    ``replacing`` is held whatever is. That the bus keeps
    the first and refuses the others is its own guard, proven on a real broker by the `nats-auth` lane
    (`tests/e2e-py/test_two_workers_share_the_queue_and_a_dead_ones_task_goes_to_the_other.py`, its burst)."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.queued: list[Task] = []
        self.followed: list[tuple[Task, datetime]] = []

    async def enqueue(self, task: Task) -> bool:
        if self.down:
            raise QueueUnavailableError("the bus did not take it")
        self.queued.append(task)
        return True

    async def follow_up(self, task: Task, at: datetime, *, replacing: bool = False) -> bool:
        if self.down:
            raise QueueUnavailableError("the bus did not take it")
        held_already = any(held == task for held, _ in self.followed)
        self.followed.append((task, at))
        return replacing or not held_already


def _queue(recording: _RecordingQueue) -> TaskQueue:
    return cast(TaskQueue, recording)


# --- the decision: which task an event asks for ---------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        pytest.param("insert", LaneTask(kind="table", uuid=TABLE_UUID, location=URI), id="a-write-asks-for-its-tables-pass"),
        pytest.param("create_table", LaneTask(kind="table", uuid=TABLE_UUID, location=URI), id="a-create-is-a-write"),
        pytest.param("register_table", LaneTask(kind="table", uuid=TABLE_UUID, location=URI, replacing=True), id="a-registration-replaces-the-held-pass"),
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


# --- the handling: what reaches the queue ------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_commit_holds_its_tables_pass_a_delay_out() -> None:
    """Two writes and a registration each ask for the table's pass, ``MAINTENANCE_COMMIT_PASS_DELAY_SECONDS`` out, and
    queue nothing: the bus keeps the first follow-up and refuses the second, which that pass covers, so one pass reads
    the table after them all. A registration waits as a write does and replaces the held follow-up rather than being
    covered by it, which an undrop needs: it announces its table before it clears the trash record, and the older
    follow-up could fire in between (ADR 0232, A6)."""
    queue = _RecordingQueue()
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    arrivals = Arrivals(settings, _queue(queue))
    delay = timedelta(seconds=settings.commit_pass_delay_seconds)
    before = datetime.now(UTC)

    answers = [await arrivals.hold_task(_body(_event(operation))) for operation in ("insert", "insert", "register_table")]

    after = datetime.now(UTC)
    assert (answers, queue.queued) == (["held", "covered", "held"], [])
    assert [task for task, _ in queue.followed] == [Task(kind="table", uuid=TABLE_UUID, location=URI)] * 3
    assert all(before + delay <= at <= after + delay for _, at in queue.followed), "a pass was not held the commit delay"


@pytest.mark.asyncio
async def test_a_drop_queues_the_release_of_the_uuid_it_names() -> None:
    queue = _RecordingQueue()

    answer = await Arrivals(MaintenanceSettings.model_validate({"s3_bucket": "b"}), _queue(queue)).hold_task(_body(_event("drop_table", uri=None)))

    assert (answer, queue.queued) == ("held", [Task(kind="trash", uuid=TABLE_UUID)])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uri", "answer", "passed"),
    [
        pytest.param(URI, "held", True, id="a-location-a-claim-holds"),
        pytest.param("s3://bucket/held-by-no-claim", "ignored", False, id="a-location-none-holds"),
    ],
)
async def test_a_write_naming_no_uuid_is_passed_as_its_locations_claim_names_it(tmp_path: Path, uri: str, answer: str, passed: bool) -> None:
    """A producer outside the catalog may name no uuid: the claim at the location names the table, and a location no
    claim holds names no table to pass, which no redelivery changes."""
    queue = _RecordingQueue()
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"}).model_copy(update={"control_root": str(tmp_path / "control")})
    claims = location_claims.ClaimStore(control_root=settings.resolved_control_root, storage_options=settings.storage_options())
    claimed = str(uuid4())
    location_claims.take(claims, URI, "db$t", ["db", "t"], is_gone=lambda _claim: False, uuid=claimed)

    answered = await Arrivals(settings, _queue(queue)).hold_task(_body(_event("insert", uri=uri, uuid=None)))

    expected = [Task(kind="table", uuid=claimed, location=URI)][: int(passed)]
    assert (answered, [task for task, _ in queue.followed]) == (answer, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "down", "raised", "match"),
    [
        pytest.param(_body(_event("insert")), True, QueueUnavailableError, "the bus did not take it", id="the-bus-did-not-take-it"),
        pytest.param(b'["no", "envelope"]', False, ValueError, "no event envelope", id="a-body-that-is-no-envelope"),
    ],
)
async def test_an_announcement_the_lane_cannot_settle_raises_what_the_worker_settles_it_by(
    body: bytes, down: bool, raised: type[Exception], match: str
) -> None:
    """A bus that did not take the task is a failure a redelivery can fix, so the worker hands the delivery back; a body
    that is no envelope reads the same on every delivery, so the worker ends it at once."""
    queue = _RecordingQueue(down=down)

    with pytest.raises(raised, match=match):
        await Arrivals(MaintenanceSettings.model_validate({"s3_bucket": "b"}), _queue(queue)).hold_task(body)


@pytest.mark.asyncio
async def test_a_claim_the_store_does_not_answer_for_is_a_failure_a_redelivery_can_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that refuses the claim's read, or cannot be reached for it, says nothing about the claim. The lane raises
    the error its worker hands a delivery back on; the store client's own error is none the worker settles, so it would
    end the worker's process on every delivery of the announcement."""

    def refused(_claims: object, _location: str) -> None:
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "reduce your request rate"}}, "GetObject")

    monkeypatch.setattr(location_claims, "holder", refused)
    arrivals = Arrivals(MaintenanceSettings.model_validate({"s3_bucket": "b"}), _queue(_RecordingQueue()))

    with pytest.raises(OSError, match="could not be read"):
        await arrivals.hold_task(_body(_event("insert", uuid=None)))


# --- the signature's half: a task is queued only when its signature lets the lane act --------------


SIGNER = "service-catalog"
OUTSIDER = "service-outsider"
AUTHOR = "alice"
REFUSED = "maintenance.signature.refused"
WOULD_REFUSE = "maintenance.signature.would_refuse"

#: What the signature counters hold: metric name -> (door, reason) -> count.
type Counts = dict[str, dict[tuple[str, str], int]]

type Lane = Callable[[str], tuple[Arrivals, _RecordingQueue]]


@pytest.fixture
def signature_counts(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], Counts]]:
    """What the lane's signature counters hold, read through a real in-memory reader.

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
def commit_lane(monkeypatch: pytest.MonkeyPatch, signature_counts: Callable[[], Counts]) -> Iterator[Lane]:
    """`commit_lane(mode)`: the worker's handling of the commit lane, configured through the environment as the chart
    configures it, and the queue it puts tasks on.

    Built after `signature_counts`, as a worker starts its lane after its MeterProvider is installed.
    """
    from maintenance.core.config import get_settings

    queue = _RecordingQueue()

    def _lane(mode: str) -> tuple[Arrivals, _RecordingQueue]:
        for key, value in {
            "RASK_SIGNATURE_DOORS": mode,
            "RASK_EVENT_SIGNERS": json.dumps([SIGNER, "service-pipeline-a"]),
            "RASK_EVENT_DELEGATORS": json.dumps([SIGNER]),
        }.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return Arrivals(get_settings(), _queue(queue)), queue

    yield _lane
    get_settings.cache_clear()


def _arrived_write() -> dict[str, Any]:
    """A catalog write as it reaches the lane: the table, its uuid, its physical URI, and the person it was made for."""
    event = _event("insert")
    event["run"]["facets"]["rask_author"] = {"name": AUTHOR, "sub": AUTHOR}
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "signed_by", "tampered", "answer", "queued", "counted"),
    [
        pytest.param("enforce", None, False, "refused", [], [(REFUSED, "unsigned")], id="enforce-refuses-an-unsigned-write"),
        pytest.param("enforce", OUTSIDER, False, "refused", [], [(REFUSED, "signer")], id="enforce-refuses-a-signer-the-chart-does-not-list"),
        pytest.param("enforce", SIGNER, True, "refused", [], [(REFUSED, "signature")], id="enforce-refuses-a-location-changed-after-signing"),
        pytest.param("enforce", SIGNER, False, "held", [URI], [], id="enforce-queues-a-write-a-listed-delegator-signed"),
        pytest.param("observe", None, False, "held", [URI], [(WOULD_REFUSE, "unsigned")], id="observe-queues-an-unsigned-write-and-counts-it"),
    ],
)
async def test_a_task_is_queued_only_when_the_lane_admits_its_signature(
    *,
    commit_lane: Lane,
    signature_counts: Callable[[], Counts],
    event_signer: Any,
    secret_store: SecretStoreDouble,
    respx_allows_unused_routes: None,
    mode: str,
    signed_by: str | None,
    tampered: bool,
    answer: str,
    queued: list[str],
    counted: list[tuple[str, str]],
) -> None:
    """ENFORCE acknowledges a refusal and queues nothing, since no redelivery can sign published bytes; OBSERVE queues as
    off does and counts what enforcing would refuse."""
    delegator = event_signer(SIGNER)
    secret_store.hold(f"signing-public-{SIGNER}", {"keys": delegator.public})
    arrived = _arrived_write()
    if signed_by is not None:
        arrived = (delegator if signed_by == SIGNER else event_signer(signed_by)).sign(arrived, on_behalf_of=AUTHOR)
    if tampered:
        arrived["outputs"][0]["facets"]["dataSource"]["uri"] = "s3://another-tenant/abc12345_db$t"
    arrivals, queue = commit_lane(mode)

    answered = await arrivals.hold_task(_body(arrived))

    expected: Counts = {}
    for name, reason in counted:
        expected.setdefault(name, {})[("maintenance-arrival", reason)] = 1
    reached = [task.location for task, _ in queue.followed]
    assert (answered, reached, signature_counts()) == (answer, queued, expected)


@pytest.mark.asyncio
async def test_a_lane_that_cannot_read_the_signers_keys_asks_for_the_event_again(
    *, commit_lane: Lane, event_signer: Any, secret_store: SecretStoreDouble
) -> None:
    """Enforcing, a lane that cannot read a signer's keys can neither admit nor refuse the event, so it raises the error the
    worker hands the delivery back for, since the read can succeed on a later delivery, and queues nothing meanwhile. Off
    reads no key: the shared judge answers off before it verifies anything (`lakehouse.lineage.door.judge`)."""
    delegator = event_signer(SIGNER)
    keys = secret_store.hold(f"signing-public-{SIGNER}", httpx.Response(503))
    arrivals, queue = commit_lane("enforce")

    with pytest.raises(ConnectionError, match="could not be read"):
        await arrivals.hold_task(_body(delegator.sign(_arrived_write(), on_behalf_of=AUTHOR)))

    assert (queue.followed, keys.called) == ([], True)
