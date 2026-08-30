"""ANN-13 — the annotator publishes under TWO shapes, and nothing said so.

`annotations/router.py` and `assist.py` declare `/api`, `jobs.py` declares `/api/jobs`, and
`projects.py` / `project_events.py` / `members.py` / `tasks.py` declare bare `/projects` and
`/tasks` with no `/api` at all. Four prefix shapes across seven constructors, and no single place
that states the service's public surface — so an eighth router could invent a fifth shape with
nothing to notice.

**This file does not resolve the split; it makes it visible and gated.** The split is not obviously
an accident: `/api/*` is the media plane the gateway publishes at the edge (`/api/explorer/
annotations` → `/api/annotations`, `services/gateway/__init__.py`), while `/projects` and `/tasks`
are the actor plane, which has NO gateway row and needs none — the annotator zone's SSR calls this
service directly on `ANNOTATOR_PROJECTS_API`
(`frontend/microfrontends/annotator/src/lib/server/doors.ts`). Unifying them is a wire change for
that zone's `projects.remote.ts` / `tasks.remote.ts` and for whatever gateway row lands with it, so
it is an owner's call rather than a refactor.

What is pinned until that call is made: the exact published set. Adding a path in a fifth shape, or
moving one of these, fails here and has to be argued for.
"""

from __future__ import annotations

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

#: The two shapes, named. A path outside both is a THIRD, and the test below says so.
_EDGE_PLANE = "/api/"
_ACTOR_PLANE = ("/projects", "/tasks")


def _published() -> dict[str, list[str]]:
    served = app.openapi()["paths"]
    return {path: sorted(served[path]) for path in served if not path.startswith(("/actors", "/dapr", "/healthz", "/livez", "/readyz"))}


def test_the_published_surface_is_exactly_what_is_recorded_here() -> None:
    """A new route in a new shape has to be argued for, not merely added."""
    assert _published() == PUBLISHED_PATHS


def test_every_published_path_belongs_to_one_of_the_two_known_planes() -> None:
    """The finding, as a live assertion: there are two shapes, and there must not be a third."""
    stray = sorted(path for path in _published() if not (path.startswith(_EDGE_PLANE) or path.startswith(_ACTOR_PLANE)))
    assert stray == [], f"these paths belong to neither the edge-published `/api` plane nor the internal actor plane: {stray}"
