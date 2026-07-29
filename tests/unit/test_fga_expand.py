"""Unit tests for the derivation primitives in ``service_kit.governed.fga``: ``expand`` / ``expand_tree``,
plus the two widenings the estate explorer needed from ``list_objects`` / ``list_users``.

What these pin, and why each matters more than it looks:

* **The tree keeps OpenFGA's operators.** ``union`` / ``intersection`` / ``difference`` survive
  normalisation. Both reference playgrounds drop intersection and difference on the floor (the official
  Types Previewer draws neither, and its TreeBuilder falls through to ``default`` on both), which is
  exactly how a grant that is actually *excluded* renders as granted. Flattening here would reproduce
  that bug.
* **A stopped walk is never a finished walk.** The depth ceiling and a revisited ``object#relation``
  are MARKED in the tree, not pruned — a partial derivation that reads as complete is worse than no
  derivation, because it is the one an operator will act on.
* **The subject widenings.** ``list_objects`` could only ever ask about a ``user:``, and ``list_users``
  discarded every userset arm of the response. In THIS model the resource rungs deliberately refuse
  ``team#member`` directly — access travels through ``role#assignee`` — so the userset arm is not an
  edge case, it is where the answers live.

stdlib ``asyncio.run`` throughout, so no pytest-asyncio dependency, matching ``test_fga_resilience.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import aiohttp
import pytest
from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient

from service_kit.governed import fga


# --------------------------------------------------------------------------- #
# Builders for the SDK's expand-tree object graph (SimpleNamespace duck-types it)
# --------------------------------------------------------------------------- #


def _node(
    name: str | None = None,
    *,
    leaf: Any = None,
    union: Any = None,
    intersection: Any = None,
    difference: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(name=name, leaf=leaf, union=union, intersection=intersection, difference=difference)


def _leaf(*, users: list[str] | None = None, computed: str | None = None, ttu: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        users=SimpleNamespace(users=users) if users is not None else None,
        computed=SimpleNamespace(userset=computed) if computed is not None else None,
        tuple_to_userset=ttu,
    )


def _ttu(tupleset: str, computed: list[str]) -> SimpleNamespace:
    return SimpleNamespace(tupleset=tupleset, computed=[SimpleNamespace(userset=c) for c in computed])


def _nodes(*children: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(nodes=list(children))


class _ExpandClient:
    """Serves a canned tree per ``object#relation`` and canned tuples per object."""

    def __init__(self, trees: dict[str, SimpleNamespace], tuples: dict[str, list[tuple[str, str, str]]] | None = None) -> None:
        self.trees = trees
        self.tuples = tuples or {}
        self.expanded: list[str] = []
        self.reads: list[str] = []

    async def expand(self, body: Any) -> object:
        key = f"{body.object}#{body.relation}"
        self.expanded.append(key)
        root = self.trees.get(key)
        return SimpleNamespace(tree=SimpleNamespace(root=root) if root is not None else None)

    async def read(self, tuple_key: Any, options: Any = None) -> object:
        del options
        self.reads.append(tuple_key.object)
        rows = self.tuples.get(tuple_key.object, [])
        return SimpleNamespace(
            tuples=[SimpleNamespace(key=SimpleNamespace(user=u, relation=r, object=o)) for u, r, o in rows],
            continuation_token=None,
        )


def _client(trees: dict[str, SimpleNamespace], tuples: dict[str, list[tuple[str, str, str]]] | None = None) -> OpenFgaClient:
    return cast(OpenFgaClient, _ExpandClient(trees, tuples))


# --------------------------------------------------------------------------- #
# expand — the one-level primitive
# --------------------------------------------------------------------------- #


def test_expand_normalises_every_leaf_kind() -> None:
    tree = _node(
        "table:t#reader",
        union=_nodes(
            _node("direct", leaf=_leaf(users=["user:alice", "user:*"])),
            _node("same-object", leaf=_leaf(computed="writer")),
            _node("from-parent", leaf=_leaf(ttu=_ttu("table:t#parent", ["reader"]))),
        ),
    )
    result = asyncio.run(fga.expand(_client({"table:t#reader": tree}), relation="reader", obj="table:t"))

    assert result["name"] == "table:t#reader"
    kinds = [child["leaf"] for child in result["union"]]
    assert kinds[0]["users"] == ["user:alice", "user:*"]  # the wildcard is preserved verbatim
    assert kinds[1]["computed"] == "writer"
    assert kinds[2]["tuple_to_userset"] == {"tupleset": "table:t#parent", "computed": ["reader"]}


