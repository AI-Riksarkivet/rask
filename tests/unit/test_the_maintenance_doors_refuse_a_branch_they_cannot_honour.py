"""Every `/maintenance/` verb either REFUSES a branch or CARRIES it — never silently acts on main.

[[LH-105]], [[LH-019]]. These doors reclaim version history, rewrite fragments and replace indexes.
A caller who names a branch and is answered 200 for work done on MAIN has been told something false,
and that is the single defect this gate exists to prevent. Refusing is one correct answer; honouring
is the other. What is never correct is declaring `branch` and dropping it.

`maintenance/reindex` is the door that moved. It refused while `IndexWorkItem` could not carry a ref
— the door publishes and answers 202, so the WORKER opens the dataset — and now that the unit has a
`branch` and the worker checks it out, it carries one instead. The gate is written over the family
and derived from the mounted routes, so a door switching sides needs no edit here; only a door doing
NEITHER fails.

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
def test_a_maintenance_verb_either_refuses_the_branch_or_PASSES_IT_ON(path: str, handler: Callable[..., Any]) -> None:
    """Declaring it and then ignoring it is the same defect with a parameter attached.

    Two acceptable answers, checked by name. `refuse_a_branch_this_door_cannot_honour` is the shared
    refusal the spec doors call — a hand-rolled one would drift from the status and problem body the
    rest of the estate answers with. `branch=branch` is the other: the door forwards the ref to
    whatever actually opens or enqueues, which is what honouring means here.

    A door doing neither is the silent-main case, and it reads exactly like a working door.
    """
    source = inspect.getsource(handler)
    refuses = "refuse_a_branch_this_door_cannot_honour" in source
    carries = "branch=branch" in source

    assert refuses or carries, f"{path} declares `branch` and neither refuses nor forwards it — a caller naming one is answered for main"
