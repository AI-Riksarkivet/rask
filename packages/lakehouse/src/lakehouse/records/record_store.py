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
stored record; ``packages/lakehouse/tests/test_lakehouse_kernel_drain.py`` pins the shape.

**Not the conditional seam.** :mod:`lakehouse.storage.conditional_records` arbitrates contended writes through
the STORE (put-if-not-exists, ETag-guarded RMW) on caller-supplied plain keys — reach for that where a
lost race would corrupt a registry. This module is the UNCONDITIONAL hashed-key half the four
per-object registries need: overwrite-is-idempotent set, absent-or-malformed-reads-as-None get, and a
tolerant listing. Neither is a substitute for the other.

Every function is BLOCKING IO — callers threadpool it, exactly as they did when the bodies were inline.
"""

import hashlib
import json
import logging
from typing import Any

import pyarrow.fs as pafs
from pydantic import BaseModel, Field, ValidationError

from lakehouse.storage import stored_objects
from lakehouse.storage.errors import store_call
from lakehouse.storage.objectfs import fs_and_base
from lakehouse.storage.options import StorageOptions, object_client, on_s3
from lakehouse.storage.outage import reads_as_store_outage
from lakehouse.storage.uri import s3_object


__all__ = [
    "Listing",
    "UnreadableRecordError",
    "delete_record",
    "get_record",
    "list_every_record",
    "list_records",
    "parse_record",
    "put_record",
    "read_listing",
    "read_record",
    "record_key",
    "record_name",
]


log = logging.getLogger(__name__)


class UnreadableRecordError(ValueError):
    """Raised when a record is stored and cannot be read as one: what it decides is unknown, so it is never read as absent."""


def parse_record[M: BaseModel](model: type[M], raw: object, *, at: str, error: type[UnreadableRecordError]) -> M:
    """``raw`` as a ``model`` record; ``at`` names where it sits, and ``error`` is the registry's own refusal.

    Raises:
        UnreadableRecordError: ``raw`` is no ``model`` record, raised as ``error``, naming ``at`` and the fields that are
            not a record's.
    """
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        fields = ", ".join(sorted({".".join(str(part) for part in problem["loc"]) or "the record" for problem in exc.errors(include_url=False)}))
        raise error(f"the record at {at} cannot be read as one: {fields} is not what a record holds") from exc


def record_name(kind: str, canonical_id: str) -> str:
    """The file name every registry keeps the object's record under, ``<kind>-<digest>.json``: the id is user-shaped
    (contains ``$``), so it is hashed.

    ``kind`` travels in BOTH the digest and the name: in the digest so two kinds can share a canonical id without
    colliding, in the name so an operator reading the bucket can tell what a record is about without decoding it.
    """
    digest = hashlib.sha256(f"{kind}:{canonical_id}".encode()).hexdigest()[:24]
    return f"{kind}-{digest}.json"


def record_key(prefix: str, kind: str, canonical_id: str) -> str:
    """The collision-free key of the object's record under the registry ``prefix`` (:func:`record_name`)."""
    return f"{prefix}/{record_name(kind, canonical_id)}"


def put_record(control_root: str, storage_options: StorageOptions, key: str, *, record: dict[str, Any]) -> None:
    """Write one record — an overwrite, so every registry's set is idempotent.

    On S3 by its key alone, through the object client: pyarrow's ``create_dir`` puts every parent of the key as a
    directory marker (measured on pyarrow 25.0.0: ``_policies/``, ``_policies/state/`` and ``_policies/state/<uuid>/``
    before a stamp), a write outside what a writer granted its own records' keys may make ([[XC-084]]).

    Raises:
        OSError: The store refused the write or did not answer it (on S3 a :class:`~lakehouse.storage.errors.StorageError`).
    """
    body = json.dumps(record).encode()
    if on_s3(control_root):
        bucket, object_key = s3_object(control_root, key)
        with store_call(f"writing {bucket}/{object_key}"):
            object_client(storage_options).put_object(Bucket=bucket, Key=object_key, Body=body)
        return
    fs, base = fs_and_base(control_root, storage_options)
    path = f"{base}/{key}"
    fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
    with fs.open_output_stream(path) as stream:
        stream.write(body)


def get_record(control_root: str, storage_options: StorageOptions, key: str, *, event: str) -> dict[str, Any] | None:
    """One record, or ``None`` when it is absent, is not JSON, or is not a JSON object.

    A malformed record reads as ABSENT rather than propagating: every caller of a registry treats
    ``None`` as "no record was set", and handing back a list or a string instead makes the fault surface
    somewhere that cannot name it. A store OUTAGE is not this case — it surfaces as an exception from
    pyarrow and is deliberately left to propagate. A caller making a DESTRUCTIVE decision reads through
    :func:`read_record` instead, which raises on a record it cannot read.
    """
    try:
        stored = _stored(control_root, storage_options, key)
        loaded = None if stored is None else json.loads(stored)
    except ValueError:
        # Not text, or not JSON: `_stored` and the parse both say so as a `ValueError`. An outage is an `OSError`.
        stored, loaded = "", None
    if stored is None:
        return None
    if not isinstance(loaded, dict):
        log.warning(f"{event}_malformed", extra={"key": key})
        return None
    return loaded