def test_expand_preserves_intersection_and_difference() -> None:
    # The operator IS the explanation. A `but not` that normalises away turns a denied subject into an
    # allowed-looking one on screen — the precise failure both reference playgrounds ship.
    tree = _node(
        "table:t#can_promote",
        intersection=_nodes(
            _node("a", leaf=_leaf(computed="writer")),
            _node(
                "b",
                difference=SimpleNamespace(
                    base=_node("base", leaf=_leaf(computed="validator")),
                    subtract=_node("sub", leaf=_leaf(users=["user:banned"])),
                ),
            ),
        ),
    )
    result = asyncio.run(fga.expand(_client({"table:t#can_promote": tree}), relation="can_promote", obj="table:t"))

    assert [c["name"] for c in result["intersection"]] == ["a", "b"]
    difference = result["intersection"][1]["difference"]
    assert difference["base"]["leaf"]["computed"] == "validator"
    assert difference["subtract"]["leaf"]["users"] == ["user:banned"]


def test_expand_returns_empty_dict_when_the_relation_resolves_to_nothing() -> None:
    # {} ("nothing here") and a 503 ("could not ask") are different answers; only the first is a result.
    assert asyncio.run(fga.expand(_client({}), relation="reader", obj="table:t")) == {}


def test_expand_fails_closed_on_network_error() -> None:
    class _Down:
        async def expand(self, *_a: object, **_k: object) -> object:
            raise aiohttp.ClientConnectionError("connection refused")

    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.expand(
                cast(OpenFgaClient, _Down()),
                relation="reader",
                obj="table:t",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


def test_expand_depth_cap_marks_rather_than_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fga, "_MAX_EXPAND_DEPTH", 1)
    tree = _node("root", union=_nodes(_node("deep", union=_nodes(_node("deeper")))))
    result = asyncio.run(fga.expand(_client({"table:t#reader": tree}), relation="reader", obj="table:t"))
    assert result["union"][0]["truncated"] is True


# --------------------------------------------------------------------------- #
# expand_tree — the recursive walk that actually answers "why"
# --------------------------------------------------------------------------- #


def test_expand_tree_depth_one_costs_exactly_one_expand_and_flags_what_continues() -> None:
    # The budget check must run BEFORE any I/O: a depth-1 call is one round trip, and the leaf still
    # advertises that it goes further, so a UI can offer "expand" without the server guessing.
    client = _ExpandClient({"table:t#reader": _node("table:t#reader", leaf=_leaf(computed="writer"))})
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="reader", obj="table:t"))
    assert result["leaf"].get("expanded") is None
    assert result["leaf"]["continues"] is True
    assert client.expanded == ["table:t#reader"]


def test_expand_tree_does_not_read_tuples_it_cannot_use() -> None:
    # An `X from Y` leaf at the budget edge must NOT spend a paginated tuple read to build a stub.
    client = _ExpandClient(
        trees={"table:t#reader": _node("table:t#reader", leaf=_leaf(ttu=_ttu("table:t#parent", ["reader"])))},
        tuples={"table:t": [("namespace:db1", "parent", "table:t")]},
    )
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="reader", obj="table:t"))
    assert client.reads == []
    assert result["leaf"]["continues"] is True


def test_expand_tree_terminal_leaf_does_not_claim_to_continue() -> None:
    client = _ExpandClient({"table:t#reader": _node("table:t#reader", leaf=_leaf(users=["user:alice"]))})
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="reader", obj="table:t"))
    assert "continues" not in result["leaf"]


def test_expand_tree_follows_a_computed_rung_on_the_same_object() -> None:
    client = _ExpandClient(
        {
            "table:t#reader": _node("table:t#reader", leaf=_leaf(computed="writer")),
            "table:t#writer": _node("table:t#writer", leaf=_leaf(users=["user:alice"])),
        }
    )
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="reader", obj="table:t", max_depth=2))
    assert result["leaf"]["expanded"][0]["leaf"]["users"] == ["user:alice"]
    assert client.expanded == ["table:t#reader", "table:t#writer"]


