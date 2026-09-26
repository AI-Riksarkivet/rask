"""An erasure asks the maintenance doors' manifest-flag gates of every ref before rewriting or reclaiming it.

The compact and GC doors refuse a dataset whose manifest carries a flag their rewrite has not been
checked against (``maintenance.require_compactable`` / ``require_reclaimable``, which ask the sweep's
``describe_compaction_unsupported_flags`` / ``describe_gc_unsupported_flags``). The erasure performs the
same two verbs through every ref's handle, so it refuses the same way and says so per ref.

One allowance, measured on pylance 12.0.0: a branch sets flag 16 and names this table's root (a branch
of a branch, its parent's ``tree/<name>`` too) as dataset-root bases its inherited files resolve through,
so the compact gate as the door asks it refuses every branch. Those bases are the table's own history,
which a branch's rewrite copies from on purpose; every other base is weighed on the gate's evidence.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import lance
import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance import DatasetBasePath

from catalog.api.dependencies import get_namespace, get_storage_options
from catalog.api.v1.endpoints import erasure as door
from catalog.core.config import Settings, get_settings
from catalog.services.dataplane import create_table, read_arrow_body
from catalog.services.erasure import ErasureReport, erase
from service_kit.lakehouse import base_refs
from service_kit.lakehouse.base_refs import BaseRefs


_PREDICATE = "pii = 'alice'"


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def _erase(uri: str) -> ErasureReport:
    return erase(
        lance.dataset(uri), storage_options={}, protected=None, reopen=lambda: lance.dataset(uri), table="t", predicate=_PREDICATE, retention=timedelta(0)
    )


def _steps(report: ErasureReport) -> dict[str, tuple[str, str]]:
    return {s.surface: (s.outcome, s.detail) for s in report.surfaces if s.surface.startswith(("compact:", "history:"))}


def test_a_table_mixing_file_versions_is_neither_rewritten_nor_reclaimed_on_any_ref(tmp_path: Path) -> None:
    """pylance 12 stamps reader flag 256 on an append at another file version, and every maintenance
    gate refuses it: no rewrite here has been checked against that layout."""
    uri = str(tmp_path / "mixed")
    lance.write_dataset(_rows("alice", "bob", "carol", "dan"), uri, data_storage_version="2.1", enable_stable_row_ids=True, max_rows_per_file=2)
    lance.write_dataset(_rows("eve"), uri, mode="append", data_storage_version="2.2")
    lance.dataset(uri).create_branch("work")

    report = _erase(uri)

    steps = _steps(report)
    assert {surface: outcome for surface, (outcome, _) in steps.items()} == {
        f"{step}:{ref}": "failed" for step in ("compact", "history") for ref in ("main", "work")
    }
    assert all("256 (mixed data file versions)" in detail for _, detail in steps.values()), steps
    assert report.complete is False


@pytest.mark.parametrize("source_name", ["source", "clone-src"], ids=["apart", "named-with-the-clones-root-as-prefix"])
def test_a_shallow_clone_is_reclaimed_but_not_rewritten_into_its_own_root(tmp_path: Path, source_name: str) -> None:
    """A clone's data files resolve through its source, so compacting it copies the source's rows into the
    clone's root — the cost the compact door refuses — and a branch of the clone names the source too.
    Reclamation is root-scoped and the GC gate admits flag 16, so both refs are still reclaimed.

    A clone's location is the caller's choice, so its source may share the clone root's spelling as a
    prefix; the table's own bases are its root and `<root>/tree/`, compared as paths."""
    source, clone = str(tmp_path / source_name), str(tmp_path / "clone")
    lance.write_dataset(_rows("alice", "bob"), source)
    lance.write_dataset(_rows("carol"), source, mode="append")
    lance.dataset(source).shallow_clone(clone, 2)
    lance.dataset(clone).create_branch("cw")

    report = _erase(clone)

    steps = _steps(report)
    assert {surface: outcome for surface, (outcome, _) in steps.items()} == {
        "compact:cw": "failed",
        "compact:main": "failed",
        "history:cw": "reclaimed",
        "history:main": "reclaimed",
    }, steps
    assert all("resolve through a base" in steps[f"compact:{ref}"][1] for ref in ("main", "cw")), steps
    assert [p.name for p in Path(clone).rglob("*.lance") if b"bob" in p.read_bytes()] == [], "the source's rows were copied into the clone"
    assert sorted(lance.dataset(source).to_table().column("pii").to_pylist()) == ["alice", "bob", "carol"]


