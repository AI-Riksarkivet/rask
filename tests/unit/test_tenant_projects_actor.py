"""The tenant-projects index actor — what makes `GET /projects` (A1's landing) answerable at all.

Project actors are one-per-id; nothing knew which ids a tenant holds. This actor is that index,
maintained the same way the project actor maintains its task index: registered synchronously at
create, idempotent, and repairable by re-registration.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from annotator.projects.tenant_actor import PROJECTS_KEY, TenantProjectsActor


class _FakeStateManager:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def save_state(self) -> None:
        return None


class _Actor(TenantProjectsActor):
    def __init__(self) -> None:  # noqa: D107 - deliberately bypasses Actor.__init__ (needs a runtime)
        self.sm = _FakeStateManager()
        self._state_manager = cast(Any, self.sm)


@pytest.mark.asyncio
async def test_register_is_idempotent_and_list_preserves_insertion_order() -> None:
    actor = _Actor()
    assert (await actor.register({"project_id": "p1"}))["created"] is True
    assert (await actor.register({"project_id": "p2"}))["created"] is True
    assert (await actor.register({"project_id": "p1"}))["created"] is False, "a retried create double-registered"

    listing = await actor.list_projects()
    assert listing["project_ids"] == ["p1", "p2"]
    assert listing["total"] == 2


@pytest.mark.asyncio
async def test_an_empty_tenant_lists_empty_rather_than_erroring() -> None:
    actor = _Actor()
    assert await actor.list_projects() == {"project_ids": [], "total": 0}
    assert PROJECTS_KEY not in actor.sm.store, "listing must not materialise state"
