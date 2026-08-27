"""The storage registry — every object store the estate knows, and its ROLE (R28).

The object browser used to address buckets through a hardcoded two-value union
(``Literal["images-batch", "images-batch-alto"]``) mirrored BY HAND into the lakehouse zone's
TypeScript. Two copies of one fact, in two languages, kept in step by discipline alone — and
neither said what either bucket was FOR, so no view could group stores by medallion tier without a
third hand-written table.

The list itself lives in ``service_kit.schemas.storage`` because the VIEWER validates bucket names
against the same registry this endpoint serves to the UI; a service importing another service's
endpoint module to share a fact would be a layering violation.

**Two sources, one registry.** DECLARED stores come from deployment config (``RASK_STORES``, or the
built-in defaults) and cannot be edited at runtime — they are what the chart shipped. ATTACHED stores
are added through ``POST /v1/stores`` and persist in the estate's state document. Both are served as
one list, because a caller browsing buckets does not care which mechanism registered one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body
from pydantic import TypeAdapter

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints.user_state import UserStateStoreDep
from service_kit.exceptions import ConflictError, ServiceUnavailableError, ValidationError
from service_kit.governed.user_state import ESTATE_SUBJECT, UserStateConflict, UserStateDocument, UserStateStore, UserStateUnreadable
from service_kit.schemas.storage import (
    GOVERNED_TIERS,
    Store,
    StoreRegistry,
    registered_stores,
)


router = APIRouter(prefix="/v1", tags=["stores"])

#: Compiled once — validating the persisted document is per-request work; building the validator is not.
_ATTACHED = TypeAdapter(list[Store])


async def _attached(state: UserStateStoreDep) -> list[Store]:
    """Stores attached at runtime, or ``[]`` when none have been.

    A registry that cannot be READ is not reported as absent: ``UserStateUnreadable`` becomes a 503,
    the same fail-closed rule the user documents follow. Reporting an unreadable registry as empty
    would make every attached bucket vanish from the browser during a state-store brownout, which
    reads as "someone deleted my stores" rather than "the store is down".
    """
    if state is None:
        return []
    try:
        stored = await state.get(subject=ESTATE_SUBJECT, document=UserStateDocument.ATTACHED_STORES)
    except UserStateUnreadable as exc:
        raise ServiceUnavailableError(f"the attached-store registry is unreadable: {exc}") from exc
    return [] if stored is None else _ATTACHED.validate_python(stored.value)


async def _attached_versioned(state: UserStateStore | None) -> tuple[list[Store], str | None]:
    """:func:`_attached`, plus the ETag the write must carry to detect a concurrent attach.

    Separate from `_attached` because every OTHER caller is a read that neither has nor wants a token —
    only the write path needs one, and giving the common read a second return value it discards is how
    the token ends up dropped at the one call site that needed it.
    """
    if state is None:
        return [], None
    try:
        stored, etag = await state.get_versioned(subject=ESTATE_SUBJECT, document=UserStateDocument.ATTACHED_STORES)
    except UserStateUnreadable as exc:
        raise ServiceUnavailableError(f"the attached-store registry is unreadable: {exc}") from exc
    return ([] if stored is None else _ATTACHED.validate_python(stored.value)), etag


@router.get("/stores", summary="Every registered object store")
async def list_stores(state: UserStateStoreDep) -> StoreRegistry:
    """The whole registry in one response — it is estate config, and the tier view needs all of it."""
    return StoreRegistry(stores=[*registered_stores(), *await _attached(state)])


@router.post(
    "/stores",
    status_code=201,
    summary="Attach an object store for browsing",
)
async def attach_store(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    state: UserStateStoreDep,
    store: Annotated[Store, Body(description="The store to attach; `endpoint` may name another host.")],
) -> StoreRegistry:
    """Attach a bucket so it can be BROWSED. Registers only — reads nothing, ingests nothing.

    Estate-admin gated on ``can_observe_events`` against the root object: attaching names a host and a
    bucket the whole estate will then see, which is estate-wide disclosure and so takes the estate-wide
    privilege — the same gate ``/v1/projects`` and ``/v1/events`` use.

    Names are unique across BOTH sources, and a declared store cannot be shadowed. The object browser
    resolves a store by NAME, so a duplicate would make which bucket you get depend on list order — the
    failure would look like the wrong data rather than a bad registration.

    Attached stores are forced ``read_only``. A bucket someone attached to LOOK at is not a bucket the
    cascade may write to, and nothing here can know whether the caller's credentials should carry write
    authority on another host.
    """
    await fga_deps.require_relation(client, settings, token, relation="can_observe_events", obj=settings.fga_root_object)
    if state is None:
        raise ServiceUnavailableError("no state store is configured, so a store cannot be attached")
    if not store.name.strip() or not store.bucket.strip():
        raise ValidationError("both `name` and `bucket` are required")

    # READ-MODIFY-WRITE UNDER AN ETAG. This document is ESTATE-scoped — every
    # estate admin writes the same key — so the blind read-then-write it used to do lost updates: two
    # concurrent attaches each read N stores and each wrote N+1, and whichever landed second erased the
    # other's. `UserStateStore.put`'s own docstring asserted there was "no second writer to lose a race
    # with", which is true of the per-USER documents this module was built for and false of this one.
    #
    # Retried rather than surfaced, once, because the caller cannot do anything with the conflict that
    # this loop cannot do for them: re-read, re-check the name, re-write. A second conflict means real
    # contention on an admin-frequency endpoint, and THAT is worth telling the caller about.
    attached = store.model_copy(update={"read_only": True})
    for attempt in range(2):
        existing, etag = await _attached_versioned(state)
        taken = {s.name for s in registered_stores()} | {s.name for s in existing}
        if store.name in taken:
            raise ConflictError(f"a store named {store.name!r} is already registered")
        try:
            await state.put(
                subject=ESTATE_SUBJECT,
                document=UserStateDocument.ATTACHED_STORES,
                value=_ATTACHED.dump_python([*existing, attached], mode="json"),
                etag=etag,
                concurrent=True,
            )
        except UserStateConflict:
            if attempt == 0:
                continue
            raise
        return StoreRegistry(stores=[*registered_stores(), *existing, attached])
    raise ServiceUnavailableError("the attached-store registry is being written concurrently — retry")


@router.get(
    "/stores/tiers",
    summary="Stores grouped by medallion tier",
)
async def stores_by_tier(state: UserStateStoreDep) -> dict[str, list[Store]]:
    """The tier -> store view, DERIVED from the registry rather than transcribed.

    Every governed tier appears even when empty: a bronze row with nothing in it says "no store
    backs bronze here", which is a fact worth showing. Roles outside the medallion (raw, derived,
    observability) are grouped under their own names — raw is deliberately not a tier (R23).
    """
    stores = [*registered_stores(), *await _attached(state)]
    grouped: dict[str, list[Store]] = {tier.value: [] for tier in GOVERNED_TIERS}
    for store in stores:
        grouped.setdefault(store.role.value, []).append(store)
    return grouped
