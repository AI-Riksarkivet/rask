"""Who may do what in the annotator: its own project roles, and the lakehouse's answers about a person (ADR 0171).

The annotator holds no OpenFGA credential. It decides its own objects and asks the lakehouse about the lakehouse's:

* **A project's roles are the annotator's** (`annotator.projects.roles`), kept in each project's own actor state
  (`AnnotationProjectActor.access`).
* **A tenant's standing is the lakehouse's.** A member of the project's tenant views it and an admin of the tenant
  owns it; the annotator learns both from the catalog's own door on the person's own token
  (`POST /management/v1/projects/{tenant}/access/my-permissions`: a 200 is a member, `can_administer` an admin), so a
  grant or a revoke at the tenant takes effect here with nothing to synchronise.
* **Data is the lakehouse's decision, made on the person's token.** A namespace or table relation (`can_create_table`
  for a publish target, `can_write_data` for assist over a corpus table) is the catalog's own answer for that person
  (`.../access/my-permissions` on the object).

The checker keeps the shape every route already calls, ``checker(user=, relation=, obj=)``, so the op→privilege map
stays on the state machines' edges. It answers only for the caller whose bearer it carries: the catalog answers for
the bearer, so a question about anyone else is refused rather than answered about the wrong person.

With authentication off (a local stack) every check is allowed, as the estate's checker allows everything with FGA off.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from annotator.audit_actions import AnnotatorAuditAction
from annotator.projects.roles import PERMISSIONS, Rung, holds
from service_kit.exceptions import ServiceUnavailableError, UnauthorizedError
from service_kit.governed.audit import AuditOutcome, audit


#: The tenant relations the annotator asks about. `member` is answered by the door admitting the caller at all.
TENANT_RELATIONS: Final[frozenset[str]] = frozenset({"member", "can_administer"})

#: The lakehouse objects whose relations the catalog answers for the caller, by the object type's door prefix.
CATALOG_OBJECTS: Final[dict[str, str]] = {"namespace": "/management/v1/namespace", "table": "/management/v1/table"}

#: How long one question to the catalog may take before the check fails closed.
CATALOG_TIMEOUT_SECONDS: Final = 10.0


class ProjectAccess(BaseModel):
    """What one project knows about one person: the tenant it belongs to and the role it granted them, if any."""

    exists: bool
    tenant: str | None = None
    rung: Rung | None = None


class MyPermissions(BaseModel):
    """The catalog's `my-permissions` answer, as far as the annotator reads it: each `can_*` the caller holds."""

    permissions: dict[str, bool]


class TenantStanding(BaseModel):
    """The person's standing in one tenant, as the catalog answered it."""

    member: bool
    admin: bool


class CatalogAnswers:
    """The catalog's answers about the person whose bearer this carries, asked through its public doors."""

    def __init__(self, client: httpx.AsyncClient, *, base_url: str, bearer: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {bearer}"}

    async def _permissions(self, path: str) -> dict[str, bool] | None:
        """The caller's `can_*` answers on one object, or None when the door refuses to tell them anything."""
        try:
            answer = await self._client.post(f"{self._base}{path}", headers=self._headers, timeout=CATALOG_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(f"the catalog did not answer {path}: {exc}") from exc
        if answer.status_code == 401:
            raise UnauthorizedError("the catalog refused the caller's credential")
        if answer.status_code in (403, 404):
            return None
        if answer.status_code != 200:
            raise ServiceUnavailableError(f"the catalog answered {path} with {answer.status_code}")
        try:
            return MyPermissions.model_validate_json(answer.content).permissions
        except ValidationError as exc:
            raise ServiceUnavailableError(f"the catalog answered {path} with no permissions to read: {exc}") from exc

    async def tenant(self, tenant: str) -> TenantStanding:
        """A 200 is a member: the door admits only a member. `can_administer` is an admin."""
        permissions = await self._permissions(f"/management/v1/projects/{quote(tenant, safe='')}/access/my-permissions")
        if permissions is None:
            return TenantStanding(member=False, admin=False)
        return TenantStanding(member=True, admin=permissions.get("can_administer", False))

    async def holds(self, object_type: str, object_id: str, relation: str) -> bool:
        """Whether the caller holds ``relation`` on one catalog object; refused or unknown is no."""
        permissions = await self._permissions(f"{CATALOG_OBJECTS[object_type]}/{quote(object_id, safe='')}/access/my-permissions")
        return bool(permissions and permissions.get(relation, False))


type ProjectAccessFor = Callable[[str, str], Awaitable[ProjectAccess]]


class AnnotatorChecker:
    """Answer one ``(user, relation, obj)`` question for the request's caller, the shape every route calls.

    One per request: it remembers each tenant's standing and each project's access for the request, so a route
    that asks twice asks the catalog and the actor once.
    """

    def __init__(self, *, subject: str, catalog: CatalogAnswers, project_access: ProjectAccessFor) -> None:
        self._subject = subject
        self._catalog = catalog
        self._project_access = project_access
        self._tenants: dict[str, TenantStanding] = {}
        self._projects: dict[str, ProjectAccess] = {}

    async def __call__(self, *, user: str, relation: str, obj: str) -> bool:
        if user != self._subject:
            # The catalog answers for the bearer alone, so a question about anyone else would be answered about the
            # wrong person. No route asks one; refusing keeps a future one from being granted by mistake.
            audit(
                AnnotatorAuditAction.ANNOTATOR_AUTHORIZE, AuditOutcome.FAILURE, subject=self._subject, resource=obj, relation=relation, reason="not_the_caller"
            )
            return False
        object_type, _, object_id = obj.partition(":")
        if not object_id:
            return False
        match object_type:
            case "annotation_project" if relation in PERMISSIONS:
                return await self._project(object_id, PERMISSIONS[relation])
            case "project" if relation in TENANT_RELATIONS:
                standing = await self._tenant(object_id)
                return standing.admin if relation == "can_administer" else standing.member
            case "namespace" | "table":
                return await self._catalog.holds(object_type, object_id, relation)
            case _:
                return False

    async def _tenant(self, tenant: str) -> TenantStanding:
        if tenant not in self._tenants:
            self._tenants[tenant] = await self._catalog.tenant(tenant)
        return self._tenants[tenant]

    async def _project(self, project_id: str, needed: str) -> bool:
        """The project's own role first; the tenant is asked only when that role does not reach ``needed``."""
        if project_id not in self._projects:
            self._projects[project_id] = await self._project_access(project_id, self._subject)
        access = self._projects[project_id]
        if not access.exists:
            return False
        if holds(access.rung, needed):
            return True
        if not access.tenant:
            return False
        standing = await self._tenant(access.tenant)
        return holds("owner" if standing.admin else "viewer" if standing.member else None, needed)


async def _allow(*, user: str, relation: str, obj: str) -> bool:
    """Authentication off: every check is allowed, as the estate's checker allows everything with FGA off."""
    del user, relation, obj
    return True


ALLOW_ALL: Final = _allow
