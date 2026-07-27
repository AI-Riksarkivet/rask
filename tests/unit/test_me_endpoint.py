"""Unit tests for the caller self-describe endpoint (``GET /v1/me``, endpoints/me.py).

Drives the handler directly (no TestClient) with fake settings + FGA wrappers, mirroring the repo's
direct-call style (``test_projects_endpoint.py``). Pins the frozen contract: any VERIFIED token → 200
with ``{sub, name, email, estate_admin, projects}``; anonymous → 401; FGA off → ``estate_admin=true``,
``projects=[]`` (dev parity); ``estate_admin`` = ``can_observe_events`` on the fixed root object;
``projects`` via the #51 list wrappers (admin first, then member, deduped) under a shared 2s budget,
degrading PER RELATION on an FGA outage — the endpoint never 5xxes over authz-lookup health.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from catalog.api.v1.endpoints import me as ep
from common.oidc import IDToken
from lance_namespace import ServiceUnavailableError, UnauthenticatedError


def _settings(*, fga_enabled: bool = False) -> Any:
    return SimpleNamespace(fga_enabled=fga_enabled, fga_root_object="warehouse:lance_catalog")


def _token(**claims: Any) -> IDToken:
    return IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1, **claims)


def _me(settings: Any, *, token: Any, client: Any = None) -> ep.MeResponse:
    return asyncio.run(ep.get_me(settings=settings, token=token, client=client))


# ── the authn floor: anonymous → 401, in every configuration ─────────────────────────────────────────


@pytest.mark.parametrize("fga_enabled", [False, True])
def test_anonymous_is_401(fga_enabled: bool) -> None:
    # There is no self to describe without a verified token — OIDC off (token None) and a missing bearer
    # both land here as token=None; the handler owns the 401 because the router gate no-ops with FGA off.
    with pytest.raises(UnauthenticatedError):
        _me(_settings(fga_enabled=fga_enabled), token=None, client=object() if fga_enabled else None)


# ── FGA off: dev parity — full standing, no tenant list ──────────────────────────────────────────────


def test_fga_off_dev_parity() -> None:
    result = _me(_settings(), token=_token(name="Alice A", email="alice@example.com"))
    assert result.sub == "alice"
    assert result.name == "Alice A" and result.email == "alice@example.com"
    assert result.estate_admin is True  # every require_* gate is a no-op there — say so honestly
    assert result.projects == []


def test_missing_claims_serialize_as_none() -> None:
    # Dex tokens without `openid email` scope carry no name/email — null, never a fabricated string.
    result = _me(_settings(), token=_token())
    assert result.name is None and result.email is None


# ── governed: estate check + admin-first project lists, deduped ──────────────────────────────────────


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    estate: Any = True,
    lists: dict[str, Any] | None = None,
    calls: list[tuple[str, str]] | None = None,
) -> None:
    """Fake the two FGA wrappers: `estate`/`lists` values may be exceptions (raised) or results."""
    lists = lists or {}

    async def _fake_check(_client: Any, *, user: str, relation: str, obj: str, **kw: Any) -> bool:
        if calls is not None:
            calls.append((relation, obj))
        assert kw == {"retry_attempts": 1}  # single attempt under the tight budget (the /v1/projects rule)
        if isinstance(estate, Exception):
            raise estate
        return bool(estate)

    async def _fake_list(_client: Any, *, user: str, relation: str, object_type: str, **kw: Any) -> Any:
        if calls is not None:
            calls.append((relation, object_type))
        assert kw == {"retry_attempts": 1}
        outcome = lists.get(relation, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(ep.fga, "check", _fake_check)
    monkeypatch.setattr(ep.fga, "list_objects", _fake_list)


def test_governed_shape_and_admin_first_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    # The model folds admins into members (`member: ... or admin`), so acme appears in BOTH lists — the
    # admin row must win and the duplicate member row must be dropped.
    _wire(
        monkeypatch,
        estate=True,
        lists={
            "can_administer": ["project:acme"],
            "member": ["project:globex", "project:acme"],
        },
        calls=calls,
    )
    result = _me(_settings(fga_enabled=True), token=_token(), client=object())
    assert result.estate_admin is True
    assert [(p.project, p.role) for p in result.projects] == [("acme", "admin"), ("globex", "member")]
    # The estate check targets the estate-OBSERVER action on the FIXED root object; the lists run
    # admin-first over the project type (the order the dedupe + budget-expiry semantics rely on).
    assert calls == [
        ("can_observe_events", "warehouse:lance_catalog"),
        ("can_administer", "project"),
        ("member", "project"),
    ]


def test_non_admin_gets_estate_admin_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, estate=False, lists={"member": ["project:acme"]})
    result = _me(_settings(fga_enabled=True), token=_token(), client=object())
    assert result.estate_admin is False
    assert [(p.project, p.role) for p in result.projects] == [("acme", "member")]


# ── degradation: an FGA outage/brownout costs display fidelity, never the endpoint ───────────────────


def test_estate_check_outage_degrades_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail-safe: a broken authz layer must never paint admin surfaces — and never 5xx the self-describe
    # (the real gates on /v1/events / /v1/projects still fail closed on their own).
    _wire(monkeypatch, estate=ServiceUnavailableError("down"), lists={"can_administer": ["project:acme"]})
    result = _me(_settings(fga_enabled=True), token=_token(), client=object())
    assert result.estate_admin is False
    assert [(p.project, p.role) for p in result.projects] == [("acme", "admin")]


def test_one_relation_outage_degrades_only_that_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # Per-relation degrade: the member lookup dying must not void the admin list (or vice versa).
    _wire(
        monkeypatch,
        lists={"can_administer": ["project:acme"], "member": ServiceUnavailableError("down")},
    )
    result = _me(_settings(fga_enabled=True), token=_token(), client=object())
    assert [(p.project, p.role) for p in result.projects] == [("acme", "admin")]


def test_slow_fga_degrades_on_the_shared_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # A BROWNOUT (slow, not down): the lookups share ONE wall-clock budget, so a stalled call degrades
    # on expiry instead of stacking timeouts toward the BFF's request deadline.
    monkeypatch.setattr(ep, "_LOOKUP_BUDGET_SECONDS", 0.05)

    async def _slow_check(*_a: Any, **_k: Any) -> bool:
        await asyncio.sleep(0.5)
        return True

    async def _slow_list(*_a: Any, **_k: Any) -> list[str]:
        await asyncio.sleep(0.5)
        return ["project:acme"]

    monkeypatch.setattr(ep.fga, "check", _slow_check)
    monkeypatch.setattr(ep.fga, "list_objects", _slow_list)
    result = _me(_settings(fga_enabled=True), token=_token(), client=object())
    assert result.estate_admin is False and result.projects == []  # degraded, response still whole


# ── the mocked-fga caveat: the (type, relation) pairs must exist in the compiled model ───────────────


def test_model_defines_the_relations_this_endpoint_checks() -> None:
    # A mock pins the passed string, not that the relation EXISTS on the type (a phantom relation 400s
    # in OpenFGA → degraded-to-empty forever while every unit test stays green).
    from common import fga as fga_module

    model = fga_module.load_model()
    defined = {(t["type"], rel) for t in model["type_definitions"] for rel in (t.get("relations") or {})}
    assert ("warehouse", "can_observe_events") in defined  # the root object is a warehouse:
    assert ("project", "can_administer") in defined
    assert ("project", "member") in defined
