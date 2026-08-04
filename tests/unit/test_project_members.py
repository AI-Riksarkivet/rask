"""Membership on an annotation project — and the revoke that must be refused.

The rungs already existed in the model, concentric, directly assignable. What did not exist was any
way to grant one: a project's creator got `owner` at create and nobody else could be added, so every
collaborative project needed someone with direct store access. That is the shape where people end up
sharing an account.

The interesting test is not that a grant works. It is that removing the LAST administrative grant is
refused — because that is the one mistake nobody can undo from inside the product.
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker, get_fga_client
from annotator.api.v1.endpoints import members as members_ep
from annotator.api.v1.endpoints.members import router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers


OBJ = "annotation_project:p1"


class _Tuple:
    def __init__(self, user: str, relation: str, obj: str) -> None:
        self.user = user
        self.relation = relation
        self.object = obj

    def key(self) -> tuple[str, str, str]:
        return (self.user, self.relation, self.object)


class _FakeStore:
    """An in-memory tuple store with the ONE behaviour that shapes the endpoint: OpenFGA's Write is
    transactional and rejects a tuple that already exists."""

    def __init__(self, tuples: list[_Tuple]) -> None:
        self.tuples = tuples
        self.written: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str, str]] = []


def _app(store: _FakeStore, monkeypatch: pytest.MonkeyPatch, *, allow: bool = True, seen: list | None = None, fga_on: bool = True) -> FastAPI:
    async def read_object_tuples(_client: Any, obj: str, **_kw: Any) -> list[_Tuple]:
        return [t for t in store.tuples if t.object == obj]

    async def write_tuples(_client: Any, tuples: list[Any], **_kw: Any) -> None:
        for t in tuples:
            if any(x.key() == (t.user, t.relation, t.object) for x in store.tuples):
                raise AssertionError("write_tuples was called with a tuple that already exists")
            store.tuples.append(_Tuple(t.user, t.relation, t.object))
            store.written.append((t.user, t.relation, t.object))

    async def delete_tuples(_client: Any, tuples: list[Any], **_kw: Any) -> None:
        for t in tuples:
            store.tuples = [x for x in store.tuples if x.key() != (t.user, t.relation, t.object)]
            store.deleted.append((t.user, t.relation, t.object))

    monkeypatch.setattr(members_ep.fga, "read_object_tuples", read_object_tuples)
    monkeypatch.setattr(members_ep.fga, "write_tuples", write_tuples)
    monkeypatch.setattr(members_ep.fga, "delete_tuples", delete_tuples)

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if seen is not None:
            seen.append({"user": user, "relation": relation, "obj": obj})
        return allow

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: "gina"
    app.dependency_overrides[get_fga_client] = lambda: object() if fga_on else None
    return app


def _store(*rungs: tuple[str, str]) -> _FakeStore:
    return _FakeStore([_Tuple(u, r, OBJ) for u, r in rungs])


def test_the_last_administrative_grant_CANNOT_be_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mistake nobody can undo from inside the product.

    `owner` and `manager` are the rungs that can grant others. Removing the final one leaves the
    project administrable only by a tenant admin — which, on a tenant whose admin has moved on, is
    nobody. Refused HERE and named, rather than discovered later.
    """
    store = _store(("user:gina", "owner"), ("user:omar", "annotator"))
    client = TestClient(_app(store, monkeypatch))

    r = client.request("DELETE", "/projects/p1/members", json={"user": "gina", "relation": "owner"})

    assert r.status_code == 409, r.text
    assert "only remaining" in r.text
    assert store.deleted == [], "the last owner was revoked anyway"


