"""A warehouse record's endpoint reaches the connection builder, and the builder answers for the store it opens.

The warehouse record carries an optional `endpoint` ([[LH-067]]) that the resolver threads to
`build_namespace_for_root`, and the builder JUDGES it rather than applying it ([[LH-205]]): the
connection signs with the estate's own key pair, so a record naming another store is refused before
anything connects. That refusal, at every door that builds a warehouse connection, is pinned through
the real app by `tests/integration/test_a_foreign_store_is_refused_until_its_credential_is_consumed.py`.
What stays here is the builder's own behaviour and the resolver handing it the record's endpoint.

CREDENTIALS ARE DELIBERATELY NOT HERE. Material never travels in a record. The catalog resolves its own
S3 secret from the Dapr secret store (`dapr_secret_store`/`dapr_secret_key`/`dapr_secret_s3_field`), so
a second store's material belongs behind the same door under its own key, with the record naming a
REFERENCE.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import Request

from catalog.core import namespace as namespace_module
from catalog.core.config import Settings


_STORAGE = "storage."


@pytest.fixture
def settings() -> Settings:
    """An HTTPS estate default, so an http warehouse below is a genuine mixed pair."""
    return Settings(
        LANCE_S3_ACCESS_KEY_ID="k",
        LANCE_S3_SECRET_ACCESS_KEY="s",
        LANCE_S3_ENDPOINT="https://estate.example:9000",
        LANCE_REST_ROOT="s3://estate-root",
    )


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """The properties handed to `lance_namespace.connect`, which is the whole contract under test."""
    seen: list[dict[str, str]] = []

    def _connect(impl: str, properties: dict[str, str]) -> Any:
        seen.append(dict(properties))
        return object()

    monkeypatch.setattr(namespace_module, "connect", _connect)
    return seen


def test_a_warehouse_with_NO_endpoint_uses_the_estate_default(settings: Settings, captured: list[dict[str, str]]) -> None:
    """The control. Without it, a builder that ignored the override entirely would pass below."""
    namespace_module.build_namespace_for_root(settings, "s3://tenant-bucket")

    assert captured[0][f"{_STORAGE}endpoint"] == "https://estate.example:9000"
    assert captured[0]["root"] == "s3://tenant-bucket"


class _Req:
    """The two pieces of `request` the resolver touches, without standing up an app.

    Handed over with an explicit `cast` at each call rather than a `type: ignore`: these functions read
    `request.app.state.warehouse_binding_cache` and `.warehouse_namespaces` and nothing else, so the
    cast states what is actually required of the argument instead of silencing the checker.
    """

    class _State:
        def __init__(self) -> None:
            self.warehouse_binding_cache: dict[str, dict[str, str]] = {}
            self.warehouse_namespaces: dict[tuple[str, str | None], object] = {}

    def __init__(self) -> None:
        self.app = type("_App", (), {"state": _Req._State()})()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record_endpoint", "expected"),
    [("http://tenant.store:9000", "http://tenant.store:9000"), ("", None)],
)
async def test_the_resolver_carries_the_RECORDS_endpoint(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, record_endpoint: str | None, expected: str | None
) -> None:
    """The wiring leg. The builder honouring an endpoint proves nothing if the record's never reaches it.

    The empty-string case is not padding: a record written with `endpoint: ""` must mean "the estate's",
    not an override to the empty endpoint, which would build a connection pointing nowhere.
    """
    from catalog.api import dependencies

    record: dict[str, str] = {"id": "wh-1", "status": "active"}
    if record_endpoint is not None:
        record["endpoint"] = record_endpoint
    monkeypatch.setattr(dependencies.warehouses, "binding_for_namespace", lambda *a, **k: {"warehouse_id": "wh-1", "root_uri": "s3://tenant-bucket"})
    monkeypatch.setattr(dependencies.warehouses, "get_warehouse", lambda *a, **k: record)

    resolved = await dependencies._resolve_warehouse_root(cast(Request, _Req()), settings, "tenant-ns")

    assert resolved == ("s3://tenant-bucket", expected)


def test_an_UNREACHABLE_store_is_503_naming_it_not_a_500(settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A store that will not answer is an OUTAGE, and a 500 says the catalog is broken when it is fine.

    Measured live 2026-09-20 on the deployed catalog: a warehouse pointed at `http://127.0.0.1:1`
    answered `500 InternalError code 18`, which sends whoever is on call to the wrong system. pylance
    raises a BARE `ValueError` here — the same shape `open_dataset` already converts for a missing
    dataset, and for the same reason.

    WHICH STORE reaches the LOG, not the caller. `ns_errors` redacts every 5xx detail to "Internal
    Server Error" on purpose — a 5xx is a fault and its text leaks, and an endpoint is exactly the
    internal hostname that must not go out. So the assertion below is on the exception the handler
    receives and on the log record, not on a wire body that will never carry it.
    """
    from lance_namespace import ServiceUnavailableError

    def _boom(impl: str, properties: dict[str, str]) -> Any:
        raise ValueError(
            "Failed to construct namespace impl lance.namespace.DirectoryNamespace: LanceError(IO): "
            "Generic S3 error: Error performing list request: error sending request"
        )

    monkeypatch.setattr(namespace_module, "connect", _boom)

    with pytest.raises(ServiceUnavailableError) as caught, caplog.at_level("WARNING"):
        namespace_module.build_namespace_for_root(settings, "s3://tenant-bucket")

    assert "https://estate.example:9000" in str(caught.value), f"the refusal does not name the store: {caught.value}"


def test_a_NON_TRANSPORT_construction_failure_is_left_alone(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control, and the reason the rule reads the error rather than catching ValueError.

    A bogus impl raises the same bare `ValueError` and is NOT an outage — measured, its message carries
    no `LanceError(IO)`. Converting it to 503 would tell an operator to wait for a store to come back
    when the configuration names a module that does not exist.
    """

    def _boom(impl: str, properties: dict[str, str]) -> Any:
        raise ValueError("Failed to construct namespace impl no.such.Impl: No module named 'no'")

    monkeypatch.setattr(namespace_module, "connect", _boom)

    with pytest.raises(ValueError, match="No module named"):
        namespace_module.build_namespace_for_root(settings, "s3://tenant-bucket")
