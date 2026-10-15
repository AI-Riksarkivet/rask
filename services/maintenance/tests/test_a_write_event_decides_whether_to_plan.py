"""The event lane's DECISION: does this lineage event mean a table's due record may have changed?

THE EVENT IS A HINT; THE EVALUATION IS THE DECISION. `build_write_event` carries the table id, the version and the
operation — no fragment count and no row count — so this module answers only "is this event worth an evaluation", and
the due evaluation answers whether work is due.

**A registration is not an arrival.** `register_table` emits a COMPLETE event indistinguishable, on the fields a
subscriber matches, from a batch landing — measured in the cascade, where one `POST /produce` fired TWO cascades until
`ingest_trigger` added the denylist. **A compaction is.** A bring-your-own job's compaction commits through the catalog
like any other write, and evaluating after it is what takes the table off the due list; the lane writes no version, so
it cannot trigger itself.

THE SIGNATURE DECIDES TOO ([[XC-078]], owner ruling R4). The app token proves only that this pod's own sidecar delivered
an event, so the location a write names is the publisher's claim. The route checks the signature of a write it would
evaluate before evaluating it, driven here through the registered `/maintenance-arrival` with the configuration the chart sets: the
signer's public keys come through the sidecar's secret API (respx), and the root conftest's `EventSigner` builds the
signatures from the wire format alone. Every reason series of the door's refusal counter exists at 0 from the moment the
door is registered, so the alert's `rate()` sees the first refusal as an increase.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable, Iterator, Sequence
from typing import Any, cast, get_args

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lineage_kit.signing import RefusalReason
from maintenance.core.lineage_emit import NoopEmitter
from maintenance.services.arrival import Acted, triggering_write


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


def test_a_COMPACTION_is_an_arrival() -> None:
    """A bring-your-own job's compaction changed the table's fragments, so its due record must be re-evaluated."""
    assert triggering_write(_event("compact_table")) is not None


def test_an_event_naming_no_output_is_ignored() -> None:
    assert triggering_write({"run": {"facets": {"lance": {"operation": "insert"}}}, "outputs": []}) is None


def test_an_event_with_no_operation_facet_is_ignored() -> None:
    """Fail CLOSED on an event whose operation cannot be read: it cannot be checked against the byte-free filter, and
    the sweep re-evaluates the table anyway."""
    assert triggering_write({"run": {"runId": "r"}, "outputs": [{"name": "db$t"}]}) is None


@pytest.mark.parametrize("malformed", [pytest.param({"run": "not-a-dict", "outputs": [{"name": "t"}]}, id="malformed2")])
def test_a_MALFORMED_event_is_ignored_rather_than_raising(malformed: dict[str, Any]) -> None:
    """Events arrive off a bus and are client-controlled. A raise here fails the subscription delivery,
    which Dapr then redelivers — turning one malformed publish into a retry loop."""
    assert triggering_write(malformed) is None


def test_a_write_with_no_version_still_triggers() -> None:
    """The version is the debounce input, not the trigger. Absent, the evaluation still runs."""
    hit = triggering_write(_event("insert", version=None))
    assert hit is not None and hit.table_id == "db$t" and hit.version is None


def test_the_physical_uri_rides_the_event() -> None:
    """The lane acts on the location: the table's id comes from that bucket's `__manifest`, never from the event.

    The catalog stamps the standard `dataSource` facet on every write (`emit_measured_write` derives it from the same
    readback that supplies the version).
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
    """Every decline here is a decision — and none of them is retryable: an event with no URI, a byte-free operation and
    a table no manifest names all decide the same way on redelivery."""
    from maintenance.api import arrival as route
    from maintenance.core.config import MaintenanceSettings

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda *a: None)
    monkeypatch.setattr(route.maintenance_policies, "write_planned_version", lambda *a: None)
    monkeypatch.setattr(route, "act_on_write", lambda *_a: Acted(verdict="unnamed"))

    assert await route.handle_arrival(_write_event(uri=None), settings, NoopEmitter()) == {"status": route.SUCCESS}
    assert await route.handle_arrival({"data": _event("register_table")}, settings, NoopEmitter()) == {"status": route.SUCCESS}
    assert await route.handle_arrival(_write_event(), settings, NoopEmitter()) == {"status": route.SUCCESS}


