"""Deletion protection has to cover the doors that destroy PART of a table, not only the one that
destroys all of it.

[[LH-056]]. `protection` exists to stand "between a mistyped id and an irreversible purge", and
`tables.py` consults it on drop, deregister and rename. Three doors that clear the SAME `can_drop`
rung consult nothing:

* `branches/delete` — the FGA map's own comment calls it "irreversible, and the heavier of the two
  deletions this table offers … destroys its data AND its own version sequence";
* `tags/delete` — removes the pin `published` is, which is what the publication door's rollback guard
  rests on;
* `version/delete` — deletes version bytes outright, with nothing behind them.

So an operator who armed protection on a table can still have any `can_drop` holder take a branch, a
tag or a version out of it, one at a time, while the whole-table drop refuses. Protection that a caller
can walk around by deleting the parts is not protection.

THE RECORD IS THE TABLE'S, and no new protectable object is invented. The 2026-09-21 ruling recorded
that lance-ns defines no branch resource — three TABLE-scoped operations and a `branch` FIELD — so a
per-branch protection record would mint a rung the spec does not have, and would leave open which of
the two answers wins when they disagree. One record, one answer: a protected table refuses every door
that destroys part of it.

`force` RIDES THE QUERY STRING, exactly as on drop, deregister and rename, and turns the protection
lock ONLY. The FGA gate ran before any of these handlers and runs identically either way.

DRIVEN THROUGH THE ROUTES against a real `dir` namespace and real pylance refs. A unit test on
`require_not_protected` already exists and passes today — it is the DOOR not calling it that is the
defect, which is the shape "a gate on the innermost call proves nothing".
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, connect

from catalog.api.dependencies import ControlEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import branches as branch_door
from catalog.api.v1.endpoints import tags as tag_door
from catalog.api.v1.endpoints import versions as version_door
from catalog.core.identifiers import with_minted_uuid
from catalog.services import table_claims
from catalog.services.dataplane import create_table
from service_kit.governed.oidc import IDToken
from service_kit.governed.person_identity import PersonPrincipal
from service_kit.governed.principal import Principal
from service_kit.lakehouse import protection
from service_kit.lakehouse.base_registry import BaseRegistry
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

#: A person the dev Dex's password connector reports: `sub` is Dex's packing of its user id with the connector id,
#: and the principal is the user id under the idp id `local` maps to.
_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
_CALLER = PersonPrincipal(
    principal=Principal(idp_id="dex", subject="08a8684b-db88-4b73-90a9-3cd1661f5466"),
    claims=IDToken(iss="https://dex", sub=_SUB, aud="lance", exp=1, iat=1),
)
#: In a namespace, because a table the catalog makes lives in one (ADR 0173 §5).
TABLE = ["db", "alpha"]
TABLE_PATH = "db$alpha"
#: `canonical_object_id` joins the parsed segments with the delimiter, so the table's canonical id is
#: its path parameter. Spelled out rather than imported so a change to either side shows up
#: here as a failure instead of agreeing with itself.
CANONICAL = "db$alpha"


def _table(start: int = 1) -> pa.Table:
    return pa.table({"id": pa.array([start, start + 1, start + 2], pa.int64())})


@pytest.fixture
def registry_root(tmp_path: Path) -> str:
    return str(tmp_path / "control")


@pytest.fixture
def ns(tmp_path: Path, registry_root: str):  # noqa: ANN201 — LanceNamespace, a runtime-only type
    root = str(tmp_path / "data")
    namespace = connect("dir", {"root": root})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE[:-1], properties=with_minted_uuid(None)))
    registry = BaseRegistry(control_root=registry_root, storage_options={})
    created = create_table(namespace, {}, TABLE, _table(), root=root, mode="create", registry=registry, holder=CANONICAL)
    # TWO MORE VERSIONS, so `version/delete` has one it may actually remove: version 1 carries the
    # `release` tag `_seed_refs` makes and the current version is never deletable, which leaves version
    # 2. Written straight to the dataset because the create door DECLARES a table and a second declare
    # is a conflict, not an append. Without them that door refuses for its own reasons and the control
    # leg could not tell that from a protection refusal.
    for start in (10, 20):
        lance.write_dataset(pa.table({"id": pa.array([start, start + 1, start + 2], pa.int64())}), str(created.location), mode="append")
    return namespace


@pytest.fixture
def table_uuid(ns, registry_root: str) -> str:  # noqa: ANN001
    """The uuid the table's claim records, which its protection record is kept under (ADR 0173 §8)."""
    return table_claims.resolve(ns, ClaimStore(control_root=registry_root), TABLE).require_uuid()


