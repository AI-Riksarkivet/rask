"""A catalog that cannot sign takes no write it would announce ([[XC-095]]), nor one whose run id it cannot derive.

Every write the catalog commits is announced after the commit, signed with the catalog's own key: its lineage event
(`catalog.core.lineage_emit`) and its control events (`lakehouse.events.control_emit`). A holder without its key withholds
both, so a write it took would stay committed and be told to nobody: not lineage's feed, not the edge list, not a
pipeline following them, not a person. While the key is unresolved the catalog therefore refuses every write it would
announce, ServiceUnavailable before the commit, classifying a write the way the read-only window does
(`maintenance_mode.is_mutating`: by the route it matches, never by its path text, and fail-closed). Its readiness says why meanwhile (`signing_ready_check`), and a missing
key is re-read every 15 s (`lakehouse.governed.signing_key`), so the refusal ends on its own once the store answers.
The commit id key (`catalog.core.commit_ids`, [[LH-497]]) is held to the same rule: an announcement names its commit by the
run id derived under it, so a write the catalog cannot name that way is refused before its commit as well.

THE WRITES IT STILL TAKES ARE THE ONES IT NEVER ANNOUNCES: a data write under the provenance namespace, lineage's own
record of the announcements, which the lineage emitter never announces (`provenance_namespace.is_provenance`). Those four
data doors announce through the lineage emitter alone; every other write also emits a control event, which needs the key
whatever namespace it names.
"""

from typing import cast

from fastapi import Request
from fastapi.responses import JSONResponse
from lance_namespace import ErrorCode, InternalError

from catalog.api.dependencies import app_wire
from catalog.api.maintenance_mode import GATED_MOUNTS, fault_response, is_mutating
from catalog.api.route_path import route_path
from catalog.core.commit_ids import STATE as COMMIT_IDS_STATE
from catalog.core.commit_ids import CommitIds
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.services.provenance_namespace import is_provenance
from lakehouse.governed import fga_ids
from lakehouse.governed.signing_key import SIGNING_STATE, SigningKeyHolder
from lakehouse.problems.envelope import problem_response
from lakehouse.process.probes import ReadyCheck
from lakehouse.schemas.health import Readiness, ReadinessStatus


#: The data doors that announce through the lineage emitter alone, so a write through one under the provenance namespace
#: announces nothing at all.
_LINEAGE_ONLY_DATA_ACTIONS = frozenset({"insert", "merge_insert", "update", "delete"})
#: How often the holder re-reads a missing key, and so how soon a refused caller may retry.
RETRY_AFTER_SECONDS = 15


def announces(path: str, settings: Settings) -> bool:
    """Whether the write at ``path`` is one the catalog announces: every write but a lineage-only data write under the
    provenance namespace. The parent is joined as the emitter joins it."""
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[:2] != ["v1", "table"] or parts[3] not in _LINEAGE_ONLY_DATA_ACTIONS:
        return True
    segments = parse_identifier(parts[2])
    return not is_provenance(fga_ids.canonical_object_id(segments[:-1]), settings)


def unsigned_response() -> JSONResponse:
    """The refusal: the spec's ServiceUnavailable as problem+json, with how soon the key is read again. Why the key is
    missing is the readiness check's to say, not every caller's to read."""
    return problem_response(
        ErrorCode.SERVICE_UNAVAILABLE,
        status=503,
        title="SigningUnavailable",
        slug="signing",
        detail="The catalog cannot sign the announcement this write would make; retry once its signing key is back.",
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def commit_ids_of(request: Request) -> CommitIds:
    """The commit id key holder the lifespan wired (`catalog.main._build`): the key holder, or the empty key for a stack
    with no secret store. Never the empty key for a catalog that wired none, whose run ids every producer could then
    compute ([[LH-497]], [[XC-219]]).

    Raises:
        InternalError: The lifespan wired no holder.
    """
    # The holder is one of two classes (`CommitIds`), which no single `type` names; `app_wire` checks no kind anyway.
    return cast("CommitIds", app_wire(request, COMMIT_IDS_STATE, object))


def _unready(request: Request) -> bool:
    """Whether this catalog holds no signing key, or no commit id key, now. A catalog that does not sign is wired the
    ``None`` holder (`lakehouse.governed.signing_key.attach_signing`); one wired no holder at all faults.

    Raises:
        InternalError: The lifespan wired no signing holder or no commit id key holder.
    """
    holder = app_wire(request, SIGNING_STATE, SigningKeyHolder, none_is_off=True)
    # An announcement also needs its commit's run id, derived under a key read from the store ([[LH-497]]).
    return (holder is not None and not holder.ready) or not commit_ids_of(request).ready


def unsigned_refusal(request: Request) -> JSONResponse | None:
    """The refusal of a write the catalog would announce while it holds no signing key or no commit id key, or ``None``
    to let the request through (the signing gate, :class:`catalog.api.refusal_gate.RefusalGate`).

    A catalog whose lifespan wired either holder not at all, or no settings, cannot say whether it could sign, so each
    such write is refused as the server fault it is (500, the spec's InternalError, with :func:`app_wire`'s ERROR line
    naming the wire), never admitted unsigned and never told to retry: no re-read of a key mends a wire nobody set.
    """
    try:
        unready, fault = _unready(request), None
    except InternalError as exc:
        unready, fault = True, exc
    # The path routing matches, never `request.url.path`: that re-parses the decoded path as a URL, so a `?` in a name
    # would end the path there and the action read would be a segment of the name ([[LH-526]]).
    path = route_path(request.scope)
    if not (unready and path.startswith(GATED_MOUNTS) and is_mutating(request)):
        return None
    try:
        if not announces(path, app_wire(request, "settings", Settings)):
            return None
    except InternalError as exc:
        fault = fault or exc
    return fault_response(fault) if fault is not None else unsigned_response()


def commit_ids_ready_check(inner: ReadyCheck) -> ReadyCheck:
    """A `/readyz` check adding the commit id key to ``inner``: a catalog without it is `unhealthy` (503), as one
    without its signing key is, so it leaves its Service while `/livez` stays 200. A catalog wired no holder answers the
    probe with the server fault :func:`commit_ids_of` raises, which takes it out of its Service as well."""

    async def check(request: Request) -> Readiness:
        body = await inner(request)
        holder = commit_ids_of(request)
        if holder.ready:
            return body
        return body.model_copy(update={"status": ReadinessStatus.unhealthy, "components": {**body.components, "commit_ids": holder.reason}})

    return check
