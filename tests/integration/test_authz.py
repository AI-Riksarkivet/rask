"""The catalog's authorization gate when OpenFGA cannot decide, and the gate's own bookkeeping, with OpenFGA stubbed.

Which rung each door opens on is decided by the real model on a real OpenFGA in
`test_each_door_opens_on_its_own_rung.py`. What is left here needs OpenFGA to fail or to be absent, which no real store
does on demand: no client wired (503), an outage (503), an item the server could not evaluate (503, never a 403), a
listing past the enumeration cap (flagged truncated), and the decisions the gate records on the audit trail. Beside
them, the checks that run before or without authorization: no token (401), a batch past the gate answering the spec's
406, and the listing with authorization off.

Every table and namespace is asked about as the object its name resolves to, ``table:<uuid>`` / ``namespace:<uuid>``
(ADR 0173 §8), resolved here through the namespace double.
"""

from collections.abc import Callable
from typing import cast, override
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import (
    DescribeNamespaceRequest,
    DescribeNamespaceResponse,
    DescribeTableResponse,
    LanceNamespace,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListTablesRequest,
    ListTablesResponse,
    ServiceUnavailableError,
)
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientTuple
from openfga_sdk.client.models.batch_check_response import ClientBatchCheckResponse
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models.check_error import CheckError
from starlette.concurrency import run_in_threadpool

from catalog.api.dependencies import get_namespace, get_resolver, request_settings, warehouse_object
from catalog.services import object_uuids, table_claims
from catalog.services.table_claims import ResolvedTable
from lakehouse.governed import fga_ids, fga_reads, fga_writes
from lakehouse.naming import UUID_PROPERTY
from tests.app_state import app_of
from tests.callers import person
from tests.catalog_settings import governed_settings
from tests.unit.named_resolver import NamedResolver, uuid_of


#: The create door reads its body before anything it orchestrates, so the stubbed write still needs a real stream.


def _table(name: str) -> str:
    """The object the table ``name`` is asked about as when its location carries no claim (:class:`_DoubleResolver`)."""
    return f"table:{uuid_of('table', name.split('$'))}"


def _described_namespace(request: DescribeNamespaceRequest | dict[str, list[str]]) -> DescribeNamespaceResponse:
    """What the namespace double answers for a namespace: the uuid :func:`_namespace` names it by, declaring no tier."""
    segments = request["id"] if isinstance(request, dict) else list(request.id or [])
    return DescribeNamespaceResponse(properties={UUID_PROPERTY: uuid_of("namespace", segments)})


class _DoubleResolver(NamedResolver):
    """Names resolve through the namespace double, as the catalog resolves them through its own connection.

    A table is what the double describes, with the claim its location carries: its object is ``table:<claim uuid>``,
    or :func:`_table` for a location no claim records, so a test whose claim is which relation is asked need not
    claim one. A namespace is what the double describes (:func:`_described_namespace`), and a name of one segment is
    its warehouse, as the catalog governs it ([[LH-593]]).
    """

    def __init__(self, ns: LanceNamespace) -> None:
        super().__init__()
        self._ns = ns

    @override
    async def table(self, segments: list[str], *, ns: LanceNamespace | None = None) -> ResolvedTable:
        return await run_in_threadpool(table_claims.resolve, self._ns, table_claims.claim_store(governed_settings()), list(segments))

    @override
    async def namespace_uuid(self, segments: list[str], *, ns: LanceNamespace | None = None) -> str | None:
        return await run_in_threadpool(object_uuids.resolve_namespace_uuid, self._ns, list(segments)) if segments else None

    @override
    async def object(self, kind: str, segments: list[str]) -> str | None:
        if kind != "table":
            if len(segments) == 1:
                return warehouse_object(governed_settings(), segments[0])
            uuid = await self.namespace_uuid(segments)
            return None if uuid is None else fga_ids.namespace_object(uuid)
        resolved = await self.table(segments)
        if resolved.location is None:
            return None
        return fga_ids.table_object(resolved.uuid or uuid_of("table", segments))


