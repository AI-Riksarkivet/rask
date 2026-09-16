"""A door that mints a table version must record WHO minted it.

[[LH-018]], unblocked by the owner's R1 acknowledgement 2026-09-16. The row's own re-measure found
that most of its Closes-when should not be built — two of the three ops it names answer 406
`UnsupportedOperationError` on the dir backend, `create_table_version` is already FGA-gated at the same
rung the governed `/commit` door uses, "protection" is a DELETION control that applies to neither, and
advertising `managed_versioning=true` would invite clients onto a catalog-mediated commit pointer,
which is the Iceberg shape CLAUDE.md's permanent LANCE-ONLY ruling exists to avoid.

**What survives that reading is one sentence: ONE live op mints a version and emits no lineage.**
`POST /v1/table/{id}/version/create` moves a manifest into the table's version slot — a write, and the
only door in this module that is one — and `versions.py` contains no `emit_measured_write` at all. So a
version minted through the spec's own door leaves no provenance, which is condition 1 failing on the
door the spec says to use.

ROUTE-DERIVED, not a list of names, and the classification is the point: every route in the module must
be decided about, so a NINTH version route cannot be added without someone saying which kind it is.
`batch_create_table_versions` is deliberately in the NO column — it is 406 on this backend, so attaching
provenance to it is decoration, and the row says so.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, cast

import pytest

from catalog.api.v1.endpoints import versions


#: Doors that MOVE a manifest into a version slot — a write, and therefore provenance.
_MINTS_A_VERSION = ("/v1/table/{id}/version/create",)

#: Reads, deletes and the 406 batch door. A delete is governed elsewhere (`require_not_protected` is the
#: deletion control); a read mints nothing; `batch-create` and `batch-commit` answer
#: `UnsupportedOperationError` on the dir backend, so an emit there would describe work that never happened.
_MINTS_NO_VERSION = (
    "/v1/table/{id}/history",
    "/v1/table/version/batch-create",
    "/v1/table/batch-commit",
    "/v1/table/{id}/version/list",
    "/v1/table/{id}/version/describe",
    "/v1/table/{id}/version/delete",
)


def _routes() -> dict[str, Callable[..., Any]]:
    """`path` and `endpoint` live on `APIRoute`, not on the `BaseRoute` the router is typed with, and the
    estate forbids the `# type: ignore` that would paper over that — so the cast is narrowed to exactly
    the two attributes this file reads."""
    found: dict[str, Callable[..., Any]] = {}
    for route in versions.router.routes:
        if hasattr(route, "endpoint") and hasattr(route, "path"):
            found[cast("str", route.path)] = cast("Callable[..., Any]", route.endpoint)
    return found


def test_every_version_route_is_classified() -> None:
    """A ninth route must be decided about rather than inheriting whichever answer is convenient."""
    found = set(_routes())
    classified = set(_MINTS_A_VERSION) | set(_MINTS_NO_VERSION)

    assert found == classified, f"unclassified version routes {found - classified}; stale entries {classified - found}"


@pytest.mark.parametrize("path", _MINTS_A_VERSION)
def test_a_door_that_mints_a_version_emits_lineage(path: str) -> None:
    source = inspect.getsource(_routes()[path])

    assert "emit_measured_write" in source, (
        f"{path} mints a table version and emits no lineage — a version created through the spec's own door "
        "leaves no record of who created it, which is condition 1 failing on the door the spec says to use"
    )


@pytest.mark.parametrize("path", _MINTS_A_VERSION)
def test_the_emitting_door_carries_the_caller_it_records(path: str) -> None:
    """An emit with no caller records a write by nobody. The trailer needs the token AND the raw bearer:
    the first is who the catalog authenticated, the second is what the lineage door re-authorizes."""
    signature = inspect.signature(_routes()[path])

    for parameter in ("token", "emitter", "authorization"):
        assert parameter in signature.parameters, f"{path} cannot emit as its caller — no {parameter!r} parameter"


@pytest.mark.parametrize("path", _MINTS_NO_VERSION)
def test_a_door_that_mints_nothing_stays_quiet(path: str) -> None:
    """The other half. An emit from a read or from a 406 door writes provenance for work that did not
    happen, which is worse than silence — it is a false record."""
    source = inspect.getsource(_routes()[path])

    assert "emit_measured_write" not in source, f"{path} mints no version and emits lineage anyway — that is a provenance record for work that did not happen"
