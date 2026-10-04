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

THE SIGNATURE DECIDES TOO ([[XC-078]], owner ruling R4). The app token proves only that this pod's own sidecar delivered
an event, so the location a write names is the publisher's claim. The route checks the signature of a write it would plan
before planning it, driven here through the registered `/maintenance-arrival` with the configuration the chart sets: the
signer's public keys come through the sidecar's secret API (respx), and the root conftest's `EventSigner` builds the
signatures from the wire format alone. Every reason series of the door's refusal counter exists at 0 from the moment the
door is registered, so the alert's `rate()` sees the first refusal as an increase.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, cast, get_args

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lineage_kit.signing import RefusalReason
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


@pytest.mark.parametrize("operation", ["insert"])
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


@pytest.mark.parametrize("malformed", [pytest.param({"run": "not-a-dict", "outputs": [{"name": "t"}]}, id="malformed2")])
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


# --- the door's half: a write is planned only when its signature lets the door act ----------------


SIGNER = "service-catalog"
OUTSIDER = "service-annotator"
AUTHOR = "alice"
URI = "s3://bucket/abc12345_db$t"
TOKEN = "the-estate-app-token"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"
REFUSED = "maintenance.signature.refused"
WOULD_REFUSE = "maintenance.signature.would_refuse"

#: What the signature counters hold: metric name -> (door, reason) -> count.
type Counts = dict[str, dict[tuple[str, str], int]]

#: Every series of the refusal counter, at the 0 it is created with: the door, every reason the kit can refuse for.
ZERO_REFUSALS: dict[tuple[str, str], int] = {("maintenance-arrival", reason): 0 for reason in get_args(RefusalReason.__value__)}


class _Sidecar:
    """The sidecar's publish as the work queue calls it: each unit the lane enqueued, as a worker would receive it."""

    def __init__(self) -> None:
        self.units: list[dict[str, Any]] = []

    async def publish_event(
        self,
        pubsub_name: str,
        topic_name: str,
        data: bytes | str,
        publish_metadata: Mapping[str, str] | None = None,
        metadata: tuple[tuple[str, str | bytes], ...] | None = None,
        data_content_type: str | None = None,
    ) -> None:
        self.units.append(json.loads(data))


type Door = Callable[[str], tuple[TestClient, _Sidecar]]


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
    """`arrival_door(mode)`: the registered route, configured through the environment as the chart configures it.

    The planner's reads are stubbed as in the handler tests above: what a plan contains is not this test's claim, whether
    the door lets the lane reach the planner is. The sidecar's publish is recorded, so the real work-queue publish runs.
    Built after `signature_counts`, as a pod registers its door after its MeterProvider is installed.
    """
    from maintenance.api import arrival as route
    from maintenance.core.config import get_settings
    from maintenance.services.sweep import DatasetPlan, DatasetWorkItem

    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda *a: None)
    monkeypatch.setattr(route.maintenance_policies, "write_planned_version", lambda *a: None)
    monkeypatch.setattr(route, "plan_one", lambda uri, s: DatasetWorkItem(uri=uri, plan=DatasetPlan()))

    def _door(mode: str) -> tuple[TestClient, _Sidecar]:
        for key, value in {
            "APP_API_TOKEN": TOKEN,
            "DAPR_HTTP_PORT": "3500",
            "MAINTENANCE_WORK_TOPIC": "maintenance.work.v1",
            "RASK_SIGNATURE_DOORS": mode,
            "RASK_EVENT_SIGNERS": json.dumps([SIGNER, "service-bronze-to-silver"]),
            "RASK_EVENT_DELEGATORS": json.dumps([SIGNER]),
        }.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        app = FastAPI()
        sidecar = _Sidecar()
        app.state.dapr_client = sidecar
        route.register_arrival_route(app, get_settings())
        return TestClient(app), sidecar

    yield _door
    get_settings.cache_clear()


def _arrived_write() -> dict[str, Any]:
    """A catalog write as it reaches the lane: the table, its version, its physical URI, and the person it was made for."""
    event = _event("insert")
    event["run"]["facets"]["author"] = {"name": AUTHOR, "sub": AUTHOR}
    event["outputs"][0]["facets"]["dataSource"] = {"uri": URI}
    return event


@respx.mock
@pytest.mark.parametrize(
    ("mode", "signed_by", "tampered", "planned", "counted"),
    [
        pytest.param("enforce", None, False, [], [(REFUSED, "unsigned")], id="enforce-refuses-an-unsigned-write"),
        pytest.param("enforce", OUTSIDER, False, [], [(REFUSED, "signer")], id="enforce-refuses-a-signer-the-chart-does-not-list"),
        pytest.param("enforce", SIGNER, True, [], [(REFUSED, "signature")], id="enforce-refuses-a-location-changed-after-signing"),
        pytest.param("enforce", SIGNER, False, [URI], [], id="enforce-plans-a-write-a-listed-delegator-signed"),
        pytest.param("observe", None, False, [URI], [(WOULD_REFUSE, "unsigned")], id="observe-plans-an-unsigned-write-and-counts-it"),
    ],
)
def test_a_write_is_planned_only_when_the_door_admits_its_signature(
    arrival_door: Door,
    signature_counts: Callable[[], Counts],
    event_signer: Any,
    respx_allows_unused_routes: None,
    mode: str,
    signed_by: str | None,
    tampered: bool,
    planned: list[str],
    counted: list[tuple[str, str]],
) -> None:
    """ENFORCE acks a refusal and plans nothing, since no redelivery can sign published bytes; OBSERVE plans as before
    and counts what enforcing would refuse."""
    delegator = event_signer(SIGNER)
    respx.get(f"{SECRETS}/signing-public-{SIGNER}").mock(return_value=httpx.Response(200, json={"keys": delegator.public}))
    arrived = _arrived_write()
    if signed_by is not None:
        arrived = (delegator if signed_by == SIGNER else event_signer(signed_by)).sign(arrived, on_behalf_of=AUTHOR)
    if tampered:
        arrived["outputs"][0]["facets"]["dataSource"]["uri"] = "s3://another-tenant/abc12345_db$t"
    client, sidecar = arrival_door(mode)
    assert signature_counts() == {REFUSED: ZERO_REFUSALS}, "registering the door must create every refusal series at 0, or a first refusal is no increase"

    answered = client.post(
        "/maintenance-arrival",
        json={"id": "ce-1", "specversion": "1.0", "type": "com.dapr.event.sent", "datacontenttype": "application/json", "data": arrived},
        headers={"dapr-api-token": TOKEN},
    )

    expected: Counts = {REFUSED: dict(ZERO_REFUSALS)}
    for name, reason in counted:
        expected.setdefault(name, {})[("maintenance-arrival", reason)] = 1
    assert (answered.status_code, answered.json(), [unit["uri"] for unit in sidecar.units], signature_counts()) == (
        200,
        {"status": "SUCCESS"},
        planned,
        expected,
    )
