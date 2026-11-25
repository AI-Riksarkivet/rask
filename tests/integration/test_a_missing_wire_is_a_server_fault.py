"""A door or a tick whose service lacks a piece its lifespan wires faults, never acts as though it were off ([[XC-219]]).

Each service's lifespan always sets what its doors and ticks read on ``app.state``, to a working object or to the
explicit off value its settings name (`catalog.main._build`, `lineage.main._build`): an off emitter or vendor, or the
``None`` it sets for a process named no bus, a catalog that does not sign, a lineage named no graph backend. Off is
what the setting says, never a missing wire (the rule `catalog.api.authz.decisions.wired` states for the authorization client,
[[LH-237]]): a door that read a missing emitter as "off" would commit, announce to nobody and answer 200; a signing gate
that read a missing key holder as "does not sign" would admit every write it exists to hold; a relay that read it so
would deliver staged events unverified. So each door depending on one refuses: 500 with the spec's ``InternalError``
(code 18, `lance_docs/ns_catalog/namespace/operations/errors.md`), and an ERROR line naming the wire, since the
caller's body is redacted. A tick, or a background task, raises instead, so its timer counts the turn failed or its
supervisor ends the process. Driven against the real apps with their lifespans run, one wire taken away at a time.
"""

import asyncio
import io
import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import ConcurrentModificationError, DescribeNamespaceRequest, InternalError, LanceNamespace, connect

from catalog.core.config import Settings
from lakehouse.records import maintenance_policies
from lakehouse.records.record_store import put_record, record_key
from tests.integration.lineage_app import live_catalog, live_lineage, run_event, served_relay_tick


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
NAMESPACE, TABLE = "lance_catalog$wire", "lance_catalog$wire$t"
#: The spec's InternalError.
_INTERNAL = (500, 18)


def _rows() -> bytes:
    sink = io.BytesIO()
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _create_namespace(client: TestClient) -> httpx.Response:
    return client.post(f"/v1/namespace/{NAMESPACE}$child/create", json={})


def _declare(client: TestClient) -> httpx.Response:
    return client.post(f"/v1/table/{NAMESPACE}$declared/declare", json={})


def _count_rows(client: TestClient) -> httpx.Response:
    return client.post(f"/v1/table/{TABLE}/count_rows", json={})


def _poll_events(client: TestClient) -> httpx.Response:
    return client.get("/management/v1/events")


def _faulted(caplog: pytest.LogCaptureFixture, wire: str) -> bool:
    return any(record.levelno == logging.ERROR and getattr(record, "wire", None) == wire for record in caplog.records)


@pytest.mark.parametrize(
    ("wire", "door"),
    [
        pytest.param("control_emitter", _create_namespace, id="control-emitter"),
        pytest.param("lineage_emitter", _declare, id="lineage-emitter"),
        pytest.param("vendor", _declare, id="vendor"),
        pytest.param("connections", _count_rows, id="connections"),
        # The signing gate's holder and commit id key holder, and the settings the read-only window and the signing gate
        # read: a write the catalog would announce is refused, never admitted unsigned or outside a window it cannot see.
        pytest.param("signing", _create_namespace, id="signing-holder"),
        pytest.param("commit_ids", _create_namespace, id="commit-id-key-holder"),
        pytest.param("settings", _create_namespace, id="settings"),
        pytest.param("bus", _poll_events, id="bus"),
    ],
)
def test_a_door_refuses_as_a_server_fault_when_its_wire_is_missing(
    real_ns_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    wire: str,
    door: Callable[[TestClient], httpx.Response],
) -> None:
    assert real_ns_client.post(f"/v1/namespace/{NAMESPACE}/create", json={}).status_code == 200
    created = real_ns_client.post(f"/v1/table/{TABLE}/create", content=_rows(), headers=ARROW)
    assert created.status_code == 200, created.text
    monkeypatch.delattr(real_ns_client.app.state, wire)

    with caplog.at_level(logging.ERROR):
        answer = door(real_ns_client)

    assert answer.status_code == _INTERNAL[0], answer.text
    assert answer.json()["code"] == _INTERNAL[1], answer.text
    assert _faulted(caplog, wire), caplog.text
    # Refused before the door acted: neither the namespace nor the declared table was made.
    assert real_ns_client.post(f"/v1/namespace/{NAMESPACE}$child/exists", json={}).status_code == 404
    assert real_ns_client.post(f"/v1/table/{NAMESPACE}$declared/exists", json={}).status_code == 404


