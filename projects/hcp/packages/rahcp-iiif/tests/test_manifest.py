"""Tests for IIIF manifest parsing."""

import httpx
import pytest
import respx

from rahcp_iiif.manifest import build_image_url, file_extension, get_image_ids

BASE = "https://test-iiif.example.com"


@pytest.mark.asyncio
@respx.mock
async def test_get_image_ids_parses_manifest():
    manifest = {
        "items": [
            {"id": "https://iiif.example.com/arkis!C0074667_00001/canvas"},
            {"id": "https://iiif.example.com/arkis!C0074667_00002/canvas"},
            {"id": "https://iiif.example.com/arkis!C0074667_00003/canvas"},
        ]
    }
    respx.get(f"{BASE}/arkis!C0074667/manifest").mock(
        return_value=httpx.Response(200, json=manifest)
    )

    ids = await get_image_ids("C0074667", base_url=BASE)
    assert ids == ["C0074667_00001", "C0074667_00002", "C0074667_00003"]


@pytest.mark.asyncio
@respx.mock
async def test_get_image_ids_empty_manifest():
    respx.get(f"{BASE}/arkis!EMPTY/manifest").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    ids = await get_image_ids("EMPTY", base_url=BASE)
    assert ids == []


@pytest.mark.asyncio
@respx.mock
async def test_get_image_ids_uppercases():
    manifest = {
        "items": [
            {"id": "https://iiif.example.com/arkis!c0074667_00001/canvas"},
        ]
    }
    respx.get(f"{BASE}/arkis!lower/manifest").mock(
        return_value=httpx.Response(200, json=manifest)
    )
    ids = await get_image_ids("lower", base_url=BASE)
    assert ids == ["C0074667_00001"]


def test_build_image_url():
    url = build_image_url("C0074667_00001", base_url=BASE)
    assert url == f"{BASE}/arkis!C0074667_00001/full/max/0/default.jpg"


def test_build_image_url_custom_params():
    url = build_image_url(
        "C0074667_00001",
        base_url=BASE,
        query_params="full/,1200/0/default.jpg",
    )
    assert url == f"{BASE}/arkis!C0074667_00001/full/,1200/0/default.jpg"


def test_file_extension():
    assert file_extension("full/max/0/default.jpg") == ".jpg"
    assert file_extension("full/max/0/default.tif") == ".tif"
    assert file_extension("full/max/0/default.png") == ".png"
