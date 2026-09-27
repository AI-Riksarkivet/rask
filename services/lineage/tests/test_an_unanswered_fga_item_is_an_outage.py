"""An OpenFGA item the server could not answer is an outage at both lineage gates, never a verdict.

The SDK hands an unanswered BatchCheck item back as ``allowed=False`` beside a ``CheckError``. Read as
a deny, the bus door parks a delivery as a refusal (or consumes it as unrepairable, deleting the
provenance) and the read filter hides a dataset the caller may hold a grant on.

The other half: a name whose ``table:<name>`` is no object id (an external ``s3://bucket/prefix``
vertex, a URI-named input) is not a question to send at all. ``fga.batch_check`` answers it False
unasked; sent, OpenFGA v1.18.3 fails the item on the default Check engine and answers it false on the
deployed ``weighted_graph_check`` one (both measured), and a space or tab in it fails the whole request.

Driven through the real ``fga.batch_check`` with a client double built from the SDK's own response
classes; nothing here replaces ``batch_check`` itself.
"""

from __future__ import annotations

import asyncio
import logging
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
from lineage.schemas import DatasetRef, Neighbors
from lineage.services.repository import LineageRepository
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse.ns_errors import install_problem_handlers


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


def _event(*, outputs: tuple[str, ...] = ("bronze$pages",), inputs: tuple[str, ...] = ()) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-09T00:00:00+00:00",
            "run": {"runId": "11111111-1111-1111-1111-111111111111", "facets": {"author": {"name": "alice", "sub": "alice"}}},
            "job": {"namespace": "bus", "name": "probe"},
            "inputs": [{"namespace": "bronze", "name": name} for name in inputs],
            "outputs": [{"namespace": "bronze", "name": name} for name in outputs],
        }
    )


class _Repo:
    """The capturing repository plus both reads the bus door's authz path makes."""

    def __init__(self) -> None:
        self.ingested: list[RunEvent] = []

    async def ingest_event(self, event: RunEvent) -> None:
        self.ingested.append(event)

    async def run_output_names(self, _run_id: str) -> list[str]:
        return []

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

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    get_settings.cache_clear()
    repo = _Repo()
    app.state.repository = repo
    app.state.fga = fga_client
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
    assert fga_client.asked == ["table:bronze$pages"]


def test_a_bus_delivery_openfga_could_not_answer_is_retried_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repo = _bus_door(monkeypatch, _OpenFga(unanswered={"table:bronze$pages"}))

    assert _deliver(client) == {"status": "RETRY"}, "an unanswered item was treated as a verdict on the delivery"
    assert repo.ingested == []


@pytest.mark.parametrize("output", ["a:b", "file//my scans"], ids=["second-colon", "space"])
def test_an_output_that_names_no_object_is_consumed_as_ungoverned_without_asking(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    """No grant can name such an object, so the refusal is one nothing can repair ([[LH-166]]): consumed,
    not ingested, and not retried as an outage the next attempt cannot clear. Neither its check nor its
    tuple read is sent: a space in it fails the whole OpenFGA request."""
    fga_client = _OpenFga(unanswered=set())
    client, repo = _bus_door(monkeypatch, fga_client)

    assert _deliver(client, _event(outputs=("bronze$pages", output))) == {"status": "SUCCESS"}
    assert repo.ingested == []
    assert fga_client.asked == ["table:bronze$pages"]
    assert fga_client.read_asked == []


def test_an_input_that_names_no_object_is_refused_without_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    fga_client = _OpenFga(unanswered=set())
    client, repo = _bus_door(monkeypatch, fga_client)

    assert _deliver(client, _event(inputs=("a#b",))) == {"status": "DROP"}
    assert repo.ingested == []
    assert fga_client.asked == ["table:bronze$pages"]


def _filter(fga_client: _OpenFga) -> fga_deps.DatasetFilter:
    settings = LineageSettings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example.com",
            "oidc_audience": "lance",
            "fga_enabled": True,
            "fga_store_id": "s",
            "fga_model_id": "m",
        }
    )
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=cast(OpenFgaClient, fga_client)))))
    return fga_deps.DatasetFilter(request, settings, IDToken(iss="https://idp.example.com", sub="alice", aud="lance", exp=0, iat=0))


def test_the_read_filter_keeps_what_openfga_answered() -> None:
    assert asyncio.run(_filter(_OpenFga(unanswered=set())).visible(["a", "b"])) == {"a", "b"}


def test_a_dataset_openfga_could_not_answer_is_a_503_not_hidden() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(_filter(_OpenFga(unanswered={"table:b"})).visible(["a", "b"]))


class _Graph:
    """The upstream walk of an ingested table: one governed parent and the external sources it read."""

    async def upstream(self, name: str, depth: int | None = None) -> Neighbors:
        del depth
        related = [DatasetRef(name="bronze$pages", namespace="bronze"), DatasetRef(name="s3://images-batch/run1/", namespace="s3://images-batch")]
        return Neighbors(dataset=name, related=[*related, DatasetRef(name="lance/s3://lane-src/tbl.lance", namespace="lance")])


def test_an_external_source_on_a_graph_read_is_hidden_without_asking() -> None:
    """The upstream of an ingested table reaches its external sources; they stay out of the answer, and
    OpenFGA is asked only about the object it can evaluate."""
    from lineage.api.v1.endpoints.datasets import get_upstream

    fga_client = _OpenFga(unanswered=set())
    result = asyncio.run(get_upstream("silver$pages", cast(LineageRepository, _Graph()), _filter(fga_client)))

    assert [ref.name for ref in result.related] == ["bronze$pages"]
    assert fga_client.asked == ["table:bronze$pages"]
