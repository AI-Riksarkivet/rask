"""The catalog's own reclaim doors, ``maintenance/run`` and ``version/delete``, held as a maintenance pass is (ADR 0243 §8).

EACH BATCH DELETES UNDER A CREDENTIAL THAT DIES WITH ITS HOLD (A1 of ``specs/maintenance/TablePass.tla``, ADR 0243 R5).
A maintenance pass deletes under a maintain-tier vend that lasts no longer than its holds, so a pass frozen past its hold
deletes nothing more once a door has been admitted beside it. These doors hold what a pass holds (the table's history and
the policy fence of each level above it, as reclaims), read the policy that governs the table under those holds and
refuse a table a hold at any level keeps, and reclaim through the one marked reclaim
(:class:`lakehouse.reclaim_marks.Reclaim`), a batch of :data:`lakehouse.reclaim_marks.BATCH` versions a call. Where the
catalog vends (``LANCE_VENDING_MODE=web_identity``), each batch FIRST mints a maintain-tier session for the table (the
maintain policy's grants on the table's own objects, none on the control root), THEN confirms every hold, which renews
each for one more hold, THEN deletes under that session and nothing else (:func:`sessions`): STS issues no session
shorter than the hold (900 s; RustFS 1.0.3 issues none shorter, measured), so the order is what makes the session die no
later than the hold it confirmed after minting, and a door frozen inside one call past its hold wakes to a store that
refuses every delete it signs. Its marks and renewals stay on the catalog's own credential. The erasure's verification
and release cleanups mint theirs the same way. Where the catalog has no web identity (a development fixture on static
keys) the vendor mints nothing, the batch deletes under the catalog's own credential, and a door frozen inside one call
past its hold can still delete that call's batch after a later reclaim judged its mark whole and removed it: at most 16
versions half removed and unmarked (ADR 0243 R5, ``docs/architecture/deployment.md``).

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

import lance
from lance_namespace import ConcurrentModificationError, InvalidTableStateError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict

from catalog.core.base_judge import BaseJudge
from catalog.core.config import Settings
from catalog.core.vending import CredentialVendor, dataset_facts, require_vendable_bases, role_session_name, vend_or_refuse
from catalog.services import table_claims
from lakehouse import history_fences, maintenance_policies, policy_fences, reclaim_marks
from lakehouse.fences import Hold
from lakehouse.governed import fga
from lakehouse.history_fences import HistoryFencedError, HistoryWork
from lakehouse.policy_fences import PolicyWork
from lakehouse.version_retention import recorded_branch


log = logging.getLogger(__name__)

#: The name a reclaim's session carries at the store, whose trail records it (:func:`role_session_name`).
_SESSION_NAME: Final = role_session_name("catalog-reclaim")


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


def sessions(vendor: CredentialVendor, settings: Settings, location: str) -> Callable[[str | None], Callable[[], lance.LanceDataset] | None]:
    """For each ref of the table at ``location``, what a reclaim of it deletes through (:attr:`Reclaim.session`): a handle
    of the ref opened under a maintain-tier session minted for one batch, lasting at most a hold, with the grants the
    maintain vend door gives a pass of that ref less any on the control root; ``None`` where the vendor mints nothing (no
    web identity). Asking for a ref mints its first batch's session at once, which is how its absence is known; each later
    batch mints its own.

    Raises:
        ServiceUnavailableError: The store refused, or could not answer, the exchange or a read of the ref (503).
        InvalidTableStateError: A base the catalog did not sanction (409).
    """

    def of(ref: str | None) -> Callable[[], lance.LanceDataset] | None:
        branch = recorded_branch(ref) or ""
        facts = dataset_facts(location, settings.storage_options(), branch=branch)
        bases = require_vendable_bases(location, facts, BaseJudge.from_settings(settings))

        def mint() -> dict[str, str] | None:
            vended = vend_or_refuse(
                vendor,
                table_location=location,
                tier="maintain",
                session_name=_SESSION_NAME,
                bases=bases.bases,
                data_directories=bases.data_directories,
                branch=branch,
                at_most_seconds=int(history_fences.HOLD.total_seconds()),
            )
            return None if vended is None else vended.as_storage_options().in_clear()

        first = mint()
        if first is None:
            return None
        minted = [first]

        def session() -> lance.LanceDataset:
            opened = lance.dataset(location, storage_options=minted.pop() if minted else mint())
            return opened if branch == "" else opened.checkout_version((branch, None))

        return session

    return of


def marked(held: Held, table: table_claims.DoorTable, *, ref: str | None, session: Callable[[], lance.LanceDataset] | None) -> reclaim_marks.Reclaim:
    """Where the door reclaims ``ref``: each cleanup call through ``session`` (:func:`sessions`), minted before a confirm
    of its holds, its marks under the catalog's own credential.

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
        session=session,
    )
