"""Protocols for the bulk transfer engine — defines the S3 interface it needs."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class TransferSettings(BaseModel, frozen=True):
    """Client settings needed by the bulk transfer engine.

    Exposed via ``BulkClient.transfer_settings`` so the engine never
    reaches into private client attributes.
    """

    verify_ssl: bool = True
    timeout: float = 60.0
    multipart_threshold: int = 100 * 1024 * 1024


class S3Client(Protocol):
    """Minimal S3 interface required by the bulk transfer engine."""

    async def head(self, bucket: str, key: str) -> dict: ...
    async def upload(self, bucket: str, key: str, data: Path) -> str: ...
    async def download(self, bucket: str, key: str, dest: Path) -> int: ...
    async def presign_put(
        self, bucket: str, key: str, *, expires: int = 3600
    ) -> str: ...
    async def presign_get(
        self, bucket: str, key: str, *, expires: int = 3600
    ) -> str: ...
    async def presign_bulk(
        self,
        bucket: str,
        keys: list[str],
        *,
        method: str = "get_object",
        expires: int = 3600,
    ) -> dict[str, str]: ...
    async def list_objects(
        self,
        bucket: str,
        prefix: str,
        *,
        max_keys: int = 1000,
        continuation_token: str | None = None,
    ) -> dict: ...


class BulkClient(Protocol):
    """Client that exposes ``s3`` and ``transfer_settings``."""

    @property
    def s3(self) -> S3Client: ...

    @property
    def transfer_settings(self) -> TransferSettings: ...
