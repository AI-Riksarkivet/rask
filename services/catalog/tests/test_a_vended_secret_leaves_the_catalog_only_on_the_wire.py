"""[[LH-072]] A vend's secret and session token leave the catalog only in the body that hands the vend to its caller.

A vend is the catalog's own token exchanged for a session narrowed to one table ([[XC-083]], ADR 0170): short-lived, and
still a credential. Inside the catalog its secret access key and session token are `SecretStr`, so a repr, a str, a model
dump, a log line or an error that echoes the vend carries neither, as Lakekeeper derives `Redact` on its credential
structs (`crates/lakekeeper/src/service/storage/s3.rs`). A client is handed one flat `storage_options` map
(`lance_docs/ns_catalog/spec.yaml:2883-2892`), the expiry beside it on the management door and inside it on a spec
door, at the keys `lance_docs/lance/guide/object_store.md:127-136` names.

Only the STS exchange is faked: the vendor, its storage options builder and both doors are the deployed ones.
"""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import DescribeNamespaceResponse, DescribeTableResponse
from pydantic import SecretStr

from catalog.api.dependencies import get_namespace, get_settings
from catalog.api.v1.endpoints import credentials, tables
from catalog.core.config import Settings
from catalog.core.vending import EncryptionAtRest, VendFacts, WebIdentityVendor
from catalog.services import table_claims
from service_kit.context import DiagnosticFormatter
from service_kit.governed.store_credential import S3KeyPair
from service_kit.governed.store_session import IssuedSession, WebIdentity
from service_kit.lakehouse import location_claims
from service_kit.lakehouse.ns_errors import install_problem_handlers


log = logging.getLogger(__name__)

_SECRET = "vended-secret-wJalrXUtnFEMI"
_TOKEN = "vended-session-token-FwoGZXIvYXdz"
_LOCATION = "s3://lakehouse/abc12345_ns$t"


def _exchange(*_: Any, **__: Any) -> IssuedSession:
    return IssuedSession(
        key_pair=S3KeyPair(access_key_id="AK", secret_access_key=SecretStr(_SECRET), session_token=SecretStr(_TOKEN)),
        expiration=datetime(2030, 1, 1, tzinfo=UTC),
    )


def _vendor(*, encryption: EncryptionAtRest | None = None) -> WebIdentityVendor:
    identity = WebIdentity(token_file=Path("projected-token"), endpoint="https://s3.example.com", region="eu-north-1", role_arn="arn:aws:iam::0:role/vend")
    return WebIdentityVendor(identity=identity, exchange=_exchange, encryption=encryption)


def test_a_vend_renders_neither_its_secret_nor_its_session_token(caplog: pytest.LogCaptureFixture) -> None:
    """Every way the catalog turns an object into text, short of the call that hands the vend over."""
    vend = _vendor(encryption=EncryptionAtRest(algorithm="aws:kms", kms_key_id="arn:aws:kms:eu-north-1:123456789012:key/k")).vend(
        table_location=_LOCATION, tier="write", session_name="writer"
    )
    assert vend is not None, "the vendor vended nothing, so nothing below was rendered"
    refused = RuntimeError("the store refused the vend", vend)
    with caplog.at_level(logging.WARNING, logger=log.name):
        log.warning("vend_refused %s %r", vend, vend, extra={"credentials": vend}, exc_info=refused)

    rendered = {
        "repr": repr(vend),
        "str": str(vend),
        "format": f"{vend}",
        "model_dump": repr(vend.model_dump()),
        "model_dump json": json.dumps(vend.model_dump(mode="json")),
        "model_dump_json": vend.model_dump_json(),
        "log": "\n".join(DiagnosticFormatter("%(message)s").format(record) for record in caplog.records),
        "error echo": "".join(traceback.format_exception(refused)),
    }

    assert [how for how, text in rendered.items() if _SECRET in text or _TOKEN in text] == []


class _Namespace:
    """A backend whose `describe_table` answers an `s3://` location, the only kind a vend scopes."""

    def describe_table(self, request: Any) -> DescribeTableResponse:  # noqa: ANN401 — the backend's request model
        return DescribeTableResponse(location=_LOCATION)

    def describe_namespace(self, request: Any) -> DescribeNamespaceResponse:  # noqa: ANN401 — the backend's request model
        """The namespace declares no tier; the tier judge reads it before a write-tier vend."""
        return DescribeNamespaceResponse(properties={})


@pytest.fixture
def doors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """Both doors that answer a vend, on the deployed vendor, over a catalog whose own connection is plaintext and root-keyed.

    A door that merged the catalog's own connection into the vend, either way round, would hand a reader the root secret
    or hand the https store a plaintext permit, so the root pair and `allow_http=true` are what that connection carries.
    The manifest read is the catalog's own open, not the vend, and needs a live store. The control root, where each door
    reads the table's location claim for its uuid, is a local directory.
    """
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(tables.router)
    application.include_router(credentials.router)
    application.state.vendor = _vendor()
    monkeypatch.setattr(tables, "dataset_facts", lambda location, storage_options: VendFacts(read_version=1))
    monkeypatch.setattr(credentials, "dataset_facts", lambda location, storage_options, branch="": VendFacts(read_version=1))
    settings = Settings(
        LANCE_S3_ENDPOINT="http://rustfs:9000",
        LANCE_S3_ACCESS_KEY_ID="ROOTKEY",
        LANCE_S3_SECRET_ACCESS_KEY="ROOTSECRET",
        LANCE_CONTROL_ROOT=str(tmp_path),
    )
    location_claims.take(
        table_claims.claim_store(settings), _LOCATION, "ns$t", ["ns", "t"], uuid="0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01", is_gone=lambda _claim: False
    )
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = _Namespace
    with TestClient(application) as client:
        yield client


#: The vend each door answered this rig with at 4c407a538, where its bodies were recorded byte for byte (2026-10-30): the
#: https store's address with no plaintext permit, the vended key pair and session, and none of the catalog's own
#: connection. Compared as JSON values, since an object's key order is nothing a client reads (RFC 8259 §4).
_OPTIONS = {
    "endpoint": "https://s3.example.com",
    "aws_access_key_id": "AK",
    "aws_secret_access_key": "vended-secret-wJalrXUtnFEMI",
    "region": "eu-north-1",
    "allow_http": "false",
    "virtual_hosted_style_request": "false",
    "aws_session_token": "vended-session-token-FwoGZXIvYXdz",
}


@pytest.mark.parametrize(
    ("path", "params", "member", "vend"),
    [
        pytest.param(
            "/management/v1/table/ns%24t/credentials",
            {"tier": "read"},
            "credentials",
            {"storage_options": _OPTIONS, "expires_at_millis": 1893456000000},
            id="management",
        ),
        pytest.param(
            "/v1/table/ns%24t/describe", {"vend_credentials": "true"}, "storage_options", {**_OPTIONS, "expires_at_millis": "1893456000000"}, id="describe"
        ),
    ],
)
def test_a_door_answers_the_vend_and_nothing_of_its_own_connection(
    doors: TestClient, path: str, params: dict[str, str], member: str, vend: dict[str, object]
) -> None:
    response = doors.post(path, params=params)

    assert response.status_code == 200, response.text
    assert response.json()[member] == vend
