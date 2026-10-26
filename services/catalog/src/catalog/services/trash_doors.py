"""What the trash doors share: how long a drop stays recoverable, which trashed object an undrop restores, and which
record no undrop restores. Where it re-attaches a table is ``dataplane.relative_location``, the rule every
re-registration shares.

A trash door stays addressed by name (ADR 0173 §9, [[LH-525]]). A name is free again the moment its object is
dropped, so several trashed objects may have held one, each its own uuid with its own grants and records: an undrop
restores exactly the one its caller names, and asks which when the name leads to several.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidTableStateError

from catalog.core.config import Settings
from catalog.services import warehouses
from service_kit.lakehouse import trash


async def drop_grace_days(settings: Settings, segments: list[str]) -> float:
    """The days a recoverable drop of the object ``segments`` names stays recoverable, 0 for a destroying drop.

    Its warehouse's delete profile, else the estate's grace period (ADR 0146), read at the drop: the trash record stamps
    the deadline, so a profile changed later shortens no window already open.
    """
    return await run_in_threadpool(
        partial(
            warehouses.grace_days_under,
            settings.registry_root,
            settings.storage_options(),
            segments,
            estate=settings.trash_grace_days,
            warehouses_enabled=settings.warehouses_enabled,
        )
    )


def the_one_to_restore(records: list[dict[str, Any]], *, kind: str, name: str) -> dict[str, Any]:
    """The one trash record among ``records`` an undrop of ``name`` restores.

    Raises:
        InvalidTableStateError: Several trashed objects held the name; the answer lists each one's uuid and deadline,
            and ``?uuid=`` names one.
    """
    if len(records) == 1:
        return records[0]
    listed = "; ".join(f"{trash.object_uuid(record)} (dropped {record.get('dropped_at')}, recoverable until {record.get('expires_at')})" for record in records)
    raise InvalidTableStateError(f"{len(records)} dropped {kind}s held the name {name!r}: {listed}. Name the one to undrop with ?uuid=<uuid>.")


def require_its_own_parent(record: dict[str, Any], *, standing: str | None, name: str) -> None:
    """Refuse restoring ``record`` into a namespace other than the one it was dropped from.

    A record keeps its namespace's uuid at the drop. Once that namespace is dropped too, its name is free, and another
    namespace created there is another uuid with its own owner: restoring into it would attach the trashed object
    under a namespace its caller was never granted a create on, while its grants still name its own.

    Raises:
        InvalidTableStateError: The live namespace at the parent's name is not the one the object was dropped from.
    """
    dropped_from = record.get("namespace_uuid")
    if isinstance(dropped_from, str) and standing is not None and standing != dropped_from:
        raise InvalidTableStateError(f"cannot undrop {name!r}: the namespace it was dropped from is gone, and another namespace holds its name now")


def require_restorable(record: dict[str, Any], *, name: str) -> None:
    """Refuse restoring a record whose drop is final: a residue record, or one past its deadline.

    A residue record names bytes a destructive drop that failed part-way left: its table was destroyed, not trashed,
    its row is gone and some of its objects may be too, so nothing an undrop re-attaches there is the table that was
    dropped ([[LH-532]]). Past ``expires_at`` the drop is DropTable's, its data deleted asynchronously by the purge, so
    no undrop restores it even while its bytes stand (ADR 0146).

    Raises:
        InvalidTableStateError: ``record`` is a residue record, or its grace period has ended.
    """
    if trash.is_residue(record):
        raise InvalidTableStateError(
            f"cannot undrop {name!r}: its bytes were left by a destructive drop that failed part-way, not kept for an undrop; the trash purge reclaims them"
        )
    if trash.past_deadline(record) or trash.is_released(record):
        raise InvalidTableStateError(
            f"cannot undrop {name!r}: its grace period ended at {record.get('expires_at')}, so the drop is final and the trash purge reclaims its bytes"
        )
