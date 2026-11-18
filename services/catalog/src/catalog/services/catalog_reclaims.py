"""The catalog's own reclaim doors, ``maintenance/run`` and ``version/delete``, held as a maintenance pass is (ADR 0243 §8).

THEIR CREDENTIAL NEVER DIES. A maintenance pass deletes under a maintain-tier vend that lasts no longer than its holds
(A1 of ``specs/maintenance/TablePass.tla``), so a pass frozen past its hold deletes nothing more once a door has been
admitted beside it. These two doors delete with the catalog's own credential, which no hold bounds. So each holds what a
pass holds (the table's history and the policy fence of each level above it, as reclaims), reads the policy that governs
the table under those holds and refuses a table a hold at any level keeps, and reclaims through the one marked reclaim
(:class:`lakehouse.reclaim_marks.Reclaim`), which CONFIRMS EVERY HOLD BEFORE EACH CLEANUP CALL it makes, one call per
:data:`lakehouse.reclaim_marks.BATCH` versions, and only then marks that call's victims; an intent a policy door left
beside one of its holds stops it there too (F4). Confirming before each delete request is not open to it: Lance issues a
cleanup's deletes with no hook between them. What stays open is a door frozen inside one call past its hold: that call's
deletes, of at most one batch of versions its own mark names, which no ref door may name while the mark stands; once a
later reclaim finds the mark with its writer's hold gone and every file of its versions still standing, it removes it, so
a door frozen before its first delete of that call and resuming after can leave that batch half removed and unmarked
(stated in ADR 0243 §9). A capped credential minted for the door would close that too, but only where the catalog vends
(``LANCE_VENDING_MODE=web_identity``), and these doors must work where it vends nothing; so the confirm is the leaner of
the two that holds everywhere.

``version/delete`` is an explicit act by an authorized principal and is not judged by retention, but a hold
(``cleanup_enabled`` false at any level that can govern the table) is: lifting the hold is a separate, audited act at a
policy door, so the delete answers 409 InvalidTableState (19) while one stands. ``maintenance/run`` answers the same.

Every function is blocking IO; callers threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager

from lance_namespace import ConcurrentModificationError, InvalidTableStateError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict

from catalog.core.config import Settings
from catalog.services import table_claims
from lakehouse import maintenance_policies, policy_fences, reclaim_marks
from lakehouse.fences import Hold
from lakehouse.governed import fga
from lakehouse.history_fences import HistoryFencedError, HistoryWork
from lakehouse.policy_fences import PolicyWork


log = logging.getLogger(__name__)


class Held(BaseModel):
    """What a reclaim door holds: the table's history (its marks), and a confirm of every hold."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    history: Hold
    confirm: Callable[[], None]


def _confirmed(holds: Sequence[Hold]) -> Callable[[], None]:
    def confirm() -> None:
        try:
            for hold in holds:
                hold.confirm_unchallenged()
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
    levels = policy_fences.levels(settings.registry_root, so, warehouse=table.segments[0], namespace_uuids=list(namespace_uuids), project=project)
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


def marked(held: Held, table: table_claims.DoorTable, *, ref: str | None) -> reclaim_marks.Reclaim:
    """Where the door reclaims ``ref``, under the catalog's own credential, each cleanup call after a confirm of its holds.

    Raises:
        UnidentifiedObjectError: The table's location carries no claim recording a uuid.
    """
    fence = table.history_fence()
    return reclaim_marks.Reclaim(
        control_root=fence.control_root,
        storage_options=fence.storage_options,
        table_uuid=fence.table_uuid,
        ref=ref,
        hold=held.history,
        before_each=held.confirm,
    )
