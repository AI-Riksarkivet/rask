"""S4 — the create pin: creating an annotation project is authorized on the PARENT tenant.

Proves the door at the HTTP tier, complementing the model-tier proof in
`packages/service-kit/src/service_kit/governed/auth/model.fga.yaml` ("creating an annotation project
is authorized on the PARENT tenant, not the child"). Both are needed: the model says the ladder is
right, this says the endpoint actually consults it.

The FGA checker is injected, so this needs no OpenFGA, no store and no network.
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker, get_fga_client
from annotator.api.v1.endpoints import projects as projects_ep
from annotator.api.v1.endpoints.projects import CREATE_RELATION, router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers


class _FakeProjectActor:
    """The project actor. Records what was persisted so a create that returns 201 without writing
    anything — which is what this endpoint did until 2026-07-28 — cannot pass again."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.created.append(payload)
        return payload


@pytest.fixture(autouse=True)
def _actor(monkeypatch: pytest.MonkeyPatch) -> _FakeProjectActor:
    actor = _FakeProjectActor()
    monkeypatch.setattr(projects_ep, "_create_actor", lambda _p: actor)
    return actor


def _app(*, allow: bool, record: list[dict[str, Any]] | None = None) -> FastAPI:
    """An app whose FGA checker answers `allow` and records what it was asked."""

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if record is not None:
            record.append({"user": user, "relation": relation, "obj": obj})
        return allow

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)  # ForbiddenError -> problem+json 403, as in the real service
    app.dependency_overrides[get_checker] = lambda: checker
    # The VERIFIED subject. Overriding current_subject (not a header) is the point: with OIDC on
    # there is no header path to this value at all.
    app.dependency_overrides[current_subject] = lambda: "gina"
    # FGA off by default here: seeding is exercised by its own test below.
    app.dependency_overrides[get_fga_client] = lambda: None
    return app


PAYLOAD = {"tenant": "acme", "slug": "labels-2026", "title": "Labels 2026"}


def test_a_tenant_member_creates_the_project_201() -> None:
    client = TestClient(_app(allow=True))
    r = client.post("/projects", json=PAYLOAD)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tenant"] == "acme"
    assert body["slug"] == "labels-2026"
    # Born in draft: `open` is a separate can_manage transition, so creating never implies ready.
    assert body["state"] == "draft"
    assert body["created_by"] == "gina"
    assert body["project_id"]


def test_a_non_member_is_denied_403() -> None:
    client = TestClient(_app(allow=False))
    r = client.post("/projects", json=PAYLOAD)
    assert r.status_code == 403, r.text
    assert CREATE_RELATION in r.json()["detail"]


def test_the_check_targets_the_PARENT_tenant_not_the_child() -> None:
    """The whole point of S4. The child has no id yet, so the object must be `project:<tenant>`."""
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, record=seen))
    client.post("/projects", json=PAYLOAD)

    assert len(seen) == 1
    assert seen[0]["obj"] == "project:acme"
    assert seen[0]["relation"] == CREATE_RELATION
    assert not seen[0]["obj"].startswith("annotation_project:"), "checked the child, which has no tuples at creation time — see design-create-on-parent"


def test_creation_fails_closed_when_the_checker_says_nothing_useful() -> None:
    """A falsy answer denies. Authorization never fails open."""
    client = TestClient(_app(allow=False))
    assert client.post("/projects", json=PAYLOAD).status_code == 403


@pytest.mark.parametrize("missing", ["tenant", "slug"])
def test_tenant_and_slug_are_required_so_the_door_can_never_be_inferred(missing: str) -> None:
    """`tenant` IS the authz parent — a request that omits it must be rejected, never guessed."""
    payload = {k: v for k, v in PAYLOAD.items() if k != missing}
    client = TestClient(_app(allow=True))
    assert client.post("/projects", json=payload).status_code == 422


# --------------------------------------------------------------------------------------------------
# Create must PERSIST and must SEED — 201 is a claim about both
# --------------------------------------------------------------------------------------------------


def test_create_persists_the_project(_actor: Any) -> None:
    """The defect this replaces: the endpoint built the model, audited, and returned it. So the one
    entry point into the whole plane was a no-op reporting 201, and the very next call —
    `POST /projects/<id>/items` — answered 409 "annotation project does not exist"."""
    client = TestClient(_app(allow=True))

    r = client.post("/projects", json=PAYLOAD)

    assert r.status_code == 201
    assert _actor.created, "create returned 201 without persisting anything"
    assert _actor.created[0]["project_id"] == r.json()["project_id"]
    assert _actor.created[0]["state"] == "draft"


def test_create_seeds_owner_and_the_TENANT_edge(_actor: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both tuples are load-bearing. Without `owner` the creator cannot manage what they made;
    without the `tenant` edge, `owner: … or admin from tenant` and `viewer: … or member from tenant`
    never resolve, so every rung on the object — including publish door 1 — denies for everyone,
    permanently, creator included.

    The relation is `tenant`, NOT `parent`. `annotation_project` spells its parent edge differently
    from every governed type, and writing `parent` produces a tuple OpenFGA accepts and no rule
    reads — a silent no-op that looks like a successful seed.
    """
    seeded: list[dict[str, Any]] = []

    async def _grant(client: Any, **kw: Any) -> None:
        seeded.append(kw)

    monkeypatch.setattr(projects_ep.fga, "grant_on_create", _grant)
    app = _app(allow=True)
    app.dependency_overrides[get_fga_client] = lambda: object()  # FGA "enabled"

    r = TestClient(app).post("/projects", json=PAYLOAD)

    assert r.status_code == 201, r.text
    assert len(seeded) == 1, "the project was created without seeding ownership"
    grant = seeded[0]
    assert grant["user_sub"] == "gina", "ownership was seeded for someone other than the creator"
    assert grant["resource"] == "annotation_project"
    assert grant["obj_id"] == r.json()["project_id"]
    assert grant["parent_object"] == "project:acme"
    assert grant["parent_relation"] == "tenant", "the parent edge was written under the wrong relation"


def test_a_denied_create_seeds_nothing(monkeypatch: pytest.MonkeyPatch, _actor: Any) -> None:
    seeded: list[dict[str, Any]] = []

    async def _grant(client: Any, **kw: Any) -> None:
        seeded.append(kw)

    monkeypatch.setattr(projects_ep.fga, "grant_on_create", _grant)
    app = _app(allow=False)
    app.dependency_overrides[get_fga_client] = lambda: object()

    assert TestClient(app).post("/projects", json=PAYLOAD).status_code == 403
    assert seeded == [] and _actor.created == [], "a denied create still wrote"
