"""catalog-api-12 — the batch authorizer asks OpenFGA once per RELATION, never once per object.

``_authorize_batch`` already batches its two other lanes (``can_write_data`` on the named tables,
``can_create_table`` on the parents) and then falls back to a ``for obj, relation in owner_checks``
loop of single ``fga.check`` calls for the owner-tier operations. The comment called that lane "rare",
which is a statement about today's ``_BATCH_OWNER_OPS`` (one entry) rather than about the request:
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
from typing import Any, cast

import pytest
from fastapi import Request
from openfga_sdk.client import OpenFgaClient

from catalog.api import fga_deps
from catalog.core.config import Settings


_CLIENT = cast("OpenFgaClient", object())


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


class _FakeRequest:
    """`_authorize_batch` touches only ``await request.json()`` on the Request."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


def _owner_body(n: int) -> dict[str, Any]:
    op = next(iter(fga_deps._BATCH_OWNER_OPS))
    return {"operations": [{op: {"id": ["db1", f"t{i}"]}} for i in range(n)]}


def _drive(body: dict[str, Any], *, allow: bool = True) -> tuple[list[str], list[tuple[str, int]]]:
    """Run the real authorizer; return (single-check objects, [(relation, batch size)])."""
    singles: list[str] = []
    batches: list[tuple[str, int]] = []

    async def rec_check(_client: object, *, user: str, relation: str, obj: str) -> bool:
        singles.append(obj)
        return allow

    async def rec_batch(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        batches.append((relation, len(objects)))
        return dict.fromkeys(objects, allow)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(fga_deps.fga, "check", rec_check)
        monkey.setattr(fga_deps.fga, "batch_check", rec_batch)
        asyncio.run(fga_deps._authorize_batch(cast("Request", _FakeRequest(body)), _CLIENT, _settings(), user="alice"))
    finally:
        monkey.undo()
    return singles, batches


def test_the_harness_reaches_the_owner_lane() -> None:
    """Guards the gate: if the body stopped routing to the owner lane, the count below proves nothing."""
    singles, batches = _drive(_owner_body(3))
    assert len(singles) + sum(n for _, n in batches) == 3, f"only {singles} / {batches} decisions — the owner lane was not reached"


def test_twenty_owner_tier_ops_cost_one_round_trip_not_twenty() -> None:
    singles, batches = _drive(_owner_body(20))
    assert not singles, f"{len(singles)} sequential single checks on the owner lane — batch them: {singles}"
    assert batches == [("can_deregister", 20)] or (len(batches) == 1 and batches[0][1] == 20), f"expected ONE batch of 20, got {batches}"


def test_a_denied_owner_object_is_still_refused_by_name() -> None:
    from lance_namespace import PermissionDeniedError

    with pytest.raises(PermissionDeniedError) as denied:
        _drive(_owner_body(2), allow=False)
    assert "db1$t0" in str(denied.value) and "db1$t1" in str(denied.value), str(denied.value)
