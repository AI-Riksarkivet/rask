"""IIIF manifest parsing — extract image IDs from Riksarkivet manifests."""

from __future__ import annotations

import logging

import httpx

from rahcp_iiif.config import IIIF_TIMEOUT, IIIF_URL

log = logging.getLogger(__name__)


async def get_image_ids(
    batch_id: str,
    *,
    base_url: str = IIIF_URL,
    timeout: float = IIIF_TIMEOUT,
) -> list[str]:
    """Fetch a IIIF manifest and extract image IDs.

    Args:
        batch_id: Volume/batch identifier (e.g. "C0074667").
        base_url: IIIF server base URL.
        timeout: HTTP request timeout in seconds.

    Returns:
        Sorted list of image IDs (e.g. ["C0074667_00001", "C0074667_00002", ...]).
    """
    manifest_url = f"{base_url}/arkis!{batch_id}/manifest"
    log.info("Fetching manifest: %s", manifest_url)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(manifest_url)
        resp.raise_for_status()
        data = resp.json()

    image_ids = []
    for item in data.get("items", []):
        raw_id = item.get("id", "")
        if "!" in raw_id:
            image_id = raw_id.split("!")[1][:14].upper()
            image_ids.append(image_id)

    log.info("Found %d images in batch %s", len(image_ids), batch_id)
    return sorted(image_ids)


def build_image_url(
    image_id: str,
    *,
    base_url: str = IIIF_URL,
    query_params: str = "full/max/0/default.jpg",
) -> str:
    """Build the IIIF image URL for a given image ID.

    Args:
        image_id: Image identifier (e.g. "C0074667_00001").
        base_url: IIIF server base URL.
        query_params: IIIF image API parameters (region/size/rotation/quality.format).

    Returns:
        Full image URL.
    """
    return f"{base_url}/arkis!{image_id}/{query_params}"


def file_extension(query_params: str = "full/max/0/default.jpg") -> str:
    """Extract file extension from IIIF query params (e.g. '.jpg')."""
    return "." + query_params.rsplit(".", 1)[-1]
