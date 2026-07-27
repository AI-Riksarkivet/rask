"""``/v1/user-state/*`` — a signed-in person's own work, stored per subject on the Dapr state store.

The media zone's workflow canvas and its saved views were ``localStorage``, so the same person on another
machine (or after clearing site data) found an empty canvas. These routes give both documents a home the
estate owns; :mod:`common.user_state` holds the store client and the key rules, and
:mod:`common.schemas.workflow` mirrors the zone's valibot schemas so a document round-trips unchanged.

**Why the catalog owns this.** Only two app-ids are in the state store's ``scopes``
(``chart/values.yaml`` → ``stateStore.scopes``): ``annotator`` and ``catalog``. Of those two, only the
catalog has a verified subject to key on — ``services/annotator/main.py`` builds no ``OIDCVerifier`` and
``services/annotator/api/dependencies.py`` re-exports only ``AuthorDep``, whose ``get_author``
(``common/deps.py``) reads a **trusted ``X-User`` header** and defaults to ``"anon"``. Per-subject storage
keyed on a client-settable header is the exact leak these routes exist to prevent. The catalog instead has
``api/security.py``'s ``CurrentToken``, the router-wide ``Depends(authorize)`` that 401s an anonymous
caller, and — the reachability half — a BFF proxy the media zone already uses
(``routes/capi/v1/me/+server.ts`` → ``makeCatalogProxy``), which forwards the signed-in user's bearer and
nothing else.

**Authenticated-only, and self-scoped by construction.** There is no FGA object here because there is no
shared resource: the only thing a caller can address is their own document, and *which* document is a
closed enum baked into the URL path, never an identity. ``/v1/user-state`` matches no resource prefix in
``fga_deps.authorize`` and carries no ``{id}`` path param, so the router-wide gate contributes exactly its
401/503 pre-checks — the ``/v1/me`` precedent. The 401 floor is asserted here as well, so it also holds
with FGA off. Not audited, for the same reason ``/v1/me`` is not: the #41 trail records authorization
DECISIONS, and there is no decision to record when the resource is definitionally the caller's own.

**``response_model_exclude_none`` is load-bearing, not tidiness.** The zone parses these documents with
valibot, where an edge's ``targetHandle`` is ``v.optional(v.string())`` — absent is fine, ``null`` is a
parse FAILURE that would drop the whole graph back to a seeded canvas. ``SearchSpec`` is likewise written
under ``exactOptionalPropertyTypes`` as ``T | undefined``. Omitting nulls is what keeps the round trip
lossless; ``exists`` carries the had-it-or-not signal that a nullable ``value`` would otherwise have to.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated

from common.schemas.workflow import SavedView, WorkflowGraph
from common.user_state import UserStateDocument, UserStateStore, UserStateUnreadable
from fastapi import APIRouter, Depends, Request, Response, status
from lance_namespace import (
    InvalidInputError,
    InvalidTableStateError,
    ServiceUnavailableError,
    UnauthenticatedError,
)
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from catalog.api.dependencies import SettingsDep
from catalog.api.security import CurrentToken


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/user-state", tags=["user-state"])

#: One adapter per document, built at import (they compile a validator — not per-request work).
_GRAPH = TypeAdapter(WorkflowGraph)
_VIEWS = TypeAdapter(list[SavedView])


def get_user_state_store(request: Request) -> UserStateStore | None:
    """The store client built in the app lifespan, or ``None`` if it was never wired."""
    return getattr(request.app.state, "user_state", None)


UserStateStoreDep = Annotated[UserStateStore | None, Depends(get_user_state_store)]


class UserStateEnvelope[T](BaseModel):
    """One document as served: who owns it, which document, whether it was ever written, and it.

    ``subject`` is echoed from the VERIFIED token — it is what makes a cross-user leak visible in a
    response body, rather than only in a diff of two documents that happen to look alike.
    """

    subject: str
    document: UserStateDocument
    exists: bool
    updated_at: datetime | None = None
    value: T | None = None


def _subject(token: CurrentToken) -> str:
    """The caller's verified subject, or 401. The ONLY identity source these routes have.

    With OIDC off there is no token and therefore no subject: per-subject state without a subject is not a
    degraded mode, it is a contradiction, so this 401s rather than inventing a shared bucket.
    """
    if token is None:
        raise UnauthenticatedError("authentication required")
    return token.sub


def _require_store(store: UserStateStore | None) -> UserStateStore:
    """Fail closed when the store was never wired — never read an absent store as an empty document."""
    if store is None:
        raise ServiceUnavailableError("user state store is not available")
    return store


async def _fetch[T](
    store: UserStateStore | None,
    token: CurrentToken,
    document: UserStateDocument,
    adapter: TypeAdapter[T],
) -> tuple[str, datetime | None, T | None]:
    """``(subject, updated_at, value)`` for the caller's document; ``value`` is ``None`` when absent.

    "Absent" here means genuinely never written. A record that EXISTS but cannot be read is a 409, never a
    200 saying ``exists: false`` — the earlier version reported both as absent, and the difference is a
    user's work: told the document is empty, the client seeds a fresh canvas and its next autosave
    overwrites the record that was still there. Verified live before this changed, by altering one field of
    a real row from ``str`` to ``int`` (an ordinary schema evolution): the row was intact and the API said
    ``exists: false``.

    Raises:
        InvalidTableStateError (409): The stored record exists and is unreadable — unparseable at the envelope
            (:class:`UserStateUnreadable`) or at the document schema. The message names the fix, because
            the user's escape is an explicit overwrite, not a silent one.
    """
    subject = _subject(token)
    try:
        stored = await _require_store(store).get(subject=subject, document=document)
    except UserStateUnreadable as exc:
        raise InvalidTableStateError(
            f"your saved {document.value} exists but cannot be read ({exc.reason}). It has NOT been "
            f"changed. Delete it to start fresh, or ask an operator to recover it."
        ) from exc
    if stored is None:
        return subject, None, None
    try:
        return subject, stored.updated_at, adapter.validate_python(stored.value)
    except ValidationError as exc:
        # Same hazard one layer in: the envelope parsed but the document no longer fits its schema.
        log.exception("user_state_stale_schema", extra={"document": document.value})
        raise InvalidTableStateError(
            f"your saved {document.value} exists but no longer fits its schema. It has NOT been changed. "
            f"Delete it to start fresh, or ask an operator to recover it."
        ) from exc


async def _store_document(
    store: UserStateStore | None,
    token: CurrentToken,
    settings: SettingsDep,
    document: UserStateDocument,
    payload: JsonValue,
) -> tuple[str, datetime]:
    """Persist the caller's document, refusing anything past the per-document byte ceiling."""
    subject = _subject(token)
    size = len(json.dumps(payload).encode())
    if size > settings.user_state_max_bytes:
        raise InvalidInputError(f"{document.value} document is {size} bytes, over the {settings.user_state_max_bytes}-byte per-document limit")
    stored = await _require_store(store).put(subject=subject, document=document, value=payload)
    return subject, stored.updated_at


