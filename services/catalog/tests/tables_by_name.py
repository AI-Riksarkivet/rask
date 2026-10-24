"""A table a test arranges or reads by its name: the checked open of the location the name describes to now.

No catalog door opens a table by its name: each acts on the location its request resolved (``table_claims.DoorTable``,
ADR 0173 §7, [[LH-530]]). A test arranging a table before a request, or reading what a request left, holds a name and no
resolution, so it opens where the backend describes that name.
"""

from __future__ import annotations

import lance
from lance_namespace import DescribeTableRequest, LanceNamespace, TableNotFoundError

from catalog.core.namespace import open_resolved_dataset


def open_by_name(
    ns: LanceNamespace, storage_options: dict[str, str], table_id: list[str], *, version: int | None = None, branch: str | None = None
) -> lance.LanceDataset:
    """``namespace.open_resolved_dataset`` on the ref named, at the location ``table_id`` describes to now.

    Raises:
        TableNotFoundError: The name describes to no location.
    """
    described = ns.describe_table(DescribeTableRequest(id=list(table_id)))
    if not described.location:
        raise TableNotFoundError(f"table {table_id} describes to no location")
    return open_resolved_dataset(str(described.location), storage_options, table_id, version=version, branch=branch)
