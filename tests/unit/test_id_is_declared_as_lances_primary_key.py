"""`id` is the merge key the tier contract requires, so it is declared as Lance's own primary key.

`ingest/catalog.py::A14` already stated the gap plainly: "NOT an 'unenforced primary key' in Lance's
sense … that feature is opt-in through field metadata (`lance-schema:unenforced-primary-key`), which
this plane sets nowhere … Declaring it properly is open work; claiming it in a comment was not the
same thing."

It matters more since 2026-09-06, when both cascade lanes became full-sync merges on `id`: the key is
now load-bearing at every write and was still only a convention each writer restated.

`lance_docs/file_format.md:2887-2910` (table/index.md § Unenforced Primary Key): declared through
field metadata, "useful for deduplication during merge-insert operations", and the field "and all its
ancestors, must not be nullable".

ADDITIVE, NOT A MIGRATION, and that is measured rather than hoped. The spec says the key is "fixed
after initial setting and must not be updated or removed", so an existing dataset cannot gain one.
Probed on pylance 10.0.0 against a dataset created WITHOUT the declaration:

    merge_insert("id")   -> OK          (the key is named by the caller)
    merge_insert(None)   -> ValueError  ("requires join keys: specify `on` columns explicitly")

So new tiers carry the format-level key, existing ones keep working, and the estate keeps naming `id`
explicitly — which is why the declaration buys correctness of DESCRIPTION rather than a call change:
the schema now says what the writers have always assumed.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from ingest.runtime import BRONZE_SCHEMA
from medallion.services.ingest import _INGEST_SCHEMA


_PK_KEY = b"lance-schema:unenforced-primary-key"


def _id_field(schema: pa.Schema) -> pa.Field:
    return schema.field("id")


def test_both_bronze_schemas_declare_id_as_the_primary_key() -> None:
    for name, schema in (("ingest.BRONZE_SCHEMA", BRONZE_SCHEMA), ("medallion._INGEST_SCHEMA", _INGEST_SCHEMA)):
        field = _id_field(schema)
        assert (field.metadata or {}).get(_PK_KEY) == b"true", (
            f"{name} does not declare `id` as Lance's unenforced primary key, so the key both cascade "
            f"lanes merge on exists only as a convention"
        )


def test_the_primary_key_field_is_not_nullable() -> None:
    """Lance's own requirement (`file_format.md:2896`): a primary key field and all its ancestors must
    not be nullable. A nullable declaration is not a weaker key, it is an invalid one."""
    for name, schema in (("ingest.BRONZE_SCHEMA", BRONZE_SCHEMA), ("medallion._INGEST_SCHEMA", _INGEST_SCHEMA)):
        assert not _id_field(schema).nullable, f"{name}'s `id` is nullable and cannot be a primary key"


def test_a_declared_schema_still_merges_into_a_dataset_created_without_one(tmp_path: Path) -> None:
    """The additive claim, driven rather than asserted: a tier written before this must keep accepting
    writes, because the key cannot be added to it and the data is not disposable."""
    import lance

    uri = str(tmp_path / "legacy.lance")
    legacy = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.string())])
    lance.write_dataset(
        pa.table({"id": [1, 2, 3], "v": ["a", "b", "c"]}, schema=legacy),
        uri,
        enable_stable_row_ids=True,
        data_storage_version="2.2",
    )
    declared = pa.schema([pa.field("id", pa.int64(), nullable=False, metadata={_PK_KEY: b"true"}), pa.field("v", pa.string())])
    source = pa.table({"id": [1, 2], "v": ["a", "B"]}, schema=declared)

    lance.dataset(uri).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(source)
    assert lance.dataset(uri).count_rows() == 3
