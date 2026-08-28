"""Every viewer route that serves corpus-derived content must resolve a verified subject.

open_python-audit (P0, E1) — "25 of the viewer's 32 routes serve corpus content with no authn and no
FGA gate, including every media-byte route". Re-verified at HEAD by the independent re-audit
(8 gated / 24 not, after the clip route was gated in the fastapi drain): the primary media blob
route, every atlas/chunks/voice/graph/topics/diarization route, and `POST /graph/cypher` all served
with no subject and no checker — reachable at the edge through the gateway's `/api/explorer` rows,
on an estate whose chart defaults auth ON. The bypass was structural: the LISTING was gated while
the content behind it was not, so knowing a `doc_id` was authorization.

DENY-BY-DEFAULT IS THE GATE'S SHAPE. The audit's sharpest observation was that `router.py` mounts
every sub-router bare, "so a new route lands ungated by default". This test inverts that: it walks
the APP's dependant graph and requires every route to resolve `current_subject` — via signature or
decorator `dependencies=[...]` — unless it appears in the exemption table WITH ITS REASON. Route 33
therefore arrives gated or argued, never silently open.

THE OBJECT FOLLOWS `datasets.py`'s OWN RULE: the search ROW table is the corpus's visibility object,
and a corpus that declares no search block is DENIED under authz rather than checked against an
invented identifier ("guessing an identifier would authorize against something the catalog never
governs"). The media-byte family keeps its established object (the document binding's table — the
same one `pages.py` and `media_clip` already check).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute


#: Routes that legitimately carry no corpus gate, each with the reason it is exempt.
EXEMPT: dict[tuple[str, str], str] = {
    ("GET", "/livez"): "process liveness — dependency-free by contract",
    ("GET", "/readyz"): "readiness — lifecycle-gated, no corpus content",
    ("GET", "/api/health"): (
        "recorded ALWAYS-200 contract (the 2026-07-28 red-dot regression) — authorization is SOFT: "
        "`CorpusFactsVisible` redacts the corpus facts in-body instead of refusing the probe, pinned "
        "by test_health_badge_redacts_for_the_unentitled.py"
    ),
}


def _resolves_current_subject(route: APIRoute) -> bool:
    """Whether the route's dependant graph — signature AND decorator deps — reaches the verified subject."""
    from viewer.api import security

    target = security._deps.current_subject  # noqa: SLF001 - the one callable identity that matters

    def _walk(dependant) -> bool:
        if dependant.call is target:
            return True
        return any(_walk(sub) for sub in dependant.dependencies)

    return _walk(route.dependant)


def _app() -> FastAPI:
    """The module-level singleton — the composition the pod actually serves, not a rebuilt copy."""
    from viewer.main import app

    return app


def _routes(app: FastAPI) -> list[APIRoute]:
    """Every APIRoute, DESCENDING the `_IncludedRouter` wrappers.

    This FastAPI version keeps included routers as wrapper objects rather than flattening them into
    `app.routes` — a plain scan sees two wrappers and zero APIRoutes, and a gate written that way
    passes VACUOUSLY on an app with 24 open routes (it did, for one commit of this file's history).
    The wrapper exposes the mounted router as `original_router`.
    """
    found: list[APIRoute] = []
    stack = list(app.routes)
    while stack:
        item = stack.pop()
        if isinstance(item, APIRoute):
            found.append(item)
            continue
        inner = getattr(item, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
    assert found, "no APIRoute found at all — the wrapper layout changed and this gate is scanning nothing"
    return found


def test_every_route_is_gated_or_argued() -> None:
    app = _app()
    open_routes = []
    for route in _routes(app):
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            if (method, route.path) in EXEMPT:
                continue
            if not _resolves_current_subject(route):
                open_routes.append(f"{method} {route.path}")
    assert not open_routes, (
        "these routes serve with no verified subject in their dependant graph — knowing a doc_id is "
        "authorization on an auth-ON estate:\n  " + "\n  ".join(sorted(open_routes))
    )


def test_the_exemptions_still_exist() -> None:
    """An exemption for a deleted route is a hole waiting for a new route to fall into."""
    app = _app()
    served = {(m, r.path) for r in _routes(app) for m in (r.methods or set())}
    for method, path in EXEMPT:
        assert (method, path) in served, f"exempt route {method} {path} no longer exists — remove the exemption"