def test_expand_tree_follows_x_from_y_into_the_parent_object() -> None:
    # The whole point: `reader from parent` is where a concentric model's answer lives, and a single
    # Expand stops right before it — naming the edge without ever saying who is on the other side.
    client = _ExpandClient(
        trees={
            "table:db1$t#reader": _node("table:db1$t#reader", leaf=_leaf(ttu=_ttu("table:db1$t#parent", ["reader"]))),
            "namespace:db1#reader": _node("namespace:db1#reader", leaf=_leaf(users=["user:alice"])),
        },
        tuples={"table:db1$t": [("namespace:db1", "parent", "table:db1$t")]},
    )
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="reader", obj="table:db1$t", max_depth=3))

    expanded = result["leaf"]["expanded"]
    assert [n["name"] for n in expanded] == ["namespace:db1#reader"]
    assert expanded[0]["leaf"]["users"] == ["user:alice"]


def test_expand_tree_ignores_tuples_that_are_not_the_tupleset_relation() -> None:
    client = _ExpandClient(
        trees={
            "table:t#reader": _node("table:t#reader", leaf=_leaf(ttu=_ttu("table:t#parent", ["reader"]))),
            "namespace:db1#reader": _node("namespace:db1#reader", leaf=_leaf(users=["user:alice"])),
        },
        # An `owner` grant sits on the same object; only the `parent` edge is the hop.
        tuples={"table:t": [("user:bob", "owner", "table:t"), ("namespace:db1", "parent", "table:t")]},
    )
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="reader", obj="table:t", max_depth=3))
    assert [n["name"] for n in result["leaf"]["expanded"]] == ["namespace:db1#reader"]


def test_expand_tree_marks_a_cycle_instead_of_spinning() -> None:
    # `namespace` self-nests under `namespace`, so a misconfigured parent tuple makes a real loop.
    client = _ExpandClient(
        trees={
            "namespace:a#reader": _node("namespace:a#reader", leaf=_leaf(ttu=_ttu("namespace:a#parent", ["reader"]))),
            "namespace:b#reader": _node("namespace:b#reader", leaf=_leaf(ttu=_ttu("namespace:b#parent", ["reader"]))),
        },
        tuples={"namespace:a": [("namespace:b", "parent", "namespace:a")], "namespace:b": [("namespace:a", "parent", "namespace:b")]},
    )
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="reader", obj="namespace:a", max_depth=6))

    hop = result["leaf"]["expanded"][0]
    assert hop["name"] == "namespace:b#reader"
    assert hop["leaf"]["expanded"][0] == {"name": "namespace:a#reader", "cycle": True}


def test_expand_tree_stops_on_budget_at_the_leaf_that_would_have_continued() -> None:
    # Budget 2 = two Expands. The chain a → b → c stops AT b, flagged; `c` is never fetched, and the
    # tree says so rather than presenting b's leaf as terminal.
    client = _ExpandClient(
        trees={
            "table:t#a": _node("table:t#a", leaf=_leaf(computed="b")),
            "table:t#b": _node("table:t#b", leaf=_leaf(computed="c")),
            "table:t#c": _node("table:t#c", leaf=_leaf(users=["user:alice"])),
        }
    )
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="a", obj="table:t", max_depth=2))
    stopped = result["leaf"]["expanded"][0]
    assert stopped["name"] == "table:t#b"
    assert stopped["leaf"]["continues"] is True
    assert stopped["leaf"].get("expanded") is None
    assert client.expanded == ["table:t#a", "table:t#b"]


def test_expand_tree_clamps_max_depth_to_the_module_ceiling() -> None:
    chain = {f"table:t#r{i}": _node(f"table:t#r{i}", leaf=_leaf(computed=f"r{i + 1}")) for i in range(20)}
    client = _ExpandClient(chain)
    asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="r0", obj="table:t", max_depth=999))
    assert len(client.expanded) == fga.MAX_EXPAND_TREE_DEPTH


# --------------------------------------------------------------------------- #
# The subject widenings
# --------------------------------------------------------------------------- #


def test_list_objects_qualify_false_sends_a_userset_verbatim() -> None:
    seen: dict[str, str] = {}

    class _Client:
        async def list_objects(self, body: Any) -> object:
            seen["user"] = body.user
            return SimpleNamespace(objects=["table:db1$t"])

    asyncio.run(fga.list_objects(cast(OpenFgaClient, _Client()), user="team:eng#member", relation="can_read_data", object_type="table", qualify=False))
    assert seen["user"] == "team:eng#member"  # NOT user:team:eng#member, which would deny everything


