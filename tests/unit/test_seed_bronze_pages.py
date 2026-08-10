"""``scripts/seed_bronze_pages.py`` — the bronze page seed, and the acquisition it had to grow back.

WHY THIS SUITE EXISTS. The script imported `medallion.services.iiif_produce.IIIFVolumeSource`, which
`09823f56` ("the medallion sheds acquisition") deleted. Nothing noticed for days: the script is run by
`scripts/seed_dev_estate.sh` (step 3 of `make seed-dev`), not by any test, so its only reader was
`ty` — where it sat as the repo's last diagnostic while step 3 failed with `ModuleNotFoundError`
before doing a single thing. A script whose only gate is a type-checker is a script that can be broken
by deleting something else.

So the adapter now lives in the script, built on the surviving `storage` IIIF helpers, and this file
is the gate that a type-checker could not be: it drives `iter_objects` end to end against a mocked
IIIF endpoint. Transport-level (`respx`) rather than by patching the helpers, because what has to be
right is the REQUEST that would reach Riksarkivet — the manifest URL, the per-image URL built from
`SEED_IIIF_BASE`, and the page cap actually capping.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx


BASE = "https://iiif.example.test"
VOLUME = "A0060198"


def _load() -> ModuleType:
    """Load the script by PATH — `scripts/` is not a package, exactly as `test_seed_estate.py` does."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_bronze_pages.py"
    spec = importlib.util.spec_from_file_location("seed_bronze_pages", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest(count: int) -> dict[str, object]:
    """A Riksarkivet manifest, in the shape `storage.get_image_ids` actually parses: the image id is
    the 14 chars after the `!`."""
    return {"items": [{"id": f"{BASE}/arkis!{VOLUME}_{n:05d}/canvas"} for n in range(1, count + 1)]}


@pytest.fixture
def seed() -> ModuleType:
    return _load()


@respx.mock
def test_it_yields_one_source_object_per_page_with_iiif_provenance(seed: ModuleType) -> None:
    respx.get(f"{BASE}/arkis!{VOLUME}/manifest").mock(return_value=httpx.Response(200, json=_manifest(2)))
    for n in (1, 2):
        respx.get(f"{BASE}/arkis!{VOLUME}_{n:05d}/full/max/0/default.jpg").mock(return_value=httpx.Response(200, content=f"page{n}".encode()))

    src = seed.IIIFVolumeSource(VOLUME, base_url=BASE, query_params="full/max/0/default.jpg", timeout=5.0, max_pages=10)
    objects = list(src.iter_objects())

    assert [o.data for o in objects] == [b"page1", b"page2"]
    # The URI is the OBJECT's identity, never the route used to fetch it — `SEED_IIIF_BASE` differs
    # between RA's internal endpoint and the public one, and provenance must not record which host a
    # developer happened to use.
    assert [o.uri for o in objects] == [f"iiif://{VOLUME}/{VOLUME}_00001", f"iiif://{VOLUME}/{VOLUME}_00002"]


@respx.mock
def test_max_pages_caps_the_FETCHES_not_only_the_result(seed: ModuleType) -> None:
    """A cap applied after fetching would still pull the whole volume — the seed's whole point is that
    three pages cost three requests, not a full harvest that is then sliced."""
    respx.get(f"{BASE}/arkis!{VOLUME}/manifest").mock(return_value=httpx.Response(200, json=_manifest(5)))
    routes = [respx.get(f"{BASE}/arkis!{VOLUME}_{n:05d}/full/max/0/default.jpg").mock(return_value=httpx.Response(200, content=b"x")) for n in range(1, 6)]

    objects = list(seed.IIIFVolumeSource(VOLUME, base_url=BASE, query_params="full/max/0/default.jpg", timeout=5.0, max_pages=2).iter_objects())

    assert len(objects) == 2
    assert [r.called for r in routes] == [True, True, False, False, False]


@respx.mock
def test_the_query_params_reach_the_image_url(seed: ModuleType) -> None:
    """The IIIF Image API request parameters are how the seed asks for a full-size JPEG rather than
    whatever the server defaults to; passed through the adapter, they must survive to the wire."""
    respx.get(f"{BASE}/arkis!{VOLUME}/manifest").mock(return_value=httpx.Response(200, json=_manifest(1)))
    route = respx.get(f"{BASE}/arkis!{VOLUME}_00001/full/500,/0/gray.jpg").mock(return_value=httpx.Response(200, content=b"x"))

    list(seed.IIIFVolumeSource(VOLUME, base_url=BASE, query_params="full/500,/0/gray.jpg", timeout=5.0, max_pages=1).iter_objects())

    assert route.called


def test_the_adapter_satisfies_the_writer_it_is_handed_to(seed: ModuleType) -> None:
    """`ingest_to_bronze` takes a `SourceAdapter`, whose whole surface is `iter_objects`.

    Read off the Protocol rather than hardcoded, so a member added to `SourceAdapter` fails HERE
    instead of at the next `make seed-dev`. `isinstance` is unavailable — the Protocol is not
    `@runtime_checkable` — and `__protocol_attrs__` is a CPython implementation detail, so the members
    are taken from the annotations and callables the Protocol declares.
    """
    from service_kit.lakehouse.sources import SourceAdapter

    src = seed.IIIFVolumeSource(VOLUME, base_url=BASE, query_params="q", timeout=1.0, max_pages=1)
    required = {name for name in vars(SourceAdapter) if not name.startswith("_")}

    assert required, "the Protocol declared nothing — this assertion would pass against any object"
    assert required <= {name for name in dir(src) if not name.startswith("_")}
    assert callable(src.iter_objects)


def test_the_module_imports_at_all(seed: ModuleType) -> None:
    """The regression, stated plainly. This is what was broken for days, and what `make seed-dev`
    step 3 needs: the module must import and expose a `main`."""
    assert callable(seed.main)
