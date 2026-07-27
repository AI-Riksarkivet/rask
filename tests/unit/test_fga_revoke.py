"""Unit tests for the revoke-on-delete path (common.fga + catalog.fga_deps).

A dropped / renamed-away object's FGA tuples must be removed, or a later object reusing the id
inherits the old grants (stale-grant privilege bleed). These pin the read+delete contract WITHOUT
the network: the paginated read returns the COMPLETE set, the delete issues exactly those tuples,
an already-absent delete is idempotent, and an OpenFGA outage fails closed (503).

Uses stdlib ``asyncio.run`` so no pytest-asyncio dependency is needed (matches test_fga_resilience).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import aiohttp
import pytest
from catalog.api import fga_deps
from common import fga
from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientTuple
from openfga_sdk.exceptions import ApiException


def _tuple(user: str, relation: str, obj: str) -> SimpleNamespace:
    """A read-response tuple: ``.key`` carries user/relation/object (the SDK Tuple shape)."""
    return SimpleNamespace(key=SimpleNamespace(user=user, relation=relation, object=obj))


class _ReadDeleteClient:
    """Fake OpenFGA client: serves canned read pages, records the options each read got, and accumulates
    the per-tuple delete writes (delete_tuples now issues one write per tuple, not a batch)."""

    def __init__(self, pages: list[tuple[list[SimpleNamespace], str]]) -> None:
        self._pages = pages
        self.read_calls = 0
        self.read_options: list[Any] = []
        self.deleted: list[ClientTuple] = []

    async def read(self, _body: Any = None, options: Any = None, *_a: object, **_k: object) -> SimpleNamespace:
        self.read_options.append(options)
        tuples, token = self._pages[self.read_calls]
        self.read_calls += 1
        return SimpleNamespace(tuples=tuples, continuation_token=token)

    async def write(self, body: Any, *_a: object, **_k: object) -> None:
        self.deleted.extend(body.deletes)


def _client(c: object) -> OpenFgaClient:
    """Cast a fake to the SDK client type (it only needs the called methods)."""
    return cast(OpenFgaClient, c)


def _keys(tuples: list[ClientTuple]) -> set[tuple[str, str, str]]:
    return {(t.user, t.relation, t.object) for t in tuples}


def test_read_object_tuples_paginates_to_completion() -> None:
    # A continuation token on page 1 MUST be followed, or a stale grant on page 2 is missed.
    fake = _ReadDeleteClient(
        [
            ([_tuple("user:alice", "owner", "table:t"), _tuple("user:bob", "reader", "table:t")], "next"),
            ([_tuple("namespace:db", "parent", "table:t")], ""),
        ]
    )
    out = asyncio.run(fga.read_object_tuples(_client(fake), "table:t"))
    assert fake.read_calls == 2
    # The 2nd read MUST carry the continuation token from page 1 (else a >1-page object silently
    # under-reads → partial revoke). The first read has no token.
    assert fake.read_options == [None, {"continuation_token": "next"}]
    assert _keys(out) == {
        ("user:alice", "owner", "table:t"),
        ("user:bob", "reader", "table:t"),
        ("namespace:db", "parent", "table:t"),
    }


def test_revoke_object_tuples_reads_then_deletes_all() -> None:
    fake = _ReadDeleteClient([([_tuple("user:alice", "owner", "table:t"), _tuple("user:bob", "writer", "table:t")], "")])
    removed = asyncio.run(fga.revoke_object_tuples(_client(fake), "table:t"))
    assert removed == 2
    assert fake.deleted is not None
    assert _keys(fake.deleted) == {
        ("user:alice", "owner", "table:t"),
        ("user:bob", "writer", "table:t"),
    }


def test_revoke_object_tuples_noop_when_no_grants() -> None:
    fake = _ReadDeleteClient([([], "")])
    removed = asyncio.run(fga.revoke_object_tuples(_client(fake), "table:gone"))
    assert removed == 0
    assert fake.deleted == []  # nothing to delete → no write issued at all


class _PartialMissingClient:
    """read returns 3 tuples; the MIDDLE one's delete raises the missing-delete 400 (a concurrent revoke).
    Per-tuple deletes must still remove the OTHER two — a single batched transactional delete would have
    400'd and left ALL three in place (the exact bug the per-tuple split fixes)."""

    def __init__(self) -> None:
        self.deleted: list[ClientTuple] = []

    async def read(self, *_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(
            tuples=[
                _tuple("user:a", "owner", "table:t"),
                _tuple("user:b", "reader", "table:t"),
                _tuple("user:c", "writer", "table:t"),
            ],
            continuation_token="",
        )

    async def write(self, body: Any, *_a: object, **_k: object) -> None:
        t = body.deletes[0]
        if t.relation == "reader":
            raise ApiException(status=400, reason="cannot delete a tuple which does not exist")
        self.deleted.append(t)


def test_per_tuple_delete_tolerates_one_missing_without_losing_the_rest() -> None:
    fake = _PartialMissingClient()
    removed = asyncio.run(fga.revoke_object_tuples(_client(fake), "table:t"))
    assert removed == 3  # all three attempted
    # The present two are removed; the concurrently-absent one is tolerated — NOT an all-or-nothing loss.
    assert _keys(fake.deleted) == {
        ("user:a", "owner", "table:t"),
        ("user:c", "writer", "table:t"),
    }


class _MissingDeleteClient:
    """read returns one tuple; the delete raises the 400 'tuple does not exist' (concurrent revoke)."""

    async def read(self, *_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(tuples=[_tuple("user:alice", "owner", "table:t")], continuation_token="")

    async def write(self, *_a: object, **_k: object) -> None:
        raise ApiException(status=400, reason="cannot delete a tuple which does not exist")


def test_delete_is_idempotent_on_already_absent_tuple() -> None:
    # A concurrent revoke (the tuple is already gone) is SUCCESS — the post-condition holds.
    removed = asyncio.run(fga.revoke_object_tuples(_client(_MissingDeleteClient()), "table:t"))
    assert removed == 1


class _DownClient:
    """Every call raises a raw transport error (server down) — the dominant outage mode."""

    async def read(self, *_a: object, **_k: object) -> object:
        raise aiohttp.ClientConnectionError("refused")

    async def write(self, *_a: object, **_k: object) -> object:
        raise ConnectionRefusedError


def test_read_object_tuples_fails_closed_on_outage() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.read_object_tuples(
                _client(_DownClient()),
                "table:t",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


def test_delete_tuples_fails_closed_on_outage() -> None:
    tuples = [ClientTuple(user="user:alice", relation="owner", object="table:t")]
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.delete_tuples(
                _client(_DownClient()),
                tuples,
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


# --------------------------------------------------------------------------- #
# fga_deps.revoke_ownership — the endpoint-facing wrapper (mirrors seed_ownership)
# --------------------------------------------------------------------------- #


def _settings(*, fga_enabled: bool) -> Any:
    return cast(Any, SimpleNamespace(fga_enabled=fga_enabled, delimiter="$"))


def test_revoke_ownership_noop_when_fga_disabled() -> None:
    fake = _ReadDeleteClient([([_tuple("user:a", "owner", "table:db$t")], "")])
    asyncio.run(fga_deps.revoke_ownership(_client(fake), _settings(fga_enabled=False), resource="table", segments=["db", "t"]))
    assert fake.read_calls == 0  # FGA off → never touched


def test_revoke_ownership_noop_when_client_unwired(monkeypatch: pytest.MonkeyPatch) -> None:
    # client None (enabled-but-unwired) → short-circuit BEFORE touching fga, so a None client never
    # reaches fga.revoke_object_tuples (which would AttributeError on None.read).
    called = False

    async def _spy(*_a: object, **_k: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(fga, "revoke_object_tuples", _spy)
    asyncio.run(fga_deps.revoke_ownership(None, _settings(fga_enabled=True), resource="table", segments=["db", "t"]))
    assert called is False


def test_revoke_ownership_deletes_every_tuple_for_the_object() -> None:
    fake = _ReadDeleteClient([([_tuple("user:a", "owner", "table:db$t"), _tuple("user:b", "reader", "table:db$t")], "")])
    asyncio.run(fga_deps.revoke_ownership(_client(fake), _settings(fga_enabled=True), resource="table", segments=["db", "t"]))
    assert fake.deleted is not None
    # The id is canonicalised the same way seed_ownership writes it → table:db$t.
    assert {t.object for t in fake.deleted} == {"table:db$t"}
    assert _keys(fake.deleted) == {
        ("user:a", "owner", "table:db$t"),
        ("user:b", "reader", "table:db$t"),
    }
