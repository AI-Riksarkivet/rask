"""A catalog DENIAL must stop the rewrite — never fall back to a credential that can do it anyway.

MEASURED ON THE LIVE ESTATE 2026-09-10. The sweep asks the catalog to plan a compaction; for eight
tables the catalog answers **403**, and the sweep rewrites them anyway with the deployment's ambient
key. In one 60-minute window: 20 `compaction_plane_unavailable_falling_back` against 352 successes,
and the vending log carries `credential vending unavailable for lakehouse$gold (403)` and seven
siblings. Nothing is red, because both seams classify by `status_code >= 400`.

THE TWO CONDITIONS ARE NOT THE SAME QUESTION:

    503 / timeout / unparseable   we could not ASK    -> degrade; reclaiming disk during a brief
                                                        catalog outage is why the fallback exists
    401 / 403                     the answer is NO    -> refuse; doing the work with a wider
                                                        credential is the authorization bypass

`catalog_identity.py` already names this shape as the dangerous one — "`credentials.py` reports any
`>=400` as 'vending unavailable'" — and `plan_via_catalog`'s own message says the plan was *refused*
while raising *Unavailable*. The word was right and the class was wrong.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from maintenance.services import catalog_compaction, credentials
from maintenance.services.compaction_executor import CompactionPlaneUnavailable, MaintenanceDenied


class _Settings:
    catalog_url = "http://catalog:2333"
    catalog_service_identity = "service-maintenance"
    #: Read by the AMBIENT-credential log line, which names the identity rather than ranking it.
    s3_access_key_id = "rask-maintenance"


def _settings() -> Any:
    return _Settings()


_FALLBACK = {"aws_access_key_id": "root", "aws_secret_access_key": "root"}


def _respond(monkeypatch: pytest.MonkeyPatch, status: int, *, target: Any, attr: str) -> None:
    def _post(*_a: object, **_k: object) -> httpx.Response:
        return httpx.Response(status_code=status, json={"detail": "nope"}, request=httpx.Request("POST", "http://catalog:2333/x"))

    monkeypatch.setattr(target, attr, _post)


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "service_headers", lambda _s: {})
    monkeypatch.setattr(catalog_compaction, "service_headers", lambda _s: {})


@pytest.mark.parametrize("status", [401, 403])
def test_a_DENIED_vend_refuses_rather_than_handing_back_the_root_key(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """The bypass, stated as a test: the catalog says no and the caller signs with something stronger."""
    _respond(monkeypatch, status, target=credentials.httpx, attr="post")
    with pytest.raises(MaintenanceDenied) as caught:
        credentials.write_options_for("s3://b/t", _settings(), fallback=_FALLBACK, declared_table_id="ns$t")
    assert "ns$t" in str(caught.value), "the refusal must name the table an operator has to grant"


@pytest.mark.parametrize("status", [500, 503])
def test_an_UNREACHABLE_vend_still_degrades_to_the_ambient_credential(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """The fallback keeps its reason for existing: a catalog outage must not stop reclaiming disk."""
    _respond(monkeypatch, status, target=credentials.httpx, attr="post")
    assert credentials.write_options_for("s3://b/t", _settings(), fallback=_FALLBACK, declared_table_id="ns$t") == _FALLBACK


@pytest.mark.parametrize("status", [401, 403])
def test_a_DENIED_plan_is_a_refusal_not_an_outage(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """`CompactionPlaneUnavailable` is the caller's signal to compact LOCALLY — exactly what a denial
    must not authorize. A denial gets its own class so the caller cannot answer it with a fallback."""
    _respond(monkeypatch, status, target=catalog_compaction.httpx, attr="post")
    with pytest.raises(MaintenanceDenied):
        catalog_compaction.plan_via_catalog("ns$t", {}, settings=_settings())


@pytest.mark.parametrize("status", [500, 503])
def test_an_UNREACHABLE_plan_stays_an_outage(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _respond(monkeypatch, status, target=catalog_compaction.httpx, attr="post")
    with pytest.raises(CompactionPlaneUnavailable):
        catalog_compaction.plan_via_catalog("ns$t", {}, settings=_settings())


def test_a_denial_is_NOT_an_unavailability_by_inheritance() -> None:
    """If `MaintenanceDenied` subclassed `CompactionPlaneUnavailable`, every existing `except` in the
    sweep would keep catching it and keep falling back — the fix would be invisible and the bypass
    would survive. They are siblings on purpose."""
    assert not issubclass(MaintenanceDenied, CompactionPlaneUnavailable)
    assert not issubclass(CompactionPlaneUnavailable, MaintenanceDenied)
