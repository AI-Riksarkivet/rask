"""An OpenFGA item the server could not answer is an outage at both lineage gates, never a verdict.

The SDK hands an unanswered BatchCheck item back as ``allowed=False`` beside a ``CheckError``. Read as
a deny, the bus door parks a delivery as a refusal (or consumes it as unrepairable, deleting the
provenance) and the read filter hides a dataset the caller may hold a grant on.

The other half: a governed dataset an event names without its table's uuid is no object any grant can name
(``table:<uuid>``, [[LH-524]]), so it is not a question to send at all, and its name never reaches OpenFGA: a space
or tab in a name sent as an object fails the whole request (measured on OpenFGA v1.18.3).

Driven through the real ``fga.batch_check`` with a client double built from the SDK's own response
classes; nothing here replaces ``batch_check`` itself.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientTuple
from openfga_sdk.client.models.batch_check_response import ClientBatchCheckResponse
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models.check_error import CheckError
from openfga_sdk.models.read_request_tuple_key import ReadRequestTupleKey
from openfga_sdk.models.read_response import ReadResponse
from starlette.testclient import TestClient

from lineage.api import fga_deps
from lineage.core.config import LineageSettings
from lineage.models import RunEvent
from service_kit.governed import fga
from service_kit.governed.decider import OpenFgaDecider
from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.openlineage import DatasetUuids, with_dataset_uuids


def _uuid(name: str) -> str:
    """The uuid the catalog minted for the governed table ``name`` (ADR 0173 §10)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


#: The one output every delivery below names with its uuid, and the object its grants are held by.
_PAGES_OBJECT = fga.table_object(_uuid("bronze$pages"))


class _OpenFga:
    """BatchCheck answering ``allowed`` for every object except the unanswered ones, and a Read answering
    no tuples, as the server does for an object nobody granted on."""

    def __init__(self, unanswered: set[str]) -> None:
        self._unanswered = unanswered
        self.asked: list[str] = []
        self.read_asked: list[str] = []

    async def batch_check(self, body: ClientBatchCheckRequest, options: dict[str, Any] | None = None) -> ClientBatchCheckResponse:
        del options
        result = []
        for index, item in enumerate(body.checks):
            self.asked.append(item.object)
            error = CheckError(input_error="validation_error", message="relation not found in the pinned model") if item.object in self._unanswered else None
            # The SDK hands the batch ITEM back as `request` (client.py `map_response`); its annotation says ClientTuple.
            result.append(ClientBatchCheckSingleResponse(allowed=error is None, request=cast("ClientTuple", item), correlation_id=f"c{index}", error=error))
        return ClientBatchCheckResponse(result)

    async def read(self, body: ReadRequestTupleKey, options: dict[str, Any] | None = None) -> ReadResponse:
        del options
        self.read_asked.append(str(body.object))
        return ReadResponse(tuples=[], continuation_token="")


def _dataset(name: str, *, with_uuid: bool) -> dict[str, Any]:
    dataset = {"namespace": "bronze", "name": name}
    return with_dataset_uuids(dataset, DatasetUuids(table_uuid=_uuid(name)), producer="rask://test") if with_uuid else dataset


def _event(*, outputs: tuple[str, ...] = (), inputs: tuple[str, ...] = ()) -> RunEvent:
    """A run writing ``bronze$pages`` as the catalog names it, with its uuid, beside ``outputs`` and ``inputs`` named
    without one."""
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-09T00:00:00+00:00",
            "run": {"runId": "11111111-1111-1111-1111-111111111111", "facets": {"rask_author": {"name": "alice", "sub": "dex~alice"}}},
            "job": {"namespace": "bus", "name": "probe"},
            "inputs": [_dataset(name, with_uuid=False) for name in inputs],
            "outputs": [_dataset("bronze$pages", with_uuid=True), *(_dataset(name, with_uuid=False) for name in outputs)],
        }
    )


class _Repo:
    """The capturing store plus both reads the bus door's authz path makes."""

    def __init__(self) -> None:
        self.ingested: list[RunEvent] = []

    async def record(self, event: RunEvent) -> None:
        self.ingested.append(event)

    async def run_author(self, _run_id: str) -> str | None:
        return None

    async def recorded_event(self, _run_id: str, _event_type: str | None) -> dict[str, Any] | None:
        return None


