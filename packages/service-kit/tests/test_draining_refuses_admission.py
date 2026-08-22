"""A draining pod still accepted new work, then died holding it.

Nine lifespans set `app.state.shutting_down`. Exactly ONE thing reads it — `/readyz`
(`service_kit/probes.py:56`) — so the flag's entire effect is to make Kubernetes stop routing NEW
connections. That is the wrong half of the problem for this estate: the doors that matter are
sidecar-delivered. Dapr's own pub/sub delivery does not consult a readiness probe, so during every
rolling deploy a pod that has begun shutting down keeps accepting cascade triggers and run
submissions, starts work it cannot finish, and takes the run down with it.

`open_batch_process.md` B6, verbatim: "Refuse new runs while draining — the flag exists, nothing
reads it on admission." §6 rejected only the `POST /drain` ENDPOINT (a process-local flag cannot mean
"this deployment is draining" behind a multi-replica Service) and adopted the admission half.

THE TWO ANSWERS ARE NOT INTERCHANGEABLE, which is why this is a dependency and not an `if`:

* An HTTP caller gets 503. It holds the request and can retry; a 4xx would tell it the request was
  wrong, which is a lie about a pod that is merely leaving.
* A sidecar-delivered route gets RETRY, never DROP. DROP is final and there is no DLQ on these
  topics, so dropping a trigger because this replica happened to be draining silently cancels a
  cascade — the exact class of failure the medallion's own comments call out. RETRY hands it back to
  the broker, which redelivers to a replica that is still alive.

A route that picks the wrong one is worse than no gate at all: it converts a survivable restart into
either a lost cascade or a caller that gives up.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from service_kit.draining import draining, refuse_when_draining, retry_when_draining


class _State:
    def __init__(self, *, shutting_down: bool) -> None:
        self.shutting_down = shutting_down


def _request(*, shutting_down: bool) -> Request:
    app = FastAPI()
    app.state.shutting_down = shutting_down
    scope = {"type": "http", "app": app, "headers": [], "method": "GET", "path": "/"}
    return Request(scope)


class TestThePredicate:
    def test_a_draining_process_is_draining(self) -> None:
        assert draining(_request(shutting_down=True)) is True

    def test_a_healthy_one_is_not(self) -> None:
        assert draining(_request(shutting_down=False)) is False

    def test_an_app_that_never_set_the_flag_is_not_draining(self) -> None:
        """Three services (ingest, compute, flows) set no lifecycle flags at all. They must read as
        SERVING — defaulting an unset flag to "draining" would take them permanently out of service
        the moment this dependency was applied."""
        app = FastAPI()
        scope = {"type": "http", "app": app, "headers": [], "method": "GET", "path": "/"}
        assert draining(Request(scope)) is False


class TestTheHttpDoorRefusesWith503:
    def _client(self, *, shutting_down: bool) -> TestClient:
        app = FastAPI()
        app.state.shutting_down = shutting_down

        @app.post("/produce", dependencies=[Depends(refuse_when_draining)])
        async def produce() -> dict[str, str]:
            return {"status": "accepted"}

        return TestClient(app, raise_server_exceptions=False)

    def test_it_serves_while_healthy(self) -> None:
        assert self._client(shutting_down=False).post("/produce").status_code == 200

    def test_it_refuses_503_while_draining(self) -> None:
        assert self._client(shutting_down=True).post("/produce").status_code == 503

    def test_the_refusal_is_a_problem_document_naming_the_cause(self) -> None:
        """An operator reading a 503 must be able to tell "this pod is leaving" from "this pod is
        broken" — they lead to opposite actions."""
        resp = self._client(shutting_down=True).post("/produce")
        assert "application/problem+json" in resp.headers.get("content-type", "")
        assert "drain" in resp.text.lower() or "shutting down" in resp.text.lower()

    def test_it_advertises_retryability(self) -> None:
        """Retry-After is what turns a 503 into "come back", rather than a caller's backoff guess."""
        assert self._client(shutting_down=True).post("/produce").headers.get("retry-after")


class TestTheSidecarDoorAsksForRedelivery:
    def _client(self, *, shutting_down: bool) -> TestClient:
        app = FastAPI()
        app.state.shutting_down = shutting_down

        @app.post("/bronze-arrival")
        async def arrival(verdict: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None) -> dict[str, str]:
            return verdict or {"status": "SUCCESS"}

        return TestClient(app, raise_server_exceptions=False)

    def test_it_handles_the_event_while_healthy(self) -> None:
        assert self._client(shutting_down=False).post("/bronze-arrival").json() == {"status": "SUCCESS"}

    def test_it_asks_for_REDELIVERY_while_draining(self) -> None:
        resp = self._client(shutting_down=True).post("/bronze-arrival")
        assert resp.status_code == 200, "a subscription answers 200 with a verdict, never an HTTP error"
        assert resp.json() == {"status": "RETRY"}

    def test_it_never_answers_DROP(self) -> None:
        """DROP is final and these topics have no DLQ, so dropping a trigger because THIS replica is
        draining silently cancels the cascade."""
        assert self._client(shutting_down=True).post("/bronze-arrival").json()["status"] != "DROP"

    def test_it_never_answers_SUCCESS_while_draining(self) -> None:
        """A SUCCESS ack is indistinguishable from having done the work — the broker discards the
        message and nobody ever runs it."""
        assert self._client(shutting_down=True).post("/bronze-arrival").json()["status"] != "SUCCESS"


class TestTheTwoAnswersAreDistinct:
    def test_they_are_not_the_same_dependency(self) -> None:
        """Applying the HTTP one to a subscription route would raise a 503 at a Dapr sidecar, which
        it reads as a delivery failure and retries — accidentally correct today, and silently wrong
        the moment resiliency policy treats 5xx as terminal. The intent must be explicit."""
        assert refuse_when_draining is not retry_when_draining


@pytest.mark.parametrize("dep", [refuse_when_draining, retry_when_draining])
def test_neither_dependency_touches_a_closing_resource(dep: Callable[..., object]) -> None:
    """`/readyz`'s comment states the rule this shares: once shutting_down flips, that is the answer
    regardless of anything else, and the check must never reach for a dependency that is already
    closing. A drain gate that opened a DB handle would fail during exactly the window it exists for."""
    import inspect

    source = inspect.getsource(dep)
    for forbidden in ("await ", "client", "session", "dataset", "connect"):
        assert forbidden not in source, f"{dep} reaches for {forbidden!r} during shutdown"
