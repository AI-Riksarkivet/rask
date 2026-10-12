"""A row inserted into a project's bronze through the catalog starts that project's cascade (ADR 0154).

The lakehouse's only way in is the catalog's namespace doors, so the cascade head has no writer of its own: it fires on
the catalog's announcement of a committed bronze write. This drives that handshake with nothing restated on either
side. The catalog app runs on a real socket over a real `dir` namespace and a real warehouse registry, so the project
in the event is the one the registry binds; the e2e seed helper (`tests/e2e-py/bronze_seed.py`) makes the writes, so
the request shape every live lane uses is the one proven here; and each announcement the catalog made is delivered to
the medallion head's own `handle_bronze_arrival`, which asks the same catalog where the table lives.

Only the lineage TRANSPORT is replaced, by a list: the event is built by the catalog's own emitter, project resolution
included. The head's signature door and the Dapr sidecar are not the claim and are not in the path.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import pytest
import uvicorn
from lance_namespace import CreateNamespaceRequest, connect


PROJECT = "acme"
_SEED_HELPER = Path(__file__).resolve().parents[1] / "e2e-py" / "bronze_seed.py"


def _bronze_seed() -> ModuleType:
    """The e2e seed helper, loaded by path: `tests/e2e-py` is not an importable package name (the hyphen)."""
    spec = importlib.util.spec_from_file_location("bronze_seed", _SEED_HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Catalog:
    def __init__(self, url: str, announced: list[dict[str, Any]]) -> None:
        self.url = url
        self.announced = announced


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Catalog]:
    """The catalog on an ephemeral port, warehouses on, with project `acme`'s bronze namespace provisioned."""
    from catalog.core import lineage_emit
    from catalog.core.config import get_settings
    from catalog.services import warehouses

    default_root, warehouse_root, registry = tmp_path / "default", tmp_path / "acme-wh", tmp_path / "registry"
    for directory in (default_root, warehouse_root, registry):
        directory.mkdir()
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(default_root),
        "LANCE_WAREHOUSES_ENABLED": "true",
        "LANCE_CONTROL_ROOT": f"file://{registry}",
        "LANCE_LINEAGE_EMIT_ENABLED": "true",
        "LANCE_LINEAGE_TRANSPORT": "http",
        "LANCE_LINEAGE_URL": "http://127.0.0.1:9",
        "LANCE_S3_ACCESS_KEY_ID": "x",
        "LANCE_S3_SECRET_ACCESS_KEY": "x",
    }.items():
        monkeypatch.setenv(key, value)

    # The provisioning a project's warehouse does on a live estate: the warehouse record naming its project, the tier
    # namespace created in the warehouse's own root, and the binding the catalog resolves the project through.
    control = f"file://{registry}"
    warehouses.put_warehouse(control, {}, {"id": "acme-wh", "bucket": "acme-wh", "root_uri": str(warehouse_root), "project": PROJECT})
    connect("dir", {"root": str(warehouse_root)}).create_namespace(CreateNamespaceRequest(id=[f"{PROJECT}-bronze"]))
    warehouses.bind_namespace(control, {}, f"{PROJECT}-bronze", "acme-wh", str(warehouse_root))

    announced: list[dict[str, Any]] = []

    async def _record(_self: object, event: dict[str, Any], **_: object) -> None:
        announced.append(event)

    monkeypatch.setattr(lineage_emit.HttpLineageEmitter, "_send", _record)
    get_settings.cache_clear()
    from catalog.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(600):
            if server.started or not thread.is_alive():
                break
            threading.Event().wait(0.05)
        assert server.started, "the catalog never came up"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield _Catalog(f"http://127.0.0.1:{port}", announced)
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        get_settings.cache_clear()


class _Bus:
    """The head's Dapr client, reduced to the publishes it makes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish_event(self, *, topic_name: str, data: str, **_: object) -> None:
        self.published.append((topic_name, json.loads(data)))


@pytest.mark.asyncio
async def test_a_catalog_insert_into_a_projects_bronze_fires_the_cascade_head_for_that_project(catalog: _Catalog, tmp_path: Path) -> None:
    from medallion.core.config import MedallionSettings
    from medallion.services.ingest_trigger import handle_bronze_arrival

    token_file = tmp_path / "catalog-token"
    token_file.write_text("unverified")
    settings = MedallionSettings(MEDALLION_CATALOG_URL=catalog.url, RASK_CATALOG_IDENTITY_TOKEN_FILE=str(token_file))
    seed = _bronze_seed()
    bus = _Bus()

    version = seed.seed_bronze_events(catalog.url, project=PROJECT, rows=4)
    for event in list(catalog.announced):
        await handle_bronze_arrival(cast("Any", bus), settings, {"data": event})

    insert_run = next(e["run"]["runId"] for e in catalog.announced if e.get("run", {}).get("facets", {}).get("lance", {}).get("operation") == "insert")
    location = httpx.post(f"{catalog.url}/v1/table/{PROJECT}-bronze$events/describe", json={}, timeout=30).json()["location"]
    assert version >= 1
    assert bus.published == [
        (
            settings.bronze_topic,
            {
                "token": insert_run,
                "cascade_id": insert_run,
                "dataset": "bronze$events",
                "namespace": "bronze",
                "project": PROJECT,
                "from_uri": location,
            },
        )
    ], (
        f"the head's publishes for the catalog's announcements {[(e.get('eventType'), e.get('run', {}).get('facets', {}).get('lance')) for e in catalog.announced]}"
    )