@pytest.fixture
def app(ns, registry_root: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:  # noqa: ANN001
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    for module in (branch_door, tag_door, version_door):
        application.include_router(module.router)

    settings = SimpleNamespace(
        storage_options=lambda: {},
        fga_enabled=False,
        registry_root=registry_root,
        external_blob_base_list=[],
        # [[LH-209]] What the judge holds a configured base against: governed roots and buckets. None here.
        root="",
        models_root="",
        model_artifacts_root="",
        reserved_bucket_set=frozenset(),
        dapr_secret_store="",
    )
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: ns
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: _CALLER
    application.dependency_overrides[ControlEmitterDep.__metadata__[0].dependency] = lambda: None

    # The control emit is a different subject with its own suite (`test_the_ref_plane_announces_itself`).
    # Stubbed so a refusal here is about protection and not about a door reaching for an emitter the
    # override deliberately supplies as None.
    async def _swallow(_emitter: Any, **_kwargs: Any) -> None:
        return None

    for module in (branch_door, tag_door):
        monkeypatch.setattr(module, "emit_control", _swallow, raising=False)
    yield application


def _protect(registry_root: str, table_uuid: str) -> None:
    protection.set_protection(registry_root, {}, {"kind": "table", "id": CANONICAL, "uuid": table_uuid, "protected": "true"})


def _seed_refs(client: TestClient) -> None:
    """A branch and a tag to delete. Asserted, because a door with nothing to destroy refuses for the
    wrong reason and every leg below would read as a pass."""
    assert client.post(f"/v1/table/{TABLE_PATH}/branches/create", json={"id": TABLE, "name": "staging"}).status_code == 200
    assert client.post(f"/v1/table/{TABLE_PATH}/tags/create", json={"id": TABLE, "tag": "release", "version": 1}).status_code == 200


#: `(label, path, body)` for each door that destroys part of a table. The version range is
#: START-INCLUSIVE, END-EXCLUSIVE (`VersionRange`'s own description), so `[2, 3)` is version 2 alone —
#: the one version neither tagged nor current.
DESTRUCTIVE = [
    ("branch", "branches/delete", {"id": TABLE, "name": "staging"}),
    ("tag", "tags/delete", {"id": TABLE, "tag": "release"}),
    ("version", "version/delete", {"id": TABLE, "ranges": [{"start_version": 2, "end_version": 3}]}),
]


@pytest.mark.parametrize(("label", "path", "body"), DESTRUCTIVE, ids=[d[0] for d in DESTRUCTIVE])
def test_the_door_reaches_its_target_when_the_table_is_UNPROTECTED(app: FastAPI, label: str, path: str, body: dict[str, Any]) -> None:
    """The control, and it has to come first: every refusal below would also be produced by a door that
    cannot find its branch, its tag or its version, and that failure looks identical from outside."""
    with TestClient(app) as client:
        _seed_refs(client)
        answer = client.post(f"/v1/table/{TABLE_PATH}/{path}", json=body)

    assert answer.status_code == 200, f"the {label} door did not reach its target, so the protection legs prove nothing: {answer.text}"
    if label == "version":
        # A 200 ALONE IS NOT THE CONTROL here: a range naming only absent versions answers
        # `deleted_count: 0` and 200, so the protection leg below would be comparing against a door that
        # destroys nothing.
        assert answer.json().get("deleted_count", 0) >= 1, f"the version door deleted nothing, so its protection leg proves nothing: {answer.text}"


@pytest.mark.parametrize(("label", "path", "body"), DESTRUCTIVE, ids=[d[0] for d in DESTRUCTIVE])
def test_a_PROTECTED_table_refuses_the_door(app: FastAPI, registry_root: str, table_uuid: str, label: str, path: str, body: dict[str, Any]) -> None:
    """The defect. A `can_drop` holder may not take a table apart one ref at a time while the
    whole-table drop refuses."""
    with TestClient(app) as client:
        _seed_refs(client)
        _protect(registry_root, table_uuid)
        answer = client.post(f"/v1/table/{TABLE_PATH}/{path}", json=body)

    assert answer.status_code == 409, f"a protected table let its {label} be destroyed: {answer.status_code} {answer.text}"
    problem = answer.json()
    assert problem.get("code") == 19, f"the refusal does not carry the spec's table-state code, so a generated client dispatches on the wrong one: {problem}"
    assert "force=true" in problem.get("detail", ""), f"the refusal does not say how to proceed: {problem}"


@pytest.mark.parametrize(("label", "path", "body"), DESTRUCTIVE, ids=[d[0] for d in DESTRUCTIVE])
def test_FORCE_turns_the_protection_lock_and_only_that_one(
    app: FastAPI, registry_root: str, table_uuid: str, label: str, path: str, body: dict[str, Any]
) -> None:
    """Same override the sibling doors carry. Without it protection would be a flag no caller can get
    past without editing a record by hand, which is not how the other three destructive doors behave."""
    with TestClient(app) as client:
        _seed_refs(client)
        _protect(registry_root, table_uuid)
        answer = client.post(f"/v1/table/{TABLE_PATH}/{path}?force=true", json=body)

    assert answer.status_code == 200, f"force did not release the {label} door: {answer.status_code} {answer.text}"
    if label == "version":
        assert answer.json().get("deleted_count", 0) >= 1, f"force returned 200 without deleting anything: {answer.text}"
