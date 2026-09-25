"""A route suffix no rung map declares is REFUSED, and every route the catalog mounts declares one.

`catalog.api.fga_deps.authorize` gates a `{id}` route by the rung its suffix resolves to. A suffix the
maps do not name has two possible answers, and only one of them is fail-closed. A DEFAULT rung is a
permission nobody chose: at the writer rung it is one every plain data writer already holds, so a new
destructive verb ships open to all of them with nothing red, and a new read door refuses its whole
audience while the audit trail records a write. A REFUSAL is loud for every caller, owners included,
so the omission cannot survive the first request.

The walk at the bottom is what makes the refusal safe to ship: it drives the real guard over every
mounted route, so a route added without a rung fails HERE rather than in front of its first caller.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute, iter_route_contexts
from lance_namespace import PermissionDeniedError
from openfga_sdk import OpenFgaClient
from starlette.types import Message

from catalog.api import fga_deps
from catalog.api.v1.router import api_router
from catalog.core.config import Settings
from service_kit.governed.oidc import IDToken


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
    with pytest.raises(PermissionDeniedError, match="declares no rung"):
        fga_deps._action_relation(fga_type, suffix)


@pytest.mark.parametrize(
    "template",
    [
        "/management/v1/table/{id}/a_verb_nobody_declared",
        "/v1/namespace/{id}/a_verb_nobody_declared",
        "/v1/materialized_view/{id}/a_verb_nobody_declared",
        "/v1/transaction/{id}/a_verb_nobody_declared",
    ],
)
def test_the_guard_refuses_an_undeclared_door_before_asking_openfga(asked: list[tuple[str, str]], template: str) -> None:
    """Through `authorize`, not only the resolver: a guard that caught the refusal and fell back would
    leave every assertion above green while the route stayed open. The transaction row is here because
    its doors resolve in `_authorize_transaction`, which never consults `_action_relation`."""
    with pytest.raises(PermissionDeniedError, match="declares no rung"):
        _authorize(template)
    assert asked == [], f"the guard asked OpenFGA {asked} for a door nobody declared"


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
