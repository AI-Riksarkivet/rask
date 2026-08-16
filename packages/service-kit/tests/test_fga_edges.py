"""The hierarchy EDGES `grant_on_create` writes and `revoke_object_tuples` removes.

Two directions, written in one batch so they cannot drift: ``parent`` carries inheritance DOWN (a
grant on a namespace reaches its tables), ``child`` carries visibility UP (``can_get_metadata:
reader or can_get_metadata from child``). Without the second, a grant on ONE table is unreachable —
the grantee reads the table and cannot see the namespace or warehouse holding it, so every list
above it is empty and the breadcrumb to their own data 403s.

Deliberately in **service-kit's** own suite rather than ``tests/unit/``: these exercise
``service_kit.governed.fga`` alone, and the root ``tests/unit/test_fga_revoke.py`` imports
``catalog.api.fga_deps``, which pulls the catalog's ``lancekit`` extra (pylance + lancedb) in behind
it. That import is why the FGA suite needs ``uv sync --all-packages``; these need only
``uv sync --package service-kit --extra governed``, which is seconds and — measured 2026-08-09 —
the difference between running and not running at all on macOS/x86_64, where lancedb publishes no
wheel.

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
# create — both edges, one batch
# --------------------------------------------------------------------------- #


def test_grant_on_create_writes_the_parent_edge_and_its_inverse_in_one_batch() -> None:
    """One write, so an object can never hold one direction without the other — which would leave it
    either uninheritable or invisible."""
    fake = _WriteClient()
    asyncio.run(
        fga.grant_on_create(
            _client(fake),
            user_sub="alice",
            resource="table",
            obj_id="ns1$t",
            actor="alice",
            origin="create",
            parent_object="namespace:ns1",
        )
    )
    assert _keys(fake.written) == {
        ("user:alice", "owner", "table:ns1$t"),
        ("namespace:ns1", "parent", "table:ns1$t"),
        ("table:ns1$t", "child", "namespace:ns1"),
    }


def test_grant_on_create_writes_no_edges_for_a_root_object() -> None:
    """No parent → no edges either way; the creator's own grant is the whole write."""
    fake = _WriteClient()
    asyncio.run(fga.grant_on_create(_client(fake), user_sub="alice", resource="table", obj_id="rootless", actor="alice", origin="create"))
    assert _keys(fake.written) == {("user:alice", "owner", "table:rootless")}