def test_the_last_admin_can_be_revoked_ONCE_another_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is about the project keeping an administrator, not about protecting one person."""
    store = _store(("user:gina", "owner"), ("user:maria", "manager"))
    client = TestClient(_app(store, monkeypatch))

    r = client.request("DELETE", "/projects/p1/members", json={"user": "gina", "relation": "owner"})

    assert r.status_code == 200, r.text
    assert store.deleted == [("user:gina", "owner", OBJ)]


def test_a_non_administrative_rung_is_freely_revocable(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(("user:gina", "owner"), ("user:omar", "annotator"))
    client = TestClient(_app(store, monkeypatch))

    r = client.request("DELETE", "/projects/p1/members", json={"user": "omar", "relation": "annotator"})

    assert r.status_code == 200, r.text
    assert store.deleted == [("user:omar", "annotator", OBJ)]


def test_a_grant_is_IDEMPOTENT_because_OpenFGAs_write_is_transactional(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-granting what someone already has must be a no-op, not a 400.

    An OpenFGA Write rejects the ENTIRE batch if one tuple exists, so the read-before-write is not an
    optimisation — it is what makes this idempotent. The fake asserts the same rule.
    """
    store = _store(("user:gina", "owner"), ("user:omar", "annotator"))
    client = TestClient(_app(store, monkeypatch))

    r = client.put("/projects/p1/members", json={"user": "omar", "relation": "annotator"})

    assert r.status_code == 200, r.text
    assert store.written == [], "a duplicate grant reached the store"


def test_a_bare_subject_is_normalised_to_a_user_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asking a UI to know the `user:` prefix is asking it to know the authorization model."""
    store = _store(("user:gina", "owner"))
    client = TestClient(_app(store, monkeypatch))

    client.put("/projects/p1/members", json={"user": "maria", "relation": "reviewer"})

    assert store.written == [("user:maria", "reviewer", OBJ)]


def test_an_already_typed_user_string_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(("user:gina", "owner"))
    client = TestClient(_app(store, monkeypatch))

    client.put("/projects/p1/members", json={"user": "role:editors#assignee", "relation": "annotator"})

    assert store.written == [("role:editors#assignee", "annotator", OBJ)]


def test_the_TENANT_edge_is_never_listed_as_a_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is the parent edge, not a person. Listing it invites someone to revoke the thing that makes
    every rung on this object resolvable."""
    store = _store(("user:gina", "owner"), ("project:acme", "tenant"))
    client = TestClient(_app(store, monkeypatch))

    body = client.get("/projects/p1/members").json()

    assert [m["relation"] for m in body["members"]] == ["owner"]


def test_reading_the_membership_requires_can_manage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Who has access is itself sensitive — it names the people worth phishing."""
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(_store(("user:gina", "owner")), monkeypatch, seen=seen))

    client.get("/projects/p1/members")

    assert [c["relation"] for c in seen] == ["can_manage"]
    assert seen[0]["obj"] == OBJ


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/projects/p1/members", None),
        ("PUT", "/projects/p1/members", {"user": "x", "relation": "annotator"}),
        ("DELETE", "/projects/p1/members", {"user": "x", "relation": "annotator"}),
    ],
)
def test_every_route_is_403_without_can_manage(method: str, path: str, body: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(("user:gina", "owner"))
    client = TestClient(_app(store, monkeypatch, allow=False))

    r = client.request(method, path, json=body)

    assert r.status_code == 403, r.text
    assert store.written == [] and store.deleted == []


def test_revoking_something_absent_is_a_no_op_not_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller asked for a state, and that state already holds."""
    store = _store(("user:gina", "owner"))
    client = TestClient(_app(store, monkeypatch))

    r = client.request("DELETE", "/projects/p1/members", json={"user": "nobody", "relation": "annotator"})

    assert r.status_code == 200, r.text
    assert store.deleted == []


def test_an_unknown_rung_is_refused_by_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """`viewer` is deliberately not grantable: the model already gives it to every tenant member, so
    a per-project grant would create a tuple that changes nothing while implying it does."""
    client = TestClient(_app(_store(("user:gina", "owner")), monkeypatch))

    assert client.put("/projects/p1/members", json={"user": "x", "relation": "viewer"}).status_code == 422
    assert client.put("/projects/p1/members", json={"user": "x", "relation": "wizard"}).status_code == 422
