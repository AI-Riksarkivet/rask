"""A route suffix no rung map declares is REFUSED, and every route the catalog mounts declares one.

`catalog.api.fga_deps.authorize` gates a `{id}` route by the rung its suffix resolves to. A suffix the
maps do not name has two possible answers, and only one of them is fail-closed. A DEFAULT rung is a
permission nobody chose: at the writer rung it is one every plain data writer already holds, so a new
destructive verb ships open to all of them with nothing red, and a new read door refuses its whole
audience while the audit trail records a write. A REFUSAL is loud for every caller, owners included,
so the omission cannot survive the first request.

The refusal is the spec's `InternalError` (code 18, "Unexpected server/implementation error"), not
`PermissionDenied`: authorization runs after routing matched, so only a route shipped without its rung
reaches it. Code 15 would send the caller to ask for access no grant can give, and a 4xx stays off the
5xx series alerting pages on.

The walk at the bottom is what makes the refusal safe to ship: it drives the real guard over every
mounted route, so a route added without a rung fails HERE rather than in front of its first caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, cast

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from lance_namespace import InternalError
from openfga_sdk import OpenFgaClient
from starlette.types import Message

from catalog.api import fga_deps
from catalog.api.dependencies import get_fga_client
from catalog.api.security import authenticate
from catalog.api.v1.router import api_router
from catalog.core.config import Settings, get_settings
from service_kit.governed.audit import AUDIT_LOGGER, FAILURE
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse.ns_errors import install_problem_handlers


_ID = "acme$events"
_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")

#: Nothing calls a method on it: `fga.check` / `fga.batch_check` are replaced below before it is used.
_CLIENT = cast("OpenFgaClient", object())


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


def _token() -> IDToken:
    return IDToken(iss="https://idp.example", sub="alice", aud="lance", exp=2_000_000_000, iat=1_000_000_000)


def _request(template: str, body: dict[str, object]) -> Request:
    """A real Starlette request for `template` with every path parameter filled in."""
    params = {name: (_ID if name == "id" else f"{name}-1") for name in _PARAM.findall(template)}
    path = _PARAM.sub(lambda match: params[match.group(1)], template)
    payload = json.dumps(body).encode()

    async def receive() -> Message:
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": path, "path_params": params, "headers": [(b"content-type", b"application/json")], "query_string": b""}
    return Request(scope, receive)


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every `(relation, object)` the guard asks an OpenFGA that allows everything."""
    recorded: list[tuple[str, str]] = []

    async def check(_client: object, *, user: str, relation: str, obj: str, **_: Any) -> bool:
        del user
        recorded.append((relation, obj))
        return True

    async def batch_check(_client: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        del user
        recorded.extend((relation, obj) for obj in objects)
        return dict.fromkeys(objects, True)

    monkeypatch.setattr(fga_deps.fga, "check", check)
    monkeypatch.setattr(fga_deps.fga, "batch_check", batch_check)
    return recorded


def _authorize(template: str, body: dict[str, object] | None = None) -> None:
    asyncio.run(fga_deps.authorize(_request(template, body or {}), _settings(), _token(), _CLIENT))


@pytest.mark.parametrize(
    ("fga_type", "suffix"),
    [
        ("table", ""),
        ("table", "a_verb_nobody_declared"),
        ("namespace", "a_verb_nobody_declared"),
        ("materialized_view", "a_verb_nobody_declared"),
        # A type the resolver serves no doors for is refused too, rather than answered with the table's.
        ("classification", "describe"),
    ],
)
def test_an_undeclared_suffix_earns_no_rung(fga_type: str, suffix: str) -> None:
    with pytest.raises(InternalError, match="declares no rung"):
        fga_deps._action_relation(fga_type, suffix)


_UNDECLARED = "a_verb_nobody_declared"


@pytest.mark.parametrize(
    ("template", "resource"),
    [
        ("/management/v1/table/{id}/a_verb_nobody_declared", "table:acme$events"),
        ("/v1/namespace/{id}/a_verb_nobody_declared", "namespace:acme$events"),
        ("/v1/materialized_view/{id}/a_verb_nobody_declared", "materialized_view:acme$events"),
        ("/v1/transaction/{id}/a_verb_nobody_declared", "transaction:acme$events"),
        ("/management/v1/classification/{id}/a_verb_nobody_declared", "classification:acme$events"),
    ],
)
def test_the_guard_refuses_an_undeclared_door_before_asking_openfga(
    asked: list[tuple[str, str]], caplog: pytest.LogCaptureFixture, template: str, resource: str
) -> None:
    """Through `authorize`, not only the resolver: a guard that caught the refusal and fell back would
    leave every assertion above green while the route stayed open. The transaction and classification
    rows are here because those doors resolve in their own branches, which never consult
    `_action_relation`.

    The refusal is a DECISION like every other the guard makes, so it writes the same `audit()` record,
    and its ERROR line names the path and suffix because the 500 body an operator's caller sees is
    redacted."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)

    with pytest.raises(InternalError, match="declares no rung"):
        _authorize(template)

    assert asked == [], f"the guard asked OpenFGA {asked} for a door nobody declared"
    audited = [r.__dict__ for r in caplog.records if r.name == AUDIT_LOGGER]
    assert [(r["audit.action"], r["audit.outcome"], r["audit.subject"], r["audit.resource"], r["audit.reason"], r["audit.suffix"]) for r in audited] == [
        ("authz", FAILURE, "alice", resource, "undeclared_door", _UNDECLARED)
    ], audited
    path = template.replace("{id}", _ID)
    errors = [r for r in caplog.records if r.name == fga_deps.log.name and r.levelno == logging.ERROR]
    assert [(r.getMessage(), r.__dict__.get("path"), r.__dict__.get("suffix")) for r in errors] == [("undeclared_door", path, _UNDECLARED)]


def test_an_undeclared_door_answers_the_specs_internal_error_on_the_wire(asked: list[tuple[str, str]]) -> None:
    """The status and code a client reads, through the real problem handlers: 500 with code 18, the
    detail redacted like every 5xx, and the endpoint never ran."""
    ran: list[str] = []
    router = APIRouter(dependencies=[Depends(fga_deps.authorize)])

    @router.post("/v1/table/{id}/a_verb_nobody_declared")
    def undeclared(id: str) -> dict[str, str]:
        ran.append(id)
        return {"ran": id}

    app = FastAPI()
    app.include_router(router)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_fga_client] = lambda: _CLIENT
    app.dependency_overrides[authenticate] = _token

    response = TestClient(app, raise_server_exceptions=False).post(f"/v1/table/{_ID}/a_verb_nobody_declared", json={})

    assert (response.status_code, response.headers["content-type"]) == (500, "application/problem+json"), response.text
    assert (response.json()["code"], response.json()["detail"]) == (18, "Internal Server Error"), response.json()
    assert ran == [] and asked == []


def _guarded_templates() -> list[str]:
    """Every mounted route path the router guard authorizes by its `{id}`, walked off the composed app.

    `iter_route_contexts` rather than `api_router.routes`: this FastAPI keeps included routers as
    opaque wrappers, and a walk over them finds nothing and passes by iterating nothing.
    """
    app = FastAPI()
    app.include_router(api_router)
    templates: set[str] = set()
    for context in iter_route_contexts(app.routes):
        path = context.path
        if not isinstance(context.original_route, APIRoute) or path is None or "{id}" not in path:
            continue
        if fga_deps._resource_for(_PARAM.sub("x", path)) is not None:
            templates.add(path)
    return sorted(templates)


def _grant_body(template: str) -> dict[str, object]:
    """The grant doors read the rung from the body, so they need one the model can grant on that type."""
    if not template.endswith(("/access/grant", "/access/revoke")):
        return {}
    resource = fga_deps._resource_for(_PARAM.sub("x", template))
    assert resource is not None
    grantable = sorted(fga_deps._grant_actions(fga_deps._FGA_TYPE[resource]))
    assert grantable, f"{resource} defines no can_grant_* rung, so its grant door can never be authorized"
    return {"relation": grantable[0].removeprefix("can_grant_"), "user": "bob"}


def test_the_walk_sees_every_guarded_resource_on_both_mounts() -> None:
    """Without this the walk below could pass by iterating a fraction of the catalog."""
    templates = _guarded_templates()
    resources = {fga_deps._resource_for(_PARAM.sub("x", template)) for template in templates}
    assert resources == set(fga_deps._RESOURCES), f"the walk reached {sorted(r for r in resources if r)}"
    assert any(t.startswith("/v1/") for t in templates), "no route on the spec mount"
    assert any(t.startswith("/management/v1/") for t in templates), "no route on the management mount"


@pytest.mark.parametrize("template", _guarded_templates())
def test_every_guarded_route_resolves_to_a_declared_rung(asked: list[tuple[str, str]], template: str) -> None:
    """A mounted route whose suffix no map declares is refused for everyone, so it must fail here first."""
    _authorize(template, _grant_body(template))
    assert asked, f"{template} was waved through without a single check"
