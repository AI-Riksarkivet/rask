"""The write judge: the one decision every catalog write door asks before it writes (ADR 0156 C8, ADR 0211).

It asks the rung the route declares, and only then reads the namespace's run rule, so a caller without the rung
learns nothing about the namespace path it named. Every decision is audited with the rule it was taken under.

THE RUN RULE. A namespace may declare the reserved property ``rask.requires_run = "true"``, once, at create, by the
project's admin; every namespace beneath it inherits it, and nothing reads it from a name. A write that adds data to a
table under such a namespace names the run that produced it and the inputs it read (:mod:`catalog.core.run_context`,
ADR 0156 C2): a catalog door reads them from the request's Context and stamps them into its commit, a stock client
writes them into its staged manifest. A door that reads no run from the request's Context (``update``,
``add_columns``), or that attaches bytes written elsewhere (``register``), is refused there, because its commit would
name no run. Every named input, under the rule or not, names a table the committer may read (``can_read_data``): naming
a table one cannot read as an input would launder it into the derived table's lineage, and an erasure names a derived
table by what its commits declared (ADR 0183). The judge answers allow or refuse before a write. A staged manifest's content is judged by ``catalog.services.managed_versions`` (LH-471), which hands its run
here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Final, Literal

from fastapi.concurrency import run_in_threadpool
from lance_namespace import DescribeNamespaceRequest, InternalError, InvalidInputError, LanceNamespace, NamespaceNotFoundError, UnsupportedOperationError
from pydantic import BaseModel, ConfigDict

from catalog.core import native
from catalog.core.provenance_guard import RESERVED_METADATA_PREFIXES, is_reserved_key
from catalog.core.run_context import INPUTS_HEADER, INPUTS_PROPERTY, RUN_ID_HEADER, RUN_ID_PROPERTY, RunContext
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit


log = logging.getLogger(__name__)

#: The reserved namespace property that declares the run rule. `rask.` is the platform's prefix
#: (`core/provenance_guard.py`), and this is the one key in it a client may name, and only an admin.
REQUIRES_RUN_PROPERTY: Final = "rask.requires_run"

#: The one value it takes: a namespace that does not require a run declares nothing.
REQUIRED: Final = "true"

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
    for a vend, whose rungs the vend door has already asked. ``table`` is the table the
    write lands in, which for a rename is the destination.
    """

    model_config = ConfigDict(frozen=True)

    op: str
    obj: str
    rung: Rung | None
    table: tuple[str, ...]
    #: The run the request's Context names.
    run: RunContext | None = None
    #: The table a rename moves, whose run rule the destination's is compared with.
    moved_from: tuple[str, ...] = ()


class Judgement(BaseModel):
    """An allowed write, and whether a namespace above its table requires a run."""

    model_config = ConfigDict(frozen=True)

    requires_run: bool


def declares_requires_run(properties: Mapping[str, str] | None) -> bool:
    """Whether a namespace's ``properties`` declare the run rule.

    Raises:
        InvalidInputError: ``rask.requires_run`` holds another value than ``"true"``, or another key sits under a prefix
            only the platform writes. ``rask.requires_run`` is the one such key a namespace create may name.
    """
    if not properties:
        return False
    reserved = sorted(key for key in properties if is_reserved_key(key) and key != REQUIRES_RUN_PROPERTY)
    if reserved:
        raise InvalidInputError(
            f"namespace properties under {', '.join(p + '*' for p in RESERVED_METADATA_PREFIXES)} are the platform's; refusing {', '.join(reserved)}"
        )
    if REQUIRES_RUN_PROPERTY not in properties:
        return False
    if properties[REQUIRES_RUN_PROPERTY] != REQUIRED:
        raise InvalidInputError(
            f"{REQUIRES_RUN_PROPERTY}={properties[REQUIRES_RUN_PROPERTY]!r} is refused: it is {REQUIRED!r}, or the namespace declares nothing"
        )
    return True


async def declared_on(ns: LanceNamespace, namespace: Sequence[str]) -> bool:
    """Whether namespace ``namespace`` itself declares the run rule (a namespace that does not exist declares nothing)."""
    try:
        described = await run_in_threadpool(native.call, ns, "describe_namespace", DescribeNamespaceRequest(id=list(namespace)))
    except NamespaceNotFoundError:
        return False
    value = (described.properties or {}).get(REQUIRES_RUN_PROPERTY)
    if value is not None and value != REQUIRED:
        # Only the create door writes the key and it refuses this value, so a stored one is not a caller's
        # mistake. Allowing would judge the write under no rule at all.
        raise InternalError(f"namespace {list(namespace)!r} stores {REQUIRES_RUN_PROPERTY}={value!r}, which the create door refuses")
    return value is not None


