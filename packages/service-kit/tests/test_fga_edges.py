"""The hierarchy EDGES `grant_on_create` writes and `revoke_object_tuples` removes.

Two directions, written in one batch so they cannot drift: ``parent`` carries inheritance DOWN (a
grant on a namespace reaches its tables), ``child`` carries visibility UP (``can_get_metadata:
reader or can_get_metadata from child``). Without the second, a grant on ONE table is unreachable —
the grantee reads the table and cannot see the namespace or warehouse holding it, so every list
above it is empty and the breadcrumb to their own data 403s.

Both edges of a table's and a namespace's create, on the uuid objects a real OpenFGA holds, are pinned by
``services/catalog/tests/test_a_grant_is_held_by_the_objects_uuid.py::test_every_tuple_a_create_writes_names_the_objects_uuid``;
these pin the branches no create door reaches.

Deliberately in **service-kit's** own suite rather than ``tests/unit/``: these exercise
``service_kit.governed.fga`` alone, and the root ``tests/unit/test_fga_revoke.py`` imports
``catalog.api.fga_deps``, which pulls the catalog's Lance stack (pylance) in behind it. That import is
why the FGA suite needs ``uv sync --all-packages``; these need only
``uv sync --package service-kit --extra governed``, which is seconds.

Sync via stdlib ``asyncio.run``, matching test_fga_revoke / test_fga_resilience — no pytest-asyncio
dependency (this repo does not set ``asyncio_mode``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientTuple

from service_kit.governed import fga


def _tuple(user: str, relation: str, obj: str) -> SimpleNamespace:
    """A read-response tuple: ``.key`` carries user/relation/object (the SDK Tuple shape)."""
    return SimpleNamespace(key=SimpleNamespace(user=user, relation=relation, object=obj))


def _keys(tuples: list[ClientTuple]) -> set[tuple[str, str, str]]:
    return {(t.user, t.relation, t.object) for t in tuples}


#: The uuids the catalog minted for a table and its namespace (ADR 0173 §8): the objects their grants are held by.
_TABLE = "table:0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01"
_NAMESPACE = "namespace:7d2e4c19-8a3b-4e6f-b1d0-5c9a2e8f3b47"


def _client(c: object) -> OpenFgaClient:
    """Cast a fake to the SDK client type (it only needs the called methods)."""
    return cast(OpenFgaClient, c)


class _WriteClient:
    """Records writes only — the create half of the edge contract."""

    def __init__(self) -> None:
        self.written: list[ClientTuple] = []

    async def write(self, body: Any, *_a: object, **_k: object) -> None:
        self.written.extend(body.writes or [])


class _ReadDeleteClient:
    """Serves one canned read page and accumulates the per-tuple deletes."""

    def __init__(self, tuples: list[SimpleNamespace]) -> None:
        self._tuples = tuples
        self.deleted: list[ClientTuple] = []

    async def read(self, _body: Any = None, options: Any = None, *_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(tuples=self._tuples, continuation_token="")

    async def write(self, body: Any, *_a: object, **_k: object) -> None:
        self.deleted.extend(body.deletes)


# --------------------------------------------------------------------------- #
# create — the branches the catalog's create doors do not reach
# --------------------------------------------------------------------------- #


def test_grant_on_create_writes_no_edges_for_a_root_object() -> None:
    """No parent → no edges either way; the creator's own grant is the whole write."""
    fake = _WriteClient()
    asyncio.run(fga.grant_on_create(_client(fake), user_sub="dex~alice", obj=_TABLE, actor="dex~alice", origin="create"))
    assert _keys(fake.written) == {("user:dex~alice", "owner", _TABLE)}


def test_grant_on_create_skips_the_inverse_when_the_parent_type_declares_no_child() -> None:
    """A ``classification`` hangs off the ``estate``, and ``estate`` declares no ``child``. A ``child`` tuple
    there is accepted by OpenFGA and read by no rule — a silent no-op. Whitelisted on the PARENT's type for
    exactly that reason."""
    fake = _WriteClient()
    asyncio.run(
        fga.grant_on_create(
            _client(fake),
            user_sub="dex~alice",
            obj="classification:pii",
            actor="dex~alice",
            origin="create",
            parent_object="estate:rask",
        )
    )
    assert _keys(fake.written) == {
        ("user:dex~alice", "owner", "classification:pii"),
        ("estate:rask", "parent", "classification:pii"),
    }


# --------------------------------------------------------------------------- #
# revoke — the edge a read-by-object cannot see
# --------------------------------------------------------------------------- #


def test_revoke_removes_the_inverse_child_edge_a_read_by_object_cannot_see() -> None:
    """The inverse edge is stored as ``<obj> child <parent>``, so its OBJECT is the parent and a read
    keyed on ``object == obj`` never returns it. Left behind it is a phantom child — the access graph
    renders an edge to an object that no longer exists and the drop looks half-done.

    The parent is recovered from the ``parent`` tuple the read DOES return: that one is
    ``<parent> parent <obj>``, so its USER is the parent object."""
    fake = _ReadDeleteClient([_tuple("user:dex~alice", "owner", _TABLE), _tuple(_NAMESPACE, "parent", _TABLE)])
    removed = asyncio.run(fga.revoke_object_tuples(_client(fake), _TABLE, actor="test", origin="create"))
    assert len(removed) == 3  # the two read back, plus the inverse edge they imply
    assert _keys(fake.deleted) == {
        ("user:dex~alice", "owner", _TABLE),
        (_NAMESPACE, "parent", _TABLE),
        (_TABLE, "child", _NAMESPACE),
    }


def test_revoke_skips_the_inverse_edge_for_a_parent_type_that_declares_no_child() -> None:
    """The revoke side of the ``estate`` case above. The two must agree, or a create/drop pair leaves
    residue in one direction."""
    fake = _ReadDeleteClient([_tuple("estate:rask", "parent", "classification:pii")])
    removed = asyncio.run(fga.revoke_object_tuples(_client(fake), "classification:pii", actor="test", origin="create"))
    assert len(removed) == 1
    assert _keys(fake.deleted) == {("estate:rask", "parent", "classification:pii")}