@pytest.mark.asyncio
async def test_an_evaluation_that_could_NOT_decide_is_RETRIED_and_not_debounced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one failure redelivery can fix, and the debounce stamp is not written for it: a stamp would skip the next
    events against an evaluation that never happened."""
    from maintenance.api import arrival as route
    from maintenance.core.config import MaintenanceSettings

    stamped: list[Any] = []
    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda *a: None)
    monkeypatch.setattr(route.maintenance_policies, "write_planned_version", lambda *a: stamped.append(a))
    monkeypatch.setattr(route, "act_on_write", lambda *_a: Acted(verdict="failed"))

    answer = await route.handle_arrival(_write_event(), MaintenanceSettings.model_validate({"s3_bucket": "b"}), NoopEmitter())

    assert (answer, stamped) == ({"status": route.RETRY}, [])


# --- the door's half: a write is evaluated only when its signature lets the door act --------------


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


type Door = Callable[[str], tuple[TestClient, list[str]]]


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

    The evaluation is stubbed as in the handler tests above: what it decides is not this test's claim, whether the door
    lets the lane reach it is. Built after `signature_counts`, as a pod registers its door after its MeterProvider is
    installed.
    """
    from maintenance.api import arrival as route
    from maintenance.core.config import get_settings

    evaluated: list[str] = []
    monkeypatch.setattr(route.maintenance_policies, "read_planned_version", lambda *a: None)
    monkeypatch.setattr(route.maintenance_policies, "write_planned_version", lambda *a: None)
    monkeypatch.setattr(route, "act_on_write", lambda location, _settings: evaluated.append(location) or Acted(verdict="due"))

    def _door(mode: str) -> tuple[TestClient, list[str]]:
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
        app.state.lineage_emitter = NoopEmitter()
        route.register_arrival_route(app, get_settings())
        return TestClient(app), evaluated

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
    ("mode", "signed_by", "tampered", "evaluated", "counted"),
    [
        pytest.param("enforce", None, False, [], [(REFUSED, "unsigned")], id="enforce-refuses-an-unsigned-write"),
        pytest.param("enforce", OUTSIDER, False, [], [(REFUSED, "signer")], id="enforce-refuses-a-signer-the-chart-does-not-list"),
        pytest.param("enforce", SIGNER, True, [], [(REFUSED, "signature")], id="enforce-refuses-a-location-changed-after-signing"),
        pytest.param("enforce", SIGNER, False, [URI], [], id="enforce-evaluates-a-write-a-listed-delegator-signed"),
        pytest.param("observe", None, False, [URI], [(WOULD_REFUSE, "unsigned")], id="observe-evaluates-an-unsigned-write-and-counts-it"),
    ],
)
def test_a_write_is_evaluated_only_when_the_door_admits_its_signature(
    *,
    arrival_door: Door,
    signature_counts: Callable[[], Counts],
    event_signer: Any,
    respx_allows_unused_routes: None,
    mode: str,
    signed_by: str | None,
    tampered: bool,
    evaluated: list[str],
    counted: list[tuple[str, str]],
) -> None:
    """ENFORCE acks a refusal and evaluates nothing, since no redelivery can sign published bytes; OBSERVE evaluates as
    before and counts what enforcing would refuse."""
    delegator = event_signer(SIGNER)
    respx.get(f"{SECRETS}/signing-public-{SIGNER}").mock(return_value=httpx.Response(200, json={"keys": delegator.public}))
    arrived = _arrived_write()
    if signed_by is not None:
        arrived = (delegator if signed_by == SIGNER else event_signer(signed_by)).sign(arrived, on_behalf_of=AUTHOR)
    if tampered:
        arrived["outputs"][0]["facets"]["dataSource"]["uri"] = "s3://another-tenant/abc12345_db$t"
    client, reached = arrival_door(mode)
    assert signature_counts() == {REFUSED: ZERO_REFUSALS}, "registering the door must create every refusal series at 0, or a first refusal is no increase"

    answered = client.post(
        "/maintenance-arrival",
        json={"id": "ce-1", "specversion": "1.0", "type": "com.dapr.event.sent", "datacontenttype": "application/json", "data": arrived},
        headers={"dapr-api-token": TOKEN},
    )

    expected: Counts = {REFUSED: dict(ZERO_REFUSALS)}
    for name, reason in counted:
        expected.setdefault(name, {})[("maintenance-arrival", reason)] = 1
    assert (answered.status_code, answered.json(), reached, signature_counts()) == (200, {"status": "SUCCESS"}, evaluated, expected)
