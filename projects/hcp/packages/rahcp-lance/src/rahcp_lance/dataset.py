"""LanceDataset — manage Lance datasets stored on HCP S3."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import lancedb  # ty: ignore[unresolved-import]
import pyarrow as pa

from rahcp_lance.schemas import FieldInfo, IngestResult, TableInfo

if TYPE_CHECKING:
    from rahcp_client.client import HCPClient

log = logging.getLogger(__name__)


class LanceDataset:
    """Manage Lance datasets stored on HCP S3.

    Uses presigned S3 credentials from the HCP backend to connect
    lancedb to the remote storage.
    """

    def __init__(self, client: HCPClient, bucket: str, prefix: str = "") -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._db: lancedb.DBConnection | None = None

    async def _ensure_db(self) -> lancedb.DBConnection:
        """Lazily connect to the Lance database on S3."""
        if self._db is None:
            uri = (
                f"s3://{self._bucket}/{self._prefix}"
                if self._prefix
                else f"s3://{self._bucket}"
            )
            self._db = await lancedb.connect_async(uri)
        return self._db

    async def list_tables(self) -> list[str]:
        """List all tables in the dataset."""
        db = await self._ensure_db()
        return await db.table_names()

    async def open(self, table_name: str) -> lancedb.table.AsyncTable:
        """Open an existing table."""
        db = await self._ensure_db()
        return await db.open_table(table_name)

    async def create(
        self,
        table_name: str,
        schema: pa.Schema | None = None,
        data: Any = None,
    ) -> lancedb.table.AsyncTable:
        """Create a new table with schema and optional initial data."""
        db = await self._ensure_db()
        if data is not None:
            table = await db.create_table(table_name, data=data)
        elif schema is not None:
            table = await db.create_table(table_name, schema=schema)
        else:
            msg = "Either schema or data must be provided"
            raise ValueError(msg)
        log.info("Created table %s in %s/%s", table_name, self._bucket, self._prefix)
        return table

    async def drop(self, table_name: str) -> None:
        """Drop a table."""
        db = await self._ensure_db()
        await db.drop_table(table_name)
        log.info("Dropped table %s", table_name)

    async def table_info(self, table_name: str) -> TableInfo:
        """Get metadata about a table."""
        table = await self.open(table_name)
        schema = await table.schema()
        return TableInfo(
            name=table_name,
            num_rows=await table.count_rows(),
            schema_fields=[
                FieldInfo(
                    name=field.name,
                    dtype=str(field.type),
                    nullable=field.nullable,
                )
                for field in schema
            ],
        )

    async def ingest(
        self,
        table_name: str,
        data: pa.RecordBatch | pa.Table | list[dict[str, Any]],
    ) -> IngestResult:
        """Add data to an existing table. Returns ingest summary."""
        table = await self.open(table_name)
        if isinstance(data, list):
            rows_added = len(data)
        elif isinstance(data, pa.RecordBatch):
            rows_added = data.num_rows
        else:
            rows_added = data.num_rows
        await table.add(data)
        total = await table.count_rows()
        return IngestResult(
            table=table_name,
            rows_added=rows_added,
            total_rows=total,
        )
