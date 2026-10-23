"""A catalog that cannot sign takes no write it would announce ([[XC-095]]).

Every write the catalog commits is announced after the commit, signed with the catalog's own key: its lineage event
(`catalog.core.lineage_emit`) and its control events (`service_kit.control_emit`). A holder without its key withholds
both, so a write it took would stay committed and be told to nobody: not lineage's feed, not the edge list, not a
pipeline following them, not a person. While the key is unresolved the catalog therefore refuses every write it would
announce, ServiceUnavailable before the commit, classifying a write the way the read-only window does
(`maintenance_mode.is_mutating`, fail-closed). Its readiness says why meanwhile (`signing_ready_check`), and a missing
key is re-read every 15 s (`service_kit.governed.signing_key`), so the refusal ends on its own once the store answers.

THE WRITES IT STILL TAKES ARE THE ONES IT NEVER ANNOUNCES: a data write under the provenance namespace, lineage's own
record of the announcements, which the lineage emitter never announces (`provenance_namespace.is_provenance`). Those four
data doors announce through the lineage emitter alone; every other write also emits a control event, which needs the key
whatever namespace it names.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from lance_namespace import ErrorCode

from catalog.api.maintenance_mode import GATED_MOUNTS, PROBLEM_JSON, is_mutating, is_read_vend
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.services.provenance_namespace import is_provenance
from service_kit.governed import fga
from service_kit.governed.signing_key import signing_of
from service_kit.lakehouse.ns_errors import problem_body


#: The data doors that announce through the lineage emitter alone, so a write through one under the provenance namespace
#: announces nothing at all.
_LINEAGE_ONLY_DATA_ACTIONS = frozenset({"insert", "merge_insert", "update", "delete"})
#: How often the holder re-reads a missing key, and so how soon a refused caller may retry.
RETRY_AFTER_SECONDS = 15


def announces(path: str, settings: Settings | None) -> bool:
    """Whether the write at ``path`` is one the catalog announces: every write but a lineage-only data write under the
    provenance namespace. The parent is joined as the emitter joins it. Without the settings no write is exempt."""
    parts = path.strip("/").split("/")
    if settings is None or len(parts) != 4 or parts[:2] != ["v1", "table"] or parts[3] not in _LINEAGE_ONLY_DATA_ACTIONS:
        return True
    segments = parse_identifier(parts[2])
    return not is_provenance(fga.canonical_object_id(segments[:-1]), settings)


def unsigned_response() -> JSONResponse:
    """The refusal: the spec's ServiceUnavailable as problem+json, with how soon the key is read again. Why the key is
    missing is the readiness check's to say, not every caller's to read."""
    return JSONResponse(
        status_code=503,
        content=problem_body(
            ErrorCode.SERVICE_UNAVAILABLE,
            status=503,
            title="SigningUnavailable",
            slug="signing",
            detail="The catalog cannot sign the announcement this write would make; retry once its signing key is back.",
        ),
        media_type=PROBLEM_JSON,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


async def signing_gate_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Refuse a write the catalog would announce while it holds no signing key."""
    holder = signing_of(request.app)
    # The path routing matches, never `request.url.path`: that re-parses the decoded path as a URL, so a `?` in a name
    # would end the path there and the action read would be a segment of the name ([[LH-526]]).
    path: str = request.scope["path"]
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if (
        holder is not None
        and not holder.ready
        and path.startswith(GATED_MOUNTS)
        and is_mutating(request.method, path)
        and not is_read_vend(path, request)
        and announces(path, settings)
    ):
        return unsigned_response()
    return await call_next(request)
