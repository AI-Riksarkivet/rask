"""FastAPI dependency wrappers — the seam between the app and the routers.

Routers depend on these instead of capturing closures or touching ``app.state``.
``StateDep`` hands a router the per-app :class:`AppState`; the search group's
encoder deps live in ``search.api.dependencies`` (each group carries its own).
Tests override via ``app.dependency_overrides``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Query, Request

from common.state import AppState


def get_state(request: Request) -> AppState:
    """The resources built in lifespan, stashed on ``app.state``."""
    return request.app.state.resources


StateDep = Annotated[AppState, Depends(get_state)]

#: Optional ``?dataset=`` query param addressing a registry dataset (None = default).
DatasetParam = Annotated[str | None, Query(description="Dataset id (default DB when omitted).")]


def get_author(x_user: Annotated[str | None, Header(alias="X-User")] = None) -> str:
    """The write author — the per-user identity seam. Today a trusted ``X-User`` header
    (dev / gateway-injected); at merge, lance-ns's auth swaps this for the VERIFIED token
    subject (OpenFGA keys on it) — the downstream stamping of `reviewer` stays the same.
    Defaults to ``anon`` so writes always carry an author."""
    return (x_user or "").strip() or "anon"


AuthorDep = Annotated[str, Depends(get_author)]
