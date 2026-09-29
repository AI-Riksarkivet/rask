"""A9 — adding a source is one adapter, one registry entry, one lineage twin.

The gate that matters here is not "does local-dir work" but "is the registry the ONLY place a source
appears". The medallion needed twelve files to hold one source type; this asserts the shape that
replaces it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingest.adapters import register_builtin_sources
from ingest.sources import SourceSpec, build_source, iter_units


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_builtin_sources()


def test_local_dir_yields_every_file_with_its_uri(tmp_path: Path) -> None:
    """The dummy lane's fixture source (A11): deterministic, no network, real bytes."""
    (tmp_path / "a.tif").write_bytes(b"\x49\x49page-a")
    (tmp_path / "b.tif").write_bytes(b"\x49\x49page-b")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.tif").write_bytes(b"\x49\x49page-c")

    spec = SourceSpec(kind="local-dir", project="p", dataset="pages", options={"root": str(tmp_path)})
    units = list(iter_units(build_source(spec)))

    assert len(units) == 3, "the adapter must recurse — a nested fixture is still a unit"
    assert {u.data for u in units} == {b"\x49\x49page-a", b"\x49\x49page-b", b"\x49\x49page-c"}
    assert all(u.uri for u in units), "every unit needs a stable uri — it becomes the row id"


def test_a_source_missing_its_required_option_refuses_loudly() -> None:
    """Refusing beats defaulting: a wrong default would ingest the wrong thing under the right name."""
    with pytest.raises(ValueError, match=r"requires options\.root"):
        build_source(SourceSpec(kind="local-dir", project="p", dataset="d"))
    with pytest.raises(ValueError, match=r"requires options\.bucket"):
        build_source(SourceSpec(kind="s3-prefix", project="p", dataset="d"))


def test_every_registered_adapter_actually_implements_iter_objects(tmp_path: Path) -> None:
    """The check that would have caught the IIIF mistake — and cannot be done with isinstance.

    `SourceAdapter` is a plain Protocol, not `runtime_checkable`, so `isinstance(x, SourceAdapter)`
    raises TypeError rather than returning False. There is no import-time or startup verification:
    the registry will hand back whatever the factory returns, and the first ENUMERATION of a real
    source is where a missing method surfaces.

    An earlier version registered `IIIFCachedSource` — a keys+read cache with no `iter_objects`, on a
    constructor signature guessed from a grep. Both were wrong and nothing complained.
    """
    from ingest.sources import build_source

    probes = {
        # Inside the confinement root, not a bare "/tmp": `local-dir` now refuses any path
        # outside RASK_INGEST_LOCAL_ROOT, and a probe that ignores that would be asserting
        # against a source shape the service will not build.
        "local-dir": {"root": str(tmp_path)},
        "s3-prefix": {"bucket": "b", "prefix": "p/"},
    }
    for kind, options in probes.items():
        adapter = build_source(SourceSpec(kind=kind, project="p", dataset="d", options=options))
        assert callable(getattr(adapter, "iter_objects", None)), (
            f"{kind} adapter has no iter_objects — it does not satisfy SourceAdapter, and no isinstance check can tell you so"
        )
