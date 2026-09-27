"""An OpenFGA item the server could not answer is an outage at BOTH of this plane's gates.

The SDK hands an unanswered BatchCheck item back as ``allowed=False`` beside a ``CheckError``. Read as
a verdict it HIDES: the render gate drops the row from the page as if the grant were revoked, and the
delivery gate acks the event as ``hidden``, so the person is never told and nothing retries.

Driven through the real ``fga.batch_check`` with a client double built from the SDK's own response
classes; nothing here replaces ``batch_check`` itself.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientTuple
from openfga_sdk.client.models.batch_check_response import ClientBatchCheckResponse
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models.check_error import CheckError

from notifications.api import inbox as inbox_module
from notifications.api import security as security_module
from notifications.api.ingest import DAPR_RETRY, DAPR_SUCCESS, ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.visibility import Visibility
from notifications.config import get_notifications_settings
from notifications.feed import paginate, unread_count
from notifications.models import InboxPointer, InboxQuery, NotificationReason
from notifications.proxies import TypedActorProxy
from service_kit.exceptions import register_handlers
from service_kit.lakehouse.ns_errors import install_problem_handlers


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


CALLER = "alice"
_UNANSWERED = CheckError(input_error="validation_error", message="relation not found in the pinned model")


class _OpenFga:
    """BatchCheck answering ``allowed`` for every object except the unanswered ones."""

    def __init__(self, unanswered: set[str]) -> None:
        self._unanswered = unanswered
        self.asked: list[str] = []

    async def batch_check(self, body: ClientBatchCheckRequest, options: dict[str, Any] | None = None) -> ClientBatchCheckResponse:
        del options
        result = []
        for index, item in enumerate(body.checks):
            self.asked.append(item.object)
            error = _UNANSWERED if item.object in self._unanswered else None
            # The SDK hands the batch ITEM back as `request` (client.py `map_response`); its annotation says ClientTuple.
            result.append(ClientBatchCheckSingleResponse(allowed=error is None, request=cast("ClientTuple", item), correlation_id=f"c{index}", error=error))
        return ClientBatchCheckResponse(result)


def _client(double: _OpenFga) -> "OpenFgaClient":
    return cast("OpenFgaClient", double)


def _pointer(index: int, obj: str) -> InboxPointer:
    return InboxPointer(
        notification_id=f"run-{index:03d}@FAIL",
        reason=NotificationReason.AUTHOR,
        object_id=obj,
        source_run_id=f"ingest-{index}",
        occurred_at=datetime(2026, 8, 9, 12, index, tzinfo=UTC),
    )


class _Inbox:
    def __init__(self, rows: list[InboxPointer]) -> None:
        self.rows = rows

    async def page(self, payload: dict[str, Any]) -> dict[str, Any]:
        return paginate(self.rows, InboxQuery.model_validate(payload)).model_dump(mode="json")

    async def unread(self) -> dict[str, Any]:
        return {"unread": unread_count(self.rows), "rows": len(self.rows)}


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _Inbox]]:
    """The inbox door with FGA on and both handler installers, as `make_service_app` composes it."""
    inbox = _Inbox([])
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(inbox_module.router)
    app.state.notifications_settings = get_notifications_settings().model_copy(update={"fga_enabled": True})
    app.state.actors_registered = True
    app.dependency_overrides[security_module._deps.current_subject] = lambda: CALLER
    monkeypatch.setattr(inbox_module, "inbox_for", lambda _subject: cast(TypedActorProxy, inbox))
    with TestClient(app) as client:
        yield client, inbox


def test_the_harness_renders_a_row_openfga_answered(door: tuple[TestClient, _Inbox]) -> None:
    """Without this, the 503 below could be a door that never reached OpenFGA."""
    client, inbox = door
    double = _OpenFga(unanswered=set())
    client.app.state.fga = _client(double)
    inbox.rows = [_pointer(1, "silver$pages")]

    response = client.get("/notifications/inbox")

    assert response.status_code == 200, response.text
    assert [row["notification_id"] for row in response.json()["notifications"]] == ["run-001@FAIL"]
    assert double.asked == ["table:silver$pages"]


def test_a_row_openfga_could_not_answer_is_a_503_not_a_hidden_row(door: tuple[TestClient, _Inbox]) -> None:
    client, inbox = door
    client.app.state.fga = _client(_OpenFga(unanswered={"table:gold$reports"}))
    inbox.rows = [_pointer(1, "silver$pages"), _pointer(2, "gold$reports")]

    response = client.get("/notifications/inbox")

    assert response.status_code == 503, f"an unanswered item rendered as a revoked grant: {response.status_code} {response.text}"
    assert response.headers["content-type"].startswith("application/problem+json")


class _Plane:
    def __init__(self) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}

    def open(self, subject: str) -> TypedActorProxy:
        plane = self

        class _Box:
            async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
                rows = plane.boxes.setdefault(subject, [])
                rows.append(payload)
                return {"delivered": True, "unread": len(rows), "rows": len(rows)}

        return cast(TypedActorProxy, _Box())


def _run_event(output: str = "silver$pages") -> dict[str, Any]:
    return {
        "eventType": "FAIL",
        "eventTime": "2026-08-09T12:00:00+00:00",
        "run": {"runId": "run-1", "facets": {"author": {"name": CALLER, "sub": CALLER}}},
        "outputs": [{"namespace": "silver", "name": output}],
    }


@pytest.mark.asyncio
async def test_the_harness_delivers_a_run_openfga_answered() -> None:
    plane = _Plane()
    visibility = Visibility(client=_client(_OpenFga(unanswered=set())), enabled=True)

    status = await ingest_run_event(_run_event(), lane=Lane.BUS, visibility=visibility, open_inbox=plane.open)

    assert status is DAPR_SUCCESS
    assert list(plane.boxes) == [CALLER]


@pytest.mark.asyncio
async def test_a_delivery_openfga_could_not_answer_is_retried_not_acked_as_hidden() -> None:
    plane = _Plane()
    visibility = Visibility(client=_client(_OpenFga(unanswered={"table:silver$pages"})), enabled=True)

    status = await ingest_run_event(_run_event(), lane=Lane.BUS, visibility=visibility, open_inbox=plane.open)

    assert status is DAPR_RETRY, f"an unanswered item was acked as a decision: {status}"
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_a_run_whose_output_is_named_like_a_uri_is_hidden_not_retried_forever() -> None:
    """`s3://images/run1` carries a `:` but names no model type; sent as an object, OpenFGA answers
    `type 's3' not found` on every attempt. As a table name it is no object id, so nobody is told and
    the delivery is acked."""
    plane = _Plane()
    double = _OpenFga(unanswered=set())
    visibility = Visibility(client=_client(double), enabled=True)

    status = await ingest_run_event(_run_event("s3://images/run1"), lane=Lane.BUS, visibility=visibility, open_inbox=plane.open)

    assert status is DAPR_SUCCESS
    assert plane.boxes == {}
    assert double.asked == []


def test_a_row_stamped_with_a_model_type_is_asked_about_that_object(door: tuple[TestClient, _Inbox]) -> None:
    client, inbox = door
    double = _OpenFga(unanswered=set())
    client.app.state.fga = _client(double)
    inbox.rows = [_pointer(1, "warehouse:acme")]

    response = client.get("/notifications/inbox")

    assert response.status_code == 200, response.text
    assert double.asked == ["warehouse:acme"]
