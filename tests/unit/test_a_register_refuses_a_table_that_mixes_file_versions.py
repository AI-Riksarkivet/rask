"""`POST /v1/table/{id}/register` must not bring a dataset carrying reader flag 256 into governance.

Flag 256 (mixed data file versions) is sticky. Measured on pylance 12.0.0: a 2.1 table with stable row
ids that takes one append at ``data_storage_version="2.2"`` carries reader/writer flags (258, 258), and
no operation removes the bit. Maintenance then refuses the table on every tick, and pylance 11 and
lancedb 0.34 (lance core 8) cannot open it. The register door is how a dataset written outside rask
enters governance, so it answers 400 and leaves the namespace exactly as it found it.

The dir backend registers a location without opening it (measured on 12.0.0: a mixed table, an absent
location and a nested path are all accepted), so the door judges the dataset itself, off the location
the backend resolved, and deregisters what it attached when it refuses.

Only bit 256 is judged. An ingest-style dataset with an external base carries flag 16 and flags
(18, 18); a generic unsupported-features gate would refuse every externally based bronze registration.

The catalog's own re-registers (table undrop, namespace undrop) restore a table rask already governed.
Refusing there would strand it in the trash, so they register it and log a WARN instead.

Driven through the real app over a real ``dir`` namespace. Stubbed only where a test says so: the
pylance-11 open refusal the installed pylance cannot produce, the seed and emit spies that pin ordering,
and the failing deregister that forces the partial-apply path.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, Literal

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from service_kit.lakehouse.features import manifest_feature_flags


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "lance-catalog"
    path.mkdir()
    return path


@pytest.fixture
def catalog(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The catalog app as it boots, rooted on a local directory, with a recoverable drop configured."""
    from catalog.core.config import get_settings

    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"file://{root}",
        "LANCE_CONTROL_ROOT": f"file://{tmp_path / 'control'}",
        "LANCE_TRASH_GRACE_DAYS": "1",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_S3_ACCESS_KEY_ID": "x",
        "LANCE_S3_SECRET_ACCESS_KEY": "x",
        "LANCE_S3_ENDPOINT_URL": "http://127.0.0.1:9",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    from catalog.main import app

    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


type _FileVersion = Literal["2.1", "2.2"]


def _write(uri: Path, ids: list[int], *, version: _FileVersion, initial_bases: list[lance.DatasetBasePath] | None = None) -> None:
    lance.write_dataset(
        pa.table({"id": pa.array(ids, pa.int64())}),
        str(uri),
        data_storage_version=version,
        enable_stable_row_ids=True,
        initial_bases=initial_bases,
    )


def _mix(uri: Path) -> None:
    """Append at 2.2 onto a 2.1 table — the one commit pylance 12 accepts and stamps with flag 256."""
    lance.write_dataset(pa.table({"id": pa.array([99], pa.int64())}), str(uri), mode="append", data_storage_version="2.2")
    assert manifest_feature_flags(lance.dataset(str(uri))) == (258, 258), "precondition: the append must have mixed the table"


def _create_namespace(client: TestClient, name: str) -> None:
    resp = client.post(f"/v1/namespace/{name}/create", json={})
    assert resp.status_code == 200, resp.text


def _is_registered(client: TestClient, table: str) -> bool:
    return client.post(f"/v1/table/{table}/exists").status_code == 200


def _mixed_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and "mixed_file_versions" in r.getMessage()]


