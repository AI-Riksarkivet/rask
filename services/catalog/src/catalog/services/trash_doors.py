"""What the undrop doors share: which trashed object an undrop restores. Where it re-attaches a table is
``dataplane.relative_location``, the rule every re-registration shares.

A trash door stays addressed by name (ADR 0173 §9, [[LH-525]]). A name is free again the moment its object is
dropped, so several trashed objects may have held one, each its own uuid with its own grants and records: an undrop
restores exactly the one its caller names, and asks which when the name leads to several.
"""

from __future__ import annotations

from typing import Any

from lance_namespace import InvalidTableStateError

from service_kit.lakehouse import trash


def the_one_to_restore(records: list[dict[str, Any]], *, kind: str, name: str) -> dict[str, Any]:
    """The one trash record among ``records`` an undrop of ``name`` restores.

    Raises:
        InvalidTableStateError: Several trashed objects held the name; the answer lists each one's uuid and deadline,
            and ``?uuid=`` names one.
    """
    if len(records) == 1:
        return records[0]
    listed = "; ".join(
        f"{trash.object_uuid(record)} (dropped {record.get('dropped_at')}, purge-eligible after {record.get('expires_at')})" for record in records
    )
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
