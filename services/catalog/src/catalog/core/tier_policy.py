"""The tier judge: the one decision every catalog write door asks before it writes (ADR 0156 C8, ADR 0160).

A table's tier is the reserved namespace property ``rask.tier``, declared once at namespace create by the
project's admin. It is never read from a name. The judge owns the write decision: it asks the rung the route
declares, and only then reads the tier, so a caller without the rung learns nothing about the namespace path
it named. Every decision is audited with the tier it was taken under.

The judge answers allow or refuse today. The tier's own rules land with the rows that need them: the run and
inputs on silver and gold (LH-225) and the hold behind ``published`` (LH-466). A staged manifest's content is judged
by ``catalog.services.managed_versions`` (LH-471).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Final

from fastapi.concurrency import run_in_threadpool
from lance_namespace import DescribeNamespaceRequest, InternalError, InvalidInputError, LanceNamespace, NamespaceNotFoundError
from pydantic import BaseModel, ConfigDict

from catalog.core.provenance_guard import RESERVED_METADATA_PREFIXES, is_reserved_key
from catalog.services import native
from service_kit.governed.audit import ALLOW, audit
from service_kit.schemas.storage import GOVERNED_TIERS


log = logging.getLogger(__name__)

#: The reserved namespace property that declares a tier. `rask.` is the platform's prefix
#: (`core/provenance_guard.py`), and this is the one key in it a client may name, and only an admin.
TIER_PROPERTY: Final = "rask.tier"

TIERS: Final = frozenset(tier.value for tier in GOVERNED_TIERS)

#: Ask one rung, raising the spec's typed refusal on a denial. ``None`` when FGA is off.
type RungCheck = Callable[["Rung"], Awaitable[None]]


class Rung(BaseModel):
    """One relation asked on one FGA object."""

    model_config = ConfigDict(frozen=True)

    relation: str
    obj: str


class Write(BaseModel):
    """What a write door is about to do, named by its route.

    ``op`` is the route suffix (``insert``, ``create``, ``rename``) and ``obj`` the object written. ``rung``
    is what the route maps declare for it, asked on the parent namespace for a create; it is ``None`` only
    for a top-level create the deployment leaves open (``fga_lock_root_create`` off). ``table`` is the table
    the write lands in, which for a rename is the destination.
    """

    model_config = ConfigDict(frozen=True)

    op: str
    obj: str
    rung: Rung | None
    table: tuple[str, ...]


class Judgement(BaseModel):
    """An allowed write and the tier it was judged under (``None``: no namespace above it declares one)."""

    model_config = ConfigDict(frozen=True)

    tier: str | None


def declared_tier(properties: Mapping[str, str] | None) -> str | None:
    """The tier a namespace's ``properties`` declare, or ``None``.

    Raises:
        InvalidInputError: The value names no tier, or another key sits under a prefix only the platform
            writes. ``rask.tier`` is the one such key a namespace create may name.
    """
    if not properties:
        return None
    reserved = sorted(key for key in properties if is_reserved_key(key) and key != TIER_PROPERTY)
    if reserved:
        raise InvalidInputError(
            f"namespace properties under {', '.join(p + '*' for p in RESERVED_METADATA_PREFIXES)} are the platform's; refusing {', '.join(reserved)}"
        )
    if TIER_PROPERTY not in properties:
        return None
    tier = properties[TIER_PROPERTY]
    if tier not in TIERS:
        raise InvalidInputError(f"{TIER_PROPERTY}={tier!r} names no tier; a tier is one of {sorted(TIERS)}")
    return tier


async def declared_on(ns: LanceNamespace, namespace: Sequence[str]) -> str | None:
    """The tier namespace ``namespace`` itself declares, or ``None`` (a namespace that does not exist declares none)."""
    try:
        described = await run_in_threadpool(native.call, ns, "describe_namespace", DescribeNamespaceRequest(id=list(namespace)))
    except NamespaceNotFoundError:
        return None
    tier = (described.properties or {}).get(TIER_PROPERTY)
    if tier is not None and tier not in TIERS:
        # Only the create door writes the key and it refuses this value, so a stored one is not a caller's
        # mistake. Allowing would judge the write under no tier at all.
        raise InternalError(f"namespace {list(namespace)!r} stores {TIER_PROPERTY}={tier!r}, which names no tier")
    return tier


async def tier_of(ns: LanceNamespace, namespace: Sequence[str]) -> str | None:
    """The tier of the namespace path ``namespace``: the shallowest declaration on it, or ``None``.

    Shallowest first, because a deeper namespace may only repeat its ancestor's tier (the create door
    refuses a different one), so the first declaration found is the answer and a top-level tier
    namespace costs one describe. A namespace that does not exist declares nothing; the door that
    needs it answers its own 404.
    """
    for depth in range(1, len(namespace) + 1):
        if (tier := await declared_on(ns, namespace[:depth])) is not None:
            return tier
    return None


async def judge(op: Write, *, caller: str | None, check: RungCheck | None, namespace: Callable[[], Awaitable[LanceNamespace]]) -> Judgement:
    """Allow ``op`` or refuse it with the spec's typed error.

    ``check`` asks the rung (``None`` with FGA off, when no rung exists to ask). ``namespace`` yields the
    connection the table's namespace path resolves through, and is only awaited once the rung allowed.

    Raises:
        PermissionDeniedError: The caller lacks the rung the route declares.
        UnauthenticatedError: FGA is on and nobody is signed in (raised by ``check``).
    """
    if check is not None and op.rung is not None:
        await check(op.rung)
    tier = await tier_of(await namespace(), op.table[:-1]) if len(op.table) > 1 else None
    audit("tier_judge", ALLOW, subject=caller, resource=op.obj, op=op.op, tier=tier or "untiered")
    return Judgement(tier=tier)
