"""The hierarchy's two load-bearing invariants, pinned where the conformance run found them BROKEN.

Both defects were found on 2026-08-05 by driving the real doors, not by reading code — which is the
point: each had unit coverage of the guard's own logic while the guard was simply absent from the
door that mattered.

- #118 the ARROW create door (`data.py`) never called `require_parent` at all — it lives in a
  different file from the three doors that did — and `require_parent` itself only counted identifier
  SEGMENTS, never checking that the parent namespace was real. So a real Lance dataset landed inside
  a namespace that does not exist, with a live owner grant and no `parent` edge: the cascade-invisible
  orphan the guard's docstring exists to prevent.
- #117 the destructive CASCADE forwarded `behavior` to a `dir` backend that answers
  NamespaceNotEmpty for every casing, so with the shipped default a non-empty namespace could not be
  dropped AT ALL, and three guarded loops below it were unreachable code.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from catalog.api import fga_deps
from lance_namespace import InvalidInputError, NamespaceNotFoundError


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

    def drop_table(self, request: Any) -> Any:
        self.calls.append(f"drop_table:{'$'.join(request.id)}")
        return type("R", (), {"model_fields_set": set()})()

    def drop_namespace(self, request: Any) -> Any:
        self.calls.append(f"drop_namespace:{'$'.join(request.id)}:{(request.behavior or 'restrict').lower()}")
        return type("R", (), {"model_fields_set": set()})()


# ---------------------------------------------------------------- #118 parent must EXIST


def test_a_table_whose_namespace_does_not_exist_is_REFUSED() -> None:
    """The defect verbatim: POST /v1/table/ghostns$t9/create returned 200 and wrote a real dataset.
    The parent must be real, not merely NAMED."""
    ns: Any = _RecordingNs(existing_namespaces=set())
    with pytest.raises(NamespaceNotFoundError, match="parent namespace 'ghostns' does not exist"):
        asyncio.run(fga_deps.require_parent_exists(ns, "table", ["ghostns", "t9"], delimiter="$"))
    assert ns.calls == ["namespace_exists:ghostns"], "the existence check never ran"


def test_a_table_in_a_REAL_namespace_passes() -> None:
    ns: Any = _RecordingNs(existing_namespaces={"bronze"})
    asyncio.run(fga_deps.require_parent_exists(ns, "table", ["bronze", "pages"], delimiter="$"))
    assert ns.calls == ["namespace_exists:bronze"]


def test_the_segment_rule_still_bites_BEFORE_the_round_trip() -> None:
    """A single-segment table has no parent to check — refuse without paying for a native call."""
    ns: Any = _RecordingNs()
    with pytest.raises(InvalidInputError, match="has no namespace to belong to"):
        asyncio.run(fga_deps.require_parent_exists(ns, "table", ["rootless"], delimiter="$"))
    assert ns.calls == [], "a nonexistent parent was probed for an identifier that names none"


def test_the_refusal_NAMES_the_fix() -> None:
    """A 404 that does not say which rung is missing costs the caller the same debugging session
    the guard was written to save."""
    ns: Any = _RecordingNs()
    with pytest.raises(NamespaceNotFoundError) as err:
        asyncio.run(fga_deps.require_parent_exists(ns, "table", ["a", "b", "t"], delimiter="$"))
    detail = str(err.value)
    assert "a$b" in detail and "POST /v1/namespace/a$b/create" in detail
    assert "invisible to every cascade" in detail


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
    asyncio.run(_destroy_subtree(ns, ["bronze"], descendants))

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
    asyncio.run(_destroy_subtree(ns, ["bronze"], [("table", ["bronze", "ghost"])]))
    assert "drop_namespace:bronze:restrict" in ns.calls, "one absent child blocked the whole cascade"