def test_grant_on_create_skips_the_inverse_when_the_parent_type_declares_no_child() -> None:
    """``annotation_project`` hangs off ``project`` by ``tenant``, and ``project`` declares no ``child``.
    A ``child`` tuple there is accepted by OpenFGA and read by no rule — a silent no-op, the same
    failure mode as writing ``parent`` where the model says ``tenant``. Whitelisted on the PARENT's
    type for exactly that reason."""
    fake = _WriteClient()
    asyncio.run(
        fga.grant_on_create(
            _client(fake),
            user_sub="alice",
            resource="annotation_project",
            obj_id="labels",
            actor="alice",
            origin="create",
            parent_object="project:acme",
            parent_relation="tenant",
        )
    )
    assert _keys(fake.written) == {
        ("user:alice", "owner", "annotation_project:labels"),
        ("project:acme", "tenant", "annotation_project:labels"),
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
    fake = _ReadDeleteClient([_tuple("user:alice", "owner", "table:ns1$t"), _tuple("namespace:ns1", "parent", "table:ns1$t")])
    removed = asyncio.run(fga.revoke_object_tuples(_client(fake), "table:ns1$t", actor="test", origin="create"))
    assert len(removed) == 3  # the two read back, plus the inverse edge they imply
    assert _keys(fake.deleted) == {
        ("user:alice", "owner", "table:ns1$t"),
        ("namespace:ns1", "parent", "table:ns1$t"),
        ("table:ns1$t", "child", "namespace:ns1"),
    }


def test_revoke_skips_the_inverse_edge_for_a_parent_type_that_declares_no_child() -> None:
    """The revoke side of the ``tenant`` case above. The two must agree, or a create/drop pair leaves
    residue in one direction."""
    fake = _ReadDeleteClient([_tuple("project:acme", "tenant", "annotation_project:labels")])
    removed = asyncio.run(fga.revoke_object_tuples(_client(fake), "annotation_project:labels", actor="test", origin="create"))
    assert len(removed) == 1
    assert _keys(fake.deleted) == {("project:acme", "tenant", "annotation_project:labels")}


# --------------------------------------------------------------------------- #
# hierarchy_edge_tuples — the pairing, exposed so no writer can re-separate it
# --------------------------------------------------------------------------- #


def test_hierarchy_edge_tuples_returns_both_directions() -> None:
    """The pairing extracted from `grant_on_create`, because it was NOT the only writer of these
    edges — `medallion.services.train` and `scripts/seed_medallion_fga.sh` wrote the forward half
    alone, leaving every object they created invisible from below. A one-directional link is a valid
    tuple that no rule ever reads, so nothing failed; the lists above it simply came back empty."""
    edges = fga.hierarchy_edge_tuples(child_object="table:ns1$t", parent_object="namespace:ns1")

    assert [(t.user, t.relation, t.object) for t in edges] == [
        ("namespace:ns1", "parent", "table:ns1$t"),
        ("table:ns1$t", "child", "namespace:ns1"),
    ]


def test_hierarchy_edge_tuples_omits_the_inverse_where_the_model_has_no_child() -> None:
    """`project` declares no `child`. A `child` tuple there is accepted by OpenFGA and read by no
    rule — a silent no-op, which is worse than an error because it looks like the link was made."""
    edges = fga.hierarchy_edge_tuples(child_object="warehouse:wh", parent_object="project:acme")

    assert [(t.user, t.relation, t.object) for t in edges] == [("project:acme", "parent", "warehouse:wh")]


def test_hierarchy_edge_tuples_honours_a_non_default_parent_relation() -> None:
    """`annotation_project` spells the edge `tenant`. Writing `parent` there produces a tuple no rule
    reads, so `admin from tenant` never resolves and the object looks unowned by everyone."""
    edges = fga.hierarchy_edge_tuples(child_object="annotation_project:p1", parent_object="project:acme", parent_relation="tenant")

    assert [(t.user, t.relation, t.object) for t in edges] == [("project:acme", "tenant", "annotation_project:p1")]


def test_grant_on_create_is_built_on_the_shared_pairing() -> None:
    """Not a duplicate of the batch test above: that one pins the RESULT, this one pins that there is
    only ONE implementation of it. A second hand-written copy is exactly how the two writers drifted."""
    import inspect

    source = inspect.getsource(fga.grant_on_create)

    assert "hierarchy_edge_tuples" in source, "grant_on_create re-implements the pairing instead of sharing it"
    assert '"child"' not in source, "grant_on_create hand-writes the inverse edge again"


def test_every_parent_type_with_a_child_relation_is_declared_here() -> None:
    """`_CHILD_EDGE_PARENT_TYPES` is a hand-kept whitelist, and the inverse is skipped silently for a
    type missing from it. So a `child` added to the model and not here would produce exactly the
    invisible-object bug this helper exists to prevent, with every test still green."""
    compiled = {td["type"]: set((td.get("relations") or {}).keys()) for td in fga.load_model()["type_definitions"]}
    declares_child = {name for name, relations in compiled.items() if "child" in relations}

    assert declares_child == set(fga._CHILD_EDGE_PARENT_TYPES), (
        f"model.fga declares `child` on {sorted(declares_child)}; _CHILD_EDGE_PARENT_TYPES says "
        f"{sorted(fga._CHILD_EDGE_PARENT_TYPES)} — the inverse edge is silently skipped for the difference"
    )


def test_revoke_hands_back_WHO_it_revoked_not_merely_how_many() -> None:
    """The people whose access was just destroyed are in hand at the moment they are discarded.

    Every destructive door in the estate ends here — a project delete, a warehouse delete, a cascading
    namespace drop, an overwrite create — and each one revokes every grant on the object and then
    reports an integer. So the estate knows that fourteen grants vanished and not whose, and the
    subjects who can no longer see their own data learn it by hitting a 403 in the middle of work:
    exactly the failure the `grant_revoked` control action exists to prevent.

    A count cannot be fanned out. Returning the tuples costs nothing (they were already read to be
    deleted) and is what lets a caller name the affected principals.

    Callers wanting the old number take `len(...)`, which is why this is a return-type change rather
    than a second function: two revoke paths would be two places for the child-edge repair to drift.
    """
    fake = _ReadDeleteClient([_tuple("user:alice", "owner", "table:ns1$t"), _tuple("namespace:ns1", "parent", "table:ns1$t")])

    revoked = asyncio.run(fga.revoke_object_tuples(_client(fake), "table:ns1$t", actor="test", origin="create"))

    assert len(revoked) == 3, "the count contract survives as len()"
    assert _keys(revoked) == {
        ("user:alice", "owner", "table:ns1$t"),
        ("namespace:ns1", "parent", "table:ns1$t"),
        ("table:ns1$t", "child", "namespace:ns1"),
    }, "including the inverse edge, so a caller sees exactly what was destroyed"


def test_revoking_nothing_hands_back_nothing() -> None:
    """The no-op stays falsy, so `if revoked:` keeps working at every call site."""
    fake = _ReadDeleteClient([])
    assert asyncio.run(fga.revoke_object_tuples(_client(fake), "table:ns1$t", actor="test", origin="create")) == []
