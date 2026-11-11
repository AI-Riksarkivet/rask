"""The hierarchy's cascade invariant, pinned where the conformance run found it BROKEN.

Found on 2026-08-05 by driving the real doors, not by reading code.

- #117 the destructive CASCADE forwarded `behavior` to a `dir` backend that answers
  NamespaceNotEmpty for every casing, so with the shipped default a non-empty namespace could not be
  dropped AT ALL, and three guarded loops below it were unreachable code.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from lance_namespace import NamespaceNotFoundError

from catalog.api.fga_deps import GovernedObject
from lakehouse import base_registry, location_claims
from lakehouse.location_claims import ClaimStore
from lakehouse.storage_options import StorageOptions


def _governed(kind: str, segments: list[str]) -> GovernedObject:
    """A descendant as the door identifies it, under a uuid of its own name."""
    return GovernedObject.model_validate({"kind": kind, "segments": segments, "uuid": f"uuid-{'$'.join(segments)}"})


def _claimed(control_root: Path, descendants: list[GovernedObject]) -> ClaimStore:
    """A claim store on ``control_root`` in which each listed table holds its location's claim under its uuid, as the
    door that made it left it: the cascade removes a table only under the claim its name resolves to."""
    claims = ClaimStore(control_root=str(control_root))
    for item in descendants:
        if item.kind == "table":
            location_claims.take(claims, _location(item.segments), "$".join(item.segments), item.segments, is_gone=lambda _claim: False, uuid=item.uuid)
    return claims


def _location(segments: list[str]) -> str:
    return f"/nonexistent/data/{'$'.join(segments)}"


class _RecordingNs:
    """A structural stand-in that records which native ops ran, in order."""

    def __init__(self, existing_namespaces: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._existing = existing_namespaces if existing_namespaces is not None else set()

    def namespace_exists(self, request: Any) -> Any:
        ident = "$".join(request.id)
        self.calls.append(f"namespace_exists:{ident}")
        if ident not in self._existing:
            raise NamespaceNotFoundError(f"Namespace not found: {ident}")
        return type("R", (), {"model_fields_set": set()})()

    def describe_table(self, request: Any) -> Any:
        from lance_namespace import DescribeTableResponse

        # A location nothing is stored at: the destroy finds no byte to delete.
        return DescribeTableResponse(location=_location(request.id))

    def deregister_table(self, request: Any) -> Any:
        from lance_namespace import DeregisterTableResponse

        self.calls.append(f"deregister_table:{'$'.join(request.id)}")
        return DeregisterTableResponse(id=request.id, location=_location(request.id))

    def drop_namespace(self, request: Any) -> Any:
        self.calls.append(f"drop_namespace:{'$'.join(request.id)}:{(request.behavior or 'restrict').lower()}")
        return type("R", (), {"model_fields_set": set()})()


# ---------------------------------------------------------------- #118 parent must EXIST


# ---------------------------------------------------------------- #117 the cascade actually destroys
async def _drain(destroying: AsyncIterator[list[str]]) -> list[list[str]]:
    """Run the destroy to its end, as the door's own loop does, returning the tables it reported gone."""
    return [item async for item in destroying]


def test_the_cascade_destroys_BOTTOM_UP_and_never_asks_the_backend_to(tmp_path: Path) -> None:
    """The dir backend refuses `behavior=Cascade` for every casing, so this door must do the work:
    tables first, then namespaces deepest-first, then the root — each drop hitting an EMPTY object."""
    from catalog.api.v1.endpoints.namespaces import _destroy_subtree  # noqa: PLC2701 — the unit under test

    ns: Any = _RecordingNs()
    descendants = [
        _governed("table", ["bronze", "pages"]),
        _governed("namespace", ["bronze", "inner"]),
        _governed("table", ["bronze", "inner", "t2"]),
    ]
    asyncio.run(
        _drain(
            _destroy_subtree(
                ns,
                ["bronze"],
                descendants,
                root_uuid="uuid-bronze",
                registry=base_registry.BaseRegistry(control_root=str(tmp_path)),
                claims=_claimed(tmp_path, descendants),
                storage_options=StorageOptions(),
                estate_roots=(),
            )
        )
    )

    assert "deregister_table:bronze$pages" in ns.calls
    assert "deregister_table:bronze$inner$t2" in ns.calls
    drops = [c for c in ns.calls if c.startswith("drop_namespace:")]
    assert drops == ["drop_namespace:bronze$inner:restrict", "drop_namespace:bronze:restrict"]
    # Every table is gone before the first namespace drop — that is what makes each drop legal.
    assert max(ns.calls.index(c) for c in ns.calls if c.startswith("deregister_table:")) < ns.calls.index(drops[0])
    # And it NEVER hands `cascade` to a backend that cannot do it.
    assert not any(c.endswith(":cascade") for c in ns.calls), "the unimplementable native cascade was called"


def test_an_already_absent_child_is_DRIFT_not_an_error(tmp_path: Path) -> None:
    """A half-finished earlier delete must not make the subtree undeletable forever: a table removed before the
    cascade's deregister reached it answers TableNotFound there and no longer describes, and that is drift, never
    bytes reported left. The cascade did not remove it, so it lifts the fence it held the table's claim by."""
    from catalog.api.v1.endpoints.namespaces import _destroy_subtree  # noqa: PLC2701

    class _Vanishing(_RecordingNs):
        def describe_table(self, request: Any) -> Any:
            from lance_namespace import TableNotFoundError

            if any(call.startswith("deregister_table:") for call in self.calls):
                raise TableNotFoundError("Table not found")
            return super().describe_table(request)

        def deregister_table(self, request: Any) -> Any:
            from lance_namespace import TableNotFoundError

            self.calls.append(f"deregister_table:{'$'.join(request.id)}")
            raise TableNotFoundError("Table not found")

    ns: Any = _Vanishing()
    ghost = [_governed("table", ["bronze", "ghost"])]
    claims = _claimed(tmp_path, ghost)
    asyncio.run(
        _drain(
            _destroy_subtree(
                ns,
                ["bronze"],
                ghost,
                root_uuid="uuid-bronze",
                registry=base_registry.BaseRegistry(control_root=str(tmp_path)),
                claims=claims,
                storage_options=StorageOptions(),
                estate_roots=(),
            )
        )
    )
    assert "drop_namespace:bronze:restrict" in ns.calls, "one absent child blocked the whole cascade"
    held = location_claims.holder(claims, _location(["bronze", "ghost"]))
    assert held is not None and held.fence is None, "the cascade left its fence on a table it did not remove"
