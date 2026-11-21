"""catalog-api-12 — the batch authorizer asks OpenFGA once per RELATION, never once per object.

``authorize_batch`` already batches its two other lanes (``can_write_data`` on the named tables,
``can_create_table`` on the parents) and then falls back to a ``for obj, relation in owner_checks``
loop of single ``fga_reads.check`` calls for the owner-tier operations. The comment called that lane "rare",
which is a statement about today's ``BATCH_OWNER_OPS`` (one entry) rather than about the request:
``operations`` is client-supplied and unbounded, so a body naming 200 ``deregister_table`` ops costs
200 sequential round trips on the AUTHORIZATION path — before any work is authorized, with the
gateway's timeout running.

Grouping by relation keeps the per-op relation exact (the reason the loop was written) and still costs
one round trip per DISTINCT relation.

Driven through the real coroutine with recording fakes, so a future rewrite that reintroduces a loop
is caught by the call COUNTS rather than by reading the code.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast, override

import pytest
from fastapi import Request
from openfga_sdk.client import OpenFgaClient

from catalog.api.authz import route_guard, rungs
from catalog.api.dependencies import Resolver
from catalog.core.config import Settings
from lakehouse.governed import fga_reads


_CLIENT = cast("OpenFgaClient", object())


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "oidc_idp_id": "dex",
            "oidc_connectors": {"local": "dex"},
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


class _EveryTableHolds(Resolver):
    """Every table name holds an object keyed by a uuid derived from the name; no store is read.

    The claim is how many round trips the batch costs, not how a name resolves.
    """

    def __init__(self) -> None:
        pass

    @override
    async def object(self, kind: str, segments: list[str]) -> str | None:
        return f"{kind}:{uuid.uuid5(uuid.NAMESPACE_URL, '$'.join(segments))}"


class _FakeRequest:
    """`authorize_batch` touches only ``await request.json()`` on the Request."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


def _owner_body(n: int) -> dict[str, Any]:
    op = next(iter(rungs.BATCH_OWNER_OPS))
    return {"operations": [{op: {"id": ["db1", f"t{i}"]}} for i in range(n)]}


def _drive(body: dict[str, Any]) -> list[tuple[str, int]]:
    """Run the real authorizer; return [(relation, batch size)]. Only ``batch_check`` is recorded: a single check would
    reach the bare-object client and fail the run."""
    batches: list[tuple[str, int]] = []

    async def rec_batch(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        batches.append((relation, len(objects)))
        return dict.fromkeys(objects, True)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(fga_reads, "batch_check", rec_batch)
        asyncio.run(route_guard.authorize_batch(cast("Request", _FakeRequest(body)), _CLIENT, _EveryTableHolds(), user="dex~alice"))
    finally:
        monkey.undo()
    return batches


def test_twenty_owner_tier_ops_cost_one_round_trip_not_twenty() -> None:
    batches = _drive(_owner_body(20))
    assert batches == [("can_deregister", 20)] or (len(batches) == 1 and batches[0][1] == 20), f"expected ONE batch of 20, got {batches}"


@pytest.mark.parametrize("owned", [pytest.param((), id="neither-owned"), pytest.param(("t0",), id="one-owned"), pytest.param(("t0", "t1"), id="both-owned")])
def test_a_denied_owner_object_is_still_refused_by_name(openfga_store: Any, owned: tuple[str, ...]) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """On the real model: an owner-tier batch is admitted only when alice owns every table it names, and the refusal
    names each table she does not own by the name the body gave, never by the uuid its grants are held by."""
    from lance_namespace import PermissionDeniedError

    from lakehouse.governed import fga_provision

    if owned:
        openfga_store.write(*(("user:dex~alice", "owner", f"table:{uuid.uuid5(uuid.NAMESPACE_URL, f'db1${name}')}") for name in owned))

    async def _authorized() -> None:
        async with fga_provision.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            await route_guard.authorize_batch(cast("Request", _FakeRequest(_owner_body(2))), client, _EveryTableHolds(), user="dex~alice")

    refused = {f"db1${name}" for name in ("t0", "t1") if name not in owned}
    if not refused:
        asyncio.run(_authorized())
        return
    with pytest.raises(PermissionDeniedError) as denied:
        asyncio.run(_authorized())
    assert {name for name in ("db1$t0", "db1$t1") if name in str(denied.value)} == refused, str(denied.value)