class TestThePublicDoor:
    def test_a_mixed_table_is_refused_and_the_namespace_is_unchanged(self, catalog: TestClient, root: Path) -> None:
        _create_namespace(catalog, "db")
        _write(root / "mixed", [1, 2], version="2.1")
        _mix(root / "mixed")

        resp = catalog.post("/v1/table/db$t/register", json={"location": "mixed"})

        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == 13, "the spec's InvalidInput code, which a generated client dispatches on"
        assert "256" in resp.json()["detail"], "the refusal must name the flag"
        assert "recreate" in resp.json()["detail"].lower(), "the refusal must name the only measured remedy"
        assert not _is_registered(catalog, "db$t"), "a refused registration must leave nothing attached"
        listed = catalog.get("/v1/namespace/db/table/list")
        assert listed.status_code == 200, listed.text
        assert listed.json().get("tables", []) == []

    def test_an_older_pylance_that_refuses_the_open_over_flag_256_is_the_same_400(
        self, catalog: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pylance 11 cannot open a flag-256 table at all. Its refusal names the flags, and that is the same fact."""
        from catalog.core import namespace as core_namespace

        _create_namespace(catalog, "db")
        _write(root / "t", [1], version="2.1")

        def _refuse(*_a: object, **_k: object) -> object:
            raise ValueError("Not supported: This dataset cannot be read by this version of Lance. Please upgrade Lance to read this dataset.\n Flags: 258")

        monkeypatch.setattr(core_namespace.lance, "dataset", _refuse)

        resp = catalog.post("/v1/table/db$t/register", json={"location": "t"})

        assert resp.status_code == 400, resp.text
        assert not _is_registered(catalog, "db$t")

    def test_an_unreadable_dataset_fails_closed(self, catalog: TestClient, root: Path) -> None:
        """A dataset the door cannot read has not been judged, so it is not attached."""
        _create_namespace(catalog, "db")
        _write(root / "t", [1], version="2.1")
        for manifest in (root / "t" / "_versions").iterdir():
            manifest.write_bytes(b"not a manifest")

        resp = catalog.post("/v1/table/db$t/register", json={"location": "t"})

        assert resp.status_code == 503, resp.text
        assert not _is_registered(catalog, "db$t")

    @pytest.mark.parametrize(("case", "status"), [("mixed", 400), ("unreadable", 503), ("clean", 200)])
    def test_a_refused_dataset_never_gains_an_owner_a_lineage_node_or_an_event(
        self, catalog: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch, case: str, status: int
    ) -> None:
        """The judgement runs before the seed and both emits; the clean case proves the spies are wired."""
        from catalog.api import fga_deps
        from catalog.api.v1.endpoints import tables

        called: list[str] = []

        def _spy(name: str) -> Callable[..., Awaitable[None]]:
            async def _record(*_a: object, **_k: object) -> None:
                called.append(name)

            return _record

        _create_namespace(catalog, "db")
        _write(root / "t", [1, 2], version="2.1")
        if case == "mixed":
            _mix(root / "t")
        elif case == "unreadable":
            for manifest in (root / "t" / "_versions").iterdir():
                manifest.write_bytes(b"not a manifest")
        monkeypatch.setattr(fga_deps, "seed_ownership_or_compensate", _spy("seed"))
        monkeypatch.setattr(tables, "emit_write_event", _spy("lineage"))
        monkeypatch.setattr(tables, "emit_control", _spy("control"))

        resp = catalog.post("/v1/table/db$t/register", json={"location": "t"})

        assert resp.status_code == status, resp.text
        assert sorted(called) == (["control", "lineage", "seed"] if case == "clean" else []), called

    @pytest.mark.parametrize(("mix", "converged"), [(True, False), (False, True)])
    def test_a_re_register_never_converges_governance_onto_a_mixed_dataset(
        self, catalog: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch, mix: bool, converged: bool
    ) -> None:
        """The 409 converge path is where a retried, un-detached refusal lands; a clean dataset still converges."""
        from catalog.api import fga_deps

        _create_namespace(catalog, "db")
        _write(root / "t", [1, 2], version="2.1")
        assert catalog.post("/v1/table/db$t/register", json={"location": "t"}).status_code == 200
        if mix:
            _mix(root / "t")
        seeded: list[bool] = []

        async def _seed(*_a: object, **_k: object) -> None:
            seeded.append(True)

        monkeypatch.setattr(fga_deps, "seed_ownership", _seed)

        resp = catalog.post("/v1/table/db$t/register", json={"location": "t"})

        assert resp.status_code == 409, resp.text
        assert bool(seeded) is converged

    def test_a_refusal_whose_detach_fails_is_a_503_not_a_clean_400(self, catalog: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 400 says nothing was attached; when the detach failed, the dataset IS attached, so the caller must hear it."""
        from catalog.api.v1.endpoints import tables

        _create_namespace(catalog, "db")
        _write(root / "t", [1, 2], version="2.1")
        _mix(root / "t")
        call = tables.native.call

        def _no_detach(ns: Any, method: str, *args: object) -> Any:
            if method == "deregister_table":
                raise RuntimeError("store unavailable")
            return call(ns, method, *args)

        monkeypatch.setattr(tables.native, "call", _no_detach)

        resp = catalog.post("/v1/table/db$t/register", json={"location": "t"})

        assert resp.status_code == 503, resp.text
        assert resp.json()["attached"] is True and "deregister" in resp.json()["remedy"]

    def test_a_registered_table_is_announced_at_its_resolved_location(self, catalog: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The caller sends a relative location; the lineage edge and the control event must carry the absolute one."""
        from catalog.api.v1.endpoints import tables

        sent: dict[str, object] = {}

        async def _lineage(*_a: object, **kwargs: object) -> None:
            sent["source_uri"] = kwargs.get("source_uri")

        async def _control(*_a: object, **kwargs: object) -> None:
            extra = kwargs.get("extra")
            sent["control"] = extra.get("location") if isinstance(extra, dict) else None

        _create_namespace(catalog, "db")
        _write(root / "t", [1], version="2.2")
        monkeypatch.setattr(tables, "emit_write_event", _lineage)
        monkeypatch.setattr(tables, "emit_control", _control)

        assert catalog.post("/v1/table/db$t/register", json={"location": "t"}).status_code == 200

        for key in ("source_uri", "control"):
            assert str(sent[key]).rstrip("/").endswith(str(root / "t")), f"{key} carried {sent[key]!r}, not the resolved location"

    @pytest.mark.parametrize("version", ["2.1", "2.2"])
    def test_a_table_at_one_file_version_registers(self, catalog: TestClient, root: Path, version: _FileVersion) -> None:
        _create_namespace(catalog, "db")
        _write(root / "t", [1, 2], version=version)

        resp = catalog.post("/v1/table/db$t/register", json={"location": "t"})

        assert resp.status_code == 200, resp.text
        assert _is_registered(catalog, "db$t")

    def test_a_table_whose_only_unusual_flag_is_initial_bases_registers(self, catalog: TestClient, root: Path, tmp_path: Path) -> None:
        """Flag 16 is what ingest's create_empty(external_base=...) writes, and it is not this door's business."""
        external = tmp_path / "source"
        external.mkdir()
        _create_namespace(catalog, "db")
        _write(root / "t", [1], version="2.2", initial_bases=[lance.DatasetBasePath(str(external), "source")])
        assert manifest_feature_flags(lance.dataset(str(root / "t"))) == (18, 18), "precondition: flag 16 plus stable row ids"

        resp = catalog.post("/v1/table/db$t/register", json={"location": "t"})

        assert resp.status_code == 200, resp.text

    def test_a_location_holding_no_dataset_still_registers(self, catalog: TestClient) -> None:
        """The backend accepts an absent location, and there are no data files there to judge."""
        _create_namespace(catalog, "db")

        resp = catalog.post("/v1/table/db$t/register", json={"location": "nothing-here"})

        assert resp.status_code == 200, resp.text


class TestTheCatalogsOwnReRegisters:
    def test_a_table_undrop_restores_a_mixed_table_and_warns(self, catalog: TestClient, root: Path, caplog: pytest.LogCaptureFixture) -> None:
        _create_namespace(catalog, "db")
        _write(root / "t", [1, 2], version="2.1")
        assert catalog.post("/v1/table/db$t/register", json={"location": "t"}).status_code == 200
        assert catalog.post("/v1/table/db$t/drop").status_code == 200
        assert not _is_registered(catalog, "db$t"), "precondition: a recoverable drop detaches the table"
        _mix(root / "t")

        with caplog.at_level(logging.WARNING):
            resp = catalog.post("/management/v1/table/db$t/undrop")

        assert resp.status_code == 200, resp.text
        assert _is_registered(catalog, "db$t"), "the undrop must restore the table, not strand it in the trash"
        assert _mixed_warnings(caplog), "the restore of a mixed table must be visible to an operator"

    def test_a_namespace_undrop_restores_a_mixed_table_and_warns(self, catalog: TestClient, root: Path, caplog: pytest.LogCaptureFixture) -> None:
        _create_namespace(catalog, "db")
        _write(root / "t", [1, 2], version="2.1")
        assert catalog.post("/v1/table/db$t/register", json={"location": "t"}).status_code == 200
        dropped = catalog.post("/v1/namespace/db/drop", json={"behavior": "Cascade"})
        assert dropped.status_code == 200, dropped.text
        _mix(root / "t")

        with caplog.at_level(logging.WARNING):
            resp = catalog.post("/management/v1/namespace/db/undrop")

        assert resp.status_code == 200, resp.text
        assert _is_registered(catalog, "db$t")
        assert _mixed_warnings(caplog)

    def test_a_clean_undrop_does_not_warn(self, catalog: TestClient, root: Path, caplog: pytest.LogCaptureFixture) -> None:
        _create_namespace(catalog, "db")
        _write(root / "t", [1, 2], version="2.1")
        assert catalog.post("/v1/table/db$t/register", json={"location": "t"}).status_code == 200
        assert catalog.post("/v1/table/db$t/drop").status_code == 200

        with caplog.at_level(logging.WARNING):
            resp = catalog.post("/management/v1/table/db$t/undrop")

        assert resp.status_code == 200, resp.text
        assert _mixed_warnings(caplog) == []