def _control_relay_tick(app: FastAPI) -> Awaitable[object]:
    from catalog.api import control_relay
    from catalog.api.ticks import Tick
    from catalog.core.config import get_settings

    return control_relay.tick(Tick(app, get_settings()))


def _estate_namespaces(app: FastAPI) -> Awaitable[object]:
    from catalog.main import _ensure_estate_namespaces

    return _ensure_estate_namespaces(app)


@pytest.mark.parametrize(
    ("wire", "work"),
    [
        # Read as "does not sign", the relay would deliver every staged control event unverified.
        pytest.param("signing", _control_relay_tick, id="control-relay-signing-holder"),
        pytest.param("bus", _control_relay_tick, id="control-relay-bus"),
        # Read as authorization off, the provenance namespace would be made with no grant for lineage to write under.
        pytest.param("fga", _estate_namespaces, id="estate-namespaces-authorization-client"),
    ],
)
def test_a_tick_fails_when_its_wire_is_missing(
    real_ns_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    wire: str,
    work: Callable[[FastAPI], Awaitable[object]],
) -> None:
    from catalog.core.config import get_settings

    app = real_ns_client.app
    assert isinstance(app, FastAPI)
    assert real_ns_client.portal is not None
    # A control outbox to drain, so a relay that read the missing wire as off would run its whole pass.
    monkeypatch.setenv("LANCE_CONTROL_OUTBOX_URI", f"file://{tmp_path}/control-outbox")
    get_settings.cache_clear()
    monkeypatch.delattr(app.state, wire)

    async def run() -> object:
        return await work(app)

    with caplog.at_level(logging.ERROR), pytest.raises(InternalError):
        real_ns_client.portal.call(run)

    assert _faulted(caplog, wire), caplog.text


#: The provenance namespace the estate-namespaces task keeps, with the retention the chart declares by default.
_PROVENANCE = {"provenance_namespace": "lance_catalog$provenance", "provenance_retain_versions": 20}
#: What a `dir` connection that has opened its index answers when the store throttles it: the backend's Debug form of
#: the store's 429 under the spec's InternalError. Measured 2026-11-22 on pylance 14.0.3, a proxy in front of moto
#: answering every request 429 `TooManyRequests` after the connection made the estate warehouse's row.
_OPENED_CONNECTION_THROTTLED = (
    'Internal error: Failed to check dataset staleness: IO { source: Generic { store: "S3", source: RetryError(RetryErrorImpl { method: HEAD, '
    "uri: Some(http://127.0.0.1:37297/lance-moto/root429/__manifest/_versions/18446744073709551613.manifest), retries: 0, max_retries: 0, "
    'elapsed: 546.224µs, retry_timeout: 5s, inner: Status { status: 429, body: Some("") } }) }, location: Location { file: '
    '"/home/runner/work/lance/lance/rust/lance-table/src/io/commit.rs", line: 933, column: 29 }, backtrace: MaybeBacktrace }'
)


class _Tries:
    """The task's failed-tries counter, holding what each add counted; ``repair`` runs at the ``repair_at``-th counted try,
    as an operator would."""

    def __init__(self, repair: Callable[[], object] = lambda: None, *, repair_at: int = 1) -> None:
        self.counted: list[dict[str, str]] = []
        self._repair = repair
        self._repair_at = repair_at

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        if amount:
            self.counted.append(attributes)
            if len(self.counted) == self._repair_at:
                self._repair()


