"""Importing annotations must not freeze the annotator's event loop.

open_fastapi-audit — "`POST /tasks/{task_id}/import` decodes a caller-supplied Arrow IPC payload and
validates every row through Pydantic inline on the event loop".

`import_annotations` is `async def`, and correctly so: it awaits four actor round-trips. But it then
called `shapes_from_ipc(payload, ...)` INLINE, and that function is fully synchronous CPU work over
the entire request body — `pa.ipc.open_stream(...).read_all()` (with an `open_file` retry, so a
file-framed payload is parsed twice), `table.to_pylist()` materialising every row as a Python dict,
per-row Pydantic construction, then a SECOND full pass building
`ShapeLike.model_validate(s.model_dump(mode="json"))` for the ontology check. None of it yields.

The annotator process has one event loop serving the whole zone, so while that ran, every other
in-flight request and the mounted `/livez` + `/readyz` probes were frozen — and the payload is
uncapped, so the stall scales with whatever a client uploads.

The reference's rule is `async def` only when the body is genuinely async-compatible; "never run
blocking code inside an `async def` — it works but kills throughput". This route cannot simply become
plain `def` (it has actor awaits), which is exactly the case the threadpool escape hatch exists for.
The right shape is already in this file's neighbours (`assist.py`, `project_events.py`) and in the
sibling Arrow route `annotations/wire.py`, which is a plain `def` and gets the threadpool for free.

MEASURED BY ELAPSED TIME, not by a tick count. Counting ticks would pass either way — a blocking
decode simply runs first and the ticker finishes afterwards, still reaching its total. Only the wall
clock separates "concurrent" from "serialized". The same reasoning is written out in
`test_actor_warmup.py`, which this mirrors deliberately.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.models import TaskState
from service_kit.exceptions import register_handlers


SUBJECT = "gina"
PROJECT_ID = "11111111-1111-1111-1111-111111111111"

#: Long enough that a serialized run is unambiguous against scheduler noise, short enough that the
#: test stays cheap. The ticker below runs for ~0.20 s alongside it.
DECODE_SECONDS = 0.30


class _Actor:
    async def get(self) -> dict[str, Any]:
        return {"state": TaskState.CLAIMED, "assignee": SUBJECT, "submitted_by": None, "task_id": "t1", "project_id": PROJECT_ID}

    async def get_draft(self) -> dict[str, Any]:
        return {"revision": 1, "shapes": [], "links": []}

    async def save_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        return {"revision": 2, **draft}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _task_id: _Actor())
    monkeypatch.setattr(tasks_ep, "_verified_project_state", lambda _p, _e: asyncio.sleep(0, result=None))

    def _slow_decode(payload: bytes, *, ontology: Any = None, taken_ids: Any = None) -> tuple[list, list]:  # noqa: ARG001
        """Stands in for the real pyarrow decode + two Pydantic passes. Sleeps, like they compute."""
        time.sleep(DECODE_SECONDS)
        return [], []

    monkeypatch.setattr(tasks_ep, "shapes_from_ipc", _slow_decode)

    async def checker(*, user: str, relation: str, obj: str) -> bool:  # noqa: ARG001
        return True

    app = FastAPI()
    register_handlers(app)
    app.include_router(tasks_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    app.state.actors_registered = True
    return app


@pytest.mark.asyncio
async def test_the_arrow_decode_does_not_serialize_the_loop(app: FastAPI) -> None:
    """The load-bearing one: other coroutines must keep running during the decode."""
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ann") as client:

        async def _import() -> httpx.Response:
            return await client.post("/tasks/t1/import", content=b"arrow-ipc-bytes")

        started = time.perf_counter()
        response, _ = await asyncio.gather(_import(), _ticker())
        elapsed = time.perf_counter() - started

    assert response.status_code == 200, response.text
    assert ticks == 20
    assert elapsed < DECODE_SECONDS + 0.15, (
        f"the import serialized with the ticker ({elapsed:.2f}s vs a {DECODE_SECONDS:.2f}s decode) — "
        f"the Arrow decode ran ON the event loop, freezing every other request and the pod's probes"
    )
