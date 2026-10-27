"""[[LH-229]] DescribeTable and DeclareTable vend the highest tier FGA grants the caller (ADR 0156 C3, ADR 0157).

A stock client opening a table through the namespace takes the ``storage_options`` the spec door returns, and a
client that gets none signs with whatever credential its environment holds. So the spec doors vend unless the
caller asks them not to: the spec lets the server decide when ``vend_credentials`` is unset
(``lance_docs/ns_catalog/spec.yaml`` § DescribeTableRequest). The tier is the highest rung FGA grants on the
table: ``can_write_data`` opens the write tier, judged by the tier judge like every write, and so do ``can_read_data``
and ``can_maintain`` together, a compaction job's rungs, whose commits ``version/create`` judges as a maintainer's;
``can_read_data`` alone the read tier; none, no credential. Maintain is never vended here (LH-473).

Every credential either door mints writes the ``vend_credentials`` issuance record (#532), the record the
management vend door already writes.

FGA's answers are faked by relation (the offline suite has no OpenFGA); the doors, the tier judge and the policy
decision run for real, and the vendor records the tier it was asked for.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient
from lance_namespace import DeclareTableResponse, DescribeNamespaceResponse

from catalog.api.dependencies import get_resolver, get_vendor
from catalog.core.config import Settings, get_settings
from catalog.core.vending import VendedCredentials
from catalog.services import table_claims
from service_kit.governed import fga as fga_module
from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal
from tests.unit.named_resolver import NamedResolver


def _auth_settings() -> Settings:
    return Settings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_connectors": {"local": "dex"},
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


@pytest.fixture
def vendor(client: TestClient, fake_ns: MagicMock, claim_described: Callable[[str], str]) -> Iterator[MagicMock]:
    """Alice signed in with FGA on, and a vendor that records each tier it is asked for.

    Every name holds an object (``NamedResolver``): the claim is which tier a grant opens, not how a name resolves. The
    table resolves to the location ``fake_ns`` describes, whose claim records its uuid, which its credential is scoped
    by (ADR 0173 §6).
    """
    client.app.dependency_overrides[get_settings] = _auth_settings
    claims = table_claims.claim_store(_auth_settings())
    client.app.dependency_overrides[get_resolver] = lambda: NamedResolver(tables=lambda segments: table_claims.resolve(fake_ns, claims, segments))
    claim_described("db$t")
    verifier = MagicMock()
    verifier.verify.return_value = PersonPrincipal(
        principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    )
    client.app.state.oidc = verifier
    fga_client = MagicMock()
    fga_client.close = AsyncMock()
    client.app.state.fga = fga_client
    fake_ns.describe_namespace.return_value = DescribeNamespaceResponse(properties={"rask.tier": "silver"})
    recorder = MagicMock()
    recorder.vend.side_effect = lambda **kw: VendedCredentials(storage_options={"aws_access_key_id": f"AK-{kw['tier']}"}, expires_at_millis=1)
    client.app.dependency_overrides[get_vendor] = lambda: recorder
    yield recorder
    client.app.dependency_overrides.pop(get_vendor, None)


def _grants(monkeypatch: pytest.MonkeyPatch, relations: set[str]) -> None:
    """FGA answers yes exactly to ``relations`` (plus the create rung a declare asks on the parent)."""

    async def check(_client: object, *, relation: str, **_kw: object) -> bool:
        return relation in relations or relation == "can_create_table"

    monkeypatch.setattr(fga_module, "check", check)
    monkeypatch.setattr(fga_module, "grant_on_create", AsyncMock())
    monkeypatch.setattr(fga_module, "revoke_object_tuples", AsyncMock(return_value=[]))


_READER = {"can_get_metadata", "can_read_data"}
_WRITER = _READER | {"can_write_data"}
_COMPACTOR = _READER | {"can_maintain"}
_HEADERS = {"Authorization": "Bearer t"}


@pytest.mark.parametrize(
    ("door", "relations", "body", "tier"),
    [
        pytest.param("describe", _READER, {}, "read", id="a-reader-gets-the-read-tier"),
        pytest.param("describe", _WRITER, {}, "write", id="a-writer-gets-the-write-tier"),
        pytest.param("describe", _COMPACTOR, {}, "write", id="a-reader-who-maintains-gets-the-write-tier"),
        pytest.param("describe", {"can_get_metadata", "can_maintain"}, {}, None, id="a-maintainer-who-may-not-read-gets-none"),
        pytest.param("describe", _WRITER, {"vend_credentials": True}, "write", id="an-explicit-ask-gets-the-same-tier"),
        pytest.param("describe", _WRITER, {"vend_credentials": False}, None, id="asking-for-none-gets-none"),
        pytest.param("describe", {"can_get_metadata"}, {}, None, id="metadata-without-data-gets-none"),
        pytest.param("describe", {"can_get_metadata"}, {"vend_credentials": True}, 403, id="metadata-without-data-that-asks-is-refused"),
        pytest.param("declare", _WRITER, {}, "write", id="a-declarer-gets-the-write-tier"),
    ],
)
def test_a_spec_door_vends_the_highest_tier_fga_grants(
    client: TestClient,
    fake_ns: MagicMock,
    vendor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    door: str,
    relations: set[str],
    body: dict[str, bool],
    tier: str | int | None,
) -> None:
    _grants(monkeypatch, relations)
    fake_ns.declare_table.return_value = DeclareTableResponse(location=fake_ns.describe_table.return_value.location)

    with caplog.at_level(logging.INFO, logger="lance.audit"):
        response = client.post(f"/v1/table/db$t/{door}", json=body, headers=_HEADERS)

    if tier == 403:
        assert response.status_code == 403, response.text
        assert not vendor.vend.called
        return
    assert response.status_code == 200, response.text
    if tier is None:
        assert "storage_options" not in response.json()
        assert not vendor.vend.called
        return
    assert vendor.vend.call_args.kwargs["tier"] == tier
    options = response.json()["storage_options"]
    assert options["aws_access_key_id"] == f"AK-{tier}"
    assert options["expires_at_millis"] == "1", "the spec carries a temporary credential's expiry inside storage_options"
    issued = [r for r in caplog.records if getattr(r, "audit.action", None) == "vend_credentials"]
    assert [(getattr(r, "audit.tier"), getattr(r, "audit.door")) for r in issued] == [(tier, door)], "every mint writes the issuance record"


@pytest.mark.parametrize(
    ("door", "body", "status"),
    [
        pytest.param("describe", {}, 200, id="an-offer-that-fails-leaves-the-answer-without-one"),
        pytest.param("describe", {"vend_credentials": True}, 503, id="an-ask-that-fails-is-the-answer"),
        pytest.param("declare", {"vend_credentials": True}, 503, id="a-declare-whose-ask-fails-takes-the-declaration-back"),
    ],
)
def test_a_vend_that_fails_fails_only_a_caller_that_asked(
    client: TestClient, fake_ns: MagicMock, vendor: MagicMock, monkeypatch: pytest.MonkeyPatch, door: str, body: dict[str, bool], status: int
) -> None:
    """A metadata read does not depend on the store's STS; a declare whose credential fails leaves no table behind."""
    _grants(monkeypatch, _WRITER)
    fake_ns.declare_table.return_value = DeclareTableResponse(location=fake_ns.describe_table.return_value.location)
    vendor.vend.side_effect = EndpointConnectionError(endpoint_url="http://sts.unreachable")

    response = client.post(f"/v1/table/db$t/{door}", json=body, headers=_HEADERS)

    assert response.status_code == status, response.text
    if status == 200:
        assert "storage_options" not in response.json()
    if door == "declare":
        assert fake_ns.drop_table.called, "the declared table is dropped so a retry can declare it again"
