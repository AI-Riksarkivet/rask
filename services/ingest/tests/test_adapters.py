"""A9 — adding a source is one adapter, one registry entry, one lineage twin.

The gate that matters here is not "does local-dir work" but "is the registry the ONLY place a source
appears". The medallion needed twelve files to hold one source type; this asserts the shape that
replaces it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ingest.adapters import register_builtin_sources
from ingest.sources import SourceSpec, build_source, iter_units, lineage_input_for, registered_kinds


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_builtin_sources()


def test_the_builtin_sources_are_registered() -> None:
    assert {"local-dir", "s3-prefix"} <= set(registered_kinds())
    assert "iiif" not in registered_kinds(), "IIIF was removed by owner ruling 2026-08-07 — it must not quietly return"


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


def test_enumeration_order_is_deterministic(tmp_path: Path) -> None:
    """Two runs over the same tree must enumerate identically.

    Not cosmetic: the row id derives from the uri, and a reproducible ingest order is what makes a
    re-run converge rather than merely 'probably match'.
    """
    for name in ("c.tif", "a.tif", "b.tif"):
        (tmp_path / name).write_bytes(name.encode())

    spec = SourceSpec(kind="local-dir", project="p", dataset="d", options={"root": str(tmp_path)})
    first = [u.uri for u in iter_units(build_source(spec))]
    second = [u.uri for u in iter_units(build_source(spec))]

    assert first == second
    assert first == sorted(first), "sorted order is what makes two runs comparable"


def test_lineage_input_names_the_EXTERNAL_system_not_a_tier() -> None:
    """R23: raw is other people's systems. The run's INPUT is never a governed tier."""
    s3 = lineage_input_for(SourceSpec(kind="s3-prefix", project="p", dataset="d", options={"bucket": "images", "prefix": "x/"}))
    local = lineage_input_for(SourceSpec(kind="local-dir", project="p", dataset="d", options={"root": "/data/x"}))

    assert s3.namespace == "s3://images"
    for got in (s3, local):
        assert "bronze" not in got.namespace and "silver" not in got.namespace


def test_a_source_missing_its_required_option_refuses_loudly() -> None:
    """Refusing beats defaulting: a wrong default would ingest the wrong thing under the right name."""
    with pytest.raises(ValueError, match=r"requires options\.root"):
        build_source(SourceSpec(kind="local-dir", project="p", dataset="d"))
    with pytest.raises(ValueError, match=r"requires options\.bucket"):
        build_source(SourceSpec(kind="s3-prefix", project="p", dataset="d"))


def test_a9_a_source_appears_ONLY_in_the_registry() -> None:
    """A9 as a grep: no source kind may be named outside adapters.py and the registry.

    The medallion held 'iiif' across twelve files — a route, a settings block, a produce module, a
    Ray entrypoint, event schemas. If a kind string starts appearing in the API or the workflow,
    the weld is growing back.
    """
    import re

    src = Path(__file__).resolve().parents[1] / "src" / "ingest"
    offenders = []
    for path in src.rglob("*.py"):
        if path.name in {"adapters.py", "sources.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        # A kind used as a literal value, not merely mentioned in prose.
        if re.search(r"""["'](?:iiif|s3-prefix|local-dir)["']\s*(?:==|:|\)|,)""", text):
            offenders.append(path.name)

    assert offenders == [], f"source kinds leaked outside the registry: {offenders}"


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


def test_every_adapter_is_DRIVEN_not_merely_present(tmp_path: Path) -> None:
    """Existence is not enough — the probe above once passed while `iter_objects` was a guaranteed
    TypeError on its first unit (the removed IIIF adapter's wrong call signatures; `ty` found them,
    a test should have). So: actually turn the crank on every kind that can be driven offline.

    With IIIF removed (owner ruling 2026-08-07) the offline-drivable set is `local-dir` — s3-prefix
    is driven for real by `test_worker_queue`'s moto lane, not mocked weaker here.
    """
    from ingest.sources import build_source

    (tmp_path / "page.tif").write_bytes(b"II*\x00fixture")

    local = build_source(SourceSpec(kind="local-dir", project="p", dataset="d", options={"root": str(tmp_path)}))
    objects = list(local.iter_objects())
    assert [obj.uri for obj in objects], "local-dir yielded nothing from a directory holding a file"
    assert objects[0].data.startswith(b"II*\x00"), "iter_objects returned no bytes"