async def _erase(store: UserStateStore | None, token: CurrentToken, document: UserStateDocument) -> Response:
    """Drop the caller's document. Idempotent — deleting what was never written is still a 204."""
    await _require_store(store).delete(subject=_subject(token), document=document)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── the workflow canvas ──────────────────────────────────────────────────────────────────────────────


@router.get("/workflow-graph", response_model_exclude_none=True)
async def get_workflow_graph(token: CurrentToken, store: UserStateStoreDep) -> UserStateEnvelope[WorkflowGraph]:
    """The caller's saved workflow canvas, or ``exists: false`` if they have never saved one."""
    document = UserStateDocument.WORKFLOW_GRAPH
    subject, updated_at, value = await _fetch(store, token, document, _GRAPH)
    return UserStateEnvelope[WorkflowGraph](
        subject=subject,
        document=document,
        exists=value is not None,
        updated_at=updated_at,
        value=value,
    )


@router.put("/workflow-graph", response_model_exclude_none=True)
async def put_workflow_graph(graph: WorkflowGraph, token: CurrentToken, store: UserStateStoreDep, settings: SettingsDep) -> UserStateEnvelope[WorkflowGraph]:
    """Save the caller's workflow canvas, replacing whatever they had."""
    document = UserStateDocument.WORKFLOW_GRAPH
    payload = graph.model_dump(mode="json", by_alias=True, exclude_none=True)
    subject, updated_at = await _store_document(store, token, settings, document, payload)
    return UserStateEnvelope[WorkflowGraph](subject=subject, document=document, exists=True, updated_at=updated_at, value=graph)


@router.delete("/workflow-graph", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow_graph(token: CurrentToken, store: UserStateStoreDep) -> Response:
    """Discard the caller's saved canvas."""
    return await _erase(store, token, UserStateDocument.WORKFLOW_GRAPH)


# ── saved views ──────────────────────────────────────────────────────────────────────────────────────


@router.get("/saved-views", response_model_exclude_none=True)
async def get_saved_views(token: CurrentToken, store: UserStateStoreDep) -> UserStateEnvelope[list[SavedView]]:
    """The caller's named read-plane selections, in the order the client stored them."""
    document = UserStateDocument.SAVED_VIEWS
    subject, updated_at, value = await _fetch(store, token, document, _VIEWS)
    return UserStateEnvelope[list[SavedView]](
        subject=subject,
        document=document,
        exists=value is not None,
        updated_at=updated_at,
        value=value,
    )


@router.put("/saved-views", response_model_exclude_none=True)
async def put_saved_views(views: list[SavedView], token: CurrentToken, store: UserStateStoreDep, settings: SettingsDep) -> UserStateEnvelope[list[SavedView]]:
    """Replace the caller's saved-view list wholesale — the client owns the ordering and the dedupe."""
    document = UserStateDocument.SAVED_VIEWS
    payload: JsonValue = [v.model_dump(mode="json", by_alias=True, exclude_none=True) for v in views]
    subject, updated_at = await _store_document(store, token, settings, document, payload)
    return UserStateEnvelope[list[SavedView]](subject=subject, document=document, exists=True, updated_at=updated_at, value=views)


@router.delete("/saved-views", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_views(token: CurrentToken, store: UserStateStoreDep) -> Response:
    """Discard the caller's saved views."""
    return await _erase(store, token, UserStateDocument.SAVED_VIEWS)