def read_record(control_root: str, storage_options: StorageOptions, key: str) -> dict[str, Any] | None:
    """One record, or ``None`` when it is absent; one that is not a JSON object RAISES, for a caller that decides on it.

    Raises:
        ValueError: The stored record is not JSON, or not a JSON object.
        OSError: The store failed the read.
    """
    stored = _stored(control_root, storage_options, key)
    if stored is None:
        return None
    loaded = json.loads(stored)
    if not isinstance(loaded, dict):
        raise ValueError(f"the record at {key} is not a JSON object")
    return loaded


def _stored(control_root: str, storage_options: StorageOptions, key: str) -> str | None:
    """The text stored at ``key``, or ``None`` when nothing is: a body of ``null`` is a record that is filed, and only
    the text tells it from a key nothing is filed under.

    Raises:
        ValueError: What is stored is not text.
        OSError: The store failed the read.
    """
    fs, base = fs_and_base(control_root, storage_options)
    try:
        with fs.open_input_stream(f"{base}/{key}") as stream:
            return stream.readall().decode()
    except FileNotFoundError:
        return None


def delete_record(control_root: str, storage_options: StorageOptions, key: str) -> bool:
    """Remove one record; ``False`` when there was none (delete is idempotent). Writes nothing: maintenance may not write
    under several record prefixes, so a directory marker would be refused (:func:`stored_objects.delete_object`).

    Raises:
        OSError: The store refused the delete.
    """
    return stored_objects.delete_object(control_root, storage_options, key)


class Listing(BaseModel):
    """What a read of a prefix's records found: each record, by the path it sits at, and the path of each record that is
    there and cannot be read as a JSON object."""

    records: list[tuple[str, dict[str, Any]]] = Field(default_factory=list)
    unreadable: list[str] = Field(default_factory=list)


def read_listing(control_root: str, storage_options: StorageOptions, prefix: str, *, event: str) -> Listing:
    """Every record under ``prefix`` (unordered), and every record there that cannot be read as one, logged
    ``<event>_unreadable`` or ``<event>_malformed``. An absent prefix reads as empty.

    NON-RECURSIVE on purpose: registries park their own state under a child prefix (``_policies/state/``,
    ``_warehouses/bucket-claims/``) and those are not records of this kind.

    A record deleted since the listing named it is no record. A read the store did not answer raises: skipped as
    unreadable, it would read as a corrupt record, which no retry mends, and leave a tolerant caller deciding on a
    listing one record short ([[LH-572]]).

    Raises:
        OSError: The store did not answer the listing or a record's read (`lakehouse.storage.outage`).
    """
    fs, base = fs_and_base(control_root, storage_options)
    listing = Listing()
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{prefix}", allow_not_found=True, recursive=False)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                loaded: Any = json.loads(stream.readall().decode())
        except FileNotFoundError:
            continue
        except Exception as exc:
            if reads_as_store_outage(exc):
                raise
            log.warning(f"{event}_unreadable", extra={"path": info.path, "error": str(exc)})
            listing.unreadable.append(info.path)
            continue
        if isinstance(loaded, dict):
            listing.records.append((info.path, loaded))
        else:
            log.warning(f"{event}_malformed", extra={"path": info.path})
            listing.unreadable.append(info.path)
    return listing


def list_records(control_root: str, storage_options: StorageOptions, prefix: str, *, event: str) -> list[dict[str, Any]]:
    """Every readable record under ``prefix`` (unordered), as :func:`read_listing` reads them.

    One record that is there and cannot be read is SKIPPED with a warning rather than voiding the listing. That is the
    tolerance every caller was written for (these feed maintenance sweeps, where one bad record must not stop every
    other tenant being maintained), and it is also why a caller making a DESTRUCTIVE decision must not use this: a record
    it cannot read is not in the answer. Such a caller reads :func:`list_every_record`.

    Raises:
        OSError: The store did not answer the listing or a record's read.
    """
    return [raw for _, raw in read_listing(control_root, storage_options, prefix, event=event).records]


def list_every_record(control_root: str, storage_options: StorageOptions, prefix: str, *, event: str) -> list[dict[str, Any]]:
    """Every record under ``prefix`` (unordered), as :func:`read_listing` reads them, deciding on the whole listing or
    none.

    Raises:
        UnreadableRecordError: A record is there and cannot be read as one.
        OSError: The store did not answer the listing or a record's read.
    """
    listing = read_listing(control_root, storage_options, prefix, event=event)
    if listing.unreadable:
        raise UnreadableRecordError(f"the records at {', '.join(listing.unreadable)} cannot be read")
    return [raw for _, raw in listing.records]
