"""[[LH-229]] DescribeTable and DeclareTable vend the highest tier FGA grants the caller (ADR 0156 C3, ADR 0157).

A stock client opening a table through the namespace takes the ``storage_options`` the spec door returns, and a
client that gets none signs with whatever credential its environment holds. So the spec doors vend unless the
caller asks them not to: the spec lets the server decide when ``vend_credentials`` is unset
(``lance_docs/ns_catalog/spec.yaml`` § DescribeTableRequest). The tier is the highest rung FGA grants on the
table: ``can_write_data`` opens the write tier, judged by the write judge like every write, and so do ``can_read_data``
and ``can_maintain`` together, a compaction job's rungs, whose commits ``version/create`` judges as a maintainer's;
``can_read_data`` alone the read tier; none, no credential. Maintain is never vended here (LH-473). Through a read-only
window (`LANCE_MAINTENANCE_READ_ONLY`) the doors vend at most the read tier: a write-tier credential would write storage
straight past a catalog that refuses every write.

Every credential either door mints writes the ``vend_credentials`` issuance record (#532), the record the
management vend door already writes.

Which tier a rung opens is judged against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA
deciding on a store of the test's own: on a table ``can_get_metadata`` and ``can_read_data`` are the one ``reader`` rung,
so a caller who may see a table may read it, and a maintainer who is no reader is refused the door. The vendor records
the tier it was asked for. A vend that fails is driven with FGA's answers faked by relation, since what it pins is the
store's STS failing, not who may act.
"""

import logging
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient
from lance_namespace import DeclareTableResponse, DescribeNamespaceResponse
from pydantic import SecretStr

from catalog.api.dependencies import get_resolver, get_vendor, request_settings
from catalog.core.config import Settings, get_settings
from catalog.core.vending import VendedSession
from catalog.services import table_claims
from lakehouse.governed import fga_reads, fga_writes
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from lakehouse.storage.credential import S3KeyPair
from tests.governed_catalog import GovernedCatalog
from tests.unit.named_resolver import NamedResolver, uuid_of


