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

A 401 refuses too, as `MaintenanceUnauthenticated`: it is about maintenance's own credential rather
than the table, so it is never counted under the table's id, but the ambient key is the same bypass.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from maintenance.services import catalog_compaction, credentials
from maintenance.services.compaction_executor import MaintenanceDenied, MaintenanceUnauthenticated, denial_remedy


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


@pytest.mark.parametrize(("status", "unauthenticated"), [(401, True), (403, False)])
def test_a_401_is_told_apart_from_a_403_at_both_doors(monkeypatch: pytest.MonkeyPatch, status: int, unauthenticated: bool) -> None:
    """A 401 is about this service's own credential and a 403 about the id; both refuse, only the second is the table's."""
    _respond(monkeypatch, status, target=credentials.httpx, attr="post")
    with pytest.raises(MaintenanceDenied) as vend:
        credentials.write_options_for("s3://b/t", _settings(), fallback=_FALLBACK, declared_table_id="ns$t")
    _respond(monkeypatch, status, target=catalog_compaction.httpx, attr="post")
    with pytest.raises(MaintenanceDenied) as plan:
        catalog_compaction.plan_via_catalog("ns$t", {}, settings=_settings())

    assert [isinstance(caught.value, MaintenanceUnauthenticated) for caught in (vend, plan)] == [unauthenticated, unauthenticated]
    assert all("ns$t" in str(caught.value) for caught in (vend, plan)), "the refusal must name the table it was asked for"


# --------------------------------------------------------------------------- #
# [[LH-164]] the refusal names a cause it can actually rule out
# --------------------------------------------------------------------------- #
# The catalog's authorization gate runs BEFORE existence resolution, so "this identity holds no
# `can_maintain`" and "no such table is registered" are the SAME 403 on the wire, and a refusal that
# instructs "Grant can_maintain on table:<id>" sends an operator to grant on an object that may not be a
# table. Measured 2026-09-15 on the live estate: a sweep pass refused five datasets that way, and
# `lakehouse$silver`, the id in the message, is a NAMESPACE (`discover_datasets` derives an id from a
# bucket path). The same pass carried 315 correct shallow-clone refusals, and an instruction nobody can
# carry out beside them is how a reader learns to skip the category.


def test_it_names_the_authorization_cause() -> None:
    remedy = denial_remedy(table_id="db1$users", identity="service-maintenance")

    assert "can_maintain" in remedy
    assert "service-maintenance" in remedy


def test_both_refusal_sites_use_the_one_wording() -> None:
    """Two call sites phrased this independently and drifted; one function is what stops that again."""
    import inspect
    import linecache

    for module in (catalog_compaction, credentials):
        linecache.checkcache(module.__file__)
        source = inspect.getsource(module)
        assert "denial_remedy(" in source, f"{module.__name__} phrases its own maintenance refusal again"
        assert "Grant can_maintain on table:" not in source, f"{module.__name__} still instructs a bare grant"
