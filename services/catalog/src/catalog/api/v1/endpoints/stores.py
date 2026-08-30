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

# The SPEC taxonomy, not `service_kit.exceptions` — the catalog's clients dispatch on the Lance
# numeric `code`, which only `install_problem_handlers`'s translation of lance_namespace errors
# carries. The fleet import stood here twice (catalog-api-01, then members.py repeated it as RV-03);
# `test_catalog_api_speaks_the_spec_taxonomy.py` now closes the class.
from lance_namespace import ConcurrentModificationError, InvalidInputError, NamespaceAlreadyExistsError, ServiceUnavailableError
from pydantic import TypeAdapter

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, SettingsDep, UserStateStoreDep
from catalog.api.security import CurrentToken
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


async def _attached(state: UserStateStore | None) -> list[Store]:
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
async def list_stores(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    state: UserStateStoreDep,
) -> StoreRegistry:
    """The whole registry in one response — it is estate config, and the tier view needs all of it.

    ESTATE-ADMIN GATED, on the same relation and object ``attach_store`` uses. Its docstring already
    states the reason and it applies to the read at least as strongly: a store record names a host
    and a bucket the whole estate would then see, which is estate-wide disclosure — and reading the
    list IS the disclosure, where attaching is only the cause of it. Ungated, any authenticated
    principal (any project member) enumerated every bucket the estate knows, including the
    ``endpoint`` hosts of third-party buckets someone attached.

    The router-level ``authorize`` cannot cover this: it authorizes ``{id}`` routes under
    ``_RESOURCES`` and lets id-less collection routes through on authentication alone, which is right
    for a listing the endpoint then FILTERS (``/v1/warehouses``, ``/v1/model``) and wrong for one it
    does not. This registry is estate-wide config with no per-item FGA object to filter on, so the
    honest gate is the estate rung rather than a per-store one.
    """
    await fga_deps.require_relation(client, settings, token, relation="can_observe_events", obj=settings.fga_root_object)
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
        raise InvalidInputError("both `name` and `bucket` are required")

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
            raise NamespaceAlreadyExistsError(f"a store named {store.name!r} is already registered")
        try:
            await state.put(
                subject=ESTATE_SUBJECT,
                document=UserStateDocument.ATTACHED_STORES,
                value=_ATTACHED.dump_python([*existing, attached], mode="json"),
                etag=etag,
                concurrent=True,
            )
        except UserStateConflict as exc:
            if attempt == 0:
                continue
            # Translated, not re-raised: UserStateConflict subclasses the FLEET's ConflictError, so a
            # bare raise here exits through register_handlers as a 4-key 409 — the seventh
            # DomainError-shaped exit the audit never named. Precedent projects.py/warehouses.py:
            # a lost etag race is the spec's ConcurrentModification, code 23.
            raise ConcurrentModificationError("the attached-store registry changed under two concurrent attaches; retry") from exc
        return StoreRegistry(stores=[*registered_stores(), *existing, attached])
    # Unreachable by construction (every path through the loop returns, raises, or continues at
    # attempt 0) — kept because the function is declared `-> StoreRegistry` and ty needs it total.
    raise ConcurrentModificationError("the attached-store registry is being written concurrently — retry")


@router.get(
    "/stores/tiers",
    summary="Stores grouped by medallion tier",
)
async def stores_by_tier(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    state: UserStateStoreDep,
) -> dict[str, list[Store]]:
    """The tier -> store view, DERIVED from the registry rather than transcribed.

    Gated identically to ``list_stores`` — it is the same disclosure in a different shape, and gating
    only the flat view would leave the grouped one as the way around it.

    Every governed tier appears even when empty: a bronze row with nothing in it says "no store
    backs bronze here", which is a fact worth showing. Roles outside the medallion (raw, derived,
    observability) are grouped under their own names — raw is deliberately not a tier (R23).
    """
    await fga_deps.require_relation(client, settings, token, relation="can_observe_events", obj=settings.fga_root_object)
    stores = [*registered_stores(), *await _attached(state)]
    grouped: dict[str, list[Store]] = {tier.value: [] for tier in GOVERNED_TIERS}
    for store in stores:
        grouped.setdefault(store.role.value, []).append(store)
    return grouped
