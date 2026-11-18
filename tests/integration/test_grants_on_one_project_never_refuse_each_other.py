"""Grants on one project never refuse each other, and none decided before its delete outlives it (ADR 0241).

Each grant on a project or warehouse is fenced by the incarnation it read before its caller's authority was asked: it
writes a ticket of its own, reads the incarnation's token, and commits in one Write that deletes its ticket
(``fga.open_fence``, ``fga.commit_fenced``). Writers of different tuples share no tuple, so none is refused by another.
The delete retires the token and drains (``fga.end_incarnation``): it deletes every ticket it reads and revokes what it
read, and reads again until a read finds nothing, so a grant committing between its read and its deletion of that
grant's ticket is revoked too, and none lands once it answered.

Before this, every grant moved the one token, three attempts with no backoff: measured on the pinned OpenFGA 1.18.3
(memory datastore) 2026-11-17, 9 to 11 of 16 concurrent grants for distinct people answered 409, and 56 to 58 of 64.

Driven against a real OpenFGA on a store of the test's own, through the functions every door writes with.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from lakehouse.governed import fga


PROJECT = "project:acme"


async def _grant(client: Any, person: int) -> bool | str:  # noqa: ANN401 — the SDK's client
    """One door's grant of ``member`` to person ``person``: True written, or why not."""
    fence = await fga.open_fence(client, PROJECT, actor="test", origin="fence")
    if fence is None:
        return "no incarnation stands"
    try:
        return await fga.commit_fenced(
            client, [fence], writes=[fga.ClientTuple(user=f"user:p{person}", relation="member", object=PROJECT)], actor="test", origin="grant_api"
        )
    except fga.IncarnationEnded:
        return "deleted while it ran"


@pytest.mark.parametrize(
    ("grants", "deleted"),
    [pytest.param(16, False, id="16-grants"), pytest.param(64, False, id="64-grants"), pytest.param(64, True, id="64-grants-beside-the-delete")],
)
def test_grants_on_one_project_never_refuse_each_other_and_none_outlives_its_delete(openfga_store: Any, grants: int, deleted: bool) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    async def _run() -> tuple[list[Any], list[fga.ClientTuple]]:
        async with fga.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            assert await fga.bear(client, PROJECT, "i1", [], actor="test", origin="project_create")
            doors: list[Coroutine[Any, Any, Any]] = [_grant(client, person) for person in range(grants)]
            if deleted:
                doors.append(fga.end_incarnation(client, PROJECT, "i1", naming=("role", "warehouse"), actor="test", origin="lifecycle_delete"))
            answered = await asyncio.gather(*doors)
            standing = [t for t in await fga.read_object_tuples(client, PROJECT) if t.relation not in fga.BOOKKEEPING_RELATIONS]
            return list(answered), standing

    answered, standing = asyncio.run(_run())

    if not deleted:
        assert (answered, len(standing)) == ([True] * grants, grants)
    else:
        assert isinstance(answered[-1], list), f"the delete answered {answered[-1]}"
        assert not [a for a in answered[:-1] if a not in (True, "no incarnation stands", "deleted while it ran")]
        assert standing == [], f"{len(standing)} grants outlived the delete"
