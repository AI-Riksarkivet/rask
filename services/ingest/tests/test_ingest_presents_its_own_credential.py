"""Ingest calls the catalog as the service account its projected token names, read when the request is sent ([[LH-220]], D1).

The kubelet replaces the token at ~515 s of its 600 s lifetime (measured 2026-10-02, LH-220 P5.3 d), so the claim is
about time as much as identity: a client that read the file once would present a token the door refuses within ten
minutes, while every test that called it only once stayed green.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pyarrow as pa
import pytest
import respx

from ingest.catalog_service import CatalogServiceClient


if TYPE_CHECKING:
    import pathlib


CREDENTIALS = "http://catalog:2333/management/v1/table/ns$ds/credentials"


@respx.mock
def test_each_catalog_request_carries_the_token_in_the_file_when_it_is_sent(sa_issuer: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    monkeypatch.setenv("RASK_CATALOG_IDENTITY_TOKEN_FILE", str(token_file))
    first = sa_issuer.mint("rask-sa-ingest", audience="rask-catalog")
    rotated = sa_issuer.mint("rask-sa-ingest", audience="rask-catalog")
    route = respx.post(CREDENTIALS).mock(return_value=httpx.Response(200, json={"credentials": {}}))
    client = CatalogServiceClient(pa.schema([("id", pa.string())]), base_url="http://catalog:2333")

    token_file.write_text(first)
    client.vend_storage_options("ns", "ds")
    token_file.write_text(rotated)
    client.vend_storage_options("ns", "ds")

    sent = [call.request.headers for call in route.calls]
    assert [headers["Authorization"] for headers in sent] == [f"Bearer {first}", f"Bearer {rotated}"]
    assert [name for headers in sent for name in headers if name.lower() in {"dapr-api-token", "x-lance-service-identity"}] == []
