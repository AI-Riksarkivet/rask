"""One primitive under every control-root record registry — the shape four registries hand-rolled.

`protection`, `maintenance_policies`, `trash` and `warehouse_records` all store the same thing the same
way: one JSON document per object, under the registry's own prefix on the control root, addressed by a
hash of ``kind:canonical_id`` because ids are user-shaped and contain ``$``. Each of them wrote that out
in full — its own ``_key`` helper (byte-identical but for the prefix constant), its own
open/decode/FileNotFoundError body, its own listing loop with a broad ``except`` around the read.

The copies had already drifted, which is why this module exists rather than being a tidiness pass:
``get_policy`` never grew the ``isinstance(loaded, dict)`` guard both its twins carry, so a malformed
policy record was returned verbatim and reached ``resolve_policy`` as whatever JSON was on disk. The
two key helpers had even swapped their parameter order (``protection._key(kind, id)`` against
``trash._key(id, kind)``) while hashing the same string.

**The key bytes are a persistence contract.** Records already written to control roots are addressed by
these digests, so :func:`record_key` reproduces the old composition exactly — 24 hex characters of
``sha256("<kind>:<canonical_id>")``, under ``<prefix>/<kind>-<digest>.json``. Changing it orphans every
stored record; ``tests/unit/test_lakehouse_kernel_drain.py`` pins the shape.

**Not the conditional seam.** :mod:`service_kit.lakehouse.records` arbitrates contended writes through
the STORE (put-if-not-exists, ETag-guarded RMW) on caller-supplied plain keys — reach for that where a
lost race would corrupt a registry. This module is the UNCONDITIONAL hashed-key half the four
per-object registries need: overwrite-is-idempotent set, absent-or-malformed-reads-as-None get, and a
tolerant listing. Neither is a substitute for the other.

Every function is BLOCKING IO — callers threadpool it, exactly as they did when the bodies were inline.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import pyarrow.fs as pafs

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)


def record_key(prefix: str, kind: str, canonical_id: str) -> str:
    """A collision-free record key: the id is user-shaped (contains ``$``), so hash it.

    ``kind`` travels in BOTH the digest and the filename: in the digest so two kinds can share a
    canonical id without colliding, in the filename so an operator reading the bucket can tell what a
    record is about without decoding it.
    """
    digest = hashlib.sha256(f"{kind}:{canonical_id}".encode()).hexdigest()[:24]
    return f"{prefix}/{kind}-{digest}.json"


def put_record(control_root: str, storage_options: StorageOptions, key: str, record: dict[str, Any]) -> None:
    """Write one record, creating its prefix — an overwrite, so every registry's set is idempotent."""
    fs, base = fs_and_base(control_root, storage_options)
    path = f"{base}/{key}"
    fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
    with fs.open_output_stream(path) as stream:
        stream.write(json.dumps(record).encode())


def get_record(control_root: str, storage_options: StorageOptions, key: str, *, event: str) -> dict[str, Any] | None:
    """One record, or ``None`` when it is absent OR is not a JSON object.

    A malformed record reads as ABSENT rather than propagating: every caller of a registry treats
    ``None`` as "no record was set", and handing back a list or a string instead makes the fault surface
    somewhere that cannot name it. A store OUTAGE is not this case — it surfaces as an exception from
    pyarrow and is deliberately left to propagate.
    """
    fs, base = fs_and_base(control_root, storage_options)
    try:
        with fs.open_input_stream(f"{base}/{key}") as stream:
            loaded: Any = json.loads(stream.readall().decode())
    except FileNotFoundError:
        return None
    if not isinstance(loaded, dict):
        log.warning(f"{event}_malformed", extra={"key": key})
        return None
    return loaded


def delete_record(control_root: str, storage_options: StorageOptions, key: str) -> bool:
    """Remove one record; ``False`` when there was none (delete is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{key}")
    except FileNotFoundError:
        return False
    return True


def list_records(control_root: str, storage_options: StorageOptions, prefix: str, *, event: str) -> list[dict[str, Any]]:
    """Every readable record under ``prefix`` (unordered). An absent prefix yields ``[]``.

    NON-RECURSIVE on purpose: registries park their own state under a child prefix
    (``_policies/state/``, ``_warehouses/bindings/``) and those are not records of this kind.

    One unreadable or malformed record is SKIPPED with a warning rather than voiding the listing. That
    is the tolerance every caller was written for — these feed maintenance sweeps, where one bad record
    must not stop every other tenant being maintained — and it is also why a caller making a DESTRUCTIVE
    decision must not use this: a record it cannot read is silently not in the answer.
    """
    fs, base = fs_and_base(control_root, storage_options)
    records: list[dict[str, Any]] = []
    selector = pafs.FileSelector(f"{base}/{prefix}", allow_not_found=True, recursive=False)
    for info in fs.get_file_info(selector):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                loaded: Any = json.loads(stream.readall().decode())
        except Exception as exc:
            log.warning(f"{event}_unreadable", extra={"path": info.path, "error": str(exc)})
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
        else:
            log.warning(f"{event}_malformed", extra={"path": info.path})
    return records
