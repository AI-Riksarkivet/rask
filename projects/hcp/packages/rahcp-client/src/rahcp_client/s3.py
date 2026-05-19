"""S3Ops — presigned-first, multipart for large files."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from rahcp_client.tracing import tracer

if TYPE_CHECKING:
    from rahcp_client.client import HCPClient

log = logging.getLogger(__name__)


def _raise_for_presigned(resp: httpx.Response, bucket: str, key: str) -> None:
    """Raise a clean error for presigned URL failures, without leaking the signed URL."""
    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"{resp.status_code} {resp.reason_phrase} for {bucket}/{key}",
            request=resp.request,
            response=resp,
        )


class S3Ops:
    """S3 data-plane operations — presigned-first, multipart for large files.

    All data transfer prefers presigned URLs. Files above
    ``client.multipart_threshold`` automatically use multipart upload.

    Path mapping to the backend API (all under /api/v1):
        GET  /buckets                              — list buckets
        GET  /buckets/{b}/objects                  — list objects
        POST /presign                              — single presigned URL
        POST /buckets/{b}/objects/presign          — bulk presigned URLs
        POST /buckets/{b}/multipart/{key}          — initiate multipart
        POST /buckets/{b}/multipart/{key}/presign  — presign parts
        POST /buckets/{b}/multipart/{key}/complete — complete multipart
        DELETE /buckets/{b}/objects/{key}           — delete object
        POST /buckets/{b}/objects/delete            — bulk delete
        POST /buckets/{b}/objects/{key}/copy       — copy object
        HEAD /buckets/{b}/objects/{key}             — head object
    """

    def __init__(self, client: HCPClient) -> None:
        self._client = client

    def _make_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._client.verify_ssl,
            timeout=httpx.Timeout(self._client.timeout, connect=10.0),
        )

    # ── Presigned URLs ────────────────────────────────────────────

    async def presign_get(self, bucket: str, key: str, *, expires: int = 3600) -> str:
        """Get a presigned download URL."""
        resp = await self._client.request(
            "POST",
            "/presign",
            json={
                "bucket": bucket,
                "key": key,
                "method": "get_object",
                "expires_in": expires,
            },
        )
        return resp.json()["url"]

    async def presign_put(self, bucket: str, key: str, *, expires: int = 3600) -> str:
        """Get a presigned upload URL."""
        resp = await self._client.request(
            "POST",
            "/presign",
            json={
                "bucket": bucket,
                "key": key,
                "method": "put_object",
                "expires_in": expires,
            },
        )
        return resp.json()["url"]

    async def presign_bulk(
        self,
        bucket: str,
        keys: list[str],
        *,
        method: str = "get_object",
        expires: int = 3600,
    ) -> dict[str, str]:
        """Presign multiple keys in one API call. Returns {key: url} mapping."""
        resp = await self._client.request(
            "POST",
            f"/buckets/{bucket}/objects/presign",
            json={
                "keys": keys,
                "method": method,
                "expires_in": expires,
            },
        )
        return {item["key"]: item["url"] for item in resp.json()["urls"]}

    # ── Single-file transfers ─────────────────────────────────────

    async def upload(self, bucket: str, key: str, data: bytes | Path) -> str:
        """Upload data — auto-selects presigned PUT or multipart based on size.

        Returns the ETag of the uploaded object.
        """
        with tracer.start_as_current_span("s3.upload") as span:
            span.set_attribute("s3.bucket", bucket)
            span.set_attribute("s3.key", key)

            if isinstance(data, Path):
                size = await asyncio.to_thread(lambda: data.stat().st_size)
                span.set_attribute("s3.size", size)
                if size >= self._client.multipart_threshold:
                    return await self.upload_multipart(bucket, key, data)
                content = await asyncio.to_thread(data.read_bytes)
            else:
                content = data
                span.set_attribute("s3.size", len(content))

            url = await self.presign_put(bucket, key)
            async with self._make_http_client() as http:
                resp = await http.put(url, content=content)
                _raise_for_presigned(resp, bucket, key)
            log.debug("Uploaded %s/%s (%s bytes)", bucket, key, len(content))
            return resp.headers.get("etag", "")

    async def download(self, bucket: str, key: str, dest: Path) -> int:
        """Download an object via presigned GET. Returns byte count."""
        with tracer.start_as_current_span("s3.download") as span:
            span.set_attribute("s3.bucket", bucket)
            span.set_attribute("s3.key", key)
            url = await self.presign_get(bucket, key)
            async with self._make_http_client() as http:
                async with http.stream("GET", url) as resp:
                    _raise_for_presigned(resp, bucket, key)
                    total = 0
                    file_handle = await asyncio.to_thread(dest.open, "wb")
                    try:
                        async for chunk in resp.aiter_bytes(chunk_size=8192):
                            await asyncio.to_thread(file_handle.write, chunk)
                            total += len(chunk)
                    finally:
                        await asyncio.to_thread(file_handle.close)
            span.set_attribute("s3.bytes", total)
            log.debug("Downloaded %s/%s (%d bytes)", bucket, key, total)
            return total

    async def download_bytes(self, bucket: str, key: str) -> bytes:
        """Download an object as bytes via presigned GET."""
        url = await self.presign_get(bucket, key)
        async with self._make_http_client() as http:
            resp = await http.get(url)
            _raise_for_presigned(resp, bucket, key)
        return resp.content

    # ── Multipart upload ──────────────────────────────────────────

    async def upload_multipart(
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        concurrency: int | None = None,
    ) -> str:
        """Presigned multipart upload for large files.

        1. Initiate multipart → upload_id
        2. Presign each part
        3. Upload parts in parallel (bounded semaphore)
        4. Complete multipart (or abort on failure)
        """
        concurrency = concurrency or self._client.multipart_concurrency
        file_size = await asyncio.to_thread(lambda: path.stat().st_size)
        default_part_size = self._client.multipart_chunk

        resp = await self._client.request(
            "POST",
            f"/buckets/{bucket}/multipart/{key}",
        )
        upload_id = resp.json()["upload_id"]

        try:
            resp = await self._client.request(
                "POST",
                f"/buckets/{bucket}/multipart/{key}/presign",
                json={
                    "upload_id": upload_id,
                    "file_size": file_size,
                    "part_size": default_part_size,
                },
            )
            data = resp.json()
            part_urls = [p["url"] for p in data["urls"]]
            part_size = data.get("part_size", default_part_size)

            sem = asyncio.Semaphore(concurrency)

            part_timeout = httpx.Timeout(
                max(300.0, self._client.timeout * 5), connect=30.0
            )

            async def _upload_part(part_num: int, url: str) -> dict[str, Any]:
                offset = part_num * part_size
                read_size = min(part_size, file_size - offset)

                def _read_chunk() -> bytes:
                    with path.open("rb") as f:
                        f.seek(offset)
                        return f.read(read_size)

                part_data = await asyncio.to_thread(_read_chunk)
                async with sem:
                    async with httpx.AsyncClient(
                        verify=self._client.verify_ssl, timeout=part_timeout
                    ) as http:
                        log.debug(
                            "Uploading part %d/%d (%d bytes) for %s/%s",
                            part_num + 1,
                            len(part_urls),
                            len(part_data),
                            bucket,
                            key,
                        )
                        resp = await http.put(url, content=part_data)
                        _raise_for_presigned(
                            resp, bucket, f"{key} (part {part_num + 1})"
                        )
                        log.debug(
                            "Part %d/%d uploaded (etag: %s)",
                            part_num + 1,
                            len(part_urls),
                            resp.headers.get("etag", "?"),
                        )
                        return {
                            "part_number": part_num + 1,
                            "etag": resp.headers["etag"],
                        }

            parts = await asyncio.gather(
                *[_upload_part(i, url) for i, url in enumerate(part_urls)]
            )

            parts_sorted = sorted(parts, key=lambda p: p["part_number"])
            resp = await self._client.request(
                "POST",
                f"/buckets/{bucket}/multipart/{key}/complete",
                json={
                    "upload_id": upload_id,
                    "parts": [
                        {"ETag": p["etag"], "PartNumber": p["part_number"]}
                        for p in parts_sorted
                    ],
                },
            )
            log.info(
                "Multipart upload complete: %s/%s (%d parts)",
                bucket,
                key,
                len(parts_sorted),
            )
            return resp.json().get("etag", "")

        except Exception:
            log.warning("Multipart upload failed, aborting: %s/%s", bucket, key)
            try:
                await self._client.request(
                    "POST",
                    f"/buckets/{bucket}/multipart/{key}/abort",
                    json={"upload_id": upload_id},
                )
            except Exception:
                log.warning("Failed to abort multipart upload: %s", upload_id)
            raise

    # ── Listing & metadata ────────────────────────────────────────

    async def list_buckets(self) -> dict[str, Any]:
        """List all S3 buckets."""
        resp = await self._client.request("GET", "/buckets")
        return resp.json()

    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        *,
        max_keys: int = 1000,
        continuation_token: str | None = None,
        delimiter: str | None = None,
    ) -> dict[str, Any]:
        """List objects in a bucket with optional prefix and pagination."""
        params: dict[str, Any] = {"prefix": prefix, "max_keys": max_keys}
        if continuation_token:
            params["continuation_token"] = continuation_token
        if delimiter:
            params["delimiter"] = delimiter
        resp = await self._client.request(
            "GET",
            f"/buckets/{bucket}/objects",
            params=params,
        )
        return resp.json()

    async def _paginate_objects(
        self, bucket: str, prefix: str, *, max_keys: int = 1000
    ) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over all objects under a prefix."""
        token: str | None = None
        while True:
            data = await self.list_objects(
                bucket, prefix, max_keys=max_keys, continuation_token=token
            )
            for obj in data.get("objects", []):
                yield obj
            if not data.get("is_truncated"):
                return
            token = data.get("next_continuation_token")

    # ── Mutations ─────────────────────────────────────────────────

    async def delete(self, bucket: str, key: str) -> None:
        """Delete a single object."""
        await self._client.request("DELETE", f"/buckets/{bucket}/objects/{key}")

    async def delete_bulk(self, bucket: str, keys: list[str]) -> dict[str, Any]:
        """Delete multiple objects."""
        resp = await self._client.request(
            "POST",
            f"/buckets/{bucket}/objects/delete",
            json={"keys": keys},
        )
        return resp.json()

    async def copy(
        self,
        bucket: str,
        key: str,
        source_bucket: str,
        source_key: str,
    ) -> None:
        """Copy an object."""
        await self._client.request(
            "POST",
            f"/buckets/{bucket}/objects/{key}/copy",
            json={
                "source_bucket": source_bucket,
                "source_key": source_key,
            },
        )

    async def head(self, bucket: str, key: str) -> dict[str, Any]:
        """Get object metadata (content-length, content-type, etag, last-modified)."""
        resp = await self._client.request("HEAD", f"/buckets/{bucket}/objects/{key}")
        return dict(resp.headers)

    # ── Staging ───────────────────────────────────────────────────

    async def commit_staging(
        self, bucket: str, staging_prefix: str, dest_prefix: str
    ) -> int:
        """Move objects from staging prefix to destination. Returns count."""
        count = 0
        async for obj in self._paginate_objects(bucket, staging_prefix):
            src_key = obj["Key"]
            dest_key = src_key.replace(staging_prefix, dest_prefix, 1)
            await self.copy(bucket, dest_key, bucket, src_key)
            await self.delete(bucket, src_key)
            count += 1
        return count

    async def cleanup_staging(self, bucket: str, staging_prefix: str) -> int:
        """Delete all objects under a staging prefix. Paginates. Returns count."""
        total = 0
        batch: list[str] = []
        async for obj in self._paginate_objects(bucket, staging_prefix):
            batch.append(obj["Key"])
            if len(batch) >= 1000:
                await self.delete_bulk(bucket, batch)
                total += len(batch)
                batch.clear()
        if batch:
            await self.delete_bulk(bucket, batch)
            total += len(batch)
        return total
