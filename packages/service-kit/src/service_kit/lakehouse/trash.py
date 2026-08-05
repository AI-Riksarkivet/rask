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
prefix, hashed key (ids are user-shaped and contain ``$``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow.fs as pafs

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

_TRASH_PREFIX = "_trash"


def _key(canonical_id: str, kind: str = "table") -> str:
    digest = hashlib.sha256(f"{kind}:{canonical_id}".encode()).hexdigest()[:24]
    return f"{_TRASH_PREFIX}/{kind}-{digest}.json"


def make_record(
    canonical_id: str, *, location: str, dropped_by: str | None, grace_days: int, now: datetime | None = None, kind: str = "table"
) -> dict[str, Any]:
    """The trash record. ``expires_at`` is stamped HERE — at drop time — so a later change to the
    estate's grace period cannot retroactively shorten a window someone is still inside.

    ``kind`` distinguishes tables from namespaces (#96 — a recoverable CASCADE trashes both). A
    namespace is a ``__manifest`` row with no bytes of its own, so its ``location`` is ``""``; the
    record's job is to let undrop know the row (and which subtree) to rebuild.
    """
    at = now or datetime.now(UTC)
    return {
        "kind": kind,
        "id": canonical_id,
        "location": location,
        "dropped_by": dropped_by or "anonymous",
        "dropped_at": at.isoformat(),
        "expires_at": (at + timedelta(days=grace_days)).isoformat(),
    }


def put(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
    fs, base = fs_and_base(control_root, storage_options)
    fs.create_dir(f"{base}/{_TRASH_PREFIX}", recursive=True)
    with fs.open_output_stream(f"{base}/{_key(str(record['id']), str(record.get('kind', 'table')))}") as stream:
        stream.write(json.dumps(record).encode())


def get(control_root: str, storage_options: StorageOptions, canonical_id: str, *, kind: str = "table") -> dict[str, Any] | None:
    fs, base = fs_and_base(control_root, storage_options)
    try:
        with fs.open_input_stream(f"{base}/{_key(canonical_id, kind)}") as stream:
            loaded: Any = json.loads(stream.read().decode())
    except FileNotFoundError:
        return None
    if not isinstance(loaded, dict):
        log.warning("trash_record_malformed", extra={"id": canonical_id})
        return None
    return loaded


def clear(control_root: str, storage_options: StorageOptions, canonical_id: str, *, kind: str = "table") -> bool:
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{_key(canonical_id, kind)}")
    except FileNotFoundError:
        return False
    return True


def list_all(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every readable trash record. An absent prefix yields ``[]``; one unreadable record is skipped
    with a warning rather than blinding the whole listing."""
    fs, base = fs_and_base(control_root, storage_options)
    selector = pafs.FileSelector(f"{base}/{_TRASH_PREFIX}", allow_not_found=True, recursive=False)
    records: list[dict[str, Any]] = []
    for info in fs.get_file_info(selector):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                loaded: Any = json.loads(stream.read().decode())
        except Exception:  # noqa: BLE001 — one bad record must not empty the list
            log.warning("trash_record_unreadable", extra={"path": info.path})
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
    return records


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
