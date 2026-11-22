"""The permission SELF-view — ``POST /v1/{table,namespace}/{id}/access/my-permissions``.

Three properties, and each exists because its opposite was the reason no such surface shipped before:

1. It answers with a ``check`` PER RELATION for the caller — never ``list_users``. The sibling
   ``access/list`` enumerates who holds what, which discloses principals and is why that route clears
   the owner bar. "What may I do here" describes only the person asking.
2. It takes NO subject. A self-view that accepts a ``user`` is the enumeration question renamed, and
   would need the owner gate back.
3. The subject is the caller's principal, turned into its OpenFGA user once (``fga_ids.user_object``); a
   user id that matches no tuple makes every relation answer a correct-looking ``false``, and the page
   renders "you may do nothing" for an owner.

Sync via ``asyncio.run`` — no async-plugin dependency (this repo does not set ``asyncio_mode``).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from catalog_settings import catalog_settings, governed_settings
from lance_namespace import ServiceUnavailableError, UnauthenticatedError, UnsupportedOperationError
from openfga_sdk.client import OpenFgaClient

from catalog.api.authz.context import AuthzContext
from catalog.api.v1.endpoints import access
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from tests.unit.named_resolver import NamedResolver, uuid_of


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

    async def fake_check(_client: object, *, fga_user: str, relation: str, obj: str, **_kw: Any) -> bool:
        if outage:
            raise ServiceUnavailableError("fga down")
        asked.append((fga_user, relation, obj))
        return relation in (allow or set())

    monkeypatch.setattr(access.fga_reads, "check_fga_user", fake_check)
    client = cast("OpenFgaClient", object())  # injected now (catalog-api-09); the fga.* module functions are faked below
    settings = governed_settings() if fga_enabled else catalog_settings()
    token = (
        PersonPrincipal(principal=Principal(idp_id="dex", subject=sub), claims=IDToken(iss="i", sub=sub, aud="lance", exp=1, iat=1)) if token_present else None
    )
    resp = asyncio.run(
        access._my_permissions(AuthzContext(client=client, settings=settings, token=token), resolver=NamedResolver(), fga_type=fga_type, id=ident)
    )
    return resp, asked


def test_answers_every_can_relation_the_model_defines_on_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """The relation set is the model's, read from model.json — never a hand-kept list, which would
    drift into a phantom relation (an OpenFGA 400 that fails closed to a 503 for every caller)."""
    resp, asked = _run(monkeypatch, allow={"can_get_metadata"})
    assert set(resp.permissions) == set(access._can_relations("namespace"))
    assert {r for _u, r, _o in asked} == set(access._can_relations("namespace"))
    assert {o for _u, _r, o in asked} == {f"namespace:{uuid_of('namespace', ['gold'])}"}


@pytest.mark.parametrize(
    ("caller", "granted", "allowed"),
    [
        pytest.param("alice", None, {"can_get_metadata": True, "can_create_table": True, "can_delete": True}, id="the-owner"),
        pytest.param("bob", "reader", {"can_get_metadata": True, "can_create_table": False, "can_delete": False}, id="a-reader"),
    ],
)
def test_reports_the_callers_own_verdicts(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    caller: str,
    granted: str | None,
    allowed: dict[str, bool],
) -> None:
    """Each caller is answered the real model's verdict for themself, on alice's namespace: her own as its creator, and a
    reader's."""
    catalog = governed_catalog
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    if granted is not None:
        door = "/management/v1/namespace/lance_catalog$db/access/grant"
        assert catalog.client.post(door, json={"user": f"dex~{caller}", "relation": granted}, headers=catalog.bearer("alice")).status_code == 200

    answered = catalog.client.post("/management/v1/namespace/lance_catalog$db/access/my-permissions", json={}, headers=catalog.bearer(caller))

    assert answered.status_code == 200, answered.text
    assert {relation: answered.json()["permissions"][relation] for relation in allowed} == allowed


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
