"""ING-17 — two `except Exception` blocks, each answering with more than it knows.

1. `GET /queue` is a 200-always diagnostic, and its failure branch put `f"{type(exc).__name__}: {exc}"`
   into the response body. For a NATS failure that string carries the broker URL, and for a DNS or
   TLS failure it carries whatever the client library felt like including. The route is reachable by
   anyone who can reach the port; the operator's diagnosis belongs in the log, where it already is.

2. `LocalCatalog.ensure_at` decided a dataset was ABSENT from `except Exception` around
   `lance.dataset(uri)`. Absent is one reason that raises. A credential that has not landed yet, a
   store that is briefly unreachable, a permission error — every one of those read as "not there",
   and the answer to "not there" is `create_empty`, which is the one operation that must never run
   over a dataset that does exist.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ingest.queue_health import router


SCHEMA = pa.schema([pa.field("id", pa.string()), pa.field("payload", pa.large_binary())])


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_queue_probe_does_not_put_the_broker_url_in_its_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(url: str, timeout: float) -> dict[str, Any]:
        raise ConnectionRefusedError(f"nats: no servers available for connection to {url}")

    monkeypatch.setattr("ingest.queue.inspect_queue", _boom)
    monkeypatch.setenv("RASK_NATS_URL", "nats://secret-broker.internal:4222")

    res = _client().get("/queue")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reachable"] is False
    assert "secret-broker.internal" not in res.text, f"the probe leaked its upstream address: {res.text}"
    assert body["detail"], "the probe must still say SOMETHING went wrong"
    assert "ConnectionRefusedError" in body["detail"], "the failure CLASS is the useful, non-leaking half"


def test_the_queue_probe_still_names_a_timeout_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The timeout branch was already safe; the redaction must not swallow it."""

    async def _hang(url: str, timeout: float) -> dict[str, Any]:
        raise TimeoutError

    monkeypatch.setattr("ingest.queue.inspect_queue", _hang)
    body = _client().get("/queue").json()
    assert "did not answer within" in body["detail"]


def test_a_dataset_that_cannot_be_READ_is_not_treated_as_ABSENT(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dangerous half. `create_empty` over a live dataset is the one write that cannot be undone."""
    from ingest.catalog import LocalCatalog

    created: list[str] = []

    def _denied(uri: str, *_a: object, **_k: object) -> object:
        raise PermissionError("403 forbidden: the S3 credential has not landed yet")

    def _create(uri: str, *_a: object, **_k: object) -> None:
        created.append(uri)

    monkeypatch.setattr("lance.dataset", _denied)
    monkeypatch.setattr("ingest.catalog.create_empty", _create)

    with pytest.raises(PermissionError):
        LocalCatalog(SCHEMA).ensure_at("s3://governed/bronze/pages.lance")

    assert created == [], "a dataset that could not be READ was created EMPTY over the top of itself"


def test_a_GENUINELY_absent_dataset_is_still_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrowing must not close the path it is narrowing."""
    from ingest.catalog import LocalCatalog

    created: list[str] = []

    def _missing(uri: str, *_a: object, **_k: object) -> object:
        raise ValueError(f"Dataset at {uri} was not found")

    monkeypatch.setattr("lance.dataset", _missing)
    monkeypatch.setattr("ingest.catalog.create_empty", lambda uri, *a, **k: created.append(uri))
    monkeypatch.setattr("ingest.catalog.assert_creation_contract", lambda uri: None)

    LocalCatalog(SCHEMA).ensure_at("s3://governed/bronze/pages.lance")

    assert created == ["s3://governed/bronze/pages.lance"]
