"""A revoke is answered by the next read even while OpenFGA caches, because every read asks for `HIGHER_CONSISTENCY`.

OpenFGA's check, iterator and list-objects caches are the platform's to turn on for scale, and the root conftest's
OpenFGA runs with all three on for an hour ([[LH-488]]). Measured on OpenFGA v1.18.3: once a check and a ListObjects
have read a role's grant on a table, deleting that grant leaves both answering from their caches (an allow, and the
table still listed), whatever clock the check carries. Only a read that asks for `HIGHER_CONSISTENCY` sees the delete.
"""

from __future__ import annotations

import asyncio
from typing import Any

from service_kit.governed import fga


def test_a_revoked_role_grant_is_denied_and_unlisted_by_the_next_reads(openfga_store: Any) -> None:
    bob, table = fga.user_object("dex~bob"), "table:7c1e0d52-1f0b-4d8e-9a57-2b1d5a3e4c10"
    grant = fga.ClientTuple(user="role:analyst#assignee", relation="reader", object=table)
    openfga_store.write((bob, "assignee", "role:analyst"), (grant.user, grant.relation, grant.object))

    async def reads(client: Any) -> tuple[bool, list[str]]:
        allowed = await fga.check(client, user="dex~bob", relation="reader", obj=table)
        listed = await fga.list_objects(client, user="dex~bob", relation="reader", object_type="table")
        return allowed, listed.objects

    async def revoke_between_reads() -> tuple[tuple[bool, list[str]], tuple[bool, list[str]]]:
        async with fga.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            before = await reads(client)
            await fga.delete_tuples(client, [grant], actor="dex~alice", origin="grant_api")
            return before, await reads(client)

    before, after = asyncio.run(revoke_between_reads())

    assert before == (True, [table])
    assert after == (False, []), f"bob keeps the role grant revoked on {table}: {after}"
