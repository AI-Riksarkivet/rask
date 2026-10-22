"""What the undrop doors share: which trashed object an undrop restores, and where it re-attaches a table.

A trash door stays addressed by name (ADR 0173 §9, [[LH-525]]). A name is free again the moment its object is
dropped, so several trashed objects may have held one, each its own uuid with its own grants and records: an undrop
restores exactly the one its caller names, and asks which when the name leads to several.
"""

from __future__ import annotations

from typing import Any

from lance_namespace import InternalError, InvalidTableStateError

from service_kit.lakehouse import trash
from service_kit.lakehouse.base_refs import decoded_segments


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


def relative_location(location: str, *, root: str) -> str:
    """The key ``location`` decodes to under ``root``: what RegisterTable takes to re-attach the table there.

    The ``dir`` backend refuses an absolute location and keeps a relative one as the literal key it is given, while a
    trash record keeps the location ``describe_table`` answered, which percent-encodes a registered table's name
    (``…/r%C3%A4ksm%C3%B6rg%C3%A5s``). The store holds the table's bytes under the key that decodes to once
    (``…/räksmörgås``), so that key, whole, is what the table re-attaches at ([[LH-517]]).

    Raises:
        InternalError: ``location`` names no place under ``root``, which no drop records.
    """
    under, base = decoded_segments(location), decoded_segments(root)
    if under is None or base is None or len(under) <= len(base) or under[: len(base)] != base:
        raise InternalError(f"the trashed location {location!r} names no place under its namespace root, so the table cannot be re-attached")
    return "/".join(under[len(base) :])
