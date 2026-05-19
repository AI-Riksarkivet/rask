"""IIIF service — manifest fetching and image URL building.

Wraps Riksarkivet's IIIF Image API. All manifest fetches go through
a shared httpx client with connection pooling and OTel tracing.
"""

from __future__ import annotations

import logging

import httpx
from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

DEFAULT_IIIF_URL = "https://iiifintern-ai.ra.se"
DEFAULT_QUERY_PARAMS = "full/max/0/default.jpg"
DEFAULT_TIMEOUT = 60.0


class IiifError(Exception):
    """Raised when an IIIF operation fails."""


class IiifService:
    """IIIF manifest and image URL service.

    Maintains a shared httpx.AsyncClient for connection pooling.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_IIIF_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def get_image_ids(self, batch_id: str) -> list[str]:
        """Fetch a IIIF manifest and extract sorted image IDs.

        Args:
            batch_id: Volume/batch identifier (e.g. "C0074667").

        Returns:
            Sorted list of image IDs.
        """
        manifest_url = f"{self._base_url}/arkis!{batch_id}/manifest"
        with tracer.start_as_current_span("iiif.get_image_ids") as span:
            span.set_attribute("iiif.batch_id", batch_id)
            span.set_attribute("iiif.manifest_url", manifest_url)
            try:
                resp = await self._client.get(manifest_url)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                raise IiifError(
                    f"Manifest fetch failed for {batch_id!r}: {exc.response.status_code}"
                ) from exc
            except Exception as exc:
                raise IiifError(
                    f"Cannot fetch manifest for {batch_id!r}: {exc}"
                ) from exc

            image_ids: list[str] = []
            for item in data.get("items", []):
                raw_id = item.get("id", "")
                if "!" in raw_id:
                    image_id = raw_id.split("!")[1][:14].upper()
                    image_ids.append(image_id)

            span.set_attribute("iiif.image_count", len(image_ids))
            logger.info("Found %d images in batch %s", len(image_ids), batch_id)
            return sorted(image_ids)

    def build_image_url(
        self,
        image_id: str,
        query_params: str = DEFAULT_QUERY_PARAMS,
    ) -> str:
        """Build the IIIF image URL for a given image ID."""
        return f"{self._base_url}/arkis!{image_id}/{query_params}"

    def build_image_urls(
        self,
        image_ids: list[str],
        query_params: str = DEFAULT_QUERY_PARAMS,
    ) -> dict[str, str]:
        """Build image URLs for multiple image IDs.

        Returns:
            Mapping of image_id → full URL.
        """
        return {
            image_id: self.build_image_url(image_id, query_params)
            for image_id in image_ids
        }

    @staticmethod
    def file_extension(query_params: str = DEFAULT_QUERY_PARAMS) -> str:
        """Extract file extension from IIIF query params (e.g. '.jpg')."""
        return "." + query_params.rsplit(".", 1)[-1]