def _declaring_provenance(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The app's settings, naming the provenance namespace and its retention, as the task reads them."""
    declared = app.state.settings.model_copy(update=_PROVENANCE)
    assert isinstance(declared, Settings)
    monkeypatch.setattr(app.state, "settings", declared)
    return declared


def _held_tries(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch, tries: _Tries) -> Callable[[], object]:
    """Run the estate-namespaces task on ``real_ns_client``'s app with no wait between tries, counting into ``tries``."""
    from catalog import main

    app = real_ns_client.app
    assert isinstance(app, FastAPI)
    portal = real_ns_client.portal
    assert portal is not None
    monkeypatch.setattr(main, "_PROVENANCE_RETRY_FIRST_SECONDS", 0.0)
    monkeypatch.setattr(main, "_failed_tries", tries)

    async def run() -> object:
        return await main._ensure_estate_namespaces(app)

    return partial(portal.call, run)


def _the_apps_own_run_has_ended(real_ns_client: TestClient) -> None:
    """Wait for the estate-namespaces run the app's lifespan started as a background task of that name."""
    portal = real_ns_client.portal
    assert portal is not None

    async def ended() -> None:
        async with asyncio.timeout(30):
            while any(task.get_name() == "estate-namespaces" and not task.done() for task in asyncio.all_tasks()):
                await asyncio.sleep(0.01)

    portal.call(ended)


def _provenance_uuid(settings: Settings) -> str:
    properties = connect("dir", {"root": settings.root}).describe_namespace(DescribeNamespaceRequest(id=["lance_catalog", "provenance"])).properties or {}
    return properties["rask.uuid"]


def _logged(caplog: pytest.LogCaptureFixture) -> list[tuple[str, bool]]:
    """Each ``provenance_namespace_error`` line's kind, and whether it carries a traceback."""
    return [(getattr(record, "kind", ""), record.exc_info is not None) for record in caplog.records if record.getMessage() == "provenance_namespace_error"]


def test_the_estate_namespaces_task_keeps_the_namespace_another_replica_created_first(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two replicas booting on a fresh store race on the create: the `dir` backend answers the loser that the namespace
    "was concurrently created by another operation" (measured 2026-11-22, pylance 14.0.3 over moto, 10 of 10 rounds), and
    the loser keeps the namespace the winner made, as it keeps one that already exists."""
    from catalog.core import native

    app = real_ns_client.app
    assert isinstance(app, FastAPI)
    settings = _declaring_provenance(app, monkeypatch)
    tries = _Tries()
    task = _held_tries(real_ns_client, monkeypatch, tries)
    task()
    created = _provenance_uuid(settings)
    keep, raced = native.call, []

    def losing_the_race(ns: LanceNamespace, method_name: str, *args: object) -> object:
        if method_name == "create_namespace" and not raced:
            raced.append(method_name)
            raise ConcurrentModificationError("Concurrent modification: Object 'lance_catalog$provenance' was concurrently created by another operation")
        return keep(ns, method_name, *args)

    monkeypatch.setattr(native, "call", losing_the_race)

    task()

    assert (raced, tries.counted, _provenance_uuid(settings)) == (["create_namespace"], [], created)


def test_the_estate_namespaces_task_holds_on_a_record_it_cannot_read_until_it_is_repaired(
    real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stored record at the provenance namespace's policy key that is no namespace policy is a fault every replica
    meets on every boot: the task holds, counting each try as a fault and logging the traceback once for the same
    failure, and the replica serves on, with the policy door that repairs the record; once it is removed, the next try
    keeps the declared retention."""
    app = real_ns_client.app
    assert isinstance(app, FastAPI)
    settings = _declaring_provenance(app, monkeypatch)
    root, options = settings.registry_root, settings.storage_options()
    _held_tries(real_ns_client, monkeypatch, _Tries())()
    namespace_uuid = _provenance_uuid(settings)
    put_record(root, options, record_key(maintenance_policies.PREFIX, "namespace", namespace_uuid), record={"kind": "table", "note": "no namespace policy"})
    tries = _Tries(partial(maintenance_policies.delete_policy, root, options, "namespace", key=namespace_uuid), repair_at=2)
    task = _held_tries(real_ns_client, monkeypatch, tries)

    with caplog.at_level(logging.WARNING):
        task()

    kept = maintenance_policies.get_policy(root, options, "namespace", key=namespace_uuid)
    fault = {"lance.catalog.kind": "fault"}
    assert (tries.counted, _logged(caplog), kept.settings.retain_versions if kept else None) == ([fault, fault], [("fault", True), ("fault", False)], 20)


def _answered(status: int, code: str) -> ClientError:
    """The S3 client's report of a store that answered ``status``."""
    answer: Any = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return ClientError(answer, "PutObject")


def _reworded(cause: Exception) -> OSError:
    """A record seam's ``OSError``, chained to the client's own failure as `record_store` chains it."""
    try:
        raise OSError("the store refused writing the record") from cause
    except OSError as reworded:
        return reworded


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        pytest.param(InternalError(_OPENED_CONNECTION_THROTTLED), "outage", id="a-429-on-an-opened-connection"),
        pytest.param(_answered(503, "ServiceUnavailable"), "outage", id="a-503-the-s3-client-reports"),
        pytest.param(ConnectionRefusedError(111, "Connection refused"), "outage", id="a-connection-refused"),
        pytest.param(_reworded(_answered(503, "SlowDown")), "outage", id="a-seam-rewording-a-503"),
        pytest.param(_answered(403, "AccessDenied"), "fault", id="a-403-the-s3-client-reports"),
        pytest.param(
            OSError("LanceError(IO): Generic S3 error: Error performing list request: Server returned non-2xx status code: 403 Forbidden"),
            "fault",
            id="a-403-on-a-fresh-connection",
        ),
    ],
)
def test_the_estate_namespaces_task_counts_a_failed_try_by_whether_a_dependency_answered(
    real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raised: Exception, kind: str
) -> None:
    """A failed try is retried whatever it is, and counted as an outage only where the store or OpenFGA did not answer (no
    response, a 5xx, a 429 rate limit); a store that answered with a refusal is a fault."""
    from catalog.services import warehouses

    keep = warehouses.ensure_catalog_row
    calls: list[str] = []

    def failing_once(ns: LanceNamespace, warehouse_id: str) -> None:
        calls.append(warehouse_id)
        if len(calls) == 1:
            raise raised
        keep(ns, warehouse_id)

    # The app runs this task once at its own start, in the background: only once that run has ended is every call
    # counted below this test's.
    _the_apps_own_run_has_ended(real_ns_client)
    monkeypatch.setattr(warehouses, "ensure_catalog_row", failing_once)
    tries = _Tries()
    task = _held_tries(real_ns_client, monkeypatch, tries)

    with caplog.at_level(logging.WARNING):
        task()

    assert (len(calls), tries.counted, _logged(caplog)) == (2, [{"lance.catalog.kind": kind}], [(kind, True)])


