"""A door whose catalog lacks a piece its lifespan wires answers a server fault, never a silent success ([[XC-219]]).

The catalog's lifespan always sets its emitters, its credential vendor and its connection registry on ``app.state``,
to a working object or to the explicit off object its settings name (`catalog.main._build`). Off is what the setting
says, never a missing wire (the rule `catalog.api.fga_deps.wired` states for the authorization client, [[LH-237]]): a
door that read a missing emitter as "off" would commit, announce to nobody and answer 200, and no notifier could
repair an event that was never emitted. So each door depending on one refuses: 500 with the spec's ``InternalError``
(code 18, `lance_docs/ns_catalog/namespace/operations/errors.md`), and an ERROR line naming the wire, since the
caller's body is redacted. Driven against the real catalog app with its lifespan run, one wire taken away at a time.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


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


@pytest.mark.parametrize(
    ("wire", "door"),
    [
        pytest.param("control_emitter", _create_namespace, id="control-emitter"),
        pytest.param("lineage_emitter", _declare, id="lineage-emitter"),
        pytest.param("vendor", _declare, id="vendor"),
        pytest.param("connections", _count_rows, id="connections"),
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
    assert [record for record in caplog.records if record.levelno == logging.ERROR and getattr(record, "wire", None) == wire], caplog.text
    # Refused before the door acted: neither the namespace nor the declared table was made.
    assert real_ns_client.post(f"/v1/namespace/{NAMESPACE}$child/exists", json={}).status_code == 404
    assert real_ns_client.post(f"/v1/table/{NAMESPACE}$declared/exists", json={}).status_code == 404
