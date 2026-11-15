"""The provenance a table's own bytes carry: the one column the platform names and the dataset id it declares.

`source_rowid` is the one column the platform names: an opt-in a pipeline writes so impact analysis can trace a row
to the row it descends from (ADR 0156 C9). The catalog's provenance guard reserves it and the publish door's quality
gate judges its type, so the name lives here, once, for both. `lineage.dataset_id` is schema metadata the catalog
stamps on every table it writes, naming the table by its uuid ([[LH-520]]).

IT LIVES IN THE LIBRARY rather than in a service because the catalog and its quality gate both read it, and a
governance contract cannot belong to one of its readers.
"""

from __future__ import annotations

from typing import Final

import pyarrow as pa


#: The row a row descends from: an opt-in column a pipeline writes, held to uint64 stable row ids.
SOURCE_ROWID_COLUMN: Final = "source_rowid"

#: The canonical catalog name of the dataset a row was written INTO — schema metadata rather than a
#: column, because it describes the dataset and not the row. Maintenance names a dataset by the catalog's
#: `__manifest` alone and never by this stamp ([[LH-520]]): a writer can set it to any name.
LINEAGE_DATASET_ID_KEY: Final = "lineage.dataset_id"


def declare_dataset_id(table: pa.Table, dataset_id: str | None) -> pa.Table:
    """Set the destination's canonical name on the schema, or DROP an inherited one when unwired.

    Schema metadata survives every column operation `set_column`/`append_column`/`drop_columns`
    performs, so without this a table derived from another publishes its PARENT's name. Merges the rest: Lance keeps
    other producers' schema metadata (the #21 self-describing coordinates among them) and a replace would destroy it.
    """
    metadata = dict(table.schema.metadata or {})
    key = LINEAGE_DATASET_ID_KEY.encode()
    if dataset_id:
        metadata[key] = dataset_id.encode()
    elif key not in metadata:
        return table
    else:
        del metadata[key]
    return table.replace_schema_metadata(metadata)