def _bus_door(monkeypatch: pytest.MonkeyPatch, fga_client: _OpenFga) -> tuple[TestClient, _Repo]:
    """The REGISTERED bus route with production wiring, so the authorizer is the one `register_dapr` passes."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_DAPR_ENABLED", "true")
    monkeypatch.setenv("RASK_FGA_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ISSUER", "https://idp.invalid/dex")
    monkeypatch.setenv("RASK_OIDC_AUDIENCE", "rask")
    monkeypatch.setenv("RASK_OIDC_CONNECTORS", '{"local": "dex"}')

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    get_settings.cache_clear()
    repo = _Repo()
    app.state.provenance = repo
    app.state.decider = OpenFgaDecider(cast("OpenFgaClient", fga_client))
    return TestClient(app), repo


def _deliver(client: TestClient, event: RunEvent | None = None) -> dict[str, Any]:
    body = {"data": (event or _event()).model_dump(by_alias=True)}
    response = client.post("/lineage-events", json=body, headers={"dapr-api-token": "s3cret"})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_bus_harness_records_a_delivery_openfga_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this, the RETRY below could be a door that never reached OpenFGA."""
    fga_client = _OpenFga(unanswered=set())
    client, repo = _bus_door(monkeypatch, fga_client)

    assert _deliver(client) == {"status": "SUCCESS"}
    assert len(repo.ingested) == 1
    assert fga_client.asked == [_PAGES_OBJECT]


def test_a_bus_delivery_openfga_could_not_answer_is_retried_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repo = _bus_door(monkeypatch, _OpenFga(unanswered={_PAGES_OBJECT}))

    assert _deliver(client) == {"status": "RETRY"}, "an unanswered item was treated as a verdict on the delivery"
    assert repo.ingested == []


@pytest.mark.parametrize("output", ["a:b", "file//my scans"], ids=["second-colon", "space"])
def test_an_output_that_names_no_object_is_consumed_as_ungoverned_without_asking(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    """An output a bus delivery names without its table's uuid is no object a grant can name, and a redelivery or a
    replay carries the same bytes, so the refusal is one nothing can repair ([[LH-166]]): consumed, not ingested, and
    not parked or retried. Neither a check nor a tuple read is sent for it: its name is no object, and a space in it
    fails the whole OpenFGA request."""
    fga_client = _OpenFga(unanswered=set())
    client, repo = _bus_door(monkeypatch, fga_client)

    assert _deliver(client, _event(outputs=(output,))) == {"status": "SUCCESS"}
    assert repo.ingested == []
    assert all(obj == _PAGES_OBJECT for obj in fga_client.asked), fga_client.asked
    assert fga_client.read_asked == []


def test_an_input_that_names_no_object_is_refused_without_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    fga_client = _OpenFga(unanswered=set())
    client, repo = _bus_door(monkeypatch, fga_client)

    assert _deliver(client, _event(inputs=("a#b",))) == {"status": "DROP"}
    assert repo.ingested == []
    assert fga_client.asked == [_PAGES_OBJECT]


def _filter(fga_client: _OpenFga) -> fga_deps.DatasetFilter:
    settings = LineageSettings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example.com",
            "oidc_audience": "lance",
            "oidc_connectors": {"local": "dex"},
            "fga_enabled": True,
            "fga_store_id": "s",
            "fga_model_id": "m",
        }
    )
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(decider=OpenFgaDecider(cast(OpenFgaClient, fga_client))))))
    return fga_deps.DatasetFilter(
        request,
        settings,
        PersonPrincipal(
            principal=Principal(idp_id="dex", subject="alice"),
            claims=IDToken(iss="https://idp.example.com", sub="alice", aud="lance", exp=0, iat=0),
        ),
    )


def test_a_dataset_openfga_could_not_answer_is_a_503_not_hidden() -> None:
    a, b = fga.table_object(_uuid("a")), fga.table_object(_uuid("b"))
    fga_client = _OpenFga(unanswered={b})

    with pytest.raises(ServiceUnavailableError):
        asyncio.run(_filter(fga_client).visible([a, b]))
    assert fga_client.asked == [a, b], "the 503 came from somewhere other than the unanswered item"
