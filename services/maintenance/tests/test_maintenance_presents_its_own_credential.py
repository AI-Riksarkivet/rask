"""Maintenance presents its own service-account token at the catalog, and nothing that names a subject.

`catalog_identity.service_headers` is the ONE builder both of maintenance's catalog doors use: the
compaction pair and credential vending. Driven here through the vend door's real HTTP client, with the
catalog behind respx, so what is asserted is the request that would reach the catalog.

The token is the kubelet-projected `rask-catalog` file, re-read per request: the kubelet rewrites it at
~515 s of a 600 s life (LH-220 probe d), so a client that read it once is refused ten minutes after boot.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from maintenance.services.compaction_executor import MaintenanceUnauthenticated


_CATALOG = "http://catalog:2333"
_URI = "s3://acme-bucket/db$t"
_VEND = f"{_CATALOG}/management/v1/table/db$t/credentials"
_SCOPED = {"aws_access_key_id": "VENDEDKEY", "aws_secret_access_key": "vended-secret-9f3a"}
_AMBIENT = {"aws_access_key_id": "ambient"}


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings(MAINTENANCE_CATALOG_URL=_CATALOG)


def _vend_door() -> respx.Route:
    payload = {"mode": "direct", "credentials": {"storage_options": _SCOPED}, "location": _URI}
    return respx.post(_VEND, params={"tier": "write"}).mock(return_value=httpx.Response(200, json=payload))


@respx.mock
def test_each_vend_presents_the_token_the_kubelet_last_wrote_and_no_claimed_name(catalog_identity_token: Path) -> None:
    settings = _settings()
    door = _vend_door()

    credentials.write_options_for(_URI, settings, fallback=_AMBIENT, declared_table_id="db$t")
    catalog_identity_token.write_text("sa-token-two\n")
    credentials.write_options_for(_URI, settings, fallback=_AMBIENT, declared_table_id="db$t")

    sent = [call.request.headers for call in door.calls]
    assert [headers.get("authorization") for headers in sent] == ["Bearer sa-token-one", "Bearer sa-token-two"]
    assert all("x-lance-service-identity" not in headers and "dapr-api-token" not in headers for headers in sent)


@respx.mock
@pytest.mark.usefixtures("respx_allows_unused_routes")
def test_an_unreadable_token_stops_the_unit_as_unauthenticated_and_sends_nothing(catalog_identity_token: Path) -> None:
    door = _vend_door()
    catalog_identity_token.unlink()

    with pytest.raises(MaintenanceUnauthenticated, match="cannot present its catalog credential"):
        credentials.write_options_for(_URI, _settings(), fallback=_AMBIENT, declared_table_id="db$t")

    assert not door.called
