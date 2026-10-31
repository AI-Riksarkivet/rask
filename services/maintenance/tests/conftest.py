"""Shared fixtures for the maintenance suite: the catalog's own app, one dataset's pass, an S3 store, and recorders for the counters."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from lance_namespace import connect
from moto.server import ThreadedMotoServer

from maintenance.core import metrics
from maintenance.core.config import MaintenanceSettings
from maintenance.services import sweep
from maintenance.services.expiry import DatasetResult
from maintenance.services.naming import TableIds
from maintenance.services.plan import DatasetPlan, PlanContext
from service_kit.lakehouse import location_claims
from service_kit.lakehouse.base_refs import BaseRefs, normalise
from service_kit.lakehouse.maintenance_due import CloneList
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.objectfs import StorageOptions


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    """A moto S3 server for one test module: S3 has buckets and key prefixes where a local filesystem has directories.

    Its backends are process-global, so a test names its buckets uniquely.
    """
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The catalog app over a real pylance `dir` namespace, with drops going to the trash.

    Rooted under `tmp_path / "catalog"`, so a test may keep datasets the catalog never registered beside it.
    """
    root = str(tmp_path / "catalog")
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", root)
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.core.config import get_settings
    from catalog.main import app

    get_settings.cache_clear()
    ns = connect("dir", {"root": root})
    app.dependency_overrides[get_namespace] = lambda: ns
    app.dependency_overrides[get_storage_options] = lambda: StorageOptions()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def maintain_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., DatasetResult]:
    """One dataset's pass through the sweep's real per-dataset path (`sweep.maintain_dataset`), outside a tick.

    ``protected`` stands in for the tick's whole-estate pre-pass, ``plan`` for what the policy registry resolves to, and
    ``table_id`` for the table the catalog's ``__manifest`` names the dataset as: maintenance acts on no dataset it cannot
    name that way ([[LH-520]]). The table carries a uuid as every catalog door gives one: its location's claim records it,
    and each namespace above it has its own in its ``__manifest`` row, since maintenance refuses a table whose policy
    owners it cannot identify ([[LH-523]]). A named dataset's due record lands under a control root in ``tmp_path``,
    where the tick would write it to the catalog's.
    """

    def _maintain(
        uri: str, settings: MaintenanceSettings, *, protected: BaseRefs | None = None, plan: DatasetPlan | None = None, table_id: str = "db$t"
    ) -> DatasetResult:
        if plan is not None:
            monkeypatch.setattr(sweep, "resolve_plan", lambda *_a, **_k: plan.model_copy())
        now = datetime.now(UTC)
        settings = settings.model_copy(update={"control_root": str(tmp_path / "control")})
        segments = table_id.split(CATALOG_DELIMITER)
        claims = location_claims.ClaimStore(control_root=settings.resolved_control_root, storage_options=settings.storage_options())
        location_claims.take(claims, normalise(uri), table_id, segments, is_gone=lambda _claim: False, uuid=str(uuid4()))
        namespaces = {CATALOG_DELIMITER.join(segments[:depth]): str(uuid4()) for depth in range(1, len(segments))}
        tick = sweep.SweepPlan(
            uris=[uri],
            protected=protected or BaseRefs(),
            clones=CloneList(taken_at=now),
            table_ids=TableIds(by_location={normalise(uri): table_id}, namespace_uuids=namespaces),
            context=PlanContext(policies=[], now=now, older_than=timedelta(days=settings.older_than_days)),
        )
        return sweep.maintain_dataset(uri, sweep=tick, settings=settings)

    return _maintain


@pytest.fixture
def refused_added(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any] | None]]:
    """Every `(amount, attributes)` the datasets-refused counter is asked to add."""
    seen: list[tuple[int, dict[str, Any] | None]] = []
    monkeypatch.setattr(metrics._refused, "add", lambda amount, attributes=None, **_: seen.append((amount, attributes)))
    return seen


@pytest.fixture
def parked_added(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any] | None]]:
    """Every `(amount, attributes)` the per-table `compaction.tables.parked` counter is asked to add."""
    seen: list[tuple[int, dict[str, Any] | None]] = []
    monkeypatch.setattr(metrics._tables_parked, "add", lambda amount, attributes=None, **_: seen.append((amount, attributes)))
    return seen


@pytest.fixture
def catalog_identity_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This pod's projected `rask-catalog` token as the kubelet leaves it: a file, named by the setting
    `MaintenanceSettings` reads, that every catalog call re-reads."""
    path = tmp_path / "rask-catalog-token"
    path.write_text("sa-token-one\n")
    monkeypatch.setenv("RASK_CATALOG_IDENTITY_TOKEN_FILE", str(path))
    return path
