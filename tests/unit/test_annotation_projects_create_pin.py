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
from annotator.api.v1.endpoints.projects import (
    CREATE_RELATION,
    get_checker,
    get_principal,
    router,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(*, allow: bool, record: list[dict[str, Any]] | None = None) -> FastAPI:
    """An app whose FGA checker answers `allow` and records what it was asked."""

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if record is not None:
            record.append({"user": user, "relation": relation, "obj": obj})
        return allow

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[get_principal] = lambda: "user:gina"
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
    assert body["created_by"] == "user:gina"
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
