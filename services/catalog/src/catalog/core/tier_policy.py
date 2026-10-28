"""The tier judge: the one decision every catalog write door asks before it writes (ADR 0156 C8, ADR 0160).

A table's tier is the reserved namespace property ``rask.tier``, declared once at namespace create by the
project's admin. It is never read from a name. The judge owns the write decision: it asks the rung the route
declares, and only then reads the tier, so a caller without the rung learns nothing about the namespace path
it named. Every decision is audited with the tier it was taken under.

A write that adds data to a silver or gold table names the run that produced it and the inputs it read
(:mod:`catalog.core.run_context`, ADR 0156 C2): a catalog door reads them from the request's Context and stamps them
into its commit, a stock client writes them into its staged manifest. A door that reads no run from the request's
Context (``update``, ``add_columns``), or that attaches bytes written elsewhere (``register``), is refused on those tiers,
because its commit would name no run. Every named input, on any tier, names a table the committer may read
(``can_read_data``): naming a table one cannot read as an input would launder it into the derived table's lineage, and an
erasure names a derived table by what its commits declared (ADR 0183). The judge answers allow or refuse before a write; a version
that breaches the project's quality gate is held when it is published, by the publish door (``catalog.services.promotions``,
ADR 0161). A staged manifest's content is judged by ``catalog.services.managed_versions`` (LH-471), which hands its run here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Final, Literal

from fastapi.concurrency import run_in_threadpool
from lance_namespace import DescribeNamespaceRequest, InternalError, InvalidInputError, LanceNamespace, NamespaceNotFoundError, UnsupportedOperationError
from pydantic import BaseModel, ConfigDict

from catalog.core.provenance_guard import RESERVED_METADATA_PREFIXES, is_reserved_key
from catalog.core.run_context import INPUTS_HEADER, INPUTS_PROPERTY, RUN_ID_HEADER, RUN_ID_PROPERTY, RunContext
from catalog.services import native
from service_kit.governed.audit import AuditAction, AuditOutcome, audit


log = logging.getLogger(__name__)

#: The reserved namespace property that declares a tier. `rask.` is the platform's prefix
#: (`core/provenance_guard.py`), and this is the one key in it a client may name, and only an admin.
TIER_PROPERTY: Final = "rask.tier"

#: The governed tiers, the medallion's three (R23: raw is the external world, never a tier).
TIERS: Final = frozenset({"bronze", "silver", "gold"})

#: The tiers whose data is derived, so every write adding data to them names its run and inputs.
DERIVED_TIERS: Final = frozenset({"silver", "gold"})

#: The write doors that add data and stamp the request's run into their commit.
STAMPING_DOORS: Final = frozenset({"create", "insert", "merge_insert"})

#: The write doors that land data and commit no run: ``update`` and ``add_columns`` read none from the request's Context,
#: and ``register`` attaches bytes written elsewhere ([[LH-463]]).
UNSTAMPABLE_DATA_DOORS: Final = frozenset({"update", "add_columns", "register"})

#: Ask one rung, raising the spec's typed refusal on a denial. ``None`` when FGA is off.
type RungCheck = Callable[["Rung"], Awaitable[None]]

#: Refuse a run whose inputs do not each name a table the committer may read, with the spec's typed refusal.
type InputsCheck = Callable[[RunContext], Awaitable[object]]


class Named(BaseModel):
    """A table or namespace by its name: the object its grants are held by is what that name resolves to (ADR 0173 §8)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["table", "namespace"]
    segments: tuple[str, ...]
    #: The door acts on an object the trash may hold under this name (undrop, tasks), so a name no live object holds
    #: resolves to the trashed one's uuid.
    in_trash: bool = False


class Rung(BaseModel):
    """One relation asked on one object: a table or namespace by its name, or an object no name resolves to (a warehouse)."""

    model_config = ConfigDict(frozen=True)

    relation: str
    on: Named | str


