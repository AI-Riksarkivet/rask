"""OpenFGA authorization wiring tests (our logic only — fga IO is faked).

These assert *our* authz layer end to end through the router-level ``authorize``
dependency and the per-endpoint tuple seeding / list filtering — the OpenFGA SDK
calls (``check`` / ``batch_check`` / ``list_objects`` / ``write_tuples`` via
``grant_on_create``) are faked so nothing leaves the process. The pure id helpers
(``canonical_object_id`` / ``parent_namespace_id``) run for real so the grant path
and the check path are proven to agree byte-for-byte.

Each test names the CONTRACT it pins, so a reviewer can catch a mismatch if another
agent's design drifts. The app checks ``can_*`` ACTION relations (the model owns the
op->privilege mapping); each ``can_*`` reduces to a reader/writer/owner rung in the
model. The contracts asserted here (owned by the fga / fga_deps / endpoint agents) are:

* Op -> ``can_*``: reads -> ``can_get_metadata`` / ``can_read_data`` (reader rung);
  generic mutations -> ``can_write_data`` (writer rung); lifecycle/destructive
  (``drop`` / ``deregister`` / namespace ``drop``) -> ``can_drop`` / ``can_deregister`` /
  ``can_delete`` (owner rung).
* Create-on-parent: ``create`` / ``declare`` / ``register`` of a *child* is authorized
  against the PARENT (``can_create_table`` / ``can_create_namespace``), never the
  not-yet-existing child; tuples are seeded AFTER a successful create via
  ``fga.grant_on_create`` — which now links every created object to its ``parent`` so
  the concentric cascade reaches it (nested namespaces included).
* Batch routes (``/v1/table/version/batch-create``, ``/v1/table/batch-commit``)
  and ``transaction`` / ``materialized_view`` routes require authz, not authn-only.
  Destructive batch ops use the owner tier (no writer back-door).
* Fail-closed: FGA enabled but no ``app.state.fga`` client -> 503 (never allow).
* ``GET /v1/table`` is filtered to the tables the caller can read via ``list_objects``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from catalog.api.v1.endpoints.access import _can_relations as _model_can_relations
from catalog.core.config import Settings, get_settings
from fastapi.testclient import TestClient
from lance_namespace import (
    CreateNamespaceResponse,
    CreateTableResponse,
    DeclareTableResponse,
    DeregisterTableResponse,
    DescribeTableResponse,
    DropNamespaceResponse,
    DropTableResponse,
    ListNamespacesResponse,
    ListTablesResponse,
    ServiceUnavailableError,
)

from service_kit.governed import fga as fga_module
from service_kit.governed.oidc import IDToken


ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}


def _auth_settings() -> Settings:
    # model_validate (not Settings(...)) so field-name keys validate cleanly: the
    # fields carry LANCE_* aliases and populate_by_name accepts the names at runtime.
    return Settings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


def _wire(client: TestClient) -> MagicMock:
    """Enable OIDC+FGA, inject a faked verifier + a sentinel FGA client.

    The verifier returns user ``alice``; the FGA client is a sentinel that the faked
    ``fga.*`` functions ignore. Returns the sentinel so a test can drop it (fail-closed).
    """
    client.app.dependency_overrides[get_settings] = _auth_settings
    verifier = MagicMock()
    verifier.verify.return_value = IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    client.app.state.oidc = verifier
    fga_client = MagicMock()  # sentinel; the faked fga.* functions below ignore it
    fga_client.close = AsyncMock()  # closed by the lifespan on shutdown
    client.app.state.fga = fga_client
    return fga_client


def _fake_check(captured: list[dict], *, allow: bool):
    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        captured.append({"user": user, "relation": relation, "obj": obj})
        return allow

    return fake_check


def _stub_create(monkeypatch, *, response: object = None, error: BaseException | None = None) -> None:
    """Stub the write facade ``dataplane.create_table`` so these tests isolate the endpoint's FGA
    orchestration from the write. Every create now routes through the direct 2.2 write (declare +
    ``lance.write_dataset``), so the old ``fake_ns.create_table.return_value`` no longer intercepts it and a
    real write would demand real Arrow + a real FGA server. The facade is the correct seam: these tests have
    always mocked the backend on purpose, to assert *only* that a create grants owner+parent and a failed
    create seeds nothing."""

    def _facade(*_a: object, **_k: object) -> object:
        if error is not None:
            raise error
        return response

    monkeypatch.setattr("catalog.services.dataplane.create_table", _facade)


# --------------------------------------------------------------------------- #
# Relation tiers: reader / writer / owner.
# --------------------------------------------------------------------------- #


def test_read_op_checks_reader_and_allows(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a read op (describe) checks ``can_get_metadata`` (reader rung) and allows."""
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=True))

    resp = client.post("/v1/table/db1$users/describe", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert captured == [{"user": "alice", "relation": "can_get_metadata", "obj": "table:db1$users"}]


def test_blob_read_checks_data_reader_and_denies(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: ``GET /v1/table/{id}/blobs`` (the credential-less blob serving path) is a DATA read —
    the router guard checks ``can_read_data`` (same rung as ``/query``, NOT the writer fallthrough)
    and a caller without it is 403'd BEFORE the endpoint touches any dataset."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.get(
        "/v1/table/db1$users/blobs",
        params={"column": "payload", "row": 0},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 403
    assert captured == [{"user": "alice", "relation": "can_read_data", "obj": "table:db1$users"}]


def test_generic_mutation_checks_writer(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a non-lifecycle mutation (update) checks ``can_write_data`` (writer rung)."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post(
        "/v1/table/db1$users/update",
        json={"predicate": "id = 1", "updates": {"id": "2"}},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_write_data", "obj": "table:db1$users"}


def test_rename_is_owner_tier_not_writer(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT (security): rename is OWNER-tier via ``can_drop`` on the SOURCE — it destroys the source id
    (revokes its tuples) and re-seeds the caller as owner of the destination, so a writer-tier gate would let
    a non-owner rename another user's table and seize sole ownership (the twin of the Overwrite escalation).
    A non-owner is 403'd BEFORE any revoke/seed."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))
    revoke, grant = AsyncMock(return_value=1), AsyncMock()
    monkeypatch.setattr(fga_module, "revoke_object_tuples", revoke)
    monkeypatch.setattr(fga_module, "grant_on_create", grant)

    resp = client.post("/v1/table/db1$users/rename", json={"new_table_name": "u2"}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_drop", "obj": "table:db1$users"}
    revoke.assert_not_awaited()  # the true owner is NOT evicted
    grant.assert_not_awaited()


def test_rename_into_unauthorized_destination_namespace_is_denied(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT (security): renaming INTO a namespace is a create there — it requires can_create_table on the
    DEST parent. A source-table owner who lacks create rights on the destination cannot plant (relocate) their
    table into another tenant's namespace; denied BEFORE the destructive native rename, no revoke/seed."""

    async def _check(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        return relation == "can_drop"  # owner on the source table yes; can_create_table on dest namespace no

    _wire(client)
    monkeypatch.setattr(fga_module, "check", _check)
    revoke, grant = AsyncMock(return_value=1), AsyncMock()
    monkeypatch.setattr(fga_module, "revoke_object_tuples", revoke)
    monkeypatch.setattr(fga_module, "grant_on_create", grant)

    resp = client.post(
        "/v1/table/staging$scratch/rename",
        json={"new_namespace_id": ["gold"], "new_table_name": "injected"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 403
    revoke.assert_not_awaited()
    grant.assert_not_awaited()
    fake_ns.rename_table.assert_not_called()  # gated before the relocating native rename


def test_drop_table_requires_owner_and_403_is_problem_json(client: TestClient, monkeypatch) -> None:
    """CONTRACT: dropping a table requires the owner tier via ``can_drop``, 403 -> problem+json.

    NOTE: this supersedes the old behaviour where ``drop`` mapped to ``writer``. Per
    ``design-permissions.md`` (``can_drop: owner``), destructive ops require ``owner``.
    """
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post("/v1/table/db1$users/drop", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == 15  # PERMISSION_DENIED
    assert captured[-1] == {"user": "alice", "relation": "can_drop", "obj": "table:db1$users"}


def test_deregister_table_requires_owner(client: TestClient, monkeypatch) -> None:
    """CONTRACT: ``deregister`` is also owner-tier, via ``can_deregister``."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post("/v1/table/db1$users/deregister", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1]["relation"] == "can_deregister"
    assert captured[-1]["obj"] == "table:db1$users"


def test_drop_namespace_requires_owner_on_namespace_object(client: TestClient, monkeypatch) -> None:
    """CONTRACT: dropping a namespace is owner-tier via ``can_delete`` on the ``namespace:`` object."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post("/v1/namespace/db1/drop", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_delete", "obj": "namespace:db1"}


def test_hash_or_question_in_id_cannot_downgrade_the_owner_tier(client: TestClient, monkeypatch) -> None:
    """CONTRACT: the action tier derives from the RAW ASGI path — an id containing an encoded
    ``#``/``?`` (decoded into ``scope['path']``) must not truncate the route suffix and collapse an
    owner-tier op (drop/deregister) to the generic ``can_write_data`` check. ``request.url.path``
    re-parses the decoded path as a URL string and DOES truncate there (the audit's tier-downgrade
    hole), which is why ``authorize`` reads the scope path."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post("/v1/table/db1$us%23ers/drop", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_drop", "obj": "table:db1$us#ers"}

    resp = client.post("/v1/table/db1$us%3Fers/deregister", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_deregister", "obj": "table:db1$us?ers"}


# --------------------------------------------------------------------------- #
# Create-on-parent: authorize the parent, then seed owner + parent tuples.
# --------------------------------------------------------------------------- #


def test_create_table_checks_writer_on_parent_namespace(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: creating ``db1$users`` checks ``can_create_table`` on PARENT ``namespace:db1``.

    The leaf table does not exist yet, so the check is on the parent (create-on-parent).
    """
    fake_ns.create_table.return_value = CreateTableResponse(location="s3://x", version=1)
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post(
        "/v1/table/db1$users/create",
        content=b"ARROW",
        headers={"Authorization": "Bearer t", **ARROW_STREAM},
    )
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_create_table", "obj": "namespace:db1"}


def test_existok_on_existing_table_does_not_seize_ownership(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """SECURITY (audit CRITICAL): an ExistOk create that KEEPS an already-existing table wrote nothing, so it
    must NOT grant the caller ``owner`` — else any authenticated user (top-level, lock off) or any
    namespace-writer could SEIZE ownership of another user's table via a no-op create (owner ⇒ can_read_data,
    write-cred vending, drop, rename). This test FAILS on the pre-fix code, which seeded ownership
    unconditionally."""
    # The table PRE-EXISTS (describe succeeds → _table_exists True) and the facade returns its existing
    # version (ExistOk kept it, wrote nothing).
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    _stub_create(monkeypatch, response=CreateTableResponse(location="s3://x", version=7))
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))  # caller passes create-on-parent
    grant = AsyncMock()
    monkeypatch.setattr(fga_module, "grant_on_create", grant)

    resp = client.post(
        "/v1/table/db1$users/create?mode=exist_ok",
        content=b"ARROW",
        headers={"Authorization": "Bearer t", **ARROW_STREAM},
    )
    assert resp.status_code == 200, resp.text
    grant.assert_not_awaited()  # NO ownership seizure — the existing owner keeps it


def test_create_table_seeds_owner_and_parent_tuples(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a successful create seeds ``owner`` on the table + a ``parent`` link.

    Seeding goes through the single write path ``fga.grant_on_create`` AFTER the backend
    create succeeds. We assert the args it receives (resource/obj_id/parent) — the real
    ``canonical_object_id`` / ``parent_namespace_id`` helpers run, proving the grant id
    (``table:db1$users``, parent ``db1``) matches what ``authorize`` would later check.
    """
    _stub_create(monkeypatch, response=CreateTableResponse(location="s3://x", version=1))
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    grant = AsyncMock()
    monkeypatch.setattr(fga_module, "grant_on_create", grant)

    resp = client.post(
        "/v1/table/db1$users/create",
        content=b"ARROW",
        headers={"Authorization": "Bearer t", **ARROW_STREAM},
    )
    assert resp.status_code == 200
    grant.assert_awaited_once()
    call = grant.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["user_sub"] == "alice"
    assert kwargs["resource"] == "table"
    assert kwargs["obj_id"] == "db1$users"  # canonical delimited id
    assert kwargs["parent_object"] == "namespace:db1"  # parent link of db1$users


def test_create_table_does_not_seed_when_backend_fails(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: no tuples are seeded if the backend create raises (no orphan grant)."""
    from lance_namespace import TableAlreadyExistsError

    _stub_create(monkeypatch, error=TableAlreadyExistsError("exists"))
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    grant = AsyncMock()
    monkeypatch.setattr(fga_module, "grant_on_create", grant)

    resp = client.post(
        "/v1/table/db1$users/create",
        content=b"ARROW",
        headers={"Authorization": "Bearer t", **ARROW_STREAM},
    )
    assert resp.status_code == 409
    grant.assert_not_awaited()


def test_declare_table_is_create_on_parent(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: ``declare`` is also create-on-parent — checks ``can_create_table`` on parent."""
    fake_ns.declare_table.return_value = DeclareTableResponse()
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post("/v1/table/db1$users/declare", json={}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_create_table", "obj": "namespace:db1"}


def test_create_namespace_seeds_owner_and_parent_link(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: creating a nested namespace checks ``can_create_namespace`` on its parent,
    then seeds an ``owner`` tuple AND a ``parent`` link to the parent namespace — so an
    owner of ``parent`` cascades into ``parent$child`` and all its tables (no lockout)."""
    fake_ns.create_namespace.return_value = CreateNamespaceResponse(properties={})
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=True))
    grant = AsyncMock()
    monkeypatch.setattr(fga_module, "grant_on_create", grant)

    resp = client.post("/v1/namespace/parent$child/create", json={}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    # authorize() checked can_create_namespace on the parent namespace, not the child.
    assert captured[-1] == {"user": "alice", "relation": "can_create_namespace", "obj": "namespace:parent"}
    grant.assert_awaited_once()
    call = grant.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["resource"] == "namespace"
    assert kwargs["obj_id"] == "parent$child"
    assert kwargs["parent_object"] == "namespace:parent"  # nested ns links to its parent


def test_create_top_level_namespace_links_to_catalog_root(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a TOP-LEVEL namespace seeds its ``parent`` as the FGA root object (the warehouse
    root), so a warehouse-/project-level grant cascades into it."""
    fake_ns.create_namespace.return_value = CreateNamespaceResponse(properties={})
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    grant = AsyncMock()
    monkeypatch.setattr(fga_module, "grant_on_create", grant)

    resp = client.post("/v1/namespace/bronze/create", json={}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    call = grant.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["obj_id"] == "bronze"
    assert kwargs["parent_object"] == "warehouse:lance_catalog"  # default fga_root_object


# --------------------------------------------------------------------------- #
# Revoke-on-delete: a successful lifecycle op CLEANS UP the object's FGA tuples.
# These guard the WIRING — deleting the revoke call in tables.py/namespaces.py
# (re-opening the stale-grant bleed) must turn these red. The deny-path tests above
# never reach the endpoint body, so they can't catch a missing revoke.
# --------------------------------------------------------------------------- #


def test_drop_table_revokes_tuples(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a successful drop_table revokes the table's tuples (obj ``table:db1$users``)."""
    fake_ns.drop_table.return_value = DropTableResponse()
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    revoke = AsyncMock(return_value=1)
    monkeypatch.setattr(fga_module, "revoke_object_tuples", revoke)

    resp = client.post("/v1/table/db1$users/drop", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    revoke.assert_awaited_once()
    call = revoke.await_args
    assert call is not None and call.args[1] == "table:db1$users"


def test_deregister_table_revokes_tuples(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a successful deregister_table revokes the table's tuples."""
    fake_ns.deregister_table.return_value = DeregisterTableResponse()
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    revoke = AsyncMock(return_value=1)
    monkeypatch.setattr(fga_module, "revoke_object_tuples", revoke)

    resp = client.post("/v1/table/db1$users/deregister", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    revoke.assert_awaited_once()
    call = revoke.await_args
    assert call is not None and call.args[1] == "table:db1$users"


def test_drop_namespace_revokes_tuples(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a successful drop_namespace revokes the namespace's tuples (obj ``namespace:db1``)."""
    fake_ns.drop_namespace.return_value = DropNamespaceResponse()
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    revoke = AsyncMock(return_value=1)
    monkeypatch.setattr(fga_module, "revoke_object_tuples", revoke)

    resp = client.post("/v1/namespace/db1/drop", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    revoke.assert_awaited_once()
    call = revoke.await_args
    assert call is not None and call.args[1] == "namespace:db1"


def test_rename_table_revokes_source_then_seeds_destination(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: rename revokes the SOURCE id's tuples and seeds the DEST — and revoke fires BEFORE seed,
    so no stale grant survives under the old id (``table:db1$users``) while the dest (``db1$u2``) is owned."""
    # rename is now an in-process relocate (#5b); stub the dataplane seam so the FGA choreography is exercised
    # without a real dataset move (returns the resolved new segments + destination location).
    monkeypatch.setattr(
        "catalog.services.dataplane.rename_table",
        lambda ns, so, segments, name, nsid: (
            [*(list(nsid) if nsid else segments[:-1]), name],
            "s3://b/db1$u2",
        ),
    )
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    order: list[str] = []

    async def _revoke(_c: object, obj: str, **_k: object) -> int:
        order.append(f"revoke:{obj}")
        return 1

    async def _grant(_c: object, **kw: object) -> None:
        order.append(f"grant:{kw['obj_id']}")

    monkeypatch.setattr(fga_module, "revoke_object_tuples", _revoke)
    monkeypatch.setattr(fga_module, "grant_on_create", _grant)

    resp = client.post(
        "/v1/table/db1$users/rename",
        json={"new_table_name": "u2"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200
    assert order == ["revoke:table:db1$users", "grant:db1$u2"]


def test_overwrite_by_owner_revokes_prior_grants_then_seeds(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: an Overwrite of an EXISTING table BY ITS OWNER (can_drop passes) is drop+recreate, so the
    prior grants are revoked BEFORE the overwriter is re-seeded (``table:db1$users``), revoke before grant."""
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://b/db1$users")  # table exists
    _stub_create(monkeypatch, response=CreateTableResponse(location="s3://b/db1$users", version=2))
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))  # owner → can_drop passes
    order: list[str] = []

    async def _revoke(_c: object, obj: str, **_k: object) -> int:
        order.append(f"revoke:{obj}")
        return 1

    async def _grant(_c: object, **kw: object) -> None:
        order.append(f"grant:{kw['obj_id']}")

    monkeypatch.setattr(fga_module, "revoke_object_tuples", _revoke)
    monkeypatch.setattr(fga_module, "grant_on_create", _grant)

    resp = client.post(
        "/v1/table/db1$users/create?mode=overwrite",
        content=b"ARROW",
        headers={"Authorization": "Bearer t", **ARROW_STREAM},
    )
    assert resp.status_code == 200
    assert order == ["revoke:table:db1$users", "grant:db1$users"]


def test_overwrite_of_existing_table_by_non_owner_is_denied_and_revokes_nothing(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT (security): Overwrite = drop+recreate, so overwriting an EXISTING table needs owner-tier
    can_drop — NOT the writer-tier can_create_table that authorize applies to a create. A namespace writer
    who is not the table owner is 403'd BEFORE the destructive write, and NO grant is revoked (the audit's
    eviction/seizure vector). The backend create is never reached."""
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://b/db1$users")  # table exists

    async def _check(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        return relation != "can_drop"  # writer-on-parent (create) allowed; owner-tier can_drop denied

    _wire(client)
    monkeypatch.setattr(fga_module, "check", _check)
    revoke = AsyncMock(return_value=1)
    grant = AsyncMock()
    monkeypatch.setattr(fga_module, "revoke_object_tuples", revoke)
    monkeypatch.setattr(fga_module, "grant_on_create", grant)

    resp = client.post(
        "/v1/table/db1$users/create?mode=overwrite",
        content=b"ARROW",
        headers={"Authorization": "Bearer t", **ARROW_STREAM},
    )
    assert resp.status_code == 403  # owner-tier gate on the destructive overwrite
    revoke.assert_not_awaited()  # the owner's grant is NOT stripped
    grant.assert_not_awaited()
    fake_ns.create_table.assert_not_called()  # gated BEFORE the irreversible write


def test_plain_create_seeds_without_revoking(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a non-overwrite create only seeds — it must NOT revoke (fresh id, nothing to clean)."""
    _stub_create(monkeypatch, response=CreateTableResponse(location="s3://b/db1$new", version=1))
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    revoke = AsyncMock(return_value=0)
    grant = AsyncMock()
    monkeypatch.setattr(fga_module, "revoke_object_tuples", revoke)
    monkeypatch.setattr(fga_module, "grant_on_create", grant)

    resp = client.post(
        "/v1/table/db1$new/create",
        content=b"ARROW",
        headers={"Authorization": "Bearer t", **ARROW_STREAM},
    )
    assert resp.status_code == 200
    revoke.assert_not_awaited()
    grant.assert_awaited_once()


def test_cascade_drop_namespace_revokes_every_child(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a Cascade namespace drop revokes the namespace AND every dropped child (tables + nested
    namespaces enumerated before the drop), so no reused child id inherits a stale grant."""
    fake_ns.drop_namespace.return_value = DropNamespaceResponse()
    fake_ns.list_tables.return_value = ListTablesResponse(tables=["users", "logs"], page_token=None)
    fake_ns.list_namespaces.return_value = ListNamespacesResponse(namespaces=[], page_token=None)
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    revoked: list[str] = []

    async def _revoke(_c: object, obj: str, **_k: object) -> int:
        revoked.append(obj)
        return 1

    monkeypatch.setattr(fga_module, "revoke_object_tuples", _revoke)

    resp = client.post(
        "/v1/namespace/db1/drop",
        json={"behavior": "Cascade"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200
    assert set(revoked) == {"namespace:db1", "table:db1$users", "table:db1$logs"}


def test_restrict_drop_namespace_revokes_only_itself(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a Restrict (default) drop does NOT enumerate or revoke children — a non-empty Restrict drop
    errors in the backend, so children (and their still-valid grants) stay put."""
    fake_ns.drop_namespace.return_value = DropNamespaceResponse()
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))
    revoked: list[str] = []

    async def _revoke(_c: object, obj: str, **_k: object) -> int:
        revoked.append(obj)
        return 1

    monkeypatch.setattr(fga_module, "revoke_object_tuples", _revoke)

    resp = client.post("/v1/namespace/db1/drop", json={}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert revoked == ["namespace:db1"]
    fake_ns.list_tables.assert_not_called()  # no descendant enumeration on the Restrict path


# --------------------------------------------------------------------------- #
# Batch + transaction + materialized_view: authz, not authn-only.
# --------------------------------------------------------------------------- #


def test_batch_commit_requires_writer_on_each_table(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: ``/v1/table/batch-commit`` requires ``can_write_data`` on every table named
    in the body (via ``batch_check``); a denial on any one is 403. Previously authn-only."""
    _wire(client)
    seen: dict = {}

    async def fake_batch_check(_c: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        seen.update(user=user, relation=relation, objects=list(objects))
        # Deny db1$b to prove a single denial fails the whole batch.
        return {o: (o != "table:db1$b") for o in objects}

    monkeypatch.setattr(fga_module, "batch_check", fake_batch_check)

    body = {
        "operations": [
            {"create_table_version": {"id": ["db1", "a"]}},
            {"create_table_version": {"id": ["db1", "b"]}},
        ]
    }
    resp = client.post("/v1/table/batch-commit", json=body, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert seen["relation"] == "can_write_data"
    assert set(seen["objects"]) == {"table:db1$a", "table:db1$b"}


def test_batch_commit_declare_seeds_ownership_like_the_declare_route(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT (audit 2026-07-12): a ``declare_table`` sub-op inside ``/v1/table/batch-commit``
    CREATES a table, so the creator is seeded owner + parent edge exactly like the dedicated
    ``/declare`` route — previously the batch path seeded NOTHING (the created table had no owner
    tuple: fail-closed, but the creator couldn't manage their own table)."""
    from lance_namespace import BatchCommitTablesResponse

    fake_ns.batch_commit_tables.return_value = BatchCommitTablesResponse(results=[])
    _wire(client)

    async def fake_batch_check(_c: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        return dict.fromkeys(objects, True)  # create-on-parent allowed

    grants: list[dict] = []

    async def fake_grant(_c: object, **kwargs: object) -> None:
        grants.append(dict(kwargs))

    monkeypatch.setattr(fga_module, "batch_check", fake_batch_check)
    monkeypatch.setattr(fga_module, "grant_on_create", fake_grant)

    body = {"operations": [{"declare_table": {"id": ["db1", "fresh"]}}]}
    resp = client.post("/v1/table/batch-commit", json=body, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert len(grants) == 1  # exactly the declared table, nothing else
    assert grants[0]["user_sub"] == "alice"
    assert grants[0]["resource"] == "table"
    assert grants[0]["obj_id"] == "db1$fresh"  # canonical id — byte-identical to the check path


def test_batch_commit_deregister_requires_owner_tier(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a destructive op inside batch-commit (``deregister_table``) is checked at the
    OWNER tier via ``can_deregister`` — closing the writer back-door the audit flagged."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    body = {"operations": [{"deregister_table": {"id": ["db1", "a"]}}]}
    resp = client.post("/v1/table/batch-commit", json=body, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_deregister", "obj": "table:db1$a"}


def test_batch_malformed_body_fails_closed(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a malformed batch body (``operations`` not a list) is a 403 deny, never a
    500 crash or a silently-skipped check. The security gate must fail closed on bad input."""
    _wire(client)

    async def boom(*_a: object, **_k: object) -> dict[str, bool]:
        raise AssertionError("batch_check must not run on a malformed body")

    monkeypatch.setattr(fga_module, "batch_check", boom)

    resp = client.post("/v1/table/batch-commit", json={"operations": 42}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert resp.json()["code"] == 15  # PERMISSION_DENIED, not a 500


def test_batch_create_versions_requires_writer(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: ``/v1/table/version/batch-create`` requires ``can_write_data`` on each entry."""
    _wire(client)
    seen: dict = {}

    async def fake_batch_check(_c: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        seen.update(relation=relation, objects=list(objects))
        return dict.fromkeys(objects, False)

    monkeypatch.setattr(fga_module, "batch_check", fake_batch_check)

    body = {"entries": [{"id": ["db1", "users"]}]}
    resp = client.post("/v1/table/version/batch-create", json=body, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert seen["relation"] == "can_write_data"
    assert seen["objects"] == ["table:db1$users"]


def test_transaction_route_requires_authz(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a transaction op is authorized, not merely authenticated. An OPAQUE (no enclosing
    namespace) txn id is checked against the dedicated ``transaction`` FGA type — no longer the
    ``table:<txn-id>`` alias that nothing ever seeds (the old always-deny). A denial is 403."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post("/v1/transaction/txn1/describe", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured, "transaction route must run an authz check, not pass on authn only"
    assert captured[-1]["obj"] == "transaction:txn1"  # dedicated transaction type, not table alias
    assert captured[-1]["relation"] == "can_describe"  # describe → viewer rung on the transaction


def test_transaction_namespaced_is_parent_scoped(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: a NAMESPACED txn id (``<ns>$<txn>``) is authorized PARENT-SCOPED against its enclosing
    namespace — where the caller's grant already lives — so it resolves on the first call with no
    per-transaction seed (the always-deny fix). ``describe`` = reader tier, ``alter`` = writer tier.

    The writer-tier relation MUST be one the ``namespace`` type defines: this test used to assert
    ``can_write_data``, a TABLE-only action, which OpenFGA rejects on a namespace (400, "relation not
    found") — fail-closed to a 503 for EVERY caller, owners included. Mocking ``fga.check`` made the
    phantom relation look fine, so ``tests/unit/test_fga_model_contract.py`` now cross-checks every
    (type, relation) the app can send against the compiled model."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    # describe → can_get_metadata on the enclosing namespace
    resp = client.post("/v1/transaction/db1$txn1/describe", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_get_metadata", "obj": "namespace:db1"}

    # alter (may commit/abort) → the namespace WRITER rung: can_update_properties (`writer`), the same
    # rung `transaction#can_set_status: committer` resolves to for a parented txn (committer ⊇ writer
    # from parent) — so both branches gate on the identical privilege.
    resp = client.post(
        "/v1/transaction/db1$txn1/alter",
        json={"actions": [{"setStatusAction": {"status": "Succeeded"}}]},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_update_properties", "obj": "namespace:db1"}


def test_materialized_view_create_is_create_on_parent(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: creating a materialized view is authorized (create-on-parent), not authn-only. It has
    its OWN ``can_create_materialized_view`` relation on the parent namespace (no longer the ``table``
    alias's ``can_create_table``); the check is on the parent namespace."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post(
        "/v1/materialized_view/db1$mv/create",
        json={"query": "select 1", "view_name": "mv"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 403
    assert captured, "materialized_view create must run an authz check"
    assert captured[-1]["relation"] == "can_create_materialized_view"
    assert captured[-1]["obj"] == "namespace:db1"  # create-on-parent
    # NOTE: the post-create owner+parent seeding (the audit's deny-all-on-own-MV fix) uses the
    # same fga.grant_on_create path proven by test_create_table_seeds_owner_and_parent_tuples,
    # now with resource="materialized_view"; a dedicated success-path test is omitted only because
    # CreateMaterializedViewRequest needs a full Arrow output_schema body to get past validation.


def test_materialized_view_refresh_checks_can_refresh(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: refreshing a materialized view (the async rematerialize) is authorized against the
    dedicated ``materialized_view`` object at the writer-tier ``can_refresh`` relation."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post("/v1/materialized_view/db1$mv/refresh", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_refresh", "obj": "materialized_view:db1$mv"}


def test_table_restore_is_owner_tier(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: ``restore`` REWINDS a table to an older version (destructive) — authorized at the OWNER
    tier via ``can_restore``, strictly ABOVE the writer rung a plain data write clears."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post("/v1/table/db1$t/restore", json={"version": 1}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_restore", "obj": "table:db1$t"}


def test_table_branch_and_tag_create_are_owner_tier(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: forking/re-pointing the ref-plane (branch/tag create, tag update) is OWNER-tier —
    a plain writer is denied. Each maps to its own ``can_create_branch``/``can_create_tag``/
    ``can_update_tag`` relation on the table."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    cases = [
        ("branches/create", {"name": "exp"}, "can_create_branch"),
        ("tags/create", {"tag": "v1", "version": 1}, "can_create_tag"),
        ("tags/update", {"tag": "v1", "version": 2}, "can_update_tag"),
    ]
    for suffix, body, relation in cases:
        resp = client.post(f"/v1/table/db1$t/{suffix}", json=body, headers={"Authorization": "Bearer t"})
        assert resp.status_code == 403, suffix
        assert captured[-1] == {"user": "alice", "relation": relation, "obj": "table:db1$t"}, suffix


# --------------------------------------------------------------------------- #
# Fail-closed: enabled FGA but no client -> 503, never allow.
# --------------------------------------------------------------------------- #


def test_fail_closed_when_fga_client_missing(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: FGA enabled but ``app.state.fga`` absent -> 503 (never silently allow)."""
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    _wire(client)
    # Simulate an unwired client (e.g. lifespan provisioning failed).
    if hasattr(client.app.state, "fga"):
        delattr(client.app.state, "fga")

    # check must never even be reached.
    def boom(*_a: object, **_k: object):
        raise AssertionError("fga.check must not be called when the client is missing")

    monkeypatch.setattr(fga_module, "check", boom)

    resp = client.post("/v1/table/db1$users/describe", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == 17  # SERVICE_UNAVAILABLE


def test_fail_closed_when_openfga_outage(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: an OpenFGA outage surfaces as 503 (fga.check raises ServiceUnavailableError),
    never as an allow. Asserts the fail-closed posture of fga.check propagates to the route."""
    from lance_namespace import ServiceUnavailableError

    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    _wire(client)

    async def outage(*_a: object, **_k: object) -> bool:
        raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(fga_module, "check", outage)

    resp = client.post("/v1/table/db1$users/describe", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503
    assert resp.json()["code"] == 17  # SERVICE_UNAVAILABLE


def test_authn_still_required_when_fga_enabled(client: TestClient, monkeypatch) -> None:
    """CONTRACT: with FGA on, a missing token is 401 (authn) before any authz check runs."""
    _wire(client)

    def boom(*_a: object, **_k: object):
        raise AssertionError("authz must not run before authentication")

    monkeypatch.setattr(fga_module, "check", boom)

    resp = client.post("/v1/table/db1$users/drop")  # no Authorization header
    assert resp.status_code == 401
    assert resp.json()["code"] == 16  # UNAUTHENTICATED


# --------------------------------------------------------------------------- #
# List filtering via list_objects.
# --------------------------------------------------------------------------- #


def test_list_tables_filtered_by_list_objects(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    """CONTRACT: ``GET /v1/table`` returns only tables the caller can read, intersected with
    the backend listing using ``fga.list_objects(relation='can_read_data', type='table')``."""
    fake_ns.list_all_tables.return_value = ListTablesResponse(tables=["users", "orders", "secret"])
    _wire(client)
    seen: dict = {}

    async def fake_list_objects(_c: object, *, user: str, relation: str, object_type: str) -> list[str]:
        seen.update(user=user, relation=relation, object_type=object_type)
        # Caller can read users + orders (and some table not in this listing).
        return ["table:users", "table:orders", "table:elsewhere"]

    monkeypatch.setattr(fga_module, "list_objects", fake_list_objects)

    resp = client.get("/v1/table", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert resp.json()["tables"] == ["users", "orders"]  # secret filtered out
    assert seen == {"user": "alice", "relation": "can_read_data", "object_type": "table"}


def test_list_tables_unfiltered_when_fga_disabled(client: TestClient, fake_ns: MagicMock) -> None:
    """CONTRACT: with FGA disabled the listing is returned verbatim (no filtering)."""
    fake_ns.list_all_tables.return_value = ListTablesResponse(tables=["users", "orders"])
    # default settings: oidc/fga disabled
    resp = client.get("/v1/table")
    assert resp.status_code == 200
    assert resp.json()["tables"] == ["users", "orders"]


@pytest.mark.parametrize("allow", [True, False])
def test_describe_allow_and_deny(client: TestClient, fake_ns: MagicMock, monkeypatch, allow: bool) -> None:
    """CONTRACT: a positive check lets the request through (200); a negative one is 403."""
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=allow))

    resp = client.post("/v1/table/db1$users/describe", headers={"Authorization": "Bearer t"})
    assert resp.status_code == (200 if allow else 403)


def test_authz_decision_emits_an_audit_event(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # #41: every authz decision (not just denials) lands on the dedicated audit stream with who/what/outcome.
    import logging

    from service_kit.governed.audit import AUDIT_LOGGER, configure_audit

    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    logging.getLogger(AUDIT_LOGGER).addHandler(handler)
    configure_audit(enabled=True)
    try:
        resp = client.post("/v1/table/db1$users/describe", headers={"Authorization": "Bearer t"})
    finally:
        logging.getLogger(AUDIT_LOGGER).removeHandler(handler)

    assert resp.status_code == 200
    events = [r.__dict__ for r in records if r.__dict__.get("audit.action") == "can_get_metadata"]
    assert events, "the authz gate emitted no audit event"
    assert events[0]["audit.outcome"] == "allow"
    assert events[0]["audit.subject"] == "alice"
    assert events[0]["audit.resource"] == "table:db1$users"


def test_table_policy_set_requires_owner_via_can_drop(client: TestClient, monkeypatch) -> None:
    """CONTRACT (#50): a retention policy authorizes destroying version history — owner tier via
    ``can_drop``, like drop/rename; 403 for a non-owner, and the native describe is never reached."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post(
        "/v1/table/db1$users/policy/set",
        headers={"Authorization": "Bearer t"},
        json={"retention_days": 7},
    )
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_drop", "obj": "table:db1$users"}


def test_namespace_policy_set_requires_owner_via_can_delete(client: TestClient, monkeypatch) -> None:
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post(
        "/v1/namespace/silver/policy/set",
        headers={"Authorization": "Bearer t"},
        json={"retain_versions": 5},
    )
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_delete", "obj": "namespace:silver"}


def test_table_policy_describe_is_reader_tier(client: TestClient, monkeypatch) -> None:
    """CONTRACT (#50): reading a policy is a metadata read (``can_get_metadata``) — 404 when unset is
    only reachable after the reader gate passes."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=True))

    resp = client.post("/v1/table/db1$users/policy/describe", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 404  # gate passed; no policy stored in this fixture
    assert captured[-1] == {"user": "alice", "relation": "can_get_metadata", "obj": "table:db1$users"}


def test_project_policy_set_requires_project_admin(client: TestClient, monkeypatch) -> None:
    """CONTRACT (#84): the project-level policy is TENANT-ADMIN tier — ``can_administer`` on
    ``project:<id>`` (checked explicitly; ``/v1/project`` is not a router-guarded resource prefix) —
    and on a deny the warehouse registry is never consulted."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))
    registry_reads: list[str] = []
    from catalog.services import warehouses as wh_svc

    monkeypatch.setattr(wh_svc, "list_warehouses", lambda *a, **k: registry_reads.append("read") or [])

    resp = client.post(
        "/v1/project/acme/policy/set",
        headers={"Authorization": "Bearer t"},
        json={"retention_days": 90},
    )
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_administer", "obj": "project:acme"}
    assert registry_reads == []  # fail-closed ORDER: gate first, registry read only after an allow


def test_project_policy_describe_and_delete_require_project_admin(client: TestClient, monkeypatch) -> None:
    """CONTRACT (#84): describe gates on ``can_administer`` too — the ``project`` type defines no
    reader-tier relation, and checking a phantom one would 400 → fail-closed 503 for everyone."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))
    for action in ("describe", "delete"):
        resp = client.post(f"/v1/project/acme/policy/{action}", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 403, action
        assert captured[-1] == {"user": "alice", "relation": "can_administer", "obj": "project:acme"}


def test_project_policies_list_requires_project_admin(client: TestClient, monkeypatch) -> None:
    """CONTRACT (#65): the project-scoped policy LISTING gates on the SAME bar as the set/describe/delete
    trio — ``can_administer`` on ``project:<id>``, checked explicitly (``/v1/projects`` is not a
    router-guarded resource prefix). A second, estate-observer door for the same information would split
    the tier: a tenant admin could be shown one of their records and denied the list of all of them.

    Fail-closed ORDER is pinned the same way ``policy/set`` pins it: on a deny the registry is never read,
    so the door cannot disclose a tenant's warehouse or binding shape to a caller it just refused."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))
    registry_reads: list[str] = []
    from catalog.services import warehouses as wh_svc

    monkeypatch.setattr(wh_svc, "list_warehouses", lambda *a, **k: registry_reads.append("read") or [])

    resp = client.get("/v1/projects/acme/policies", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_administer", "obj": "project:acme"}
    assert registry_reads == []  # gate first, registry read only after an allow


def test_table_access_list_requires_owner_via_can_drop(client: TestClient, monkeypatch) -> None:
    """CONTRACT (#51): enumerating who holds access reveals principals — owner tier via ``can_drop``;
    403 for a non-owner, and no ListUsers query is ever issued on a deny."""
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))
    queried: list[str] = []

    async def fake_list_users(_c: object, *, relation: str, obj: str) -> list[str]:
        queried.append(f"{obj}#{relation}")
        return []

    monkeypatch.setattr(fga_module, "list_users", fake_list_users)

    resp = client.post("/v1/table/db1$users/access/list", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_drop", "obj": "table:db1$users"}
    assert queried == []  # the enumeration never ran for a denied reviewer


def test_namespace_access_list_requires_owner_via_can_delete(client: TestClient, monkeypatch) -> None:
    _wire(client)
    captured: list[dict] = []
    monkeypatch.setattr(fga_module, "check", _fake_check(captured, allow=False))

    resp = client.post("/v1/namespace/silver/access/list", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert captured[-1] == {"user": "alice", "relation": "can_delete", "obj": "namespace:silver"}


def test_access_list_enumerates_grants_for_an_owner(client: TestClient, monkeypatch) -> None:
    """The allow leg: an owner's review asks ListUsers once per ``can_*`` relation on the type and
    returns the expanded subjects per relation."""
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))

    async def fake_list_users(_c: object, *, relation: str, obj: str) -> list[str]:
        del obj
        return ["alice", "bob"] if relation == "can_get_metadata" else ["alice"]

    monkeypatch.setattr(fga_module, "list_users", fake_list_users)

    resp = client.post("/v1/table/db1$users/access/list", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "table:db1$users"
    grants = {g["relation"]: g["users"] for g in body["grants"]}
    assert grants["can_get_metadata"] == ["alice", "bob"]
    assert grants["can_drop"] == ["alice"]
    # The enumerated set is exactly the model's can_* actions for the type (pinned in the
    # model-contract test; spot-checked here through the real route).
    assert set(grants) == set(_model_can_relations("table"))


def test_access_list_fails_closed_when_enumeration_hits_an_fga_outage(client: TestClient, monkeypatch) -> None:
    """CONTRACT (#51): an OpenFGA outage during the ListUsers fan-out is a 503 at the endpoint —
    never a partial or empty grant list that reads as "nobody has access"."""
    _wire(client)
    monkeypatch.setattr(fga_module, "check", _fake_check([], allow=True))

    async def down(_c: object, *, relation: str, obj: str) -> list[str]:
        raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(fga_module, "list_users", down)

    resp = client.post("/v1/table/db1$users/access/list", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503
