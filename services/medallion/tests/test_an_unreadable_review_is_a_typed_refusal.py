"""A live review whose stored input no longer validates is refused as UNUSABLE, not as a crash or an outage.

The review instance outlives the code that wrote it: the engine keeps its serialized input for as long as
the approval window runs (72 hours by default), and the estate takes no compatibility shims, so a
`PromotionSpec` change can leave a live instance whose input the running producer cannot parse.

Both routes that read it mis-answered that. `GET /promotions/{id}` let the pydantic `ValidationError`
escape to the catch-all, a 500 that reads as a producer bug; `POST .../decision` mapped every
non-domain exception to `ServiceUnavailable`, a 503 that tells the caller to retry a request that can
never succeed. The truthful answer is the spec's `InvalidTableState` (code 19, 409): the review exists,
and its stored state cannot serve the operation.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from dapr.ext.workflow.workflow_state import WorkflowStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import ErrorCode

from medallion.api import promotions
from medallion.api.promotions import instance_for
from service_kit.exceptions import register_handlers
from service_kit.lakehouse.ns_errors import install_problem_handlers


_INSTANCE = instance_for("tok-1")


def _held() -> dict[str, Any]:
    return {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "acme-silver",
        "from_dataset": "acme-silver$features",
        "to_namespace": "acme-gold",
        "to_dataset": "acme-gold$catalog",
        "operation": "aggregate_gold",
        "author": "analyst",
        "version": 7,
        "reasons": ["row_delta_band"],
        "approver": "CiQwOGE4Njg0Yi1kYjg4",
        "originator": "CiQwOGE4Njg0Yi1kYjg4",
        "approval_hours": 72,
    }


def _without_operation() -> str:
    stored = _held()
    del stored["operation"]
    return json.dumps(stored)


#: Two ways a stored input stops being readable: a field the model now requires is absent, and bytes
#: that are not JSON at all.
_UNREADABLE = [pytest.param(_without_operation(), id="a-required-field-is-absent"), pytest.param("{not json", id="not-json")]


class _State:
    def __init__(self, serialized_input: str) -> None:
        self.serialized_input = serialized_input
        self.runtime_status = WorkflowStatus.RUNNING


class _Engine:
    """Hosts one LIVE instance with the given stored input, and records any event raised at it."""

    def __init__(self, serialized_input: str) -> None:
        self._state = _State(serialized_input)
        self.raised: list[tuple[str, str, Any]] = []

    def raise_workflow_event(self, instance_id: str, event_name: str, *, data: Any = None) -> None:
        self.raised.append((instance_id, event_name, data))

    def schedule_new_workflow(self, *, workflow: Any, input: Any, instance_id: str) -> str:  # noqa: A002
        raise AssertionError("no route under test schedules a workflow")

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _State | None:
        return self._state if instance_id == _INSTANCE else None


@contextmanager
def _serving(engine: _Engine) -> Iterator[TestClient]:
    """The producer's handler stack (`build_lance_service_app` installs exactly these two), FGA off."""
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(promotions.router)
    app.state.workflow_client = engine
    app.dependency_overrides[promotions.authenticate_subject] = lambda: "CiQwOGE4Njg0Yi1kYjg4"
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize("stored", _UNREADABLE)
def test_reading_it_answers_InvalidTableState(stored: str) -> None:
    with _serving(_Engine(stored)) as client:
        response = client.get(f"/promotions/{_INSTANCE}")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == ErrorCode.INVALID_TABLE_STATE


@pytest.mark.parametrize("stored", _UNREADABLE)
def test_deciding_it_answers_InvalidTableState_and_reaches_no_workflow(stored: str) -> None:
    """Not 503: a retry reads the same stored bytes and fails the same way, forever."""
    engine = _Engine(stored)
    with _serving(engine) as client:
        response = client.post(f"/promotions/{_INSTANCE}/decision", json={"approved": True})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == ErrorCode.INVALID_TABLE_STATE
    assert engine.raised == [], "a decision on a review nobody can read must not reach the workflow"


def test_the_log_names_the_instance_an_operator_must_clear(caplog: pytest.LogCaptureFixture) -> None:
    """The response carries no internals, so the log line is where an operator learns WHICH review is
    stuck and which fields failed."""
    with caplog.at_level(logging.WARNING, logger="medallion.api.promotions"), _serving(_Engine(_without_operation())) as client:
        client.get(f"/promotions/{_INSTANCE}")

    unreadable = [record for record in caplog.records if record.message == "medallion_promotion_review_unreadable"]
    assert unreadable, f"nothing named the unreadable review: {[record.message for record in caplog.records]}"
    assert getattr(unreadable[0], "instance_id", None) == _INSTANCE
    assert getattr(unreadable[0], "fields", None) == ["operation"]
