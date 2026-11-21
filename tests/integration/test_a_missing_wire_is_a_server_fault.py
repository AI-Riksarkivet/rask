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

from __future__ import annotations

import io
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import InternalError

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
