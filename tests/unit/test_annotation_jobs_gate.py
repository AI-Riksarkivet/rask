"""`POST /api/jobs/apply` is a SERVICE-ONLY seam, and now says so.

THE DEFECT. The jobs router submits a corpus-scale batch deriver and shipped with **no auth
dependency at all**, while every sibling task/project route on the same app carries
`CheckerDep`/`CurrentSubject`. Nothing marked the omission deliberate, so it read as an oversight —
and it is the same class as the unauthenticated `POST /flows/runs` the estate had just closed.

WHY `require_dapr_token` AND NOT THE NEIGHBOURS' FGA PAIR. `JobRequest` names a producer, an op, a
scope and an OPTIONAL dataset — there is no project id to check a per-project relation against, and
`scope.level="corpus"` is estate-wide by construction. Inventing a door for an object the request
never identifies would be a worse answer than naming the boundary the seam actually sits behind, and
it genuinely sits behind one: the gateway proxies only `/api/explorer/annotations` →
`/api/annotations`, so `/api/jobs` has NO public row and is reachable in-cluster only.

What `require_dapr_token` proves is "arrived via Dapr", never "trusted caller" — the gateway itself
invokes through Dapr. That is exactly the claim being made here, so the tests below assert the
service-only boundary, NOT a user-authorization one.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.v1.endpoints import jobs as jobs_ep
from service_kit.lakehouse.ns_errors import install_problem_handlers


BODY = {"producer": "grounding-dino", "op": "predict", "scope": {"level": "corpus", "keys": []}}


def _client() -> TestClient:
    app = FastAPI()
    # The translator every app using this guard installs in production. The Dapr-token refusal is a
    # `PermissionDeniedError` now rather than an `HTTPException`, so its 403 wears the same problem+json
    # envelope as every other error (open_fastapi-audit) — and a bare app that installs no translator
    # renders it as a 500 instead.
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(jobs_ep.router)
    return TestClient(app, raise_server_exceptions=False)


def test_the_router_declares_the_gate_at_ROUTER_level() -> None:
    """Router-level, not per-route: a NEW endpoint added to this file inherits the gate.

    The defect was one ungated route; gating each route individually would leave the next one open
    the same way, which is how this class recurs.
    """
    assert jobs_ep.router.dependencies, "the jobs router declares no dependencies — /apply is open again"


def test_a_PUBLIC_front_door_caller_is_refused() -> None:
    """The measured bypass: a public front door's Dapr app-token authenticates the PROXY, not the
    caller — so an invocation arriving through one is never legitimate for a sidecar-only route.

    This refusal is deliberately NOT conditional on `APP_API_TOKEN`, so it is the only guard that
    holds in dev, where the token is unset.
    """
    response = _client().post("/api/jobs/apply", json=BODY, headers={"dapr-caller-app-id": "gateway"})

    assert response.status_code == 403
    assert "public front door" in response.text


def test_a_WRONG_token_is_refused_when_one_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", "the-real-token")

    response = _client().post("/api/jobs/apply", json=BODY, headers={"dapr-api-token": "not-the-token"})

    assert response.status_code == 403


def test_a_MISSING_token_is_refused_when_one_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing must fail exactly like wrong — an absent header is not a bypass."""
    monkeypatch.setenv("APP_API_TOKEN", "the-real-token")

    response = _client().post("/api/jobs/apply", json=BODY)

    assert response.status_code == 403


def test_the_right_token_reaches_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate must not be so tight that the legitimate sidecar delivery cannot get through.

    Asserting `!= 403` rather than a specific success code on purpose: what happens past the door is
    the handler's business (it may 503 without a runner configured), and pinning that here would make
    this an unrelated test of the deriver.
    """
    monkeypatch.setenv("APP_API_TOKEN", "the-real-token")

    response = _client().post("/api/jobs/apply", json=BODY, headers={"dapr-api-token": "the-real-token"})

    assert response.status_code != 403, "a correctly-authenticated sidecar delivery was refused"


def test_the_dev_default_still_reaches_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """`APP_API_TOKEN` unset is the open dev default, and the token check is a no-op there.

    That is not a hole this test is blessing: `assert_app_token_configured` makes an unset token a
    STARTUP error once Dapr ingest is enabled, so the no-op can only ever apply in dev — and the
    public-caller refusal above still holds even here.
    """
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    response = _client().post("/api/jobs/apply", json=BODY)

    assert response.status_code != 403


def test_the_gate_precedes_the_BODY_so_a_refusal_costs_no_validation() -> None:
    """A router-level dependency runs before body validation: garbage from an unauthenticated caller
    must be refused as unauthorized (403), never reported as malformed (422). Leaking which payloads
    parse is a small oracle, and it is free to close."""
    response = _client().post("/api/jobs/apply", json={"nonsense": True}, headers={"dapr-caller-app-id": "gateway"})

    assert response.status_code == 403
