"""The tenant-projects index — one virtual actor per TENANT, listing its annotation projects.

Project actors are one-per-id, so without this nothing can answer "which projects does tenant X
hold?" — the question A1's landing page IS. Same shape as the project actor's task index: the
create endpoint registers synchronously after seeding the project actor, registration is
idempotent (a retried create must not double-list), and a failure fails the create loudly so an
unlisted-but-existing project cannot silently haunt the estate — `unregister` is how the create
takes its own entry back when a LATER write in that sequence fails. Order is insertion order; the
landing sorts by whatever the project documents say once they are fanned out.
"""

from __future__ import annotations

import json
from typing import Any

from dapr.actor import Actor, ActorInterface, actormethod


#: The single state key: a JSON list of project ids, insertion-ordered.
PROJECTS_KEY = "projects"


class TenantProjectsActorInterface(ActorInterface):
    """The wire surface — names are the sidecar's routing ids, fixed here."""

    @actormethod(name="Register")
    async def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="Unregister")
    async def unregister(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="ListProjects")
    async def list_projects(self) -> dict[str, Any]:
        raise NotImplementedError


class TenantProjectsActor(Actor, TenantProjectsActorInterface):
    """Holds one tenant's project-id index."""

    async def _load(self) -> list[str]:
        has, raw = await self._state_manager.try_get_state(PROJECTS_KEY)
        return list(json.loads(raw)) if has and raw else []

    async def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload["project_id"])
        ids = await self._load()
        if project_id in ids:
            return {"project_id": project_id, "created": False, "total": len(ids)}
        ids.append(project_id)
        await self._state_manager.set_state(PROJECTS_KEY, json.dumps(ids))
        await self._state_manager.save_state()
        return {"project_id": project_id, "created": True, "total": len(ids)}

    async def unregister(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Take an id back OUT of the index — the create saga's compensation, and nothing else.

        `POST /projects` writes three things in sequence and this undoes the second. It is
        idempotent for the reason every compensation must be: it runs on a path that has already
        failed once, and a compensation that can fail again buys nothing. Removing an id that is
        not there is a no-op, reported as `removed: false` rather than raised.
        """
        project_id = str(payload["project_id"])
        ids = await self._load()
        if project_id not in ids:
            return {"project_id": project_id, "removed": False, "total": len(ids)}
        ids = [i for i in ids if i != project_id]
        await self._state_manager.set_state(PROJECTS_KEY, json.dumps(ids))
        await self._state_manager.save_state()
        return {"project_id": project_id, "removed": True, "total": len(ids)}

    async def list_projects(self) -> dict[str, Any]:
        ids = await self._load()
        return {"project_ids": ids, "total": len(ids)}
