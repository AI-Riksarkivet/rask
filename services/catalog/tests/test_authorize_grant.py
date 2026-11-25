"""``authorize_grant`` — the per-rung gate on grant/revoke.

Granting used to clear the owner bar (``access/grant`` mapped to ``can_drop``/``can_delete``), which
welded handing out access to owning the data. The rung being handed out lives in the BODY, so a
path-suffix gate cannot express "may grant reader" — this dependency reads the body, exactly as
``authorize_batch`` already does for the batch routes.

It runs BEFORE Pydantic on client-controlled JSON, so every malformed shape must be a 403 rather than
a 500 — and, more importantly, a rung with no ``can_grant_*`` on the type must be refused HERE rather
than reaching OpenFGA, where an undefined relation is a 400 that fails closed to a 503 for every
caller: an outage wearing a permission error's clothes.

Sync via ``asyncio.run`` — no async-plugin dependency (this repo does not set ``asyncio_mode``).
"""

import asyncio
import json
from typing import Any, cast

import pytest
from fastapi import Request
from lance_namespace import PermissionDeniedError
from starlette.types import Message

from catalog.api.authz import grant_authority
from lakehouse.naming import UUID_PROPERTY
from tests.unit.named_resolver import NamedResolver


def _run(body: Any) -> None:
    """Drive the gate on a grant of the namespace ``gold``. The client is a bare object, so a gate that asked OpenFGA
    anything would fail with its ``AttributeError``: only a refusal made WITHOUT consulting OpenFGA raises the
    ``PermissionDeniedError`` the callers expect."""
    asyncio.run(
        grant_authority.authorize_grant(
            _request(body),
            cast(Any, object()),
            NamedResolver(),
            user="dex~alice",
            fga_type="namespace",
            segments=["gold"],
        )
    )


def _request(body: Any) -> Request:  # noqa: ANN401 — any JSON the client sends
    """A POST whose body is ``body`` as JSON, as the server receives it."""
    payload = json.dumps(body).encode()

    async def _receive() -> Message:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/", "headers": []}, _receive)


#: alice's namespace, the object every grant below is asked on.
_GRANTED_ON = "lance_catalog$db"


@pytest.mark.parametrize(
    ("caller", "held", "door", "body", "status", "detail"),
    [
        pytest.param(
            "kevin",
            [("reader", None), ("pass_grants", None)],
            "grant",
            {"user": "dex~bob", "relation": "reader"},
            200,
            None,
            id="a-delegate-may-grant-the-rung-it-holds",
        ),
        pytest.param(
            "kevin",
            [("reader", None), ("pass_grants", None)],
            "grant",
            {"user": "dex~bob", "relation": "writer"},
            403,
            None,
            id="and-is-refused-the-rung-above-it",
        ),
        pytest.param("mallory", [], "grant", {"user": "dex~bob", "relation": "owner"}, 403, None, id="a-caller-holding-nothing-is-refused"),
        pytest.param("alice", [], "grant", {"user": "dex~alice", "relation": "owner"}, 200, None, id="re-granting-yourself-what-you-hold-is-allowed"),
        pytest.param(
            "judy",
            [("manage_grants", None), ("assignee", "role:stewards")],
            "grant",
            {"user": "role:stewards#assignee", "relation": "owner"},
            403,
            "may not raise your own access",
            id="a-grant-to-a-userset-you-are-inside-raises-your-own-access",
        ),
        pytest.param(
            "judy",
            [("manage_grants", None), ("owner", None)],
            "revoke",
            {"user": "dex~judy", "relation": "owner"},
            200,
            None,
            id="revoking-your-own-rung-is-never-an-elevation",
        ),
        pytest.param(
            "judy",
            [("manage_grants", None)],
            "revoke",
            {"user": "dex~judy", "relation": "writer"},
            200,
            None,
            id="revoking-a-rung-you-do-not-hold-is-no-elevation-either",
        ),
    ],
)
def test_a_grant_is_gated_on_the_rung_in_its_body_and_never_raises_its_callers_own_access(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    caller: str,
    held: list[tuple[str, str | None]],
    door: str,
    body: dict[str, str],
    status: int,
    detail: str | None,
) -> None:
    """The rung handed out lives in the BODY, so the gate asks ``can_grant_<rung>`` there (``can_revoke_grant`` on a
    revoke), and a grant may not hand its caller a rung they do not already hold, directly or through a userset they are
    inside. OpenFGA cannot express "the grantee is not the caller", so this separation of duties is the door's.

    Judged on the real model: each caller holds the tuples ``held`` names on alice's namespace (or the object a pair
    names), seeded as the catalog's declaration grants them. A refusal is 403 code 15 and writes nothing; an admitted
    grant writes the tuple its body names unless it is already held, and an admitted revoke removes it if it is.
    """
    catalog = governed_catalog
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    described = catalog.client.post(f"/v1/namespace/{_GRANTED_ON}/describe", json={}, headers=catalog.bearer("alice"))
    namespace = f"namespace:{described.json()['properties'][UUID_PROPERTY]}"
    if held:
        catalog.store.write(*((catalog.person(caller), relation, obj or namespace) for relation, obj in held))
    before = catalog.store.tuples()

    answered = catalog.client.post(f"/management/v1/namespace/{_GRANTED_ON}/access/{door}", json=body, headers=catalog.bearer(caller))

    assert answered.status_code == status, answered.text
    if status == 403:
        assert answered.json()["code"] == 15, answered.text
    if detail is not None:
        assert detail in answered.json()["detail"], answered.text
    grantee = body["user"] if body["user"].startswith("role:") else f"user:{body['user']}"
    changed = catalog.store.tuples() ^ before
    named = (grantee, body["relation"], namespace)
    expected = {named} if status == 200 and (door == "grant") != (named in before) else set()
    assert changed == expected, f"the door changed {changed}, not {expected}"


# --------------------------------------------------------------------------- #
# Fail-closed on client-controlled input — each of these must deny WITHOUT a Check
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([], id="list-body"),
    ],
)
def test_a_non_dict_body_is_denied_not_crashed(body: Any) -> None:
    with pytest.raises(PermissionDeniedError, match=r"^malformed access request body$"):
        _run(body)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"user": "dex~bob"}, id="missing-relation"),
    ],
)
def test_a_malformed_relation_is_denied(body: Any) -> None:
    with pytest.raises(PermissionDeniedError, match=r"^malformed access request body: 'relation' must be a string$"):
        _run(body)


@pytest.mark.parametrize("gate", [pytest.param(grant_authority.authorize_grant, id="grant"), pytest.param(grant_authority.authorize_revoke, id="revoke")])
def test_a_phantom_rung_is_refused_before_reaching_openfga(gate: Any) -> None:
    """The phantom-relation guard, on a grant and a revoke alike. Passing the structural `parent` edge through would
    build ``can_grant_parent`` and ask OpenFGA for a relation the model does not define — a 400 that fails closed to a
    503 for EVERY caller, owners included. A rung the model cannot grant is not a rung the API may be asked to revoke
    either, so the shape check holds whichever authority decides. What matters is that it is
    refused locally, on a bare-object client that any question to OpenFGA would fail on."""
    request = _request({"user": "dex~bob", "relation": "parent"})

    with pytest.raises(PermissionDeniedError, match=r"^'parent' is not a grantable rung on namespace$"):
        asyncio.run(gate(request, cast(Any, object()), NamedResolver(), user="dex~alice", fga_type="namespace", segments=["gold"]))
