"""Unit tests for the caller self-describe endpoint (``GET /management/v1/me``, endpoints/me.py).

Drives the handler directly (no TestClient) with fake settings + FGA wrappers, mirroring the repo's
direct-call style (``test_projects_endpoint.py``). Pins the frozen contract: any VERIFIED token → 200
with ``{sub, name, email, estate_admin, can_create_project, projects}``; anonymous → 401; FGA off → ``estate_admin=true``,
``projects=[]`` (dev parity); ``estate_admin`` = ``can_administer_estate`` on the fixed root object;
``projects`` via the #51 list wrappers (admin first, then member, deduped) under a shared 2s budget,
degrading PER RELATION on an FGA outage — the endpoint never 5xxes over authz-lookup health.
"""

import asyncio
from typing import Any

import pytest
from catalog_settings import catalog_settings, governed_settings
from lance_namespace import ServiceUnavailableError, UnauthenticatedError

from catalog.api.authz.context import AuthzContext
from catalog.api.v1.endpoints import me as ep
from catalog.core.config import Settings
from lakehouse.governed import fga_resilience
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal


def _settings(*, fga_enabled: bool = False) -> Settings:
    if fga_enabled:
        return governed_settings(fga_root_object="warehouse:lance_catalog")
    return catalog_settings(fga_root_object="warehouse:lance_catalog")


def _token(**claims: Any) -> PersonPrincipal:
    return PersonPrincipal(principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1, **claims))


def _me(settings: Any, *, token: Any, client: Any = None) -> ep.MeResponse:
    return asyncio.run(ep.get_me(context=AuthzContext(client=client, settings=settings, token=token)))


# ── the authn floor: anonymous → 401, in every configuration ─────────────────────────────────────────


@pytest.mark.parametrize("fga_enabled", [False])
def test_anonymous_is_401(fga_enabled: bool) -> None:
    # There is no self to describe without a verified token — OIDC off (token None) and a missing bearer
    # both land here as token=None; the handler owns the 401 because the router gate no-ops with FGA off.
    with pytest.raises(UnauthenticatedError):
        _me(_settings(fga_enabled=fga_enabled), token=None, client=object() if fga_enabled else None)


# ── FGA off: dev parity — full standing, no tenant list ──────────────────────────────────────────────


def test_fga_off_dev_parity() -> None:
    result = _me(_settings(), token=_token(name="Alice A", email="alice@example.com"))
    assert result.sub == "dex~alice"
    assert result.name == "Alice A" and result.email == "alice@example.com"
    assert (result.estate_admin, result.can_create_project) == (True, True)  # every require_* gate is a no-op there — say so honestly
    assert result.projects == []


# ── governed: estate check + admin-first project lists, deduped ──────────────────────────────────────


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    estate: Any = True,
    lists: dict[str, Any] | None = None,
) -> None:
    """Fake the two FGA wrappers for an outage: `estate`/`lists` values may be exceptions (raised) or results."""
    lists = lists or {}

    async def _fake_check(_client: Any, *, user: str, relation: str, obj: str, **kw: Any) -> bool:
        assert kw == {"retry": fga_resilience.ONE_ATTEMPT}  # single attempt under the tight budget (the /management/v1/projects rule)
        if isinstance(estate, Exception):
            raise estate
        return bool(estate)

    async def _fake_list(_client: Any, *, user: str, relation: str, object_type: str, **kw: Any) -> Any:
        del object_type
        assert kw == {"retry": fga_resilience.ONE_ATTEMPT}
        outcome = lists.get(relation, [])
        if isinstance(outcome, Exception):
            raise outcome
        # `list_objects` returns an `ObjectListing` now — the truncation flag travels with the result so
        # a caller can tell a short answer from a complete one.
        return ep.fga_reads.ObjectListing(objects=list(outcome), truncated=False)

    monkeypatch.setattr(ep.fga_reads, "check", _fake_check)
    monkeypatch.setattr(ep.fga_reads, "list_objects", _fake_list)


@pytest.mark.parametrize(
    ("held", "estate_admin", "can_create_project", "projects"),
    [
        pytest.param(
            [("owner", "estate:rask"), ("admin", "project:acme"), ("member", "project:globex")],
            True,
            False,
            [("acme", "admin"), ("globex", "member")],
            id="an-estate-owner-who-is-no-estate-admin-lists-an-admin-row-once-and-its-member-rows",
        ),
        pytest.param([("member", "project:acme")], False, False, [("acme", "member")], id="a-member-holding-nothing-estate-wide"),
        pytest.param([("admin", "estate:rask")], True, True, [], id="the-estate-admin-may-create-a-project"),
    ],
)
def test_the_caller_is_told_their_standing_and_their_projects(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    held: list[tuple[str, str]],
    estate_admin: bool,
    can_create_project: bool,
    projects: list[tuple[str, str]],
) -> None:
    """``estate_admin`` is ``can_administer_estate`` and ``can_create_project`` the create door's verb, both on the estate
    root, asked apart; ``projects`` lists the caller's admin rows first and drops the member row an admin also holds (the
    model folds ``admin`` into ``member``). Judged on the real model, carol holding exactly ``held``."""
    catalog = governed_catalog
    catalog.store.write(*((catalog.person("carol"), relation, obj) for relation, obj in held))

    answered = catalog.client.get("/management/v1/me", headers=catalog.bearer("carol"))

    assert answered.status_code == 200, answered.text
    me = answered.json()
    assert (me["estate_admin"], me["can_create_project"]) == (estate_admin, can_create_project), me
    assert [(project["project"], project["role"]) for project in me["projects"]] == projects, me


# ── degradation: an FGA outage/brownout costs display fidelity, never the endpoint ───────────────────


def test_estate_check_outage_degrades_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail-safe: a broken authz layer must never paint admin surfaces — and never 5xx the self-describe
    # (the real gates on /management/v1/events / /management/v1/projects still fail closed on their own).
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
    # on expiry instead of stacking timeouts toward the caller's request deadline.
    monkeypatch.setattr(ep, "_LOOKUP_BUDGET_SECONDS", 0.05)

    async def _slow_check(*_a: Any, **_k: Any) -> bool:
        await asyncio.sleep(0.5)
        return True

    async def _slow_list(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(0.5)
        return ep.fga_reads.ObjectListing(objects=["project:acme"], truncated=False)

    monkeypatch.setattr(ep.fga_reads, "check", _slow_check)
    monkeypatch.setattr(ep.fga_reads, "list_objects", _slow_list)
    result = _me(_settings(fga_enabled=True), token=_token(), client=object())
    assert result.estate_admin is False and result.projects == []  # degraded, response still whole


# ── the mocked-fga caveat: the (type, relation) pairs must exist in the compiled model ───────────────
