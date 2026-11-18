"""A fence reads nothing whose size grows with its object's use, and a delete drains whatever stands (ADR 0241 §8 B1, B2).

A door writing on or naming a project or warehouse opens a fence (``fga.fenced``): it writes a ticket naming the object,
reads the incarnation's token, and commits in one Write deleting the ticket (``fga.commit_fenced``). Every top-level
namespace stores a ``child`` edge on its warehouse and every member a tuple on its project, and a request that dies
leaves its ticket; a fence that read every tuple on its object grew with all of them, and refused 503 for good past
5,000 (measured 2026-11-18 on the pinned OpenFGA 1.18.3: a namespace create in a warehouse holding 5,100 child edges,
and a member grant and a delete on a project holding 5,100 left tickets). The fence reads the ``revision`` relation and
each tuple it changes by its key; tickets live off the object; the delete pages to the end and deletes tickets in
Writes of 100.

Driven against a real OpenFGA on a store of the test's own, through the functions every door writes with.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from openfga_sdk.client.models import ClientWriteRequest

from lakehouse.governed import fga


#: Past the ceiling the fence's read refused at: 100 pages of 50.
MANY = 5_100


async def _plant(client: Any, tuples: list[fga.ClientTuple]) -> None:  # noqa: ANN401 — the SDK's client
    for start in range(0, len(tuples), fga.TUPLES_PER_WRITE):
        await client.write(ClientWriteRequest(writes=tuples[start : start + fga.TUPLES_PER_WRITE]))


def _run(openfga_store: Any, scenario: Any) -> Any:  # noqa: ANN401 — the root conftest's OpenFgaStore and a coroutine function
    async def _go() -> Any:  # noqa: ANN401 — what the scenario answers
        async with fga.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            return await scenario(client)

    return asyncio.run(_go())


def test_a_namespace_is_made_in_a_warehouse_holding_more_namespaces_than_a_read_once_took(openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    warehouse = "warehouse:wh"

    async def _scenario(client: Any) -> bool:  # noqa: ANN401 — the SDK's client
        assert await fga.bear(client, warehouse, "w1", [], actor="test", origin="warehouse_create")
        await _plant(client, [fga.ClientTuple(user=fga.namespace_object(uuid.uuid4().hex), relation="child", object=warehouse) for _ in range(MANY)])
        made = fga.namespace_object(uuid.uuid4().hex)
        async with fga.fenced(client, warehouse, actor="test", origin="fence") as fence:
            assert fence is not None
            await fga.grant_on_create(client, user_sub="dex~alice", obj=made, actor="test", origin="create", parent_object=warehouse, fences=[fence])
        return await fga.holds(client, fga.ClientTuple(user=warehouse, relation="parent", object=made))

    assert _run(openfga_store, _scenario)


def test_tickets_requests_left_on_a_project_slow_no_grant_and_its_delete_drains_them(openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    project = "project:acme"

    async def _scenario(client: Any) -> tuple[bool, list[fga.ClientTuple], int]:  # noqa: ANN401 — the SDK's client
        assert await fga.bear(client, project, "p1", [], actor="test", origin="project_create")
        await _plant(client, [fga.ClientTuple(user=project, relation=fga.TICKET, object=f"{fga.FENCE_TYPE}:{uuid.uuid4().hex}") for _ in range(MANY)])
        async with fga.fenced(client, project, actor="test", origin="fence") as fence:
            assert fence is not None
            granted = await fga.commit_fenced(
                client, [fence], writes=[fga.ClientTuple(user="user:bob", relation="member", object=project)], actor="test", origin="grant_api"
            )
        revoked = await fga.end_incarnation(client, project, "p1", naming=("role", "warehouse"), actor="test", origin="lifecycle_delete")
        return granted, revoked, len(await fga.read_tickets(client, project))

    granted, revoked, left = _run(openfga_store, _scenario)

    assert (granted, [str(t.user) for t in revoked], left) == (True, ["user:bob"], 0)