def test_list_objects_still_qualifies_a_bare_id_by_default() -> None:
    seen: dict[str, str] = {}

    class _Client:
        async def list_objects(self, body: Any) -> object:
            seen["user"] = body.user
            return SimpleNamespace(objects=[])

    asyncio.run(fga.list_objects(cast(OpenFgaClient, _Client()), user="alice", relation="can_read_data", object_type="table"))
    assert seen["user"] == "user:alice"


def test_list_users_returns_the_userset_arm_it_used_to_discard() -> None:
    class _Client:
        async def list_users(self, body: Any) -> object:
            del body
            return SimpleNamespace(
                users=[
                    SimpleNamespace(object=None, userset=SimpleNamespace(type="role", id="validators", relation="assignee"), wildcard=None),
                    SimpleNamespace(object=SimpleNamespace(type="user", id="alice"), userset=None, wildcard=None),
                    SimpleNamespace(object=None, userset=None, wildcard=SimpleNamespace(type="user")),
                ]
            )

    result = asyncio.run(fga.list_users(cast(OpenFgaClient, _Client()), relation="reader", obj="table:t", qualified=True))
    assert result == ["role:validators#assignee", "user:*", "user:alice"]


def test_list_users_passes_the_requested_filter_through() -> None:
    seen: dict[str, Any] = {}

    class _Client:
        async def list_users(self, body: Any) -> object:
            seen["filters"] = body.user_filters
            return SimpleNamespace(users=[])

    asyncio.run(fga.list_users(cast(OpenFgaClient, _Client()), relation="reader", obj="table:t", user_type="role", user_relation="assignee"))
    assert (seen["filters"][0].type, seen["filters"][0].relation) == ("role", "assignee")


def test_list_users_default_contract_is_unchanged() -> None:
    # Every existing access-review caller reads bare ids; widening must not have moved that.
    class _Client:
        async def list_users(self, body: Any) -> object:
            del body
            return SimpleNamespace(users=[SimpleNamespace(object=SimpleNamespace(type="user", id="bob"), userset=None, wildcard=None)])

    assert asyncio.run(fga.list_users(cast(OpenFgaClient, _Client()), relation="reader", obj="table:t")) == ["bob"]


def test_expand_tree_normalises_a_qualified_computed_userset() -> None:
    """OpenFGA answers `computed.userset` QUALIFIED — `"table:t#reader"`, not `"reader"`.

    Passing that straight back as a relation is a malformed Expand, which the server answers with a
    500 and this module fails closed into a 503 — so every relation resolving through a rung reported
    "derivation unavailable", which is all of the interesting ones. Captured from the live store:
    expand(can_read_data, table:bronze$pages) → {"computed": {"userset": "table:bronze$pages#reader"}}.
    """
    client = _ExpandClient(
        {
            "table:t#can_read_data": _node("table:t#can_read_data", leaf=_leaf(computed="table:t#reader")),
            "table:t#reader": _node("table:t#reader", leaf=_leaf(users=["user:alice"])),
        }
    )
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="can_read_data", obj="table:t", max_depth=2))

    # The follow-up asks for the RELATION on the object, never the qualified string.
    assert client.expanded == ["table:t#can_read_data", "table:t#reader"]
    assert result["leaf"]["expanded"][0]["leaf"]["users"] == ["user:alice"]


def test_expand_tree_normalises_qualified_tuple_to_userset_entries() -> None:
    client = _ExpandClient(
        trees={
            "table:db1$t#owner": _node(
                "table:db1$t#owner",
                leaf=_leaf(ttu=_ttu("table:db1$t#parent", ["namespace:db1#owner"])),
            ),
            "namespace:db1#owner": _node("namespace:db1#owner", leaf=_leaf(users=["user:alice"])),
        },
        tuples={"table:db1$t": [("namespace:db1", "parent", "table:db1$t")]},
    )
    result = asyncio.run(fga.expand_tree(cast(OpenFgaClient, client), relation="owner", obj="table:db1$t", max_depth=3))

    assert client.expanded == ["table:db1$t#owner", "namespace:db1#owner"]
    assert result["leaf"]["expanded"][0]["leaf"]["users"] == ["user:alice"]


def test_relation_and_object_helpers_accept_both_shapes() -> None:
    assert fga._relation_of("table:db1$t#reader") == "reader"
    assert fga._relation_of("reader") == "reader"  # bare passes through; the shape is not guaranteed
    assert fga._object_of("table:db1$t#reader", "fallback:x") == "table:db1$t"
    assert fga._object_of("reader", "table:same") == "table:same"  # a rung on the SAME object