def _wire(client: TestClient) -> MagicMock:
    """Enable OIDC+FGA, inject a faked verifier + a sentinel FGA client, and resolve names through the double.

    The verifier proves the person ``dex~alice``; the FGA client is a sentinel that the faked
    ``fga.*`` functions ignore. Returns the sentinel so a test can drop it (fail-closed).
    """
    app_of(client).dependency_overrides[request_settings] = lambda: governed_settings()
    double = app_of(client).dependency_overrides[get_namespace]()
    double.describe_namespace.side_effect = _described_namespace
    app_of(client).dependency_overrides[get_resolver] = lambda: _DoubleResolver(double)
    verifier = MagicMock()
    verifier.verify.return_value = person()
    app_of(client).state.oidc = verifier
    fga_client = MagicMock()  # sentinel; the faked fga.* functions below ignore it
    fga_client.close = AsyncMock()  # closed by the lifespan on shutdown
    app_of(client).state.fga = fga_client
    return fga_client


def _fake_check(captured: list[dict], *, allow: bool):
    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        captured.append({"user": user, "relation": relation, "obj": obj})
        return allow

    return fake_check


# --------------------------------------------------------------------------- #
# Batch + transaction + materialized_view: authz, not authn-only.
# --------------------------------------------------------------------------- #


