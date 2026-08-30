"""The lakehouse control-plane kernel drain — SKG-08, SKG-11, SKG-13, SKG-17.

Four findings against `service_kit.lakehouse`, all of them about a seam written more than once:

* **SKG-08** — four hand-rolled object-store record registries (`protection`, `maintenance_policies`,
  `trash`, `warehouse_records`) each carrying their own byte-identical hashed-key helper and their own
  list-with-broad-except body. The copies had already DRIFTED: `get_policy` grew no `isinstance(loaded,
  dict)` guard while both its twins have one, so a malformed policy record propagates into
  `resolve_policy` as whatever JSON happened to be on disk.
* **SKG-11** — the warehouse resolver's positive cache is a module-level `dict` with no bound and no
  eviction: an expired entry is only ever overwritten by a lookup of that same key, so a long-lived
  process accumulates one entry per (control root, project, serving class) it has ever resolved and
  frees none of them.
* **SKG-13** — `lakehouse.sources.S3Source` / `lakehouse.sinks.S3Sink` shadow `storage.S3Source` /
  `storage.S3Sink` by name with an incompatible API. `services/medallion` imports BOTH under that one
  name, in two modules of the same service.
* **SKG-17** — `S3FileSystemSource.iter_objects` re-derived its own listing-and-sort beside the `_listing` helper
  its `iter_keys` twin already goes through, so the two enumeration paths can disagree.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pyarrow.fs as pafs
import pytest

from service_kit.lakehouse import maintenance_policies, protection, trash, warehouse_registry
from service_kit.lakehouse.maintenance_policies import get_policy, put_policy
from service_kit.lakehouse.warehouse_registry import clear_cache, project_root


# --------------------------------------------------------------------------------------------------
# SKG-08 — one registry primitive, and the drift the four copies produced
# --------------------------------------------------------------------------------------------------


def test_a_malformed_policy_record_is_refused_like_its_two_siblings(tmp_path: Path) -> None:
    """`get_protection` and `trash.get` both return None on a non-dict record. `get_policy` did not."""
    put_policy(str(tmp_path), {}, {"kind": "table", "id": "db$t", "path": "b/db$t"})
    key = next(p for p in (tmp_path / "_policies").iterdir() if p.suffix == ".json")
    key.write_text(json.dumps(["not", "a", "record"]))

    assert get_policy(str(tmp_path), {}, "table", "db$t") is None


def test_the_hashed_record_key_is_written_once() -> None:
    """Three byte-identical `_key` bodies, differing only in the prefix constant."""
    copies = [module.__name__ for module in (protection, maintenance_policies, trash) if "def _key(" in inspect.getsource(module)]

    assert copies == [], f"the hashed record key is still hand-rolled in {copies}"


def test_the_record_key_shape_is_unchanged_by_the_de_duplication() -> None:
    """A golden key per prefix: records already on the control root must stay addressable."""
    from service_kit.lakehouse.record_store import record_key

    assert record_key("_protection", "table", "db$t") == "_protection/table-f74c29b36c7f71a511912ddc.json"
    assert record_key("_policies", "namespace", "db") == "_policies/namespace-71a39747d8c07c7a2f42ec42.json"
    assert record_key("_trash", "table", "db$t") == "_trash/table-f74c29b36c7f71a511912ddc.json"

    # The sweep's per-(policy, dataset) stamp is the same deriver under `_policies/state/`: an orphaned
    # stamp re-maintains a dataset that was maintained an hour ago, so its bytes are pinned too.
    from service_kit.lakehouse.maintenance_policies import _state_key

    assert _state_key({"kind": "namespace", "id": "db"}, "s3://b/db$t") == record_key("_policies/state", "namespace", "db:s3://b/db$t")
    assert _state_key({"kind": "namespace", "id": "db"}, "s3://b/db$t") == "_policies/state/namespace-f3f67a9f17a3a307de98bf99.json"


# --------------------------------------------------------------------------------------------------
# SKG-11 — the warehouse resolver's cache is bounded and evicts
# --------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


#: The ceiling the resolver cache must hold to. Named here because the test is the contract: an
#: unbounded per-process dict keyed on caller-supplied strings is the finding.
_EXPECTED_CEILING = 512


def _control_root_with_warehouse(root: Path, name: str) -> str:
    """One control root holding exactly one active warehouse for project ``acme``.

    DISTINCT CONTROL ROOTS rather than distinct projects: the cache key is
    ``(control_root, project, serving)``, so this grows the cache by one entry per call while keeping
    every registry listing a single file — the growth is what is under test, not the listing cost.
    """
    registry = root / name / "_warehouses"
    registry.mkdir(parents=True, exist_ok=True)
    record = {"id": "wh1", "bucket": f"{name}-bucket", "project": "acme", "root_uri": f"s3://{name}-wh", "status": "active"}
    (registry / "wh1.json").write_text(json.dumps(record))
    return str(root / name)


def test_the_warehouse_resolution_cache_is_bounded(tmp_path: Path) -> None:
    """One entry per (control root, project, serving) ever resolved, retained forever, no ceiling."""
    for index in range(_EXPECTED_CEILING + 40):
        control_root = _control_root_with_warehouse(tmp_path, f"root{index}")
        assert project_root(control_root, {}, "acme", ttl_seconds=300) == f"s3://root{index}-wh"

    held = len(warehouse_registry._cache)
    assert held <= _EXPECTED_CEILING, f"the resolver cache grew to {held} entries with no ceiling"


def test_an_expired_entry_is_not_retained(tmp_path: Path) -> None:
    """A non-positive TTL is documented as "re-read the registry on every call" — it still cached."""
    for index in range(20):
        control_root = _control_root_with_warehouse(tmp_path, f"ghost{index}")
        assert project_root(control_root, {}, "acme", ttl_seconds=0.0) == f"s3://ghost{index}-wh"

    held = len(warehouse_registry._cache)
    assert held == 0, f"{held} entries that expire the instant they are written are still resident"


# --------------------------------------------------------------------------------------------------
# SKG-13 — the lakehouse adapters must not shadow the storage ones by name
# --------------------------------------------------------------------------------------------------


def test_the_lakehouse_adapters_do_not_shadow_the_storage_ones_by_name() -> None:
    """Two live classes per name, incompatible constructors — and `services/medallion` imports both."""
    import storage
    from service_kit.lakehouse import sinks, sources

    lakehouse_classes = {
        name for module in (sources, sinks) for name, obj in vars(module).items() if isinstance(obj, type) and obj.__module__ == module.__name__
    }
    collisions = sorted(lakehouse_classes & {name for name in dir(storage) if not name.startswith("_")})

    assert collisions == [], f"`storage` and `service_kit.lakehouse` both export {collisions}"


# --------------------------------------------------------------------------------------------------
# SKG-17 — one listing seam behind both enumeration paths
# --------------------------------------------------------------------------------------------------


class _FakeFs:
    """The slice of ``pyarrow.fs.FileSystem`` the S3 adapter touches, over an in-memory dict."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def get_file_info(self, selector: pafs.FileSelector) -> list[pafs.FileInfo]:
        base = selector.base_dir.rstrip("/") + "/"
        return [pafs.FileInfo(path, pafs.FileType.File) for path in self.files if path.startswith(base)]

    def open_input_stream(self, path: str) -> _FakeStream:
        return _FakeStream(self.files[path])


class _FakeStream:
    """``readall`` + the context-manager pair, which is all the adapter uses."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def readall(self) -> bytes:
        return self._data


def test_both_enumeration_paths_go_through_the_one_listing_seam() -> None:
    """`iter_keys` honours `_listing`; `iter_objects` re-derived its own, so the two can disagree."""
    from service_kit.lakehouse.sources import S3FileSystemSource

    class _PngOnlySource(S3FileSystemSource):
        def _listing(self) -> list[pafs.FileInfo]:
            return [info for info in super()._listing() if info.path.endswith(".png")]

    fs = _FakeFs({"bucket/prefix/a.png": b"AAA", "bucket/prefix/notes.txt": b"TXT"})
    source = _PngOnlySource(cast("pafs.S3FileSystem", fs), "bucket", "prefix")

    assert list(source.iter_keys()) == ["s3://bucket/prefix/a.png"]
    assert [obj.uri for obj in source.iter_objects()] == ["s3://bucket/prefix/a.png"]
