"""The permission SELF-view — ``POST /v1/{table,namespace}/{id}/access/my-permissions``.

Three properties, and each exists because its opposite was the reason no such surface shipped before:

1. It answers with a ``check`` PER RELATION for the caller — never ``list_users``. The sibling
   ``access/list`` enumerates who holds what, which discloses principals and is why that route clears
   the owner bar. "What may I do here" describes only the person asking.
2. It takes NO subject. A self-view that accepts a ``user`` is the enumeration question renamed, and
   would need the owner gate back.
3. The subject is prefix-aware. An OIDC ``sub`` may already carry a type prefix; prefixing
   unconditionally yields ``user:user:<sub>``, which matches no tuple, so every relation answers a
   correct-looking ``false`` and the page renders "you may do nothing" for an owner.

Sync via ``asyncio.run`` — no async-plugin dependency (this repo does not set ``asyncio_mode``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from catalog.api.v1.endpoints import access
from catalog.core.config import Settings
from lance_namespace import ServiceUnavailableError, UnauthenticatedError, UnsupportedOperationError

from service_kit.governed.oidc import IDToken


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sub: str = "alice",
    allow: set[str] | None = None,
    fga_type: str = "namespace",
    ident: str = "gold",
    fga_enabled: bool = True,
    token_present: bool = True,
    outage: bool = False,
) -> tuple[access.MyPermissionsResponse, list[tuple[str, str, str]]]:
    """Drive `_my_permissions` against a fake Check. Returns the response + every (user, relation, obj)
    the endpoint asked about, so a test can assert on the QUESTIONS as well as the answers."""
    asked: list[tuple[str, str, str]] = []

    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        if outage:
            raise ServiceUnavailableError("fga down")
        asked.append((user, relation, obj))
        return relation in (allow or set())

    monkeypatch.setattr(access.fga, "check", fake_check)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object())))
    settings = cast(Settings, SimpleNamespace(fga_enabled=fga_enabled, delimiter="$"))
    token = cast(IDToken, SimpleNamespace(sub=sub)) if token_present else None
    resp = asyncio.run(access._my_permissions(cast(access.Request, request), settings, token, fga_type, ident))
    return resp, asked


def test_answers_every_can_relation_the_model_defines_on_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """The relation set is the model's, read from model.json — never a hand-kept list, which would
    drift into a phantom relation (an OpenFGA 400 that fails closed to a 503 for every caller)."""
    resp, asked = _run(monkeypatch, allow={"can_get_metadata"})
    assert set(resp.permissions) == set(access._can_relations("namespace"))
    assert {r for _u, r, _o in asked} == set(access._can_relations("namespace"))


def test_reports_the_callers_own_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    resp, _ = _run(monkeypatch, allow={"can_get_metadata", "can_delete"})
    assert resp.permissions["can_get_metadata"] is True
    assert resp.permissions["can_delete"] is True
    assert resp.permissions["can_create_table"] is False


def test_every_check_is_asked_about_the_caller_and_this_object_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-view must never probe a third party — that is `access/check`'s job, behind the owner
    bar. One subject, one object, N relations."""
    resp, asked = _run(monkeypatch, allow=set())
    assert {u for u, _r, _o in asked} == {"user:alice"}
    assert {o for _u, _r, o in asked} == {"namespace:gold"}
    assert resp.subject == "user:alice"
    assert resp.object == "namespace:gold"


def test_a_sub_that_already_carries_a_prefix_is_not_double_prefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`user:user:alice` matches no tuple, so the whole answer would be a plausible all-false. Same
    rule the home zone's /settings gate applies to `me.sub`."""
    resp, asked = _run(monkeypatch, sub="user:alice", allow={"can_get_metadata"})
    assert resp.subject == "user:alice"
    assert {u for u, _r, _o in asked} == {"user:alice"}
    assert resp.permissions["can_get_metadata"] is True


def test_the_table_surface_answers_the_table_relation_set(monkeypatch: pytest.MonkeyPatch) -> None:
    resp, _ = _run(monkeypatch, fga_type="table", ident="gold$catalog", allow={"can_read_data"})
    assert set(resp.permissions) == set(access._can_relations("table"))
    assert resp.permissions["can_read_data"] is True
    assert resp.object == "table:gold$catalog"


def test_no_token_is_refused_rather_than_answered_for_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    """The siblings fall back to "anonymous" for their AUDIT row; here the subject IS the subject of
    every Check, so an anonymous fallback would silently answer a different question."""
    with pytest.raises(UnauthenticatedError):
        _run(monkeypatch, token_present=False)


def test_auth_off_stack_is_unsupported_not_all_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """With FGA off there are no verdicts to report. Answering all-true would make a dev stack teach
    the UI the wrong shape; answering all-false would hide every control from everyone."""
    with pytest.raises(UnsupportedOperationError):
        _run(monkeypatch, fga_enabled=False)


def test_fga_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ServiceUnavailableError):
        _run(monkeypatch, outage=True)
