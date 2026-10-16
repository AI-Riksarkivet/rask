"""The control-root records a governed object holds under its name, in one list ([[LH-215]]).

A lance-ns table or namespace id is its name path, and Lance keeps no table UUID (`lance_docs/ns_catalog/catalog/dir/index.md`;
RenameTable in `lance_docs/ns_catalog/spec.yaml`). So every record rask attaches to an object on the control root is keyed
``record_key(<prefix>, kind, canonical_id)``, and a door that retires a name carries each record or ends it. A record left
under a retired name governs no object, or governs the next one created there. The doors iterate
:data:`OBJECT_RECORD_PREFIXES` rather than naming the stores one by one, so none carries some and forgets the rest.

Not listed, each for its own reason:

- The trash record (:mod:`service_kit.lakehouse.trash`) is the record of an object that is not live. A rename's source is
  live and its destination is refused while one stands, and a drop files the record rather than ending it.
- Grants live in OpenFGA; the catalog's ``fga_deps.move_ownership`` carries them.
- Location claims and base records are keyed by the table's location, which a rename keeps. A claim names its holder,
  and the rename hands each claim the table holds to the new name.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from typing import Final

from service_kit.lakehouse import maintenance_policies, protection
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import delete_record, get_record, put_record, record_key


#: Every control-root registry holding one record per object under its name.
OBJECT_RECORD_PREFIXES: Final = (protection.PREFIX, maintenance_policies.PREFIX)

#: The event stem for a malformed record read here (`object_record_malformed`).
_EVENT = "object_record"


def move_object_records(control_root: str, storage_options: StorageOptions, kind: str, *, old_id: str, new_id: str) -> list[str]:
    """Move every record ``old_id`` holds to ``new_id``, and return the prefixes of the records moved.

    Each record is written under the new name before it is deleted under the old one, so a failure part-way leaves a
    record under both names rather than under neither: the object keeps its protection and configuration, and the
    caller logs the failure, naming the retired name whose copy an operator ends.
    """
    moved: list[str] = []
    for prefix in OBJECT_RECORD_PREFIXES:
        record = get_record(control_root, storage_options, record_key(prefix, kind, old_id), event=_EVENT)
        if record is None:
            continue
        put_record(control_root, storage_options, record_key(prefix, kind, new_id), {**record, "id": new_id})
        delete_record(control_root, storage_options, record_key(prefix, kind, old_id))
        moved.append(prefix)
    return moved


def end_object_records(control_root: str, storage_options: StorageOptions, kind: str, canonical_id: str) -> list[str]:
    """Delete every record ``canonical_id`` holds, and return the prefixes of the records that existed."""
    ended: list[str] = []
    for prefix in OBJECT_RECORD_PREFIXES:
        if delete_record(control_root, storage_options, record_key(prefix, kind, canonical_id)):
            ended.append(prefix)
    return ended
