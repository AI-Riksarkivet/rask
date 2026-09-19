"""The spec prefixes serve SPEC OPERATIONS and nothing else — a shrinking allowlist ([[LH-021]]).

`test_spec_conformance.py` asserts one direction: every spec operation has a served route. That
direction cannot see the defect this row is about, which is the OTHER one — rask-only routes mounted
on the spec's own prefixes, where a spec client meets them. Lakekeeper separates `/management` from
`/catalog` for exactly this reason; rask has not, and the audit
(`docs/audits/lakehouse-2026-09/lance-conformance-and-build-rules.md` §4) listed the ~25 groups as
PROSE. A list nothing evaluates is a list that gets longer.

SO THIS IS A RATCHET, not a pass/fail. `_STILL_ON_THE_SPEC_SURFACE` holds every violation that exists
today; the gate fails on anything NOT in it, and fails again when an entry in it stops being true. A
route may leave the set only by moving to the management prefix — never by being added back, and never
by the set being edited to match a regression. The estate already uses this shape for a closed set
that must shrink (`_HIERARCHY_EDGE_WRITERS`, `_SANCTIONED_TUPLE_WRITERS`).

WHY THE SPEC SIDE IS `SPEC_ROUTES` AND NOT THE VENDORED YAML. The set is committed in
`service_kit.lakehouse.spec_routes` precisely because no image carries `lance_docs/`, and
`test_spec_conformance.py` re-derives it from the vendored spec on every run — so it is the one
authority that is both importable here and provably equal to the document.

PATH PARAMETER NAMES ARE COLLAPSED to `{}`, the same normalisation the conformance suite applies: the
spec and this estate need not agree on what a path parameter is CALLED, only on the route's shape.
"""

from __future__ import annotations

import re
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.lakehouse.spec_routes import SPEC_ROUTES


_METHODS = frozenset({"get", "put", "post", "delete", "patch"})

#: The prefixes the spec owns. Everything mounted under one of these is answerable to the spec;
#: everything else in the catalog is rask's own surface and this gate says nothing about it.
_SPEC_PREFIXES = ("/v1/namespace", "/v1/table", "/v1/materialized_view", "/v1/transaction")

#: THE VIOLATIONS THAT EXIST TODAY, measured rather than transcribed from the audit. Each is a
#: rask-only operation a spec client meets on a spec prefix. They leave this set by moving to the
#: management API — nothing else removes an entry, and an entry that stops being true fails the gate
#: below rather than being quietly correct.
_STILL_ON_THE_SPEC_SURFACE: frozenset[tuple[str, str]] = frozenset()

#: PERMANENT, JUSTIFIED DEVIATIONS — a SEPARATE set because they are not debt and will never move.
#:
#: The Lance namespace spec defines `count_rows` and `tags/list` as POST at every tag from v0.9.0 to
#: v0.12.0. But the REST client pylance BUNDLES (`rust/lance-namespace-impls/src/rest.rs`) calls
#: `get_json` for exactly those two, and the reference SERVER it bundles mounts them as GET — the lance
#: repo disagrees with its own document on both sides of the wire, and `lance_namespace.connect("rest",
#: …)` resolves to that class. rask dual-mounts so the stock client works; mounting POST only gave it
#: FastAPI's 405, which carries no `code` and surfaced as `InternalError 18`.
#:
#: So these are not rask-only verbs a spec client should never meet — they are SPEC operations reached
#: by the method the shipped client actually sends. Moving them to `/management/v1` would break the
#: stock client, and deleting them would too. Recording them as violations would make this gate
#: permanently un-closable and teach a reader that the remaining count is debt when it is not.
#: `tests/integration/test_spec_method_and_status_conformance.py` (A3) holds the behaviour; the real
#: fix is upstream, a one-line change in lance, and this set shrinks when that lands.
_METHOD_ALIASES_THE_STOCK_CLIENT_SENDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/table/{}/count_rows"),  # spec: POST
        ("GET", "/v1/table/{}/tags/list"),  # spec: POST
    }
)


