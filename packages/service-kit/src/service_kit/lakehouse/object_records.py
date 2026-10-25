"""The control-root records a governed object holds under its uuid, in one list (ADR 0173 §8, [[LH-523]]).

Every record rask attaches to a table or namespace on the control root is keyed ``record_key(<prefix>, kind, uuid)``,
by the uuid the catalog minted for the object, never by its name. A rename keeps the uuid, so it moves no record, and a
name another object gave up reaches none of them. A door that ends an object ends its records through
:data:`OBJECT_RECORD_PREFIXES` rather than naming the stores one by one, so none ends some and forgets the rest; a
record an ending missed governs nothing, since no other object holds its uuid.

Not listed, each for its own reason:

- The trash record (:mod:`service_kit.lakehouse.trash`) is the record of an object that is not live, and a drop files it
  rather than ending it.
- Grants live in OpenFGA; the catalog's ``fga_deps`` revokes them.
- Location claims and base records are keyed by the table's location, and the claim records the uuid.
- A promotion hold, a due record and an erasure obligation each end on their own terms: the hold expires, the sweep
  clears a due record no table holds, and an obligation stays as the record of a request.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from typing import Final

from service_kit.lakehouse import commit_records, history_fences, maintenance_policies, protection
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import delete_record, record_key


#: Every control-root registry holding one record per object under its uuid.
OBJECT_RECORD_PREFIXES: Final = (protection.PREFIX, maintenance_policies.PREFIX, history_fences.PREFIX, commit_records.MARKS_PREFIX)


def end_object_records(control_root: str, storage_options: StorageOptions, kind: str, uuid: str) -> list[str]:
    """Delete every record the object ``uuid`` names holds, and return the prefixes of the records that existed.

    A table also holds one record per stock commit whose author the catalog recorded (:mod:`commit_records`), under a
    directory of its uuid, which goes with the rest.
    """
    ended: list[str] = []
    for prefix in OBJECT_RECORD_PREFIXES:
        if delete_record(control_root, storage_options, record_key(prefix, kind, uuid)):
            ended.append(prefix)
    if kind == "table" and commit_records.end_authors(control_root, storage_options, uuid):
        ended.append(commit_records.AUTHORS_PREFIX)
    return ended
