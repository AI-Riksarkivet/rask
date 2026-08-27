"""The details fan-out must be paged, and `include` must be a closed set.

open_fastapi-audit — "`GET /projects/{project_id}/tasks?include=details` fans out one Dapr actor
round trip per task with no page parameter and no project-level task ceiling".

The route takes `project_id`, `checker`, `subject` and `include` — no `limit`, no `cursor`, no
`offset`. The project actor's `list_tasks` returns the ENTIRE index, and `include=details` then does
one `_task_proxy(tid).get()` per task id.

THE SEMAPHORE IS NOT THE BOUND, and this is the distinction the finding turns on. `Semaphore(16)`
bounds CONCURRENCY, not COUNT — this route is the exemplar ANN-03 tells other routes to copy, so that
finding explicitly does not cover it and its fix is already applied here. Sixteen at a time, ten
thousand times, is still ten thousand round trips.

NOTHING CAPS THE COLLECTION EITHER. The send door caps one call at 1000 items, but that is PER SEND;
tasks accumulate one per sent item per consensus replica, forever, for the life of an annotation
project — and `consensus_n` multiplies each item into several. So the growth is unbounded in exactly
the way `pagination.md` says a list endpoint over a growing collection must not be.

`include: str | None` is also an open string where a closed set belongs: `?include=detials` silently
returns the bare index instead of 422, and the caller sees an answer with no `details` key and no
error to explain it.

WHAT MUST NOT CHANGE: `may_publish` is computed inside the actor from the FULL index, so slicing the
details fan-out cannot affect it. The test pins that, because a page that quietly changed the publish
precondition would be a far worse bug than the one being fixed.
"""

from __future__ import annotations

import pytest
from annotator.api.v1.endpoints import project_events as ep
from fastapi.routing import APIRoute


def _param(name: str):
    for route in ep.router.routes:
        if isinstance(route, APIRoute) and route.path.endswith("/tasks") and "GET" in (route.methods or set()):
            for field in route.dependant.query_params:
                if field.name == name:
                    return field
            return None
    pytest.fail("no GET .../tasks route on the project_events router")


def test_the_details_fanout_takes_a_page_limit() -> None:
    field = _param("limit")
    assert field is not None, (
        "the tasks route declares no `limit`, so `include=details` does one actor round trip per task "
        "for a collection that grows one row per sent item per consensus replica, forever"
    )
    limits = {type(c).__name__: c for c in field.field_info.metadata}
    assert "Ge" in limits and limits["Ge"].ge >= 1
    assert "Le" in limits and limits["Le"].le <= 200


def test_the_route_offers_a_cursor() -> None:
    assert _param("cursor") is not None, (
        "a limit with no cursor makes the tail of a project's tasks unreachable rather than merely "
        "slow — the caller can see the first page and nothing beyond it"
    )


def test_include_is_a_closed_set_not_a_bare_string() -> None:
    """`?include=detials` must 422, not silently return the index with no details and no explanation."""
    field = _param("include")
    assert field is not None
    annotation = str(field.field_info.annotation)
    assert "Literal" in annotation or "Enum" in annotation, (
        f"`include` is typed {annotation} — a typo returns the bare index instead of a 422, so the "
        f"caller cannot tell a misspelling from a project with no details"
    )


@pytest.mark.asyncio
async def test_the_page_bounds_the_ACTOR_ROUND_TRIPS_not_just_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slicing after the gather would still make every call. This is the assertion that catches that.

    A response trimmed to `limit` while the handler still asked every actor would look identical to a
    correct fix from the outside — same body, same page — and would have fixed nothing at all.
    """
    calls: list[str] = []
    total = 500

    class _ProjectActor:
        async def list_tasks(self) -> dict[str, object]:
            return {"tasks": {f"t{i:04d}": "claimed" for i in range(total)}, "may_publish": True, "total": total}

        async def get(self) -> dict[str, object]:
            return {"state": "labeling"}

    class _TaskActor:
        def __init__(self, task_id: str) -> None:
            self._id = task_id

        async def get(self) -> dict[str, object]:
            calls.append(self._id)
            return {"task_id": self._id, "state": "claimed"}

    monkeypatch.setattr(ep, "_project_proxy", lambda _p: _ProjectActor())
    monkeypatch.setattr(ep, "_task_proxy", _TaskActor)

    async def _allow(**_kw: object) -> bool:
        return True

    result = await ep.list_project_tasks(project_id="p1", checker=_allow, subject="gina", include="details", limit=100, cursor=None)

    assert len(calls) == 100, f"the handler made {len(calls)} actor round trips for a 100-row page"
    assert len(result["details"]) == 100
    assert result["next_cursor"] == "t0099"

    # THE PRECONDITION MUST NOT MOVE WITH THE PAGE. `may_publish` is computed by the actor from the
    # full index; a page that changed it would be a far worse bug than the one being fixed.
    assert result["may_publish"] is True
    assert result["total"] == total


@pytest.mark.asyncio
async def test_the_cursor_reaches_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A limit with no working cursor hides the rest of the project behind the first page."""

    class _ProjectActor:
        async def list_tasks(self) -> dict[str, object]:
            return {"tasks": {f"t{i:04d}": "claimed" for i in range(5)}, "may_publish": False}

        async def get(self) -> dict[str, object]:
            return {"state": "labeling"}

    class _TaskActor:
        def __init__(self, task_id: str) -> None:
            self._id = task_id

        async def get(self) -> dict[str, object]:
            return {"task_id": self._id, "state": "claimed"}

    monkeypatch.setattr(ep, "_project_proxy", lambda _p: _ProjectActor())
    monkeypatch.setattr(ep, "_task_proxy", _TaskActor)

    async def _allow(**_kw: object) -> bool:
        return True

    first = await ep.list_project_tasks(project_id="p", checker=_allow, subject="g", include="details", limit=2, cursor=None)
    assert [d["task_id"] for d in first["details"]] == ["t0000", "t0001"]

    second = await ep.list_project_tasks(project_id="p", checker=_allow, subject="g", include="details", limit=2, cursor=first["next_cursor"])
    assert [d["task_id"] for d in second["details"]] == ["t0002", "t0003"]

    last = await ep.list_project_tasks(project_id="p", checker=_allow, subject="g", include="details", limit=2, cursor=second["next_cursor"])
    assert [d["task_id"] for d in last["details"]] == ["t0004"]
    assert last["next_cursor"] is None, "the final page must not advertise another"