def _served(client: TestClient) -> set[tuple[str, str]]:
    """(METHOD, structural path) for every route the app serves, read from its OPENAPI.

    Not `app.routes`: starlette 1.3's lazy `include_router` keeps included routes off it, so a walk
    there sees a fraction of the surface and reports the rest as absent — the same reason
    `test_spec_conformance.py` reads the OpenAPI.

    `client.app` is typed as the ASGI protocol, which has no `openapi`; the cast says what the fixture
    actually builds rather than silencing the checker.
    """
    app = cast(FastAPI, client.app)
    paths: dict[str, Any] = app.openapi().get("paths", {})
    return {(method.upper(), re.sub(r"\{[^}]+\}", "{}", path)) for path, item in paths.items() for method in item if method.lower() in _METHODS}


def _on_the_spec_surface(routes: set[tuple[str, str]]) -> set[tuple[str, str]]:
    return {(method, path) for method, path in routes if path.startswith(_SPEC_PREFIXES)}


def test_no_NEW_rask_route_appears_on_a_spec_prefix(client: TestClient) -> None:
    """The ratchet's forward half: a route added to a spec prefix must be a spec operation."""
    intruders = _on_the_spec_surface(_served(client)) - SPEC_ROUTES - _STILL_ON_THE_SPEC_SURFACE - _METHOD_ALIASES_THE_STOCK_CLIENT_SENDS

    assert not intruders, (
        f"{len(intruders)} rask-only operation(s) were added to the SPEC surface, where a spec client "
        f"meets them:\n  " + "\n  ".join(f"{m} {p}" for m, p in sorted(intruders)) + "\n"
        "Mount them on the management prefix instead. This set only shrinks."
    )


def test_every_recorded_violation_is_still_real(client: TestClient) -> None:
    """The ratchet's backward half, and the half that keeps the set honest.

    Without it the allowlist is write-only: a route could move to the management API and its entry
    would linger, so the next reader could not tell how much of this row is left — and a REGRESSION
    onto a path that happened to be listed would pass.
    """
    served = _on_the_spec_surface(_served(client))
    stale = _STILL_ON_THE_SPEC_SURFACE - served

    assert not stale, f"{len(stale)} recorded violation(s) no longer exist — remove them from `_STILL_ON_THE_SPEC_SURFACE`:\n  " + "\n  ".join(
        f"{m} {p}" for m, p in sorted(stale)
    )


def test_the_allowlist_and_the_spec_do_not_overlap() -> None:
    """A spec operation listed as a violation would make the gate un-closable: moving it off the spec
    surface is exactly what `test_spec_conformance.py` forbids."""
    overlap = _STILL_ON_THE_SPEC_SURFACE & SPEC_ROUTES

    assert not overlap, f"these are SPEC operations and must not be recorded as violations: {sorted(overlap)}"


def test_every_method_alias_is_still_served(client: TestClient) -> None:
    """The justified set gets the same backward check as the violation set, for the same reason.

    An alias recorded here but no longer mounted would silently widen what the forward gate permits,
    and the stock client would be broken with nothing saying so — this set is small and permanent,
    which is exactly the condition under which nobody re-reads it.
    """
    missing = _METHOD_ALIASES_THE_STOCK_CLIENT_SENDS - _on_the_spec_surface(_served(client))

    assert not missing, f"the stock client sends these and the catalog no longer answers them: {sorted(missing)}"


def test_the_two_sets_are_disjoint() -> None:
    """A route cannot be both debt to move and a permanent deviation — the first would demand it leave
    the spec surface and the second forbids it, so an entry in both makes the gate un-closable."""
    assert not (_STILL_ON_THE_SPEC_SURFACE & _METHOD_ALIASES_THE_STOCK_CLIENT_SENDS)
