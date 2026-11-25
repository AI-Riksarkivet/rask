"""A read of the store's tuples that reaches its page ceiling with a cursor outstanding is refused, never answered short.

Every reader that pages to the end hands its caller "all of them": a revoke deletes exactly what it was handed, a
converge takes back what it read as undeclared, and a census decides which objects hold no grant. A short answer is
indistinguishable from a whole one, so the ceiling (which exists so a cursor that never ends cannot spin a request) ends
the read as an outage. Driven on a real OpenFGA with the page and the ceiling set small.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from lance_namespace.errors import ServiceUnavailableError

from lakehouse.governed import fga_ids, fga_provision, fga_reads, fga_writes


TABLE = "table:7c1e0d52-1f0b-4d8e-9a57-2b1d5a3e4c10"
READERS = [fga_ids.user_object(f"dex~reader-{n}") for n in range(3)]
#: Three readers of one table, the first of whom reads two more: every whole read here spans three pages of one tuple.
ELSEWHERE = {(READERS[0], "reader", f"table:7c1e0d52-1f0b-4d8e-9a57-2b1d5a3e4c1{n}") for n in (1, 2)}
GRANTS = {(reader, "reader", TABLE) for reader in READERS} | ELSEWHERE


async def _the_objects_tuples(client: Any) -> object:
    return await fga_reads.read_object_tuples(client, TABLE)


async def _a_subjects_tuples(client: Any) -> object:
    return await fga_reads.read_subject_tuples(client, READERS[0], "table")


async def _the_undeclared_grants_revoked(client: Any) -> object:
    return await fga_writes.revoke_undeclared(client, declared=[], obj=TABLE, relation="reader", own=READERS, actor="dex~alice", origin="grant_api")


async def _the_census(client: Any) -> object:
    return await fga_reads.census(client)


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(_the_objects_tuples, id="an-objects-tuples"),
        pytest.param(_the_undeclared_grants_revoked, id="a-converge-of-undeclared-grants"),
        pytest.param(_the_census, id="the-census"),
        pytest.param(_a_subjects_tuples, id="a-subjects-tuples"),
    ],
)
def test_a_read_that_reaches_its_ceiling_is_an_outage_and_changes_nothing(
    openfga_store: Any, monkeypatch: pytest.MonkeyPatch, read: Callable[[Any], Awaitable[object]]
) -> None:
    openfga_store.write(*GRANTS)
    monkeypatch.setattr(fga_reads, "READ_PAGE_SIZE", 1)
    monkeypatch.setattr(fga_reads, "READ_MAX_PAGES", 2)

    async def asked() -> object:
        async with fga_provision.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            return await read(client)

    with pytest.raises(ServiceUnavailableError, match="refusing to answer with a partial set"):
        asyncio.run(asked())

    assert openfga_store.tuples() == GRANTS


def test_a_read_that_ends_on_its_last_admitted_page_answers_every_tuple(openfga_store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    openfga_store.write(*GRANTS)
    monkeypatch.setattr(fga_reads, "READ_PAGE_SIZE", 1)
    monkeypatch.setattr(fga_reads, "READ_MAX_PAGES", 3)

    async def asked() -> list[fga_writes.ClientTuple]:
        async with fga_provision.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            return await fga_reads.read_object_tuples(client, TABLE)

    assert {(held.user, held.relation, held.object) for held in asyncio.run(asked())} == GRANTS - ELSEWHERE
