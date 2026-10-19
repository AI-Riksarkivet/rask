"""Who may do what on an annotation project: the annotator's own roles, and the catalog's answer about the tenant.

The annotator holds no OpenFGA credential (ADR 0171). A project's roles live in its own actor's state, its creator
the first owner in the same turn as the document, and the members door changes them there. A person's standing in the
project's tenant is the catalog's, asked on the person's own token at the catalog's own door: a member views every
project of the tenant and an admin owns them, so a grant or a revoke at the tenant takes effect here with nothing to
synchronise. A question about a lakehouse object (a publish target, a corpus table) is the catalog's answer for that
person.

Driven through the real project actor (its state manager in memory), the real checker and the real members door, with
the catalog's doors served by respx.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.authorization import AnnotatorChecker, CatalogAnswers, ProjectAccess
from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import members
from annotator.projects.models import AnnotationProject
from annotator.projects.project_actor import AnnotationProjectActor
from service_kit.exceptions import ServiceUnavailableError, UnauthorizedError, register_handlers


CATALOG = "http://catalog.test"
TENANT = "acme"
CREATOR = "dex~alice"
BEARER = "the-persons-own-token"


class _StateManager:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def try_remove_state(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    async def save_state(self) -> None:
        return None


class _ProjectActor(AnnotationProjectActor):
    def __init__(self) -> None:  # noqa: D107 - bypasses Actor.__init__, which needs a runtime
        self._state_manager = cast(Any, _StateManager())


async def _created() -> _ProjectActor:
    actor = _ProjectActor()
    await actor.create(AnnotationProject(project_id="p1", tenant=TENANT, slug="charters", created_by=CREATOR).model_dump(mode="json"))
    return actor


def _checker(actor: _ProjectActor, subject: str, client: httpx.AsyncClient) -> AnnotatorChecker:
    async def project_access(_project_id: str, who: str) -> ProjectAccess:
        return ProjectAccess.model_validate(await actor.access({"subject": who}))

    return AnnotatorChecker(subject=subject, catalog=CatalogAnswers(client, base_url=CATALOG, bearer=BEARER), project_access=project_access)


def _tenant_door(router: respx.Router, answer: httpx.Response) -> respx.Route:
    return router.post(f"{CATALOG}/management/v1/projects/{TENANT}/access/my-permissions").mock(return_value=answer)


MEMBER = httpx.Response(200, json={"object": f"project:{TENANT}", "subject": "x", "permissions": {"can_administer": False}})
ADMIN = httpx.Response(200, json={"object": f"project:{TENANT}", "subject": "x", "permissions": {"can_administer": True}})
OUTSIDER = httpx.Response(403, json={"status": 403})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("who", "role", "tenant", "expected"),
    [
        pytest.param(CREATOR, None, OUTSIDER, {"can_view": True, "can_annotate": True, "can_manage": True}, id="the-creator-owns-what-they-made"),
        pytest.param("dex~bob", "annotator", OUTSIDER, {"can_view": True, "can_annotate": True, "can_manage": False}, id="a-direct-role-needs-no-tenant"),
        pytest.param("dex~carol", None, ADMIN, {"can_view": True, "can_annotate": True, "can_manage": True}, id="a-tenant-admin-owns-every-project"),
        pytest.param("dex~dave", None, MEMBER, {"can_view": True, "can_annotate": False, "can_manage": False}, id="a-tenant-member-views"),
        pytest.param("dex~erin", None, OUTSIDER, {"can_view": False, "can_annotate": False, "can_manage": False}, id="an-outsider-has-nothing"),
    ],
)
async def test_a_permission_is_the_projects_role_or_the_tenants_standing_the_catalog_answers(
    who: str, role: str | None, tenant: httpx.Response, expected: dict[str, bool]
) -> None:
    actor = await _created()
    if role is not None:
        await actor.grant_member({"user": who, "relation": role})
    # A role that already reaches the permission asks the catalog nothing, so the tenant's door may go unused.
    with respx.mock(assert_all_called=False) as router:
        door = _tenant_door(router, tenant)
        async with httpx.AsyncClient() as client:
            checker = _checker(actor, who, client)
            answered = {relation: await checker(user=who, relation=relation, obj="annotation_project:p1") for relation in expected}
    assert answered == expected
    for call in door.calls:
        assert call.request.headers["authorization"] == f"Bearer {BEARER}", "the catalog was asked on something other than the person's token"


def _members_app(actor: _ProjectActor, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(members, "_project", lambda _p: actor)

    async def allow(**_kw: Any) -> bool:
        return True

    app = FastAPI()
    app.include_router(members.router)
    register_handlers(app)
    app.dependency_overrides[get_checker] = lambda: allow
    app.dependency_overrides[current_subject] = lambda: CREATOR
    return TestClient(app)


@pytest.mark.asyncio
async def test_the_members_door_keeps_the_roles_in_the_projects_own_state_and_never_its_last_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = await _created()
    client = _members_app(actor, monkeypatch)

    assert client.put("/projects/p1/members", json={"user": "dex~bob", "relation": "manager"}).status_code == 200
    assert client.put("/projects/p1/members", json={"user": "dex~bob", "relation": "annotator"}).status_code == 200
    assert client.delete("/projects/p1/members", params={"user": "dex~bob", "relation": "annotator"}).status_code == 200
    assert client.delete("/projects/p1/members", params={"user": CREATOR, "relation": "owner"}).status_code == 200
    refused = client.delete("/projects/p1/members", params={"user": "dex~bob", "relation": "manager"})

    assert refused.status_code == 409, "the project's last owner or manager was revoked, leaving it administrable by nobody"
    listed = client.get("/projects/p1/members").json()["members"]
    assert listed == [{"user": "dex~bob", "relation": "manager"}], "revoking one role took another with it"
    stored = json.loads(cast(_StateManager, actor._state_manager).store["members"])
    assert stored == listed, "the door answered something other than what the project holds"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        pytest.param(httpx.Response(200, json={"object": "o", "subject": "s", "permissions": {"can_create_table": True}}), True, id="granted"),
        pytest.param(httpx.Response(200, json={"object": "o", "subject": "s", "permissions": {"can_create_table": False}}), False, id="not-granted"),
        pytest.param(httpx.Response(403, json={"status": 403}), False, id="not-even-readable"),
        pytest.param(httpx.Response(401, json={"status": 401}), UnauthorizedError, id="an-unknown-caller"),
        pytest.param(httpx.Response(502, json={"status": 502}), ServiceUnavailableError, id="the-catalog-failed"),
    ],
)
async def test_a_lakehouse_objects_permission_is_the_catalogs_answer_on_the_persons_token(answer: httpx.Response, outcome: bool | type[Exception]) -> None:
    actor = await _created()
    with respx.mock() as router:
        door = router.post(f"{CATALOG}/management/v1/namespace/acme-silver/access/my-permissions").mock(return_value=answer)
        async with httpx.AsyncClient() as client:
            ask: Callable[[], Any] = lambda: _checker(actor, CREATOR, client)(user=CREATOR, relation="can_create_table", obj="namespace:acme-silver")  # noqa: E731
            if isinstance(outcome, bool):
                assert await ask() is outcome
            else:
                with pytest.raises(outcome):
                    await ask()
    assert door.calls.last.request.headers["authorization"] == f"Bearer {BEARER}"


@pytest.mark.asyncio
async def test_the_checker_answers_only_for_the_caller_whose_token_it_carries() -> None:
    """The catalog answers for the bearer alone, so a question about anyone else is refused, never asked."""
    actor = await _created()
    with respx.mock() as router:
        async with httpx.AsyncClient() as client:
            assert await _checker(actor, "dex~bob", client)(user=CREATOR, relation="can_manage", obj="annotation_project:p1") is False
        assert not router.calls, "the catalog was asked about someone other than the caller"