def _placed(root: Path, base: Path) -> str:
    """A table at ``root`` whose data files ``target_bases`` lands under the plain prefix ``base``."""
    base.mkdir(parents=True)
    lance.write_dataset(_rows("alice", "bob"), str(root), initial_bases=[DatasetBasePath(str(base), "payloads")], target_bases=["payloads"])
    lance.write_dataset(_rows("carol"), str(root), mode="append", target_bases=["payloads"])
    return str(root)


@pytest.mark.parametrize(
    "base",
    [lambda root: root.parent / "payloads", lambda root: root / "payloads", lambda root: root.with_name(f"{root.name}-payloads")],
    ids=["beside-the-root", "inside-the-root", "sharing-the-roots-prefix"],
)
def test_a_table_whose_data_files_live_under_a_plain_base_is_not_rewritten(tmp_path: Path, base: Callable[[Path], Path]) -> None:
    """``target_bases`` lands a table's own data files under a registered prefix that no reading calls a
    dataset root; compacting would pull them into the table's root and leave the originals behind. Only
    the root itself and `<root>/tree/` are the table's own, wherever else the prefix is."""
    root = tmp_path / "placed"
    uri = _placed(root, base(root))

    report = _erase(uri)

    outcome, detail = _steps(report)["compact:main"]
    assert (outcome, "data files resolve through a base" in detail) == ("failed", True), detail


def _branched(tmp_path: Path) -> str:
    uri = str(tmp_path / "branched")
    lance.write_dataset(_rows("alice", "bob"), uri)
    lance.write_dataset(_rows("carol"), uri, mode="append")
    lance.dataset(uri).create_branch("work", 2)
    return uri


@pytest.mark.parametrize(
    ("build", "outcomes"),
    [
        (lambda tmp_path: _placed(tmp_path / "placed", tmp_path / "payloads"), {"compact:main": "failed"}),
        (_branched, {"compact:work": "failed", "compact:main": "rewritten"}),
    ],
    ids=["a-plain-base", "a-branch"],
)
@pytest.mark.parametrize(
    "failure",
    [TypeError("LanceDataset cannot map the bases [1] its data files name"), ValueError("data files name bases [1] the manifest does not declare")],
    ids=["no-accessor", "undeclared-base"],
)
def test_data_file_bases_that_cannot_be_read_refuse_the_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build: Callable[[Path], str], outcomes: dict[str, str], failure: Exception
) -> None:
    """Which base a ref's data files resolve through decides whether its rewrite pulls them home, so a
    ref whose files cannot be mapped is refused on flag 16: unread is never the permit. A ref without
    the flag (main of a plain table) is not asked, and is rewritten."""
    import catalog.services.erasure as module

    uri = build(tmp_path)

    def _unmapped(ds: object) -> set[str]:
        raise failure

    monkeypatch.setattr(module, "data_file_base_paths", _unmapped)
    steps = {surface: step for surface, step in _steps(_erase(uri)).items() if surface.startswith("compact:")}

    assert {surface: outcome for surface, (outcome, _) in steps.items()} == outcomes
    assert all("fragments could not be read" in detail for outcome, detail in steps.values() if outcome == "failed"), steps


def test_a_branch_of_a_table_with_an_external_base_is_still_rewritten(tmp_path: Path) -> None:
    """The cascade's tables register a plain blob prefix through ``initial_bases`` and the compact gate
    admits it on evidence; a branch of one names that prefix beside the table's root, and must be
    rewritten like any other branch."""
    blob = tmp_path / "payloads"
    blob.mkdir()
    uri = str(tmp_path / "bronze")
    lance.write_dataset(_rows("alice", "bob"), uri, initial_bases=[DatasetBasePath(str(blob), "payloads")])
    lance.write_dataset(_rows("carol"), uri, mode="append")
    lance.write_dataset(_rows("eve"), lance.dataset(uri).create_branch("work", 2), mode="append")

    report = _erase(uri)

    assert {surface: outcome for surface, (outcome, _) in _steps(report).items() if surface.startswith("compact:")} == {
        "compact:main": "rewritten",
        "compact:work": "rewritten",
    }
    assert report.complete is True, [(s.surface, s.outcome, s.detail) for s in report.surfaces]