async def requires_run(ns: LanceNamespace, namespace: Sequence[str]) -> bool:
    """Whether the namespace path ``namespace`` requires a run: any namespace on it, the shallowest first, declares it.

    The walk starts at the name's first segment, its warehouse's own row, which no door gives the rule, so a namespace
    directly in a warehouse costs two describes. A namespace that does not exist declares nothing; the door that needs
    it answers its own 404.
    """
    for depth in range(1, len(namespace) + 1):
        if await declared_on(ns, namespace[:depth]):
            return True
    return False


async def judge(
    op: Write, *, caller: str | None, check: RungCheck | None, inputs: InputsCheck, namespace: Callable[[str], Awaitable[LanceNamespace]]
) -> Judgement:
    """Allow ``op`` or refuse it with the spec's typed error.

    ``check`` asks the rung (``None`` with FGA off, when no rung exists to ask). ``inputs`` judges the inputs a stamping
    door's run names. ``namespace`` yields the connection of the warehouse a name's first segment names, and is only
    awaited once the rung allowed.

    A rename commits nothing, so no run can reach it: one that moves a table into a namespace requiring a run from one
    that does not is refused, because it would place data there that no run produced.

    Raises:
        PermissionDeniedError: The caller lacks the rung the route declares.
        UnauthenticatedError: FGA is on and nobody is signed in (raised by ``check``).
    """
    if check is not None and op.rung is not None:
        await check(op.rung)
    required = await _requires_run_for(namespace, op.table)
    try:
        await _judge_under_the_run_rule(op, required=required, inputs=inputs, namespace=namespace)
    except InvalidInputError, UnsupportedOperationError:
        # The rung's own denial is audited where it is asked; this is the rule's, or an input the committer may not read.
        audit(AuditAction.WRITE_JUDGE, AuditOutcome.DENY, subject=caller, resource=op.obj, op=op.op, requires_run=str(required).lower())
        raise
    audit(AuditAction.WRITE_JUDGE, AuditOutcome.ALLOW, subject=caller, resource=op.obj, op=op.op, requires_run=str(required).lower())
    return Judgement(requires_run=required)


async def _judge_under_the_run_rule(op: Write, *, required: bool, inputs: InputsCheck, namespace: Callable[[str], Awaitable[LanceNamespace]]) -> None:
    """Refuse ``op`` where the run rule, read as ``required``, does not admit it.

    Raises:
        InvalidInputError: A rename into a namespace that requires a run from one that does not, or a stamping door's
            write that names no run or an input the committer may not read.
        UnsupportedOperationError: A door that cannot carry a run, under a namespace that requires one.
    """
    if op.moved_from and required and not await _requires_run_for(namespace, op.moved_from):
        raise InvalidInputError(
            f"rename refused: moving a table into a namespace that requires a run ({REQUIRES_RUN_PROPERTY}) from one that does not "
            "places data there no run produced; write it there with the run that derives it"
        )
    if op.op in UNSTAMPABLE_DATA_DOORS and required:
        raise UnsupportedOperationError(
            f"{op.op} is refused under a namespace that requires a run ({REQUIRES_RUN_PROPERTY}): it cannot carry the run and inputs into "
            "a commit; write the rows with create, insert or merge_insert and the run in the request's Context"
        )
    if op.op in STAMPING_DOORS:
        await judge_run(required, op.run, inputs=inputs, names=f"the request's Context headers {RUN_ID_HEADER} and {INPUTS_HEADER}")


async def _requires_run_for(namespace: Callable[[str], Awaitable[LanceNamespace]], table: Sequence[str]) -> bool:
    """Whether the namespace path table ``table`` lies in requires a run, read through its warehouse's connection."""
    return await requires_run(await namespace(table[0]), table[:-1]) if len(table) > 1 else False


async def judge_run(required: bool, run: RunContext | None, *, inputs: InputsCheck, names: str) -> None:
    """Refuse a data write under the run rule that names no run and inputs, and any write naming an input that is not a
    table the committer may read.

    ``names`` says where the caller puts the run, for the refusal.

    Raises:
        InvalidInputError: ``required`` and the write names no run, or no input; or a named input is not a table the
            committer may read (raised by ``inputs``).
    """
    if required and (run is None or not run.inputs):
        raise InvalidInputError(
            f"commit refused: a write under a namespace that requires a run names its run ({RUN_ID_PROPERTY}) and its inputs ({INPUTS_PROPERTY}) in {names}"
        )
    if run is not None and run.inputs:
        await inputs(run)
