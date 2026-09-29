"""A draining pod still accepted new work, then died holding it.

Nine lifespans set `app.state.shutting_down`. Exactly ONE thing reads it — `/readyz`
(`service_kit/probes.py:56`) — so the flag's entire effect is to make Kubernetes stop routing NEW
connections. That is the wrong half of the problem for this estate: the doors that matter are
sidecar-delivered. Dapr's own pub/sub delivery does not consult a readiness probe, so during every
rolling deploy a pod that has begun shutting down keeps accepting cascade triggers and run
submissions, starts work it cannot finish, and takes the run down with it.

`docs/architecture/batch-processing-invariants.md` B6, verbatim: "Refuse new runs while draining — the flag exists, nothing
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

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from service_kit.draining import draining, refuse_when_draining, retry_when_draining
from service_kit.exceptions import register_handlers


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

    def test_an_app_that_never_set_the_flag_is_not_draining(self) -> None:
        """Three services (ingest, compute, flows) set no lifecycle flags at all. They must read as
        SERVING — defaulting an unset flag to "draining" would take them permanently out of service
        the moment this dependency was applied."""
        app = FastAPI()
        # The refusal is a `ServiceUnavailableError` now rather than an `HTTPException` carrying a
        # Content-Type header (open_fastapi-audit: that header claimed problem+json over a `{"detail"}`
        # body). `DomainError` subclasses `HTTPException`, so without this the starlette fallback still
        # answers 503 with the header and no envelope — the defect this file asserts against.
        register_handlers(app)
        scope = {"type": "http", "app": app, "headers": [], "method": "GET", "path": "/"}
        assert draining(Request(scope)) is False


class TestTheHttpDoorRefusesWith503:
    def _client(self, *, shutting_down: bool) -> TestClient:
        app = FastAPI()
        # The refusal is a `ServiceUnavailableError` now rather than an `HTTPException` carrying a
        # Content-Type header (open_fastapi-audit: that header claimed problem+json over a `{"detail"}`
        # body). `DomainError` subclasses `HTTPException`, so without this the starlette fallback still
        # answers 503 with the header and no envelope — the defect this file asserts against.
        register_handlers(app)
        app.state.shutting_down = shutting_down

        @app.post("/produce", dependencies=[Depends(refuse_when_draining)])
        async def produce() -> dict[str, str]:
            return {"status": "accepted"}

        return TestClient(app, raise_server_exceptions=False)

    def test_it_serves_while_healthy(self) -> None:
        assert self._client(shutting_down=False).post("/produce").status_code == 200

    def test_it_refuses_503_while_draining(self) -> None:
        assert self._client(shutting_down=True).post("/produce").status_code == 503


class TestTheSidecarDoorAsksForRedelivery:
    def _client(self, *, shutting_down: bool) -> TestClient:
        app = FastAPI()
        # The refusal is a `ServiceUnavailableError` now rather than an `HTTPException` carrying a
        # Content-Type header (open_fastapi-audit: that header claimed problem+json over a `{"detail"}`
        # body). `DomainError` subclasses `HTTPException`, so without this the starlette fallback still
        # answers 503 with the header and no envelope — the defect this file asserts against.
        register_handlers(app)
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
