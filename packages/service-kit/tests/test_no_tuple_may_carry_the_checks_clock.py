"""No tuple OpenFGA will evaluate may carry the check's clock, whichever door it came through.

OpenFGA evaluates a condition parameter stored on the tuple over the one a check sends (pinned in
`auth/model.fga.yaml`), so a `current_time` on a `non_expired_grant` tuple freezes "now" inside the
window and the grant never lapses. `write_tuples` and a check's `contextual_tuples` are the two places a
tuple reaches evaluation, so they refuse it themselves rather than trusting every caller to have asked.
A delete names the tuple by key and evaluates nothing, so it stays open: a pinned grant that reached the
store must remain revocable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import InvalidInputError
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientCheckRequest, ClientTuple, ClientWriteRequest

from service_kit.governed import fga


REFUSAL = f"cannot carry {fga.CLOCK_PARAMETER} on the tuple"


def _grant(**context: str) -> ClientTuple:
    return ClientTuple(
        user="user:alice",
        relation="writer",
        object="namespace:bronze",
        condition=fga.RelationshipCondition(name="non_expired_grant", context={"grant_time": "2026-07-29T09:00:00Z", "grant_duration": "4h", **context}),
    )


def _pinned() -> ClientTuple:
    return _grant(current_time="2026-07-29T10:00:00Z")


class _Store:
    """Records every request that reached the client."""

    def __init__(self) -> None:
        self.writes: list[ClientWriteRequest] = []
        self.checks: list[ClientCheckRequest] = []

    async def write(self, request: ClientWriteRequest, options: dict[str, Any] | None = None) -> object:
        self.writes.append(request)
        return SimpleNamespace()

    async def check(self, request: ClientCheckRequest) -> object:
        self.checks.append(request)
        return SimpleNamespace(allowed=True)


def _write(store: _Store, tuples: list[ClientTuple]) -> None:
    asyncio.run(fga.write_tuples(cast(OpenFgaClient, store), tuples, actor="test", origin="admin_api"))


def test_write_tuples_refuses_a_tuple_carrying_the_clock() -> None:
    store = _Store()
    with pytest.raises(InvalidInputError, match=REFUSAL):
        _write(store, [_pinned()])
    assert store.writes == [], "a clock-pinned grant reached OpenFGA"


def test_write_tuples_refuses_the_whole_batch_not_only_the_pinned_tuple() -> None:
    """OpenFGA's Write is one transaction, so the refusal is too: a batch is written whole or not at all."""
    store = _Store()
    with pytest.raises(InvalidInputError, match=REFUSAL):
        _write(store, [ClientTuple(user="user:bob", relation="reader", object="namespace:bronze"), _pinned()])
    assert store.writes == []


def test_write_tuples_still_writes_a_condition_that_carries_only_its_window() -> None:
    """The control: the refusal is about the clock, not about conditions."""
    store = _Store()
    _write(store, [_grant()])
    assert len(store.writes) == 1


def test_a_check_refuses_a_contextual_tuple_carrying_the_clock() -> None:
    """A contextual tuple is evaluated exactly like a stored one, so a pinned clock would answer for a
    grant that `write_tuples` refuses to create."""
    store = _Store()
    with pytest.raises(InvalidInputError, match=REFUSAL):
        asyncio.run(
            fga.check(cast(OpenFgaClient, store), user="user:alice", relation="writer", obj="namespace:bronze", qualify=False, contextual_tuples=[_pinned()])
        )
    assert store.checks == [], "a check ran with a clock-pinned contextual tuple"


def test_a_check_may_still_name_its_own_clock_in_the_request_context() -> None:
    """The clock belongs to the CHECK, so the request context is exactly where it may be named."""
    store = _Store()
    asyncio.run(
        fga.check(
            cast(OpenFgaClient, store),
            user="user:alice",
            relation="writer",
            obj="namespace:bronze",
            qualify=False,
            contextual_tuples=[_grant()],
            context={fga.CLOCK_PARAMETER: "2026-07-29T10:00:00Z"},
        )
    )
    assert [c.context for c in store.checks] == [{fga.CLOCK_PARAMETER: "2026-07-29T10:00:00Z"}]


def test_a_check_that_names_no_clock_is_evaluated_at_the_servers_now() -> None:
    """What the estate-admin `/v1/access/check` docstring promises: an omitted clock is not a DENY."""
    store = _Store()
    before = datetime.now(UTC)
    asyncio.run(fga.check(cast(OpenFgaClient, store), user="user:alice", relation="writer", obj="namespace:bronze", qualify=False))
    context = store.checks[0].context
    assert context is not None, "the check was sent with no context, so a time-boxed grant reads as expired"
    sent = context[fga.CLOCK_PARAMETER]
    assert isinstance(sent, str), f"the clock was sent as {type(sent).__name__}, not an RFC 3339 string"
    assert before <= datetime.fromisoformat(sent) <= datetime.now(UTC)


def test_delete_tuples_still_removes_a_tuple_carrying_the_clock() -> None:
    """A delete is by key and evaluates nothing; refusing it would make the one grant that never
    expires also the one grant that cannot be revoked."""
    store = _Store()
    asyncio.run(fga.delete_tuples(cast(OpenFgaClient, store), [_pinned()], actor="test", origin="admin_api"))
    assert [(t.user, t.relation, t.object) for request in store.writes for t in request.deletes or []] == [("user:alice", "writer", "namespace:bronze")]