@pytest.mark.parametrize(
    ("wire", "env"),
    [
        pytest.param("bus", {}, id="bus"),
        # The forward client is `None` only where no graph backend is named; one is named here.
        pytest.param("forward_client", {"LINEAGE_FORWARD_URL": "http://graph.invalid"}, id="forward-client"),
    ],
)
def test_lineages_relay_tick_fails_when_its_wire_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, wire: str, env: dict[str, str]
) -> None:
    from lineage.main import app

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with (
        live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url,
        live_lineage(catalog_url, monkeypatch) as lineage_url,
        monkeypatch.context() as unwired,
    ):
        unwired.delattr(app.state, wire)
        with caplog.at_level(logging.ERROR), pytest.raises(InternalError):
            served_relay_tick(lineage_url)

    assert _faulted(caplog, wire), caplog.text


def test_lineages_intake_refuses_as_a_server_fault_without_its_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from lineage.main import app

    run = str(uuid.uuid4())
    event = run_event(run, "COMPLETE", "2026-10-15T10:00:00+00:00", outputs=["lance_catalog$acme-gold$t"])
    with (
        live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url,
        live_lineage(catalog_url, monkeypatch) as lineage_url,
        httpx.Client(base_url=lineage_url, timeout=60) as lineage,
    ):
        with monkeypatch.context() as unwired:
            unwired.delattr(app.state, "provenance")
            with caplog.at_level(logging.ERROR):
                answer = lineage.post("/api/v1/lineage", json=event)
        recorded = [row for row in lineage.get("/events", params={"since": 0}).json()["events"] if row["event"]["run"]["runId"] == run]

    assert (answer.status_code, answer.json()["code"]) == _INTERNAL, answer.text
    assert _faulted(caplog, "provenance"), caplog.text
    assert recorded == []