class Write(BaseModel):
    """What a write door is about to do, named by its route.

    ``op`` is the route suffix (``insert``, ``create``, ``rename``) and ``obj`` the object written, by its name, for the
    audit. ``rung`` is what the route maps declare for it, asked on the parent namespace for a create; it is ``None``
    only for a top-level create the deployment leaves open (``fga_lock_root_create`` off). ``table`` is the table the
    write lands in, which for a rename is the destination.
    """

    model_config = ConfigDict(frozen=True)

    op: str
    obj: str
    rung: Rung | None
    table: tuple[str, ...]
    #: The run the request's Context names.
    run: RunContext | None = None
    #: The table a rename moves, whose tier the destination's is compared with.
    moved_from: tuple[str, ...] = ()


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


async def judge(
    op: Write, *, caller: str | None, check: RungCheck | None, inputs: InputsCheck, namespace: Callable[[str], Awaitable[LanceNamespace]]
) -> Judgement:
    """Allow ``op`` or refuse it with the spec's typed error.

    ``check`` asks the rung (``None`` with FGA off, when no rung exists to ask). ``inputs`` judges the inputs a stamping
    door's run names. ``namespace`` yields the connection a top-level namespace resolves through, and is only awaited
    once the rung allowed.

    A rename commits nothing, so no run can reach it: one that moves a table into a silver or gold namespace from a
    namespace of another tier is refused, because it would place data in the derived tier that no run produced.

    Raises:
        PermissionDeniedError: The caller lacks the rung the route declares.
        UnauthenticatedError: FGA is on and nobody is signed in (raised by ``check``).
    """
    if check is not None and op.rung is not None:
        await check(op.rung)
    tier = await _tier_of_table(namespace, op.table)
    if op.moved_from and tier in DERIVED_TIERS and (source := await _tier_of_table(namespace, op.moved_from)) != tier:
        raise InvalidInputError(
            f"rename refused: moving a {source or 'untiered'} table into a {tier} namespace places data no run produced; "
            f"write it there with the run that derives it"
        )
    if op.op in UNSTAMPABLE_DATA_DOORS and tier in DERIVED_TIERS:
        raise UnsupportedOperationError(
            f"{op.op} on a {tier} table is refused: it cannot carry the run and inputs a {tier} write names into a commit; "
            "write the rows with create, insert or merge_insert and the run in the request's Context"
        )
    if op.op in STAMPING_DOORS:
        await judge_run(tier, op.run, inputs=inputs, names=f"the request's Context headers {RUN_ID_HEADER} and {INPUTS_HEADER}")
    audit(AuditAction.TIER_JUDGE, AuditOutcome.ALLOW, subject=caller, resource=op.obj, op=op.op, tier=tier or "untiered")
    return Judgement(tier=tier)


async def _tier_of_table(namespace: Callable[[str], Awaitable[LanceNamespace]], table: Sequence[str]) -> str | None:
    """The tier of the namespace path table ``table`` lies in, read through its top-level namespace's connection."""
    return await tier_of(await namespace(table[0]), table[:-1]) if len(table) > 1 else None


async def judge_run(tier: str | None, run: RunContext | None, *, inputs: InputsCheck, names: str) -> None:
    """Refuse a data write to a derived tier that names no run and inputs, and any write naming an input that is not a
    table the committer may read.

    ``names`` says where the caller puts the run, for the refusal.

    Raises:
        InvalidInputError: ``tier`` is silver or gold and the write names no run, or no input; or a named input is not a
            table the committer may read (raised by ``inputs``).
    """
    if tier in DERIVED_TIERS and (run is None or not run.inputs):
        raise InvalidInputError(f"commit refused: a write to a {tier} table names its run ({RUN_ID_PROPERTY}) and its inputs ({INPUTS_PROPERTY}) in {names}")
    if run is not None and run.inputs:
        await inputs(run)
