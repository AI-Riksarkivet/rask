"""The provenance a table's own bytes carry: the dataset id it declares.

`lineage.dataset_id` is schema metadata the catalog stamps on every table it writes, naming the table by its uuid
([[LH-520]]). The platform names no column of a table's: a row's run and inputs ride its commit (ADR 0156 C9).
"""

from typing import Final

import pyarrow as pa


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
