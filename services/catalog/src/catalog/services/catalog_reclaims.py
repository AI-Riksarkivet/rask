"""The catalog's own reclaim doors, ``maintenance/run`` and ``version/delete``, held as a maintenance pass is (ADR 0243 §8).

THEIR CREDENTIAL NEVER DIES. A maintenance pass deletes under a maintain-tier vend that lasts no longer than its holds
(A1 of ``specs/maintenance/TablePass.tla``), so a pass frozen past its hold deletes nothing more once a door has been
admitted beside it. These two doors delete with the catalog's own credential, which no hold bounds. So each holds what a
pass holds (the table's history and the policy fence of each level above it, as reclaims), reads the policy that governs
the table under those holds and refuses a table a hold at any level keeps, marks its cleanup's victims before its first
delete (:mod:`lakehouse.reclaim_marks`), and CONFIRMS EVERY HOLD BEFORE EACH CLEANUP CALL it makes, one call per
:data:`BATCH` versions. Confirming before each delete request is not open to it: Lance issues a cleanup's deletes with no
hook between them. What stays open is a door frozen inside one call past its hold: that call's deletes of versions its
mark names, which no ref door may name while the mark stands. A capped credential minted for the door would close that
too, but only where the catalog vends (``LANCE_VENDING_MODE=web_identity``), and these doors must work where it vends
nothing; so the confirm is the leaner of the two that holds everywhere.

``version/delete`` is an explicit act by an authorized principal and is not judged by retention, but a hold
(``cleanup_enabled`` false at any level that can govern the table) is: lifting the hold is a separate, audited act at a
policy door, so the delete answers 409 InvalidTableState (19) while one stands. ``maintenance/run`` answers the same.

Every function is blocking IO; callers threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager
from typing import Final

from lance_namespace import ConcurrentModificationError, InvalidTableStateError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict

from catalog.core.config import Settings
from catalog.services import table_claims
from lakehouse import maintenance_policies, reclaim_marks
from lakehouse.fences import Hold
from lakehouse.governed import fga
from lakehouse.history_fences import HistoryFencedError, HistoryWork
from lakehouse.policy_fences import PolicyFence, PolicyWork


log = logging.getLogger(__name__)

#: How many versions one cleanup call removes between two confirms of the door's holds: Lance plans each call by reading
#: the ref's retained manifests, so a call per version would read them once per version.
BATCH: Final = 16


class Held(BaseModel):
    """What a reclaim door holds: the table's history (its marks), and a confirm of every hold."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    history: Hold
    confirm: Callable[[], None]


def _confirmed(holds: Sequence[Hold]) -> Callable[[], None]:
    def confirm() -> None:
        try:
            for hold in holds:
                hold.confirm()
        except HistoryFencedError as exc:
            raise ConcurrentModificationError(f"this reclaim's hold lapsed before its next cleanup; nothing more was deleted, retry: {exc}") from exc

    return confirm


@contextmanager
def held(table: table_claims.DoorTable, settings: Settings, *, namespace_uuids: Sequence[str]) -> Generator[Held]:
    """The table's history and each level's policy fence held as reclaims, the table's governing policy read under them
    and refused where it holds the table's whole history.

    Raises:
        ConcurrentModificationError: A ref write holds the table's history, or a policy write holds a level's fence (14).
        InvalidTableStateError: A hold at a level that can govern the table keeps its whole history, or a record there
            cannot be read as a policy (19).
        ServiceUnavailableError: The store did not answer a policy read (503).
    """
    so = settings.storage_options()
    try:
        project = maintenance_policies.governing_project(settings.registry_root, so, table.location)
    except (OSError, ValueError) as exc:
        raise ServiceUnavailableError("the bucket's claim could not be read, so the policies that may hold the table are unknown; nothing was deleted") from exc
    warehouse = table.segments[0]
    levels = [
        PolicyFence(control_root=settings.registry_root, storage_options=so, level="namespace", key=uuid, warehouse=warehouse) for uuid in namespace_uuids
    ]
    if project is not None:
        levels.append(PolicyFence(control_root=settings.registry_root, storage_options=so, level="project", key=project))
    with ExitStack() as stack:
        try:
            history = stack.enter_context(table.history_fence().hold_for(HistoryWork.RECLAIM))
            holds = [history, *(stack.enter_context(level.hold_for(PolicyWork.RECLAIM)) for level in levels)]
        except HistoryFencedError as exc:
            raise ConcurrentModificationError(f"table {fga.canonical_object_id(table.segments)}: {exc}") from exc
        _refuse_a_held_table(table, settings, namespace_uuids=namespace_uuids)
        yield Held(history=history, confirm=_confirmed(holds))


def _refuse_a_held_table(table: table_claims.DoorTable, settings: Settings, *, namespace_uuids: Sequence[str]) -> None:
    try:
        policy = maintenance_policies.governing_policy(
            settings.registry_root,
            settings.storage_options(),
            claims_root=settings.registry_root,
            location=table.location,
            table_uuid=table.resolved.require_uuid(),
            namespace_uuids=list(namespace_uuids),
        )
        kept = maintenance_policies.keeps_whole_history(policy)
    except maintenance_policies.MalformedPolicyError as exc:
        raise InvalidTableStateError("a maintenance policy that may govern the table cannot be read as one, and may hold it; nothing was deleted") from exc
    except maintenance_policies.UnreadablePolicyError as exc:
        raise ServiceUnavailableError("the maintenance policies could not be read, so whether a hold keeps the table is unknown; nothing was deleted") from exc
    if kept:
        by = (policy or {}).get("held_by") or f"{(policy or {}).get('kind')}:{(policy or {}).get('id')}"
        log.info("reclaim_refused_under_a_hold", extra={"table": table.segments, "held_by": by})
        raise InvalidTableStateError(
            f"a maintenance policy holds the table's whole history ({by}: cleanup_enabled false), so no version is deleted; lift the hold at its policy door first. Nothing was deleted"
        )


def marked(held: Held, table: table_claims.DoorTable, *, ref: str | None) -> reclaim_marks.MarkedCleanup:
    """Where the door's cleanups are marked, under the catalog's own credential, each call after a confirm of its holds.

    Raises:
        ServiceUnavailableError: The store failed a read of a standing mark (503); nothing was deleted.
        UnidentifiedObjectError: The table's location carries no claim recording a uuid.
    """
    fence = table.history_fence()
    try:
        standing = reclaim_marks.read(fence.control_root, fence.storage_options, table_uuid=fence.table_uuid, marks=held.history.marks)
    except OSError as exc:
        raise ServiceUnavailableError("the store did not answer reading a cleanup's mark, so nothing was deleted") from exc
    return reclaim_marks.MarkedCleanup(
        control_root=fence.control_root,
        storage_options=fence.storage_options,
        table_uuid=fence.table_uuid,
        ref=ref,
        standing=standing,
        before_each=held.confirm,
        batch=BATCH,
    )
