"""The hierarchy's cascade invariant, pinned where the conformance run found it BROKEN.

Found on 2026-08-05 by driving the real doors, not by reading code.

- #117 the destructive CASCADE forwarded `behavior` to a `dir` backend that answers
  NamespaceNotEmpty for every casing, so with the shipped default a non-empty namespace could not be
  dropped AT ALL, and three guarded loops below it were unreachable code.
"""

from __future__ import annotations

import asyncio
from typing import Any

from lance_namespace import NamespaceNotFoundError

from service_kit.lakehouse import base_registry
from service_kit.lakehouse.location_claims import ClaimStore


#: A registry the cascade's record-forget may be pointed at: the double's drops name no location, so it is never read.
_NO_RECORDS = base_registry.BaseRegistry(control_root="/nonexistent/control")


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

        # No location, as for a declared-only table: the claim check before the drops has nothing to judge.
        return DescribeTableResponse()

    def drop_table(self, request: Any) -> Any:
        from lance_namespace import DropTableResponse

        self.calls.append(f"drop_table:{'$'.join(request.id)}")
        return DropTableResponse(id=request.id)

    def drop_namespace(self, request: Any) -> Any:
        self.calls.append(f"drop_namespace:{'$'.join(request.id)}:{(request.behavior or 'restrict').lower()}")
        return type("R", (), {"model_fields_set": set()})()


# ---------------------------------------------------------------- #118 parent must EXIST


# ---------------------------------------------------------------- #117 the cascade actually destroys


def test_the_cascade_destroys_BOTTOM_UP_and_never_asks_the_backend_to() -> None:
    """The dir backend refuses `behavior=Cascade` for every casing, so this door must do the work:
    tables first, then namespaces deepest-first, then the root — each drop hitting an EMPTY object."""
    from catalog.api.v1.endpoints.namespaces import _destroy_subtree  # noqa: PLC2701 — the unit under test

    ns: Any = _RecordingNs()
    descendants = [
        ("table", ["bronze", "pages"]),
        ("namespace", ["bronze", "inner"]),
        ("table", ["bronze", "inner", "t2"]),
    ]
    asyncio.run(_destroy_subtree(ns, ["bronze"], descendants, registry=_NO_RECORDS, claims=ClaimStore(control_root="/nonexistent/control"), delimiter="$"))

    assert "drop_table:bronze$pages" in ns.calls
    assert "drop_table:bronze$inner$t2" in ns.calls
    drops = [c for c in ns.calls if c.startswith("drop_namespace:")]
    assert drops == ["drop_namespace:bronze$inner:restrict", "drop_namespace:bronze:restrict"]
    # Every table is gone before the first namespace drop — that is what makes each drop legal.
    assert max(ns.calls.index(c) for c in ns.calls if c.startswith("drop_table:")) < ns.calls.index(drops[0])
    # And it NEVER hands `cascade` to a backend that cannot do it.
    assert not any(c.endswith(":cascade") for c in ns.calls), "the unimplementable native cascade was called"


def test_an_already_absent_child_is_DRIFT_not_an_error() -> None:
    """A half-finished earlier delete must not make the subtree undeletable forever."""
    from catalog.api.v1.endpoints.namespaces import _destroy_subtree  # noqa: PLC2701

    class _Vanishing(_RecordingNs):
        def drop_table(self, request: Any) -> Any:
            from lance_namespace import TableNotFoundError

            self.calls.append(f"drop_table:{'$'.join(request.id)}")
            raise TableNotFoundError("Table not found")

    ns: Any = _Vanishing()
    asyncio.run(
        _destroy_subtree(
            ns, ["bronze"], [("table", ["bronze", "ghost"])], registry=_NO_RECORDS, claims=ClaimStore(control_root="/nonexistent/control"), delimiter="$"
        )
    )
    assert "drop_namespace:bronze:restrict" in ns.calls, "one absent child blocked the whole cascade"
