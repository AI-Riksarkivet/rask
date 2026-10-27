"""The record of a principal the estate admin offboarded, kept under the control root ([[LH-233]]).

A principal exists in the estate only as the tuples naming it, so once its grants are revoked nothing would say it was
ever removed, by whom or when. The offboarding door writes one record per principal at
``_principals/<blake2s(principal)>.json``: the key is a digest because a principal key may hold any character its
issuer's subject does, which an object key cannot carry as written. The first deletion's record stands; a re-run of the
door, which revokes whatever was granted since, keeps it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import blake2s
from typing import Final

from catalog.schemas import PrincipalRecord
from service_kit.governed.principal import Principal
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.records import RecordExistsError, create_json, read_json


_PRINCIPALS_PREFIX: Final = "_principals"


def _key(principal: str) -> str:
    return f"{_PRINCIPALS_PREFIX}/{blake2s(principal.encode('utf-8')).hexdigest()}.json"


def read_record(control_root: str, storage_options: StorageOptions, principal: str) -> PrincipalRecord | None:
    """The deletion record of ``principal``, or ``None`` when it was never offboarded."""
    stored = read_json(control_root, storage_options, _key(principal))
    return None if stored is None else PrincipalRecord.model_validate(stored[0])


def record_deletion(control_root: str, storage_options: StorageOptions, principal: str, *, deleted_by: str) -> PrincipalRecord:
    """Record ``principal`` deleted by ``deleted_by`` now, or answer the record an earlier deletion wrote."""
    record = PrincipalRecord(
        principal=principal,
        kind="service" if Principal.parse(principal).is_machine else "person",
        deleted_at=datetime.now(UTC),
        deleted_by=deleted_by,
    )
    try:
        create_json(control_root, storage_options, _key(principal), record.model_dump(mode="json"))
    except RecordExistsError:
        existing = read_record(control_root, storage_options, principal)
        if existing is None:
            raise
        return existing
    return record