# --------------------------------------------------------------------------- #
# assert-before-grant (contextual tuples) + the changelog
# --------------------------------------------------------------------------- #


def test_check_passes_contextual_tuples_through_without_writing() -> None:
    """ "Would this grant work?" answered against an UNTOUCHED store.

    A concentric model cascades a grant to every child, which is exactly the reasoning people get
    wrong by reading the DSL. Contextual tuples let the store answer instead — and they are
    per-request, so nothing is persisted and no cleanup is owed.
    """
    seen: dict[str, Any] = {}
    writes: list[Any] = []

    class _Client:
        async def check(self, body: Any) -> object:
            seen["user"] = body.user
            seen["contextual"] = body.contextual_tuples
            return SimpleNamespace(allowed=True)

        async def write(self, *_a: object, **_k: object) -> object:
            writes.append(1)
            return SimpleNamespace()

    hypothetical = [fga.ClientTuple(user="user:bob", relation="reader", object="warehouse:acme")]
    allowed = asyncio.run(
        fga.check(
            cast(OpenFgaClient, _Client()),
            user="user:bob",
            relation="can_read_data",
            obj="table:acme_gold_catalog",
            qualify=False,
            contextual_tuples=hypothetical,
        )
    )
    assert allowed is True
    assert seen["contextual"] is not None and len(seen["contextual"]) == 1
    # The whole point: a simulation must not mutate the store.
    assert writes == []


def test_check_without_contextual_tuples_is_unchanged() -> None:
    seen: dict[str, Any] = {}

    class _Client:
        async def check(self, body: Any) -> object:
            seen["contextual"] = body.contextual_tuples
            return SimpleNamespace(allowed=False)

    asyncio.run(fga.check(cast(OpenFgaClient, _Client()), user="alice", relation="reader", obj="table:t"))
    assert seen["contextual"] is None


def test_read_changes_normalises_operation_and_timestamp() -> None:
    class _Client:
        async def read_changes(self, body: Any, options: Any = None) -> object:
            del body, options
            return SimpleNamespace(
                changes=[
                    SimpleNamespace(
                        tuple_key=SimpleNamespace(user="user:alice", relation="owner", object="table:t"),
                        operation="TUPLE_OPERATION_WRITE",
                        timestamp="2026-07-29T09:37:21.909224Z",
                    ),
                    SimpleNamespace(
                        tuple_key=SimpleNamespace(user="user:bob", relation="reader", object="table:t"),
                        operation="TUPLE_OPERATION_DELETE",
                        timestamp="2026-07-29T10:00:00Z",
                    ),
                ],
                continuation_token="next",
            )

    changes, token = asyncio.run(fga.read_changes(cast(OpenFgaClient, _Client()), object_type="table"))
    assert [c["operation"] for c in changes] == ["write", "delete"]
    assert changes[0]["user"] == "user:alice"
    # A DELETE is visible here and invisible to a Read — that asymmetry is the point of a changelog.
    assert changes[1]["relation"] == "reader"
    assert token == "next"


def test_read_changes_fails_closed_on_network_error() -> None:
    # An outage must never render as "nothing ever happened on this type", which is what an empty
    # history would say — and it is the reading an access review would act on.
    class _Down:
        async def read_changes(self, *_a: object, **_k: object) -> object:
            raise aiohttp.ClientConnectionError("connection refused")

    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.read_changes(
                cast(OpenFgaClient, _Down()),
                object_type="table",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


def test_read_changes_never_claims_an_actor() -> None:
    """OpenFGA change records carry no principal, and this wrapper must not invent one.

    The acting subject lives only in this estate's audit stream. A field named `actor`/`by`/`subject`
    appearing here — even as None — would invite a UI to render a timestamp as attribution.
    """

    class _Client:
        async def read_changes(self, body: Any, options: Any = None) -> object:
            del body, options
            return SimpleNamespace(
                changes=[
                    SimpleNamespace(
                        tuple_key=SimpleNamespace(user="user:a", relation="owner", object="table:t"),
                        operation="TUPLE_OPERATION_WRITE",
                        timestamp="2026-07-29T09:37:21Z",
                    )
                ],
                continuation_token=None,
            )

    changes, _ = asyncio.run(fga.read_changes(cast(OpenFgaClient, _Client()), object_type="table"))
    assert set(changes[0]) == {"user", "relation", "object", "operation", "timestamp"}
