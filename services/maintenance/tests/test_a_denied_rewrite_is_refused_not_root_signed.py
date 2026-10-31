"""A catalog DENIAL must stop the pass — never fall back to a credential that can do it anyway.

MEASURED ON THE LIVE ESTATE 2026-09-10: the vending log carried `credential vending unavailable for
lakehouse$gold (403)` and seven siblings, each table then maintained under the deployment's ambient key,
because the client classified by `status_code >= 400`.

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

from maintenance.services import credentials
from maintenance.services.credentials import MaintenanceDenied, MaintenanceUnauthenticated, denial_remedy
from service_kit.lakehouse.objectfs import StorageOptions


class _Settings:
    catalog_url = "http://catalog:2333"
    catalog_service_identity = "service-maintenance"
    #: Read by the AMBIENT-credential log line, which names the identity rather than ranking it.
    s3_access_key_id = "rask-maintenance"


def _settings() -> Any:
    return _Settings()


_FALLBACK = StorageOptions.from_flat({"aws_access_key_id": "root", "aws_secret_access_key": "root"})


def _respond(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def _post(*_a: object, **_k: object) -> httpx.Response:
        return httpx.Response(status_code=status, json={"detail": "nope"}, request=httpx.Request("POST", "http://catalog:2333/x"))

    monkeypatch.setattr(credentials.httpx, "post", _post)


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "service_headers", lambda _s: {})


@pytest.mark.parametrize(("status", "unauthenticated"), [(401, True), (403, False)])
def test_a_401_is_told_apart_from_a_403_at_the_vend_door(*, monkeypatch: pytest.MonkeyPatch, status: int, unauthenticated: bool) -> None:
    """A 401 is about this service's own credential and a 403 about the id; both refuse, only the second is the table's."""
    _respond(monkeypatch, status)
    with pytest.raises(MaintenanceDenied) as vend:
        credentials.write_options_for("s3://b/t", _settings(), table_id="ns$t", fallback=_FALLBACK)

    assert isinstance(vend.value, MaintenanceUnauthenticated) is unauthenticated
    assert "ns$t" in str(vend.value), "the refusal must name the table it was asked for"


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
