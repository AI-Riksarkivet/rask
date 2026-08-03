"""The catalog seam — creation is server-side, appends are the lander's (D6, §0 C10).

`LocalCatalog` is the filesystem implementation used by tests and local runs. The in-cluster client
that calls the catalog service lands with the commit-through step; both satisfy
`ingest.lander.CatalogClient`, so the lander never learns which one it has.

Creation lives HERE and not in the lander because the catalog's own door refuses it: the
client-direct fragment endpoint hardcodes `LanceOperation.Append` and rules that "CREATE and
OVERWRITE stay server-side to centralize it and to owner-govern the destructive reset"
(`services/catalog/.../dataplane.py:594-615`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ingest.lander import create_empty


if TYPE_CHECKING:
    import pyarrow as pa


class LocalCatalog:
    """Filesystem catalog: creates the dataset empty with the creation-time flags, records versions."""

    def __init__(self, schema: pa.Schema) -> None:
        self._schema = schema
        self.registered: list[tuple[str, int, str]] = []

    def ensure_dataset(self, project: str, dataset: str, schema: pa.Schema | None = None) -> str:
        raise NotImplementedError("resolved through dataset_uri(); ensure_at() is the path-based form")

    def ensure_at(self, uri: str) -> str:
        """Create the dataset EMPTY if absent.

        The creation-time flags (`enable_stable_row_ids`, `data_storage_version=2.2`) are set here or
        never: they are silent no-ops afterwards (`lance_docs/file_format.md:4011-4013`), and CDF
        plus every silver `source_rowid` reference depends on them existing from version 1.
        """
        if not Path(uri).exists():
            Path(uri).parent.mkdir(parents=True, exist_ok=True)
            create_empty(uri, self._schema)
        return uri

    def register_version(self, dataset_uri: str, version: int, run_id: str) -> None:
        """Record the committed version with the run id — the reconciliation anchor."""
        self.registered.append((dataset_uri, version, run_id))
