"""``_authorize_grant`` — the per-rung gate on grant/revoke.

Granting used to clear the owner bar (``access/grant`` mapped to ``can_drop``/``can_delete``), which
welded handing out access to owning the data. The rung being handed out lives in the BODY, so a
path-suffix gate cannot express "may grant reader" — this dependency reads the body, exactly as
``_authorize_batch`` already does for the batch routes.

It runs BEFORE Pydantic on client-controlled JSON, so every malformed shape must be a 403 rather than
a 500 — and, more importantly, a rung with no ``can_grant_*`` on the type must be refused HERE rather
than reaching OpenFGA, where an undefined relation is a 400 that fails closed to a 503 for every
caller: an outage wearing a permission error's clothes.

Sync via ``asyncio.run`` — no async-plugin dependency (this repo does not set ``asyncio_mode``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from catalog.api import fga_deps
from catalog.core.config import Settings
from lance_namespace import PermissionDeniedError


def _run(
    monkeypatch: pytest.MonkeyPatch,
    body: Any,
    *,
    fga_type: str = "namespace",
    segments: list[str] | None = None,
    allow: bool = True,
) -> list[tuple[str, str, str]]:
    """Drive the gate against a fake Check. Returns every (user, relation, object) it asked about —
    an empty list proves the gate refused WITHOUT consulting OpenFGA."""
    asked: list[tuple[str, str, str]] = []

    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        asked.append((user, relation, obj))
        return allow

    monkeypatch.setattr(fga_deps.fga, "check", fake_check)
    request = cast(fga_deps.Request, SimpleNamespace(json=lambda: _as_coro(body)))
    settings = cast(Settings, SimpleNamespace(delimiter="$"))
    asyncio.run(
        fga_deps._authorize_grant(
            request,
            cast(Any, object()),
            settings,
            user="alice",
            fga_type=fga_type,
            segments=segments if segments is not None else ["gold"],
        )
    )
    return asked


async def _as_coro(value: Any) -> Any:
    return value


def test_gates_on_the_rung_in_the_body_not_a_blanket_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _run(monkeypatch, {"user": "bob", "relation": "reader"})
    assert asked == [("alice", "can_grant_reader", "namespace:gold")]


def test_each_rung_maps_to_its_own_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the split: granting `owner` and granting `reader` are different privileges."""
    for rung in ("owner", "writer", "reader", "validator", "manage_grants", "pass_grants"):
        asked = _run(monkeypatch, {"user": "bob", "relation": rung})
        assert asked == [("alice", f"can_grant_{rung}", "namespace:gold")]


def test_the_table_surface_gates_on_the_table_object(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _run(monkeypatch, {"user": "bob", "relation": "writer"}, fga_type="table", segments=["gold", "catalog"])
    assert asked == [("alice", "can_grant_writer", "table:gold$catalog")]


def test_a_denied_check_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PermissionDeniedError):
        _run(monkeypatch, {"user": "bob", "relation": "owner"}, allow=False)


# --------------------------------------------------------------------------- #
# Fail-closed on client-controlled input — each of these must deny WITHOUT a Check
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([], id="list-body"),
        pytest.param("reader", id="string-body"),
        pytest.param(None, id="null-body"),
    ],
)
def test_a_non_dict_body_is_denied_not_crashed(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    with pytest.raises(PermissionDeniedError):
        _run(monkeypatch, body)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"user": "bob"}, id="missing-relation"),
        pytest.param({"user": "bob", "relation": 7}, id="non-string-relation"),
        pytest.param({"user": "bob", "relation": ""}, id="empty-relation"),
        pytest.param({"user": "bob", "relation": None}, id="null-relation"),
    ],
)
def test_a_malformed_relation_is_denied(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    with pytest.raises(PermissionDeniedError):
        _run(monkeypatch, body)


@pytest.mark.parametrize(
    "relation",
    [
        pytest.param("parent", id="structural-edge"),
        pytest.param("child", id="inverse-edge"),
        pytest.param("can_read_data", id="derived-action"),
        pytest.param("admin", id="rung-of-another-type"),
        pytest.param("nonsense", id="unknown"),
    ],
)
def test_a_rung_with_no_grant_action_is_refused_before_reaching_openfga(monkeypatch: pytest.MonkeyPatch, relation: str) -> None:
    """The phantom-relation guard. Passing these through would build ``can_grant_parent`` and ask
    OpenFGA for a relation the model does not define — a 400 that fails closed to a 503 for EVERY
    caller, owners included. The assertion that matters is the empty ask-list: refused locally."""
    asked: list[tuple[str, str, str]] = []

    async def fake_check(_c: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        asked.append((user, relation, obj))
        return True

    monkeypatch.setattr(fga_deps.fga, "check", fake_check)
    request = cast(fga_deps.Request, SimpleNamespace(json=lambda: _as_coro({"user": "bob", "relation": relation})))
    settings = cast(Settings, SimpleNamespace(delimiter="$"))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(fga_deps._authorize_grant(request, cast(Any, object()), settings, user="alice", fga_type="namespace", segments=["gold"]))
    assert asked == [], f"{relation!r} reached OpenFGA as a phantom relation"


def test_the_grant_actions_come_from_the_compiled_model() -> None:
    """Never a hand-kept list — a renamed rung must drop out here the same turn it changes in the DSL."""
    for fga_type in ("warehouse", "namespace", "table"):
        actions = fga_deps._grant_actions(fga_type)
        assert actions, f"no can_grant_* enumerated for {fga_type}"
        assert all(a.startswith("can_grant_") for a in actions)
        # Every rung the grant API will accept must have a gate, or granting it would be unreachable.
        for rung in ("owner", "writer", "reader", "validator", "manage_grants", "pass_grants"):
            assert f"can_grant_{rung}" in actions, f"{fga_type} can grant {rung} with no gate"