def _auth_settings(*, read_only: bool = False) -> Settings:
    return Settings.model_validate(
        {
            "maintenance_read_only": read_only,
            "oidc_enabled": True,
            "oidc_idp_id": "dex",
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
    client.app.dependency_overrides[request_settings] = _auth_settings
    claims = table_claims.claim_store(_auth_settings())
    client.app.dependency_overrides[get_resolver] = lambda: NamedResolver(tables=lambda segments: table_claims.resolve(fake_ns, claims, segments))
    claim_described("lance_catalog$db$t")
    verifier = MagicMock()
    verifier.verify.return_value = PersonPrincipal(
        principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    )
    client.app.state.oidc = verifier
    fga_client = MagicMock()
    fga_client.close = AsyncMock()
    client.app.state.fga = fga_client
    # The namespace the backend describes is the one the resolver names: a declare reads its parent again once its row stands.
    fake_ns.describe_namespace.return_value = DescribeNamespaceResponse(
        properties={"rask.requires_run": "true", "rask.uuid": uuid_of("namespace", ["lance_catalog", "db"])}
    )
    recorder = _recording_vendor()
    client.app.dependency_overrides[get_vendor] = lambda: recorder
    yield recorder
    client.app.dependency_overrides.pop(get_vendor, None)


def _grants(monkeypatch: pytest.MonkeyPatch, relations: set[str]) -> None:
    """FGA answers yes exactly to ``relations`` (plus the create rung a declare asks on the parent)."""

    async def check(_client: object, *, relation: str, **_kw: object) -> bool:
        return relation in relations or relation == "can_create_table"

    monkeypatch.setattr(fga_reads, "check", check)
    monkeypatch.setattr(fga_writes, "grant_on_create", AsyncMock())
    monkeypatch.setattr(fga_writes, "grant_before_arrival", AsyncMock())
    monkeypatch.setattr(fga_writes, "name_child", AsyncMock())
    monkeypatch.setattr(fga_writes, "revoke_object_tuples", AsyncMock(return_value=[]))


_READER = {"can_get_metadata", "can_read_data"}
_WRITER = _READER | {"can_write_data"}
_HEADERS = {"Authorization": "Bearer t"}


def _recording_vendor() -> MagicMock:
    """A vendor that records each tier it is asked for and mints a credential naming it."""
    recorder = MagicMock()
    recorder.vend.side_effect = lambda **kw: VendedSession(
        key_pair=S3KeyPair(access_key_id=f"AK-{kw['tier']}", secret_access_key=SecretStr("SK")), endpoint=None, region="us-east-1", expires_at_millis=1
    )
    return recorder


@pytest.fixture
def estate(governed_catalog: GovernedCatalog) -> Iterator[tuple[GovernedCatalog, MagicMock]]:
    """bob's ``lance_catalog$db`` holding ``t``, made through the doors, and the catalog's vendor recording each tier."""
    assert governed_catalog.create_in_the_estate("bob", "db").status_code == 200
    governed_catalog.create_table("bob", "lance_catalog$db$t")
    recorder = _recording_vendor()
    governed_catalog.client.app.dependency_overrides[get_vendor] = lambda: recorder
    yield governed_catalog, recorder
    governed_catalog.client.app.dependency_overrides.pop(get_vendor, None)


@pytest.mark.parametrize(
    ("door", "rungs", "body", "tier", "read_only"),
    [
        pytest.param("describe", ("reader",), {}, "read", False, id="a-reader-gets-the-read-tier"),
        pytest.param("describe", ("writer",), {}, "write", False, id="a-writer-gets-the-write-tier"),
        pytest.param("describe", ("reader", "maintainer"), {}, "write", False, id="a-reader-who-maintains-gets-the-write-tier"),
        pytest.param("describe", ("maintainer",), {}, 403, False, id="a-maintainer-who-may-not-read-is-refused"),
        pytest.param("describe", ("writer",), {"vend_credentials": True}, "write", False, id="an-explicit-ask-gets-the-same-tier"),
        pytest.param("describe", ("writer",), {"vend_credentials": False}, None, False, id="asking-for-none-gets-none"),
        pytest.param("describe", (), {"vend_credentials": True}, 403, False, id="no-rung-that-asks-is-refused"),
        pytest.param("declare", ("writer",), {}, "write", False, id="a-declarer-gets-the-write-tier"),
        pytest.param("describe", ("writer",), {"vend_credentials": True}, "read", True, id="a-writer-in-a-read-only-window-gets-the-read-tier"),
    ],
)
def test_a_spec_door_vends_the_highest_tier_fga_grants(  # noqa: PLR0913 - parametrized
    estate: tuple[GovernedCatalog, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    door: str,
    rungs: tuple[str, ...],
    body: dict[str, bool],
    tier: str | int | None,
    read_only: bool,
) -> None:
    catalog, vendor = estate
    if read_only:
        monkeypatch.setenv("LANCE_MAINTENANCE_READ_ONLY", "true")
        get_settings.cache_clear()
    # A declare is judged on the namespace it declares in, a describe on the table.
    name, kind = ("lance_catalog$db$fresh", "namespace") if door == "declare" else ("lance_catalog$db$t", "table")
    obj = f"{kind}:{catalog.uuid(kind, 'lance_catalog$db' if kind == 'namespace' else name, 'bob')}"
    if rungs:
        catalog.store.write(*((catalog.person("alice"), rung, obj) for rung in rungs))
    vendor.vend.reset_mock()
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="lance.audit"):
        response = catalog.client.post(f"/v1/table/{name}/{door}", json=body, headers=catalog.bearer("alice"))

    if tier == 403:
        assert (response.status_code, response.json()["code"]) == (403, 15), response.text
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

    response = client.post(f"/v1/table/lance_catalog$db$t/{door}", json=body, headers=_HEADERS)

    assert response.status_code == status, response.text
    if status == 200:
        assert "storage_options" not in response.json()
    if door == "declare":
        assert fake_ns.drop_table.called, "the declared table is dropped so a retry can declare it again"