def test_an_AUTHORIZED_batch_commit_answers_406_and_seeds_nothing(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT ([[LH-206]]): past the batch gate, ``/v1/table/batch-commit`` answers the spec's 406 while
    the catalog does not manage table versions. A ``declare_table`` sub-op creates no table and seeds no
    tuple, and the backend is never reached."""
    _wire(client)

    async def fake_batch_check(_c: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        return dict.fromkeys(objects, True)

    grants: list[dict] = []

    async def fake_grant(_c: object, **kwargs: object) -> None:
        grants.append(dict(kwargs))

    monkeypatch.setattr(fga_reads, "batch_check", fake_batch_check)
    monkeypatch.setattr(fga_writes, "grant_on_create", fake_grant)

    body = {"operations": [{"declare_table": {"id": ["lance_catalog", "db1", "fresh"]}}]}
    resp = client.post("/v1/table/batch-commit", json=body, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 406, resp.text
    assert resp.json()["code"] == 0
    assert grants == []
    fake_ns.batch_commit_tables.assert_not_called()


class _BatchOpenFga:
    """An OpenFGA client whose BatchCheck answers through the SDK's own response classes.

    Every object is allowed except those in ``unanswered``, which come back as the SDK hands back an
    item the server could not evaluate: ``allowed=False`` beside a ``CheckError``.
    """

    def __init__(self, unanswered: set[str]) -> None:
        self._unanswered = unanswered
        self.asked: list[str] = []

    async def batch_check(self, body: ClientBatchCheckRequest, options: dict | None = None) -> ClientBatchCheckResponse:
        del options
        result = []
        for index, item in enumerate(body.checks):
            self.asked.append(item.object)
            error = CheckError(input_error="validation_error", message="relation not found in the pinned model") if item.object in self._unanswered else None
            # The SDK hands the batch ITEM back as `request` (client.py `map_response`); its annotation says ClientTuple.
            result.append(ClientBatchCheckSingleResponse(allowed=error is None, request=cast("ClientTuple", item), correlation_id=f"c{index}", error=error))
        return ClientBatchCheckResponse(result)

    async def close(self) -> None:
        return None


_TWO_TABLE_COMMIT = {
    "operations": [{"create_table_version": {"id": ["lance_catalog", "db1", t], "version": 2, "manifest_path": "_versions/2.manifest"}} for t in ("a", "b")],
}


def test_batch_commit_answers_503_when_openfga_cannot_answer_an_item(client: TestClient, fake_ns: MagicMock) -> None:
    """CONTRACT: an item OpenFGA could not evaluate is an outage, not a denial — the same 503 the
    single-object guard answers, never a 403 naming a table the caller may well hold a grant on."""
    _wire(client)
    app_of(client).state.fga = _BatchOpenFga(unanswered={_table("lance_catalog$db1$b")})

    resp = client.post("/v1/table/batch-commit", json=_TWO_TABLE_COMMIT, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 503, f"an unanswered item was refused as a permission: {resp.status_code} {resp.text}"
    fake_ns.batch_commit_tables.assert_not_called()


# --------------------------------------------------------------------------- #
# Fail-closed: enabled FGA but no client -> 503, never allow.
# --------------------------------------------------------------------------- #


def test_fail_closed_when_fga_client_missing(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: FGA enabled but ``app.state.fga`` absent -> 503 (never silently allow)."""
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    _wire(client)
    # Simulate an unwired client (e.g. lifespan provisioning failed).
    if hasattr(app_of(client).state, "fga"):
        delattr(app_of(client).state, "fga")

    # check must never even be reached.
    def boom(*_a: object, **_k: object):
        raise AssertionError("fga_reads.check must not be called when the client is missing")

    monkeypatch.setattr(fga_reads, "check", boom)

    resp = client.post("/v1/table/lance_catalog$db1$users/describe", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == 17  # SERVICE_UNAVAILABLE


def test_fail_closed_when_openfga_outage(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: an OpenFGA outage surfaces as 503 (fga_reads.check raises ServiceUnavailableError),
    never as an allow. Asserts the fail-closed posture of fga_reads.check propagates to the route."""
    from lance_namespace import ServiceUnavailableError

    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    _wire(client)

    async def outage(*_a: object, **_k: object) -> bool:
        raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(fga_reads, "check", outage)

    resp = client.post("/v1/table/lance_catalog$db1$users/describe", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503
    assert resp.json()["code"] == 17  # SERVICE_UNAVAILABLE


def test_authn_still_required_when_fga_enabled(client: TestClient, monkeypatch) -> None:
    """CONTRACT: with FGA on, a missing token is 401 (authn) before any authz check runs."""
    _wire(client)

    def boom(*_a: object, **_k: object):
        raise AssertionError("authz must not run before authentication")

    monkeypatch.setattr(fga_reads, "check", boom)

    resp = client.post("/v1/table/lance_catalog$db1$users/drop")  # no Authorization header
    assert resp.status_code == 401
    assert resp.json()["code"] == 16  # UNAUTHENTICATED


# --------------------------------------------------------------------------- #
# List filtering: each listing judges its own candidates.
# --------------------------------------------------------------------------- #


def _walked(monkeypatch: pytest.MonkeyPatch, fake_ns: MagicMock, tables: list[str]) -> None:
    """Serve ``GET /v1/table``'s walk of the estate's own warehouse from the double: one namespace ``db`` holding
    ``tables``. The walk opens each warehouse's catalog itself, since a collection route has no ``{id}`` to route by."""

    async def _the_double(_request: object, _settings: object, _warehouse_id: str) -> MagicMock:
        return fake_ns

    def _list_tables(request: ListTablesRequest | dict[str, list[str]]) -> ListTablesResponse:
        segments = request["id"] if isinstance(request, dict) else list(request.id or [])
        return ListTablesResponse(tables=tables if segments == ["lance_catalog", "db"] else [])

    def _list_namespaces(request: ListNamespacesRequest | dict[str, list[str]]) -> ListNamespacesResponse:
        segments = request["id"] if isinstance(request, dict) else list(request.id or [])
        return ListNamespacesResponse(namespaces=["db"] if segments == ["lance_catalog"] else [])

    monkeypatch.setattr("catalog.api.v1.endpoints.tables.namespace_for_warehouse", _the_double)
    fake_ns.list_tables.side_effect = _list_tables
    fake_ns.list_namespaces.side_effect = _list_namespaces


def test_list_tables_unfiltered_when_fga_disabled(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: with FGA disabled no table is FILTERED OUT — every backend table survives.

    "Verbatim" is the wrong word and was the reason this test was stale: the endpoint still sorts and
    dedupes, unconditionally, because pagination depends on it. What FGA disabled changes is membership,
    not order.
    """
    _walked(monkeypatch, fake_ns, ["users", "orders"])
    # default settings: oidc/fga disabled
    resp = client.get("/v1/table")
    assert resp.status_code == 200
    assert resp.json()["tables"] == ["lance_catalog$db$orders", "lance_catalog$db$users"]


def test_authz_decision_emits_an_audit_event(
    client: TestClient, fake_ns: MagicMock, monkeypatch, claim_described: Callable[[str], str], caplog: pytest.LogCaptureFixture
) -> None:
    # #41: every authz decision (not just denials) lands on the dedicated audit stream with who/what/outcome.
    import logging

    from lakehouse.governed.audit import AUDIT_LOGGER

    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    minted = claim_described("lance_catalog$db1$users")
    _wire(client)
    monkeypatch.setattr(fga_reads, "check", _fake_check([], allow=True))

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        resp = client.post("/v1/table/lance_catalog$db1$users/describe", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200
    events = [r.__dict__ for r in caplog.records if r.__dict__.get("audit.action") == "authz" and r.__dict__.get("audit.relation") == "can_get_metadata"]
    assert events, "the authz gate emitted no audit event"
    assert events[0]["audit.outcome"] == "allow"
    assert events[0]["audit.subject"] == "dex~alice"
    assert events[0]["audit.resource"] == f"table:{minted}"


def test_access_list_fails_closed_when_enumeration_hits_an_fga_outage(client: TestClient, monkeypatch) -> None:
    """CONTRACT (#51): an OpenFGA outage during the ListUsers fan-out is a 503 at the endpoint —
    never a partial or empty grant list that reads as "nobody has access"."""
    _wire(client)
    monkeypatch.setattr(fga_reads, "check", _fake_check([], allow=True))

    async def down(_c: object, *, relation: str, obj: str) -> list[str]:
        raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(fga_reads, "list_users", down)

    resp = client.post("/management/v1/table/lance_catalog$db1$users/access/list", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503


def test_access_list_reports_truncation_instead_of_silently_shortening(client: TestClient, monkeypatch) -> None:
    """diff2 F10 item 8 — a capped enumeration must SAY it was capped.

    `fga_reads.list_users` stops at OpenFGA's `listUsersMaxResults` and only warned into the log, so the
    response looked complete. On an access review that is the dangerous direction to be wrong in: a
    reviewer reading 1000 of 1500 holders concludes the other 500 hold nothing — the exact opposite of
    the truth, and precisely the question the review exists to answer. The admin door
    (`AccessListUsersResponse.truncated`) has always reported it; this is the per-object review, which
    is the surface a human actually reads.

    Per-relation, because the cap applies per call: one relation can be complete while its neighbour
    is cut off, and only a per-relation flag can say which.
    """
    _wire(client)
    monkeypatch.setattr(fga_reads, "check", _fake_check([], allow=True))
    capped = [f"user:u{i}" for i in range(fga_reads.LIST_USERS_SERVER_CAP)]

    async def fake_list_users(_c: object, *, relation: str, obj: str, **_kw: object) -> list[str]:
        # Exactly ONE relation is at the cap — the rest are small. A response-level-only flag would
        # lose which, and a test that capped everything could not tell the two designs apart.
        return capped if relation == "can_read_data" else ["user:dex~alice"]

    monkeypatch.setattr(fga_reads, "list_users", fake_list_users)

    resp = client.post("/management/v1/table/lance_catalog$db1$users/access/list", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["truncated"] is True, "the roll-up must flag a review that is missing holders"
    by_relation = {g["relation"]: g for g in body["grants"]}
    assert by_relation["can_read_data"]["truncated"] is True
    # …and the relations that were NOT capped must not be smeared as incomplete.
    others = [g for r, g in by_relation.items() if r != "can_read_data"]
    assert others, "expected more than one can_* relation on table"
    assert all(g["truncated"] is False for g in others), [g["relation"] for g in others if g["truncated"]]
