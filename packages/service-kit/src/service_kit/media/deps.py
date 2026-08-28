"""FastAPI dependency wrappers — the seam between the app and the routers.

Routers depend on these instead of capturing closures or touching ``app.state``.
``StateDep`` hands a router the per-app :class:`AppState`; the search group's
encoder deps live in ``search.api.dependencies`` (each group carries its own).
Tests override via ``app.dependency_overrides``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request

from service_kit.media.state import AppState


def get_state(request: Request) -> AppState:
    """The resources built in lifespan, stashed on ``app.state``."""
    return request.app.state.resources


StateDep = Annotated[AppState, Depends(get_state)]

#: Optional ``?dataset=`` query param addressing a registry dataset (None = default).
DatasetParam = Annotated[str | None, Query(description="Dataset id (default DB when omitted).")]


# `get_author` / `AuthorDep` (the trusted `X-User` header) lived here and are DELETED. The
# docstring's own promise — "at merge, lance-ns's auth swaps this for the VERIFIED token subject" —
# is the change that removed them: the annotator's write routes now take `CurrentSubject`
# (annotator/api/security.py), which is `anon` with OIDC off and the verified `sub` with it on,
# with deliberately no header fallback. The gateway additionally strips `x-user` at the public edge
# (`_CLIENT_SPOOFABLE`), so the header means nothing anywhere. Pinned by
# `tests/unit/test_annotator_governed_auth.py::test_the_header_seam_itself_is_GONE`.
