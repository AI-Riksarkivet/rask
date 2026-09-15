"""Every `/maintenance/` verb refuses a branch rather than silently acting on main.

[[LH-105]]. These four doors reclaim version history, rewrite fragments and replace indexes. None of
them can scope any of that to a branch — `open_dataset` resolves main and nothing downstream carries a
ref — so a caller who names one and is answered 200 has been told their branch was acted on when
MAIN was.

THE ESTATE HAS ALREADY PAID FOR THIS ONCE, TWICE. `indices.py:146-149` records why the spec index
doors declare `branch` only to refuse it: "The route did not accept it at all, which read as safe and
was not: a caller who asked for a branch's indices got 200 and MAIN's list." And `ff9604be` fixed the
destructive version of the same thing — a branch-targeted `drop_table_index` was destroying MAIN's
index and answering 200. The reindex door added on 2026-09-15 reintroduced the pattern on a new route
the same day, which is why this gate is written over the WHOLE family and derived from the mounted
routes rather than asserted door by door: a fifth `/maintenance/` verb inherits it without an edit.

NOT ACCEPTING A PARAMETER IS NOT REFUSING IT. FastAPI drops an undeclared query parameter silently, so
"the door takes no branch" is precisely the shape that answers 200 for a branch it ignored. The
refusal has to be declared and raised.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from catalog.api.v1.endpoints import maintenance


def _maintenance_handlers() -> list[tuple[str, Callable[..., Any]]]:
    """Every POST handler mounted under `/{id}/maintenance/`, read off the router.

    A route with no callable endpoint is DROPPED rather than carried as ``None``: it cannot be the
    defect this suite hunts, and carrying it would make every assertion below take a null check that
    reads as though the absence were a case worth handling.
    """
    found: list[tuple[str, Callable[..., Any]]] = []
    for route in maintenance.router.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if "POST" in getattr(route, "methods", set()) and "/{id}/maintenance/" in path and callable(endpoint):
            found.append((path, endpoint))
    return found


def test_the_router_actually_mounts_maintenance_verbs() -> None:
    """Without this the parametrized suite below would pass by iterating nothing."""
    assert len(_maintenance_handlers()) >= 4, f"expected preview/run/compact/reindex at minimum, found {_maintenance_handlers()}"


@pytest.mark.parametrize(("path", "handler"), _maintenance_handlers(), ids=lambda v: v if isinstance(v, str) else "")
def test_a_maintenance_verb_declares_a_branch_so_it_can_refuse_one(path: str, handler: Callable[..., Any]) -> None:
    """Declared, because an undeclared query parameter is dropped in silence rather than rejected."""
    parameters = inspect.signature(handler).parameters

    assert "branch" in parameters, f"{path} accepts no `branch`, so a caller naming one is answered 200 having acted on main"


@pytest.mark.parametrize(("path", "handler"), _maintenance_handlers(), ids=lambda v: v if isinstance(v, str) else "")
def test_a_maintenance_verb_actually_calls_the_refusal(path: str, handler: Callable[..., Any]) -> None:
    """Declaring it and then ignoring it would be the same defect with a parameter attached.

    Asserted against the shared helper by name: `refuse_a_branch_this_door_cannot_honour` is the one
    the eleven spec doors call, and a door that hand-rolled its own refusal would drift from the status
    and problem-body the rest of the estate answers with.
    """
    source = inspect.getsource(handler)

    assert "refuse_a_branch_this_door_cannot_honour" in source, f"{path} declares `branch` but never refuses it"
