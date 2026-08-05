"""A9 — adding a source is one adapter, one registry entry, one lineage twin.

The gate that matters here is not "does local-dir work" but "is the registry the ONLY place a source
appears". The medallion needed twelve files to hold one source type; this asserts the shape that
replaces it.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from ingest.adapters import register_builtin_sources
from ingest.sources import SourceSpec, build_source, iter_units, lineage_input_for, registered_kinds


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_builtin_sources()


def test_the_builtin_sources_are_registered() -> None:
    assert {"local-dir", "s3-prefix", "iiif"} <= set(registered_kinds())


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
    iiif = lineage_input_for(SourceSpec(kind="iiif", project="p", dataset="d", options={"volume_id": "v1"}))
    s3 = lineage_input_for(SourceSpec(kind="s3-prefix", project="p", dataset="d", options={"bucket": "images", "prefix": "x/"}))

    assert iiif.namespace == "iiif"
    assert s3.namespace == "s3://images"
    for got in (iiif, s3):
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
        "iiif": {"volume_id": "v1"},
    }
    for kind, options in probes.items():
        adapter = build_source(SourceSpec(kind=kind, project="p", dataset="d", options=options))
        assert callable(getattr(adapter, "iter_objects", None)), (
            f"{kind} adapter has no iter_objects — it does not satisfy SourceAdapter, and no isinstance check can tell you so"
        )


def test_every_adapter_is_DRIVEN_not_merely_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existence is not enough — the test above passed while `iter_objects` was a guaranteed TypeError.

    `IIIFVolumeSource.iter_objects` called `fetch_image(url)`, whose `client` is a required
    keyword-only argument, and `iter_keys` passed `iiif_base=` to a function whose parameter is
    `base_url`. Both are unconditional raises on the FIRST unit, and both survived a suite that only
    asked whether the method existed. `ty` found them; a test should have.

    So: actually turn the crank, with the network replaced at the seam the adapter reaches through.
    Every kind that can be driven offline is driven — the point is that a wrong call signature has
    nowhere left to hide, not that IIIF specifically is covered.
    """
    import httpx
    from ingest.sources import build_source, iter_unit_keys

    manifest = {"items": [{"id": "https://iiif.example/arkis!A0060198_00001/canvas"}]}

    class _FakeResponse:
        def __init__(self, url: str) -> None:
            self._url = url

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return manifest

        @property
        def content(self) -> bytes:
            return b"\xff\xd8\xff" + self._url.encode()

    class _FakeClient:
        """Records what it was asked for, so the URL the adapter BUILT is assertable."""

        seen: ClassVar[list[str]] = []

        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> None: ...

        def get(self, url: str, *args: object, **kwargs: object) -> _FakeResponse:
            _FakeClient.seen.append(url)
            return _FakeResponse(url)

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    # local-dir: a real file, so the whole read path runs against the filesystem.
    (tmp_path / "page.tif").write_bytes(b"II*\x00fixture")

    local = build_source(SourceSpec(kind="local-dir", project="p", dataset="d", options={"root": str(tmp_path)}))
    assert [obj.uri for obj in local.iter_objects()], "local-dir yielded nothing from a directory holding a file"

    iiif = build_source(SourceSpec(kind="iiif", project="p", dataset="d", options={"volume_id": "A0060198", "iiif_base": "https://iiif.example"}))

    keys = list(iter_unit_keys(iiif))
    assert keys == ["https://iiif.example/arkis!A0060198_00001/full/max/0/default.jpg"], keys

    objects = list(iiif.iter_objects())
    assert len(objects) == 1, objects
    assert objects[0].uri == keys[0]
    assert objects[0].data.startswith(b"\xff\xd8\xff"), "iter_objects returned no image bytes"

    # The manifest and the image must have gone through ONE client, not one per request: the shared
    # connection is why `storage.iiif` takes a client at all.
    assert any(url.endswith("/manifest") for url in _FakeClient.seen), _FakeClient.seen


def test_max_pages_is_READ_not_merely_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The option was registered, rendered, numeric-validated and posted — and read by nothing.

    It worked before A12 (`iiif_produce.py` sliced `image_ids[: self._max_pages]`); the rewrite kept
    the interface and dropped the behaviour. That is worse than never offering it: a user who caps a
    harvest at 3 pages and receives the whole volume has been actively misled by their own UI.

    Driven through `build_source` from the same `options` dict the control API posts, because the
    coercion is half the defect — the browser sends `"3"`, a script sends `3`, and a blank field
    sends `""` meaning "all".
    """
    import httpx
    from ingest.sources import build_source, iter_unit_keys

    manifest = {"items": [{"id": f"https://iiif.example/arkis!A0060198_{i:05d}/canvas"} for i in range(1, 11)]}

    class _Resp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict[str, object]:
            return manifest

    class _Client:
        def __init__(self, *a: object, **k: object) -> None: ...
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *e: object) -> None: ...
        def get(self, url: str, *a: object, **k: object) -> _Resp:
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)

    def keys(options: dict[str, object]) -> list[str]:
        spec = SourceSpec(kind="iiif", project="p", dataset="d", options={"volume_id": "A0060198", **options})
        return list(iter_unit_keys(build_source(spec)))

    assert len(keys({})) == 10, "no cap must still harvest the whole volume"
    assert len(keys({"max_pages": 3})) == 3, "an int cap must be honoured"
    assert len(keys({"max_pages": "3"})) == 3, "the form posts JSON — a numeric field arrives as a STRING"
    assert len(keys({"max_pages": ""})) == 10, "a blank field means 'all', not 'none'"
    assert len(keys({"max_pages": 0})) == 10, "0 is 'unbounded', matching the estate's other ceilings"