def test_a_branchs_own_bases_cost_no_store_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch of a branch names only this table's root and its parent's `tree/work`, which the gate's
    evidence drops, so probing them would be a store round trip per base per ref for nothing."""
    import catalog.services.erasure as module

    uri = str(tmp_path / "chain")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows("alice", "carol"), uri, mode="append")
    lance.write_dataset(_rows("eve"), lance.dataset(uri).create_branch("work", 2), mode="append")
    lance.dataset(uri).create_branch("deeper", ("work", 3))
    probed: list[str] = []

    def _recording(root: str, storage_options: dict[str, str]) -> Any:
        return lambda path: probed.append(path) is None

    monkeypatch.setattr(module, "dataset_root_probe", _recording)
    report = _erase(uri)

    assert [s.outcome for s in report.surfaces if s.surface.startswith("compact:")] == ["rewritten"] * 3
    assert probed == []


def test_the_gates_probe_is_bound_to_the_store_the_erasure_was_handed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A base probed with no storage options reads an `s3://` spelling as a local path that is not there
    and answers "not a dataset root", a wrong permit."""
    import catalog.services.erasure as module

    uri = _branched(tmp_path)
    so = {"aws_region": "eu-north-1"}
    bound: list[dict[str, str]] = []
    probe = module.dataset_root_probe

    def _recording(dataset_uri: str, storage_options: dict[str, str]) -> Callable[[str], bool]:
        bound.append(storage_options)
        return probe(dataset_uri, storage_options)

    monkeypatch.setattr(module, "dataset_root_probe", _recording)
    erase(lance.dataset(uri), storage_options=so, protected=None, reopen=lambda: lance.dataset(uri), table="t", predicate=_PREDICATE, retention=timedelta(0))

    assert bound == [so], "work's flag-16 gate must build its probe once, on the erasure's options"


def test_the_door_reads_the_store_the_table_lives_in_with_the_requests_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hop the door takes to the store carries the request's storage options: the gate's probe, the
    #114 pre-pass's listing and the verification's reopen. Without them an `s3://` table is read as a
    local path — the probe answers "not a dataset root", a wrong permit; the listing and the reopen fail."""
    ns = lance_namespace.connect("dir", {"root": str(tmp_path / "data")})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, _rows("alice").schema) as writer:
        writer.write_table(_rows("alice"))
    create_table(ns, {}, ["subjects"], read_arrow_body(sink.getvalue().to_pybytes()), mode="create")
    so = {"aws_region": "eu-north-1"}
    seen: dict[str, Any] = {}
    listed: list[dict[str, str]] = []
    siblings = base_refs.sibling_base_refs

    def _record(dataset: Any, **kwargs: Any) -> ErasureReport:
        seen.update(kwargs)
        return ErasureReport(table="subjects", predicate=_PREDICATE)

    def _listing(location: str, storage_options: dict[str, str]) -> BaseRefs:
        listed.append(storage_options)
        return siblings(location, storage_options)

    monkeypatch.setattr(door, "erase", _record)
    monkeypatch.setattr(base_refs, "sibling_base_refs", _listing)
    application = FastAPI()
    application.include_router(door.router)
    application.dependency_overrides[get_settings] = lambda: Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s")
    application.dependency_overrides[get_namespace] = lambda: ns
    application.dependency_overrides[get_storage_options] = lambda: so
    with TestClient(application) as client:
        response = client.post("/management/v1/table/subjects/erasure", json={"predicate": _PREDICATE})

    assert response.status_code == 200, response.text
    assert seen["storage_options"] == so
    assert listed == [so]
    assert _options_the_reopen_passes(seen["reopen"], monkeypatch) == [so]


def _options_the_reopen_passes(reopen: Callable[[], Any], monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Call ``reopen`` and answer the ``storage_options`` each `lance.dataset` it made was handed."""
    opened: list[Any] = []
    real = lance.dataset
    signature = inspect.signature(real)

    def _dataset(*args: Any, **kwargs: Any) -> lance.LanceDataset:
        opened.append(signature.bind(*args, **kwargs).arguments.get("storage_options"))
        return real(*args, **kwargs)

    monkeypatch.setattr(lance, "dataset", _dataset)
    reopen()
    return opened
