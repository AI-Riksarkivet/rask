"""ANN-13, RATIFIED: the annotator publishes under TWO planes, and this file is where that is said.

**The prefix split is a trust boundary, not an inconsistency.**

* The **edge plane** — every path under `/api` — is what the gateway publishes to the browser:
  `Route("/api/explorer/annotations", "/api/annotations", *annotator)` in
  `services/gateway/src/gateway/__init__.py`. A path here is reachable from outside the cluster.
* The **actor plane** — `/projects` and `/tasks` — has NO gateway row and must not acquire one. The
  annotator zone's SSR calls it directly, server-side and in-cluster, on `ANNOTATOR_PROJECTS_API`
  (`frontend/microfrontends/annotator/src/lib/server/doors.ts`), and `require_actor_plane` refuses
  it 503 when the Dapr actors are unregistered. The shape of a path therefore states which side of
  the boundary it is on; one uniform prefix would erase that.

**Unification is blocked at the door, not merely unattractive.** `/api/projects` belongs to the
CONTROLPLANE at the gateway (`Route(f"{prefix}/projects", …, *controlplane)`), so the annotator
cannot take it. Moving the actor plane under `/api` means inventing some third path for it, rewiring
`projects.remote.ts` and `tasks.remote.ts`, and adding a gateway row that publishes actor state at
the edge. The split costs one extra shape to read; the unification costs the boundary.

**What is pinned here:** the exact published set, and that every published path belongs to one of the
two planes. A new path, a moved path, or a router inventing a third shape fails here and has to be
argued for. Plane membership and the not-the-API exclusions are both matched on a SEGMENT boundary —
a router mounted at `/daprtools` or `/projectsx` is a new public shape, not a sidecar callback and
not the actor plane, and the assertions below see it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi import APIRouter

from annotator.main import app


#: The annotator's whole public surface. Health and the sidecar's actor callbacks are mounted
#: separately in `main.py` and are deliberately excluded — they are not the API.
PUBLISHED_PATHS = {
    "/api/annotations/tags": ["post"],
    "/api/annotations/{doc_id}/{speech_id}/{chunk_id}": ["get", "post"],
    "/api/annotations/{doc_id}/{speech_id}/{chunk_id}/versions": ["get"],
    "/api/assist/generation-schema": ["get"],
    "/api/assist/producers": ["get"],
    "/api/assist/{doc_id}/{speech_id}/{chunk_id}": ["post"],
    "/api/jobs/apply": ["post"],
    "/api/jobs/{job_id}": ["get"],
    "/projects": ["get", "post"],
    "/projects/{project_id}": ["get"],
    "/projects/{project_id}/adjudications/{group_id}": ["delete", "put"],
    "/projects/{project_id}/events": ["post"],
    "/projects/{project_id}/items": ["post"],
    "/projects/{project_id}/members": ["delete", "get", "put"],
    "/projects/{project_id}/ontology": ["patch"],
    "/projects/{project_id}/tasks": ["get"],
    "/projects/{project_id}/tasks/{task_id}": ["delete"],
    "/tasks/{task_id}": ["get"],
    "/tasks/{task_id}/draft": ["get", "put"],
    "/tasks/{task_id}/events": ["post"],
    "/tasks/{task_id}/import": ["post"],
}

#: The two planes, named. A path outside both is a THIRD, and the test below says so.
_EDGE_PLANE = ("/api",)
_ACTOR_PLANE = ("/projects", "/tasks")

#: The kubelet's probes and the sidecar's actor callbacks — mounted at the root by `make_probes_router`
#: and `DaprActor`, and not part of the API this file governs.
_NOT_THE_API = ("/actors", "/dapr", "/healthz", "/livez", "/readyz")


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    """Membership on a SEGMENT boundary. A bare `startswith` lets a new shape inherit an exemption it
    only shares characters with — `/daprtools` reading as the sidecar's `/dapr`, `/projectsx` as the
    actor plane — which is the one thing this gate exists to notice."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _published() -> dict[str, list[str]]:
    served = app.openapi()["paths"]
    return {path: sorted(served[path]) for path in served if not _under(path, _NOT_THE_API)}


def _strays() -> list[str]:
    return sorted(path for path in _published() if not _under(path, _EDGE_PLANE + _ACTOR_PLANE))


def test_the_published_surface_is_exactly_what_is_recorded_here() -> None:
    """A new route in a new shape has to be argued for, not merely added."""
    assert _published() == PUBLISHED_PATHS


def test_every_published_path_belongs_to_one_of_the_two_known_planes() -> None:
    """The two planes, as a live assertion: there are exactly two, and there must not be a third."""
    assert _strays() == [], f"these paths belong to neither the edge-published `/api` plane nor the internal actor plane: {_strays()}"


@contextmanager
def _also_serving(path: str) -> Iterator[None]:
    """Mount one extra route on the real app, exactly as an eighth router would, then take it back off."""
    stray = APIRouter()
    stray.add_api_route(path, lambda: {}, methods=["GET"])
    before = list(app.router.routes)
    schema = app.openapi_schema
    app.include_router(stray)
    app.openapi_schema = None
    try:
        yield
    finally:
        app.router.routes[:] = before
        app.openapi_schema = schema


@pytest.mark.parametrize("path", ["/v2/thing", "/daprtools/run", "/readyzz", "/projectsx/y", "/tasksx/y", "/actorsx/y"])
def test_a_third_shape_is_refused_however_it_is_spelled(path: str) -> None:
    """A new shape must not be excused by merely SHARING CHARACTERS with a probe, a callback or a plane."""
    with _also_serving(path):
        assert _strays() == [path]


def test_the_exclusions_still_excuse_what_they_are_for() -> None:
    """Tightening the match must not start counting the probes and the actor callbacks as API."""
    served = set(app.openapi()["paths"])
    assert {"/healthz", "/livez", "/readyz", "/dapr/config"} <= served
    assert not {path for path in served if path.startswith("/actors/")} & set(_published())
    assert {"/healthz", "/livez", "/readyz", "/dapr/config"}.isdisjoint(_published())
