"""A warehouse record's endpoint reaches the connection builder, and the builder answers for the store it opens.

The warehouse record carries an optional `endpoint` ([[LH-067]]) that the resolver threads to
`build_namespace_for_root`, and the builder JUDGES it rather than applying it ([[LH-205]]): the
connection signs with the estate's own key pair, so a record naming another store is refused before
anything connects. That refusal, at every door that builds a warehouse connection, is pinned through
the real app by `tests/integration/test_a_foreign_store_is_refused_until_its_credential_is_consumed.py`.
What stays here is the builder's own behaviour and the resolver handing it the record's endpoint.

CREDENTIALS ARE DELIBERATELY NOT HERE. Material never travels in a record: a record names a REFERENCE at most, and no
door resolves one.
"""

from typing import Any

import pytest
from fastapi import Request

from catalog.core import namespace as namespace_module
from catalog.core.config import Settings
from lakehouse.records.warehouse_records import WarehouseRecord
from tests.app_state import request_on
from tests.catalog_settings import catalog_settings


_STORAGE = "storage."


@pytest.fixture
def settings() -> Settings:
    """An HTTPS estate default, so an http warehouse below is a genuine mixed pair."""
    return catalog_settings(LANCE_S3_ENDPOINT="https://estate.example:9000", LANCE_REST_ROOT="s3://estate-root")


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """The properties the directory namespace is built with, which is the whole contract under test."""
    seen: list[dict[str, str]] = []

    def _directory(session: object = None, **properties: str) -> Any:
        seen.append(dict(properties))
        return object()

    monkeypatch.setattr(namespace_module, "DirectoryNamespace", _directory)
    return seen


def test_a_warehouse_with_no_endpoint_uses_the_estate_default(settings: Settings, captured: list[dict[str, str]]) -> None:
    """The control. Without it, a builder that ignored the override entirely would pass below."""
    namespace_module.build_namespace_for_root(settings, "s3://tenant-bucket")

    assert captured[0][f"{_STORAGE}aws_endpoint"] == "https://estate.example:9000"
    assert captured[0]["root"] == "s3://tenant-bucket"


def _req() -> Request:
    """A request with an empty state: the resolver keeps what it resolved on the request's own `state`."""
    return request_on()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record_endpoint", "expected"),
    [("http://tenant.store:9000", "http://tenant.store:9000"), ("", None)],
)
async def test_the_resolver_carries_the_records_endpoint(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, record_endpoint: str | None, expected: str | None
) -> None:
    """The wiring leg. The builder honouring an endpoint proves nothing if the record's never reaches it.

    The empty-string case is not padding: a record written with `endpoint: ""` must mean "the estate's",
    not an override to the empty endpoint, which would build a connection pointing nowhere.
    """
    from catalog.api import dependencies

    stored: dict[str, str] = {"id": "wh-1", "bucket": "tenant-bucket", "project": "acme", "status": "active", "root_uri": "s3://tenant-bucket"}
    if record_endpoint is not None:
        stored["endpoint"] = record_endpoint
    record = WarehouseRecord.model_validate(stored)
    monkeypatch.setattr(dependencies.warehouses, "get_warehouse", lambda *a, **k: record)

    resolved = await dependencies._resolve_warehouse_root(_req(), settings, "wh-1")

    assert resolved == dependencies.ServedWarehouse(root_uri="s3://tenant-bucket", endpoint=expected)


def test_an_unreachable_store_is_503_naming_it_not_a_500() -> None:
    """A store that will not answer is an OUTAGE, and a 500 says the catalog is broken when it is fine.

    Measured live 2026-09-20 on the deployed catalog: a warehouse pointed at `http://127.0.0.1:1` answered `500
    InternalError code 18`, which sends whoever is on call to the wrong system. Built here on real pylance against that
    address, where nothing listens.

    WHICH STORE reaches the exception the handler receives, not the wire: `ns_errors` redacts every 5xx detail.
    """
    from lance_namespace import ServiceUnavailableError

    dead = catalog_settings(LANCE_S3_ENDPOINT="http://127.0.0.1:1", LANCE_REST_ROOT="s3://estate-root")

    with pytest.raises(ServiceUnavailableError) as caught:
        namespace_module.build_namespace_for_root(dead, "s3://tenant-bucket")

    assert "http://127.0.0.1:1" in str(caught.value), f"the refusal does not name the store: {caught.value}"


def test_a_store_that_answers_is_not_an_outage(moto_url: str) -> None:
    """The control, and the reason the rule reads the error rather than catching it: a store that answered said
    something a retry will not change.

    Measured on pylance 14.0.3 against a moto server: a root in a bucket that does not exist raises `LanceError(IO)`
    carrying the store's `404 Not Found`. Converting it to 503 would tell an operator to wait for a store that is up; it
    stays the error it is, which the app's handler answers 500 code 18 as it does on the estate's own root.
    """
    from lance_namespace import ServiceUnavailableError

    store = catalog_settings(LANCE_S3_ENDPOINT=moto_url, LANCE_REST_ROOT="s3://estate-root")

    with pytest.raises(OSError, match="404 Not Found") as caught:
        namespace_module.build_namespace_for_root(store, "s3://no-such-bucket")

    assert not isinstance(caught.value, ServiceUnavailableError)
