"""Trash records for the drop→undrop path (#75, open_lakehouse_diff §1.2).

`docs/COVERAGE.md` recorded soft-delete/undrop as N/A, "replaced by Lance version time-travel +
`restore_table`". That argument does not hold and is corrected there: **time-travel does not survive
`drop_table`** — `restore_table` rewinds a LIVE table; a drop deletes the bytes and leaves no version
to rewind to. Time-travel covers bad-WRITE recovery, never bad-DROP recovery.

The durable fix: a drop DEREGISTERS the table (detach, bytes untouched) and writes a trash record
carrying where the bytes are and when the grace period ends. Undrop re-registers from that record.
The maintenance sweep is what eventually expires it.

Two disciplines this module exists to keep:

- **Report-only by default.** ``expired()`` SELECTS what is past its deadline and does nothing else.
  Purging is a separate, explicitly-enabled step, exactly like the orphan pass — the estate's rule is
  that a reclaimer earns its delete permission by first proving its report runs clean.
- **The deadline is data, not policy read at expiry time.** ``expires_at`` is stamped WHEN THE DROP
  HAPPENS, so shortening the estate-wide grace period can never retroactively destroy something a
  user was still inside their window to recover.

Same store shape as ``maintenance_policies``/``protection``: one JSON per object under its own
prefix, hashed key (ids are user-shaped and contain ``$``). The shape itself lives in
:mod:`service_kit.lakehouse.record_store` — this module is the trash vocabulary over it, and the
``_key`` helper it used to carry (with its two arguments in the OPPOSITE order to protection's, while
hashing the same string) is gone.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import delete_record, get_record, list_records, put_record, record_key


log = logging.getLogger(__name__)

_TRASH_PREFIX = "_trash"

#: The event stem for this registry's store warnings (`<stem>_malformed`, `<stem>_unreadable`).
_EVENT = "trash_record"


def make_record(
    canonical_id: str,
    *,
    location: str,
    dropped_by: str | None,
    grace_days: int,
    now: datetime | None = None,
    kind: str = "table",
    binding: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The trash record. ``expires_at`` is stamped HERE — at drop time — so a later change to the
    estate's grace period cannot retroactively shorten a window someone is still inside.

    ``kind`` distinguishes tables from namespaces (#96 — a recoverable CASCADE trashes both). A
    namespace is a ``__manifest`` row with no bytes of its own, so its ``location`` is ``""``; the
    record's job is to let undrop know the row (and which subtree) to rebuild.

    ``binding`` is ``{"warehouse_id": …, "root_uri": …}`` and is set on exactly one record: the ROOT
    namespace record of a recoverable cascade over a TOP-LEVEL namespace (diff2 F6 leg c). The
    warehouse binding is what routes that subtree to its own bucket, and the drop now REMOVES it —
    so this field is the only surviving copy of where the subtree lived, and undrop re-binds from it.

    Why the binding moved onto the record at all: it used to be KEPT on a recoverable drop so undrop
    could still route, which meant it OUTLIVED the namespace, and when the grace window expired the
    purge reclaimed the bytes and left the binding behind forever. Nothing could see the leak either
    — the reconciler's `dangling_bindings` keys on a MISSING WAREHOUSE record, and the warehouse is
    still there. Recording it here and unbinding at drop removes the leak class rather than adding a
    second reclaimer that has to remember to fire.

    Absent on every other record, and absent on records written before this landed — read it with
    `.get("binding")` and treat absence as "there was never a binding to restore".
    """
    at = now or datetime.now(UTC)
    record: dict[str, Any] = {
        "kind": kind,
        "id": canonical_id,
        "location": location,
        "dropped_by": dropped_by or "anonymous",
        "dropped_at": at.isoformat(),
        "expires_at": (at + timedelta(days=grace_days)).isoformat(),
    }
    if binding:
        record["binding"] = dict(binding)
    return record


def put(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
    put_record(control_root, storage_options, record_key(_TRASH_PREFIX, str(record.get("kind", "table")), str(record["id"])), record)


def get(control_root: str, storage_options: StorageOptions, canonical_id: str, *, kind: str = "table") -> dict[str, Any] | None:
    return get_record(control_root, storage_options, record_key(_TRASH_PREFIX, kind, canonical_id), event=_EVENT)


def clear(control_root: str, storage_options: StorageOptions, canonical_id: str, *, kind: str = "table") -> bool:
    return delete_record(control_root, storage_options, record_key(_TRASH_PREFIX, kind, canonical_id))


def list_all(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every readable trash record. An absent prefix yields ``[]``; one unreadable record is skipped
    with a warning rather than blinding the whole listing."""
    return list_records(control_root, storage_options, _TRASH_PREFIX, event=_EVENT)


def expired(records: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Which records are past their deadline — a SELECTION, never a deletion (#75 report-only).

    A record whose ``expires_at`` is missing or unparseable is NOT selected: an undated record is a
    bug in whatever wrote it, and the safe reading of "we do not know when this expires" is "not
    yet". Failing toward not-deleting is the same stance the sweep takes when the policy registry is
    unreadable.
    """
    at = now or datetime.now(UTC)
    due: list[dict[str, Any]] = []
    for record in records:
        raw = record.get("expires_at")
        if not isinstance(raw, str):
            log.warning("trash_record_undated", extra={"id": record.get("id")})
            continue
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError:
            log.warning("trash_record_bad_deadline", extra={"id": record.get("id"), "expires_at": raw})
            continue
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if deadline <= at:
            due.append(record)
    return due
