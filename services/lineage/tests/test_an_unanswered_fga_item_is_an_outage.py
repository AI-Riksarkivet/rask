"""An OpenFGA item the server could not answer is an outage at both lineage gates, never a verdict.

The SDK hands an unanswered BatchCheck item back as ``allowed=False`` beside a ``CheckError``. Read as
a deny, the bus door parks a delivery as a refusal (or takes it as unrepairable, deleting the
provenance) and the read filter hides a dataset the caller may hold a grant on.

The other half: a governed dataset an event names without its table's uuid is no object any grant can name
(``table:<uuid>``, [[LH-524]]), so it is not a question to send at all, and its name never reaches OpenFGA: a space
or tab in a name sent as an object fails the whole request (measured on OpenFGA v1.18.3).

Driven through the real ``fga.batch_check`` with a client double built from the SDK's own response
classes; nothing here replaces ``batch_check`` itself.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from lance_namespace import ServiceUnavailableError
from lineage_lane import Feed, governed, started
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientTuple
from openfga_sdk.client.models.batch_check_response import ClientBatchCheckResponse
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models.check_error import CheckError
from openfga_sdk.models.read_request_tuple_key import ReadRequestTupleKey
from openfga_sdk.models.read_response import ReadResponse

from lakehouse.bus_delivery import Outcome, Settlement
from lakehouse.governed import fga
from lakehouse.governed.decider import OpenFgaDecider
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from lakehouse.lineage_events import DatasetUuids, with_dataset_uuids
from lineage.api import fga_deps
from lineage.core.config import LineageSettings, get_settings
from lineage.models import RunEvent


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


def _deliver(monkeypatch: pytest.MonkeyPatch, fga_client: _OpenFga, event: RunEvent | None = None) -> tuple[Settlement, Feed, float]:
    """Deliver ``event`` to lineage's own ingest lane, its gate asking ``fga_client``; answers the settlement, the feed,
    and the delay the lane gives a delivery it asks for again."""
    governed(monkeypatch)
    with started(monkeypatch, Feed(), decider=OpenFgaDecider(cast("OpenFgaClient", fga_client))) as lineage:
        settled = lineage.deliver({"id": "evt-1", "data": (event or _event()).model_dump(by_alias=True)})
        retry_seconds = get_settings().delivery_retry_seconds
    return settled, lineage.feed, retry_seconds


def test_a_bus_delivery_openfga_could_not_answer_is_retried_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fga_client = _OpenFga(unanswered={_PAGES_OBJECT})

    settled, feed, retry_seconds = _deliver(monkeypatch, fga_client)

    assert settled == Settlement(outcome=Outcome.AGAIN, delay_seconds=retry_seconds), "an unanswered item was treated as a verdict on the delivery"
    assert (feed.recorded, feed.kept, fga_client.asked) == ([], {}, [_PAGES_OBJECT])


@pytest.mark.parametrize("output", ["a:b", "file//my scans"], ids=["second-colon", "space"])
def test_an_output_that_names_no_object_is_consumed_as_ungoverned_without_asking(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    """An output a bus delivery names without its table's uuid is no object a grant can name, and a redelivery or a
    replay carries the same bytes, so the refusal is one nothing can repair ([[LH-166]]): taken, not recorded, and
    neither parked nor asked for again. Neither a check nor a tuple read is sent for it: its name is no object, and a
    space in it fails the whole OpenFGA request."""
    fga_client = _OpenFga(unanswered=set())

    settled, feed, _ = _deliver(monkeypatch, fga_client, _event(outputs=(output,)))

    assert settled == Settlement(outcome=Outcome.TAKEN)
    assert (feed.recorded, feed.kept) == ([], {})
    assert all(obj == _PAGES_OBJECT for obj in fga_client.asked), fga_client.asked
    assert fga_client.read_asked == []


def test_an_input_that_names_no_object_is_refused_without_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    fga_client = _OpenFga(unanswered=set())

    settled, feed, _ = _deliver(monkeypatch, fga_client, _event(inputs=("a#b",)))

    assert settled == Settlement(outcome=Outcome.PARKED)
    assert (feed.recorded, list(feed.kept)) == ([], ["dead_letter:evt-1"])
    assert fga_client.asked == [_PAGES_OBJECT]


def _filter(fga_client: _OpenFga) -> fga_deps.DatasetFilter:
    settings = LineageSettings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example.com",
            "oidc_audience": "lance",
            "oidc_idp_id": "dex",
            "oidc_connectors": {"local": "dex"},
            "fga_enabled": True,
            "fga_store_id": "s",
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
