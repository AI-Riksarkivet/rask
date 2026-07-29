"""#3-B — the multi-base data-base ALLOWLIST gate over the create endpoint (integration).

Reuses the shared ``client`` fixture (mocked namespace backend) and stubs ``dataplane.create_table`` so no
real Lance write happens — the point is the governance decision: an off-allowlist ``data_base`` is a 400
BEFORE any write; an on-allowlist one is threaded through to the write path.
"""

from __future__ import annotations

import io
from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from catalog.core.config import Settings, get_settings
from catalog.services import dataplane
from fastapi.testclient import TestClient
from lance_namespace import CreateTableResponse


ARROW_STREAM = "application/vnd.apache.arrow.stream"


def _arrow() -> bytes:
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _settings() -> Settings:
    return Settings.model_validate({"multibase_data_bases": "s3://ok-bucket", "s3_access_key_id": "x", "s3_secret_access_key": "x"})


def _stub_create(calls: list[dict[str, Any]]) -> Any:
    """A dataplane.create_table stub that records its kwargs (so no real Lance write happens)."""

    def fake(*_a: Any, **k: Any) -> CreateTableResponse:
        calls.append(k)
        return CreateTableResponse(location="s3://x", version=1)

    return fake


def test_off_allowlist_data_base_rejected_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = _settings
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dataplane, "create_table", _stub_create(calls))
    r = client.post(
        "/v1/table/db$t/create?data_base=s3://evil-bucket",
        content=_arrow(),
        headers={"content-type": ARROW_STREAM},
    )
    assert r.status_code == 400, r.text
    assert not calls  # rejected BEFORE any write — a caller can never target an unapproved bucket


def test_on_allowlist_data_base_threaded_through(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = _settings
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dataplane, "create_table", _stub_create(calls))
    r = client.post(
        "/v1/table/db$t/create?data_base=s3://ok-bucket",
        content=_arrow(),
        headers={"content-type": ARROW_STREAM},
    )
    assert r.status_code == 200, r.text
    assert calls and calls[0]["data_bases"] == ["s3://ok-bucket"]


def test_no_data_base_is_unchanged(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = _settings
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dataplane, "create_table", _stub_create(calls))
    r = client.post("/v1/table/db$t/create", content=_arrow(), headers={"content-type": ARROW_STREAM})
    assert r.status_code == 200, r.text
    assert calls and calls[0]["data_bases"] is None  # backward-compatible single-location create
