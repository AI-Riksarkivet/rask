"""An erasure's verification reports what STORAGE holds, not what a warm session remembers.

Two measurements on pylance 12.0.0 make this necessary, both on a table whose branch is cut after the
subject arrives and whose head still stands on a file of that fork version the erasure does not rewrite:

* the parent's cleanup keeps the fork version's MANIFEST and deletes its other files, so the version
  stays listed while the subject's only data file is gone and a whole-version read raises "Not found";
* read through the session the erasure ran on — the door opens on the process-wide one — that same
  version still answers the predicate with the subject's row.

So verifying on the erasure's own handle reports the subject as surviving where no byte of it is left,
and the operator is told to delete a working branch for nothing. The verification opens on a fresh
session, and a version that cannot be read whole is judged fragment by fragment.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, cast

import lance
import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance.dataset import DatasetOptimizer
from lance_namespace import LanceNamespace

import catalog.services.erasure as erasure_module
from catalog.api.dependencies import get_namespace, get_storage_options
from catalog.api.v1.endpoints import erasure as door
from catalog.core import base_judge
from catalog.core.config import Settings, get_settings
from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table, read_arrow_body
from catalog.services.erasure import ErasureReport, erase
from catalog.services.maintenance import COMPACTION_BOUND
from service_kit.lakehouse import base_registry
from service_kit.lakehouse.ns_errors import install_problem_handlers


#: The catalog's default body cap (LANCE_MAX_BODY_BYTES), far above every body here.
_BODY_LIMIT = 256 * 1024 * 1024


_SUBJECT = "alice"
_PREDICATE = "pii = 'alice'"
_TABLE_ID = ["subjects"]


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def _alone(uri: str) -> None:
    """main v1 bob, v2 the subject in its own fragment, tagged; ``work`` cut from v2.

    The tag makes step 4 READ main v2 through the erasure's session before dropping it. Work's delete
    drops the subject's whole fragment, so its head is main's lone bob fragment: nothing to rewrite.
    """
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT), uri, mode="append")
    lance.dataset(uri).tags.create("snap", 2)
    lance.dataset(uri).create_branch("work", 2)


def _beside_a_large_base(uri: str) -> None:
    """A base fragment too large to be a compaction candidate, the subject beside carol in a small one;
    ``work`` cut after it with its own write, so work's rewrite leaves the base."""
    lance.write_dataset(pa.table({"pii": pa.array([f"bob-{i}" for i in range(1_100_000)])}), uri)
    lance.write_dataset(_rows(_SUBJECT, "carol"), uri, mode="append")
    lance.dataset(uri).create_branch("work", 2)
    lance.write_dataset(_rows("eve"), lance.dataset(uri).checkout_version(("work", None)), mode="append")


def _holding(uri: str) -> list[str]:
    return [str(p.relative_to(uri)) for p in Path(uri).rglob("*.lance") if _SUBJECT.encode() in p.read_bytes()]


def _dangles(uri: str) -> bool:
    try:
        lance.dataset(uri).checkout_version((None, 2)).count_rows(filter=_PREDICATE)
    except OSError:
        return True
    return False


@pytest.mark.parametrize("build", [_alone, _beside_a_large_base], ids=["alone", "large-base"])
def test_a_version_whose_subject_file_is_gone_is_not_a_residual(tmp_path: Path, build: Callable[[str], None]) -> None:
    uri = str(tmp_path / "subjects")
    build(uri)
    shared = lance.Session(metadata_cache_size_bytes=64 << 20, index_cache_size_bytes=64 << 20)

    report = erase(
        lance.dataset(uri, session=shared),
        storage_options={},
        protected=None,
        reopen=lambda: lance.dataset(uri),
        table="t",
        predicate=_PREDICATE,
        retention=timedelta(0),
    )

    assert _holding(uri) == [] and _dangles(uri), "main v2 must be listed with the subject's file gone for this to be the right test"
    assert report.residual_versions == [], [(s.surface, s.outcome, s.detail) for s in report.surfaces]
    assert [(s.surface, s.outcome) for s in report.surfaces if s.outcome == "dangling"] == [("dangling:main@2", "dangling")]
    assert report.pinned_by == []
    assert report.complete is True


def _erase(uri: str, *, retention: timedelta = timedelta(0), reopen: Callable[[], Any] | None = None) -> ErasureReport:
    return erase(
        lance.dataset(uri),
        storage_options={},
        protected=None,
        reopen=reopen or (lambda: lance.dataset(uri)),
        table="t",
        predicate=_PREDICATE,
        retention=retention,
    )


def _unavailable(*_: object, **__: object) -> None:
    raise RuntimeError("object store unavailable")


def _kept_by_a_tag(uri: str, monkeypatch: pytest.MonkeyPatch) -> ErasureReport:
    """``work`` drops the subject at work v3, which a clean tag keeps, then overwrites its head: only the
    tag holds a version standing on main v2's files."""
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT), uri, mode="append")
    lance.dataset(uri).create_branch("work", 2).delete(_PREDICATE)
    lance.dataset(uri).tags.create("kept", ("work", 3))
    lance.write_dataset(_rows("zed"), lance.dataset(uri).checkout_version(("work", None)), mode="overwrite")
    return _erase(uri)


def _inside_the_window(uri: str, monkeypatch: pytest.MonkeyPatch) -> ErasureReport:
    """Erased once, so main v2 dangles for ``work``; erased again under a window every version is inside."""
    _alone(uri)
    _erase(uri)
    return _erase(uri, retention=timedelta(days=1))


def _not_reclaimed(uri: str, monkeypatch: pytest.MonkeyPatch) -> ErasureReport:
    """Erased once, so main v2 dangles; erased again with every ref's cleanup failing."""
    _alone(uri)
    _erase(uri)
    monkeypatch.setattr(lance.LanceDataset, "cleanup_old_versions", _unavailable)
    return _erase(uri)


def _holder_unread(uri: str, monkeypatch: pytest.MonkeyPatch) -> ErasureReport:
    """The verification cannot list ``work``'s versions, so whether one stands on main v2 is unknown."""
    _alone(uri)
    return _erase(uri, reopen=lambda: _Failing(lance.dataset(uri), ("work", None), _Unlistable))


@pytest.mark.parametrize(
    ("build", "keepers"),
    [
        (_kept_by_a_tag, ["tag:kept"]),
        (_inside_the_window, ["branch:work", "the retention window"]),
        (_not_reclaimed, ["history:main (not reclaimed)"]),
        (_holder_unread, ["a holder this could not read"]),
    ],
    ids=["a-tag", "the-window", "an-unreclaimed-ref", "an-unread-holder"],
)
def test_a_dangling_version_names_every_kind_of_keeper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build: Callable[[str, pytest.MonkeyPatch], ErasureReport], keepers: list[str]
) -> None:
    """What releases a dangling version depends on what keeps it, so the surface names every keeper:
    a tag, the window, a ref whose reclaim did not run, and a holder the walk could not read."""
    uri = str(tmp_path / "subjects")

    report = build(uri, monkeypatch)

    detail = next(s.detail for s in report.surfaces if s.surface == "dangling:main@2")
    assert f"until what keeps it lets go: {keepers}" in detail, detail


class _Forwarding:
    """A real Lance object with some members replaced; everything else is forwarded."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _UnreadableFragment(_Forwarding):
    def count_rows(self, *args: Any, **kwargs: Any) -> int:
        raise OSError("transient read failure")

    def scanner(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError("transient read failure")


class _UnreadableVersion(_Forwarding):
    """A version whose reads fail as a transient store error would, with every data file still present."""

    def count_rows(self, *args: Any, **kwargs: Any) -> int:
        raise OSError("transient read failure")

    def scanner(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError("transient read failure")

    def get_fragments(self, filter: Any = None) -> list[_UnreadableFragment]:  # noqa: A002 — pylance's own keyword
        return [_UnreadableFragment(fragment) for fragment in self._inner.get_fragments(filter)]


class _Untracked(_UnreadableVersion):
    """An unreadable version whose file listing fails too, so no fragment can be judged."""

    def tracked_files(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError("listing unavailable")


class _Unlistable(_Forwarding):
    def versions(self) -> list[dict[str, Any]]:
        raise OSError("version listing unavailable")


class _Failing(_Forwarding):
    """The verification's root handle, failing ONE reference the way the test names."""

    def __init__(self, inner: Any, reference: tuple[str | None, int | None], failure: type[_Forwarding]) -> None:
        super().__init__(inner)
        self._reference, self._failure = reference, failure

    def checkout_version(self, reference: Any) -> Any:
        handle = self._inner.checkout_version(reference)
        return self._failure(handle) if tuple(reference) == self._reference else handle


def _clean_v1_then_the_subject(uri: str) -> None:
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT), uri, mode="append")


def test_a_version_that_cannot_be_read_is_a_residual_while_its_files_remain(tmp_path: Path) -> None:
    """Unreadable is not evidence of absence: main v1 never held the subject, but a verification that
    cannot read it — every data file still on storage — has not proved that, so it is a residual."""
    uri = str(tmp_path / "subjects")
    _clean_v1_then_the_subject(uri)

    report = erase(
        lance.dataset(uri),
        storage_options={},
        protected=None,
        reopen=lambda: cast("Any", _Failing(lance.dataset(uri), (None, 1), _UnreadableVersion)),
        table="t",
        predicate=_PREDICATE,
        retention=timedelta(days=1),
    )

    assert "main@1" in report.residual_versions
    assert "['main@1'] could not be read" in next(s.detail for s in report.surfaces if s.surface == "verify")
    assert not [s for s in report.surfaces if s.outcome == "dangling"]
    assert report.complete is False


def test_a_ref_whose_versions_cannot_be_listed_is_not_verified_clean(tmp_path: Path) -> None:
    uri = str(tmp_path / "subjects")
    _clean_v1_then_the_subject(uri)
    lance.dataset(uri).create_branch("work", 1)

    report = erase(
        lance.dataset(uri),
        storage_options={},
        protected=None,
        reopen=lambda: cast("Any", _Failing(lance.dataset(uri), ("work", None), _Unlistable)),
        table="t",
        predicate=_PREDICATE,
        retention=timedelta(0),
    )

    verify = next(s for s in report.surfaces if s.surface == "verify")
    assert (verify.outcome, "['work'] could not be listed" in verify.detail) == ("failed", True), verify.detail
    assert report.complete is False


class _NoBranchList:
    def list(self) -> dict[str, Any]:
        raise OSError("branch store unavailable")


class _BranchesUnlistable(_Forwarding):
    @property
    def branches(self) -> _NoBranchList:
        return _NoBranchList()


def test_a_branch_list_the_verification_cannot_read_again_is_not_verified_clean(tmp_path: Path) -> None:
    """Step 1 listed the branches; a branch cut after that is found only by listing them again, so a
    verification that cannot has not proved the table clean."""
    uri = str(tmp_path / "subjects")
    _clean_v1_then_the_subject(uri)
    lance.dataset(uri).create_branch("work", 1)

    report = erase(
        lance.dataset(uri),
        storage_options={},
        protected=None,
        reopen=lambda: cast("Any", _BranchesUnlistable(lance.dataset(uri))),
        table="t",
        predicate=_PREDICATE,
        retention=timedelta(0),
    )

    verify = next(s for s in report.surfaces if s.surface == "verify")
    assert (verify.outcome, "could not be read again" in verify.detail) == ("failed", True), verify.detail
    assert report.complete is False


def _subject_in_a_fragment_no_rewrite_takes(uri: str) -> None:
    """main's first write is two files with the subject in the first, a 1,048,576-row fragment the test
    keeps out of every rewrite, so its one deleted row stays behind a deletion vector; carol lands at v2,
    ``work`` is cut from v2 with eve, main gains dan. Both rewrites take carol's fragment, and main's
    cleanup keeps v2 for work's head, which stands on the first file: v2 stays listed, cannot be read
    whole, and its first fragment still answers."""
    lance.write_dataset(pa.table({"pii": pa.array([_SUBJECT, *(f"bob-{i}" for i in range(1_100_000))])}), uri)
    lance.write_dataset(_rows("carol"), uri, mode="append")
    lance.write_dataset(_rows("eve"), lance.dataset(uri).create_branch("work", 2), mode="append")
    lance.write_dataset(_rows("dan"), uri, mode="append")


def _keep_fragment_zero_out_of_every_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exclude fragment 0 from every `compact_files` call, the erasure's two passes included, as a store
    that cannot rewrite one fragment would."""
    compact = DatasetOptimizer.compact_files

    def _without_zero(self: DatasetOptimizer, **options: Any) -> Any:
        kwargs: dict[str, Any] = {**options, "excluded_fragment_ids": sorted({0, *(options.get("excluded_fragment_ids") or [])})}
        return compact(self, **kwargs)

    monkeypatch.setattr(DatasetOptimizer, "compact_files", _without_zero)


def test_a_version_unreadable_whole_is_a_residual_while_one_of_its_fragments_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uri = str(tmp_path / "subjects")
    _subject_in_a_fragment_no_rewrite_takes(uri)
    _keep_fragment_zero_out_of_every_rewrite(monkeypatch)

    report = erase(
        lance.dataset(uri), storage_options={}, protected=None, reopen=lambda: lance.dataset(uri), table="t", predicate=_PREDICATE, retention=timedelta(0)
    )

    answering = lance.dataset(uri).checkout_version((None, 2)).get_fragments()[0].count_rows(filter=_PREDICATE)
    assert _dangles(uri) and answering == 1, "main v2 must fail a whole read while its first fragment answers for this to be the right test"
    assert "main@2" in report.residual_versions
    assert not [s.surface for s in report.surfaces if s.surface.startswith("dangling:")]
    assert report.complete is False


class _CheckoutFails(_Forwarding):
    """The verification's root handle, unable to check out one version."""

    def __init__(self, inner: Any, reference: tuple[str | None, int]) -> None:
        super().__init__(inner)
        self._reference = reference

    def checkout_version(self, reference: Any) -> Any:
        if tuple(reference) == self._reference:
            raise OSError("checkout failed")
        return self._inner.checkout_version(reference)


def test_a_version_that_cannot_be_checked_out_is_a_residual(tmp_path: Path) -> None:
    uri = str(tmp_path / "subjects")
    _clean_v1_then_the_subject(uri)

    report = erase(
        lance.dataset(uri),
        storage_options={},
        protected=None,
        reopen=lambda: cast("Any", _CheckoutFails(lance.dataset(uri), (None, 1))),
        table="t",
        predicate=_PREDICATE,
        retention=timedelta(days=1),
    )

    assert "main@1" in report.residual_versions
    assert "['main@1'] could not be read" in next(s.detail for s in report.surfaces if s.surface == "verify")
    assert report.complete is False


class _StoreUnlisted(_Failing):
    def all_files(self) -> Any:
        raise OSError("listing unavailable")


@pytest.mark.parametrize(
    "reopen",
    [
        lambda uri: _Failing(lance.dataset(uri), (None, 1), _Untracked),
        lambda uri: _StoreUnlisted(lance.dataset(uri), (None, 1), _UnreadableVersion),
    ],
    ids=["version-files", "store-listing"],
)
def test_a_version_whose_files_cannot_be_listed_is_a_residual(tmp_path: Path, reopen: Callable[[str], Any]) -> None:
    """The fragment-by-fragment judgement needs both listings; without either the version is unproved."""
    uri = str(tmp_path / "subjects")
    _clean_v1_then_the_subject(uri)

    report = erase(
        lance.dataset(uri), storage_options={}, protected=None, reopen=lambda: reopen(uri), table="t", predicate=_PREDICATE, retention=timedelta(days=1)
    )

    assert "['main@1'] could not be read" in next(s.detail for s in report.surfaces if s.surface == "verify")
    assert report.complete is False


def _reader(rows: list[dict[str, Any]], schema: pa.Schema) -> pa.RecordBatchReader:
    return pa.RecordBatchReader.from_batches(schema, pa.Table.from_pylist(rows, schema=schema).to_batches())


class _ListedElsewhere(_Forwarding):
    """The store's listing, spelled under a base that no location Lance tracks lies under."""

    def all_files(self) -> pa.RecordBatchReader:
        listed = self._inner.all_files().read_all()
        return _reader([{**row, "base_uri": "memory:///elsewhere"} for row in listed.to_pylist()], listed.schema)


class _TrackedTwice(_Forwarding):
    """Every data file tracked under a second base inside the table's root as well as its own."""

    def tracked_files(self, *args: Any, **kwargs: Any) -> pa.RecordBatchReader:
        tracked = self._inner.tracked_files(*args, **kwargs).read_all()
        rows = tracked.to_pylist()
        again = [{**row, "base_uri": f"{row['base_uri']}/tree/elsewhere"} for row in rows if row["type"] == "data file"]
        return _reader([*rows, *again], tracked.schema)


class _TrackedNowhere(_Forwarding):
    """No data file of the version is placed by Lance's own listing."""

    def tracked_files(self, *args: Any, **kwargs: Any) -> pa.RecordBatchReader:
        tracked = self._inner.tracked_files(*args, **kwargs).read_all()
        return _reader([row for row in tracked.to_pylist() if row["type"] != "data file"], tracked.schema)


@pytest.mark.parametrize(
    "reopen",
    [
        lambda uri: _ListedElsewhere(lance.dataset(uri)),
        lambda uri: _Failing(lance.dataset(uri), (None, 2), _TrackedTwice),
        lambda uri: _Failing(lance.dataset(uri), (None, 2), _TrackedNowhere),
    ],
    ids=["listing-spelled-elsewhere", "file-under-two-bases", "file-placed-nowhere"],
)
def test_a_lost_file_the_listing_cannot_place_is_not_proved_gone(tmp_path: Path, reopen: Callable[[str], Any]) -> None:
    """``_alone`` leaves main v2 dangling; each double removes one piece of the evidence that proves it,
    so v2 must stay a residual rather than be reported dangling."""
    uri = str(tmp_path / "subjects")
    _alone(uri)

    report = erase(lance.dataset(uri), storage_options={}, protected=None, reopen=lambda: reopen(uri), table="t", predicate=_PREDICATE, retention=timedelta(0))

    assert _dangles(uri), "main v2 must be listed with a file gone for this to be the right test"
    assert report.residual_versions == ["main@2"], [(s.surface, s.outcome, s.detail) for s in report.surfaces]
    assert not [s.surface for s in report.surfaces if s.surface.startswith("dangling:")]
    assert report.complete is False


@pytest.mark.parametrize(
    "reopen",
    [lambda uri: _CheckoutFails(lance.dataset(uri), (None, 1)), lambda uri: _Failing(lance.dataset(uri), ("work", None), _Unlistable)],
    ids=["its-own-files", "a-branch-cut-from-it"],
)
def test_a_residual_whose_holders_cannot_all_be_read_is_called_unexplained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reopen: Callable[[str], Any]
) -> None:
    """The tag whose delete failed keeps main v1, but what else keeps it could not be read, so the tag is
    named AND the residual is said to be unexplained: pinned_by may be short."""
    uri = str(tmp_path / "subjects")
    lance.write_dataset(_rows(_SUBJECT, "bob"), uri)
    lance.dataset(uri).tags.create("snap", 1)
    lance.dataset(uri).create_branch("work", 1)

    def _boom(self: Any, name: str) -> None:
        raise OSError("tag store unavailable")

    monkeypatch.setattr(type(lance.dataset(uri).tags), "delete", _boom)
    report = erase(lance.dataset(uri), storage_options={}, protected=None, reopen=lambda: reopen(uri), table="t", predicate=_PREDICATE, retention=timedelta(0))

    assert "main@1" in report.residual_versions
    assert [(pin.ref, pin.holds) for pin in report.pinned_by] == [("tag:snap", "main@1")]
    assert "nothing this could read accounts for ['main@1']" in next(s.detail for s in report.surfaces if s.surface == "verify")


_CLEANUP = lance.LanceDataset.cleanup_old_versions


def _keeps_every_version(
    self: lance.LanceDataset,
    older_than: timedelta | None = None,
    retain_versions: int | None = None,
    *,
    delete_unverified: bool = False,
    error_if_tagged_old_versions: bool = True,
    delete_rate_limit: int | None = None,
    versions: list[int] | None = None,
) -> Any:
    """Lance's own cleanup, told to retain every version: a reason to keep one the erasure cannot read."""
    return _CLEANUP(
        self,
        older_than,
        1 << 20,
        delete_unverified=delete_unverified,
        error_if_tagged_old_versions=error_if_tagged_old_versions,
        delete_rate_limit=delete_rate_limit,
        versions=versions,
    )


def test_a_residual_nothing_keeps_is_called_unexplained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No tag, branch, window or unreclaimed ref keeps main v2, so the report names no holder for it and
    says nothing it could read accounts for it."""
    uri = str(tmp_path / "subjects")
    _clean_v1_then_the_subject(uri)
    monkeypatch.setattr(lance.LanceDataset, "cleanup_old_versions", _keeps_every_version)

    report = _erase(uri)

    assert (report.residual_versions, report.pinned_by, report.held_by_retention) == (["main@2"], [], [])
    assert "nothing this could read accounts for ['main@2']" in next(s.detail for s in report.surfaces if s.surface == "verify")


class _Undated(_Forwarding):
    """The verification's root handle, whose version listing carries no commit instant."""

    def versions(self) -> list[dict[str, Any]]:
        return [{**entry, "timestamp": None} for entry in self._inner.versions()]


def test_a_residual_whose_commit_instant_cannot_be_read_is_counted_inside_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without its instant the report cannot show the window has passed for main v2, so the window is
    what it names, and erasing again once it has passed is the remedy it gives."""
    uri = str(tmp_path / "subjects")
    _clean_v1_then_the_subject(uri)
    monkeypatch.setattr(lance.LanceDataset, "cleanup_old_versions", _keeps_every_version)

    report = _erase(uri, reopen=lambda: _Undated(lance.dataset(uri)))

    assert (report.residual_versions, report.pinned_by, report.held_by_retention) == (["main@2"], [], ["main@2"])
    assert "accounts for" not in next(s.detail for s in report.surfaces if s.surface == "verify")


def test_a_verification_that_cannot_open_the_table_still_answers_with_the_report(tmp_path: Path) -> None:
    """The verification opens its handle after steps 1-5 have deleted, dropped the pinning tag and
    reclaimed history, so a failed open must not lose their report: it is a failed `verify`."""
    uri = str(tmp_path / "subjects")
    _alone(uri)

    def _unopenable() -> Any:
        raise OSError("SlowDown: please reduce your request rate")

    report = erase(lance.dataset(uri), storage_options={}, protected=None, reopen=_unopenable, table="t", predicate=_PREDICATE, retention=timedelta(0))

    verify = next(s for s in report.surfaces if s.surface == "verify")
    assert verify.outcome == "failed"
    assert "could not open the table" in verify.detail and "nothing was verified" in verify.detail, verify.detail
    assert [(s.surface, s.outcome) for s in report.surfaces if s.surface != "verify"] == [
        ("branch:work", "deleted"),
        ("main", "deleted"),
        ("compact:work", "rewritten"),
        ("compact:main", "rewritten"),
        ("tag:snap", "untagged"),
        ("history:work", "reclaimed"),
        ("history:main", "reclaimed"),
    ]
    assert (report.residual_versions, report.pinned_by, report.complete) == ([], [], False)


def _table(root: Path, first: pa.Table, *more: pa.Table) -> LanceNamespace:
    """``subjects`` through the catalog's own create path, one fragment per batch."""
    ns = lance_namespace.connect("dir", {"root": str(root)})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, first.schema) as writer:
        writer.write_table(first)
    create_table(ns, {}, _TABLE_ID, read_arrow_body(sink.getvalue().to_pybytes(), max_bytes=_BODY_LIMIT), mode="create", registry=None)
    for batch in more:
        open_dataset(ns, {}, _TABLE_ID).insert(batch)
    return ns


def _namespace(root: Path) -> LanceNamespace:
    ns = _table(root, _rows("bob"), _rows(_SUBJECT))
    open_dataset(ns, {}, _TABLE_ID).tags.create("snap", 2)
    open_dataset(ns, {}, _TABLE_ID).create_branch("work", 2)
    return ns


class _ResolvesOnce(_Forwarding):
    """A namespace that answers the first ``describe_table`` and then fails, as a store throttling
    after a heavy cleanup would."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner)
        self._calls = 0

    def describe_table(self, request: Any) -> Any:
        self._calls += 1
        if self._calls > 1:
            raise OSError("SlowDown: please reduce your request rate")
        return self._inner.describe_table(request)


@contextmanager
def _door(namespace: Any, settings: Settings | None = None) -> Iterator[TestClient]:
    settings = settings or Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s")
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: namespace
    application.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(application, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with _door(_namespace(tmp_path / "data")) as test_client:
        yield test_client


def test_the_door_verifies_the_location_it_erased_without_resolving_it_again(tmp_path: Path) -> None:
    """The evidence must come from the location the erasure ran on, and a namespace that fails once the
    erasure has run must not turn it into a 500 that says nothing happened."""
    with _door(_ResolvesOnce(_namespace(tmp_path / "data"))) as test_client:
        response = test_client.post("/management/v1/table/subjects/erasure", json={"predicate": _PREDICATE})

    assert response.status_code == 200, response.text
    report = response.json()
    assert [s["outcome"] for s in report["surfaces"] if s["surface"] == "verify"] == ["clean"], report["surfaces"]
    assert report["complete"] is True


def test_a_verification_the_door_cannot_open_answers_200_with_the_report(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_session() -> Any:
        raise OSError("cannot allocate the verification's session")

    monkeypatch.setattr(door, "fresh_lance_session", _no_session)
    response = client.post("/management/v1/table/subjects/erasure", json={"predicate": _PREDICATE})

    assert response.status_code == 200, response.text
    report = response.json()
    verify = next(s for s in report["surfaces"] if s["surface"] == "verify")
    assert (verify["outcome"], "nothing was verified" in verify["detail"]) == ("failed", True), verify
    assert [s["outcome"] for s in report["surfaces"] if s["surface"] in ("main", "branch:work", "tag:snap")] == ["deleted", "deleted", "untagged"]
    assert report["complete"] is False


_IDENTIFIER = "alice-19700101-1234"
_UNDER_THE_THRESHOLD = _rows(*(f"person-{i}" for i in range(19)), _IDENTIFIER)


def _indexed(kind: Literal["BTREE", "BITMAP"]) -> Callable[[Path], LanceNamespace]:
    """The subject under the rewrite threshold, ``pii`` indexed: a segment keeps a key through compaction."""

    def build(root: Path) -> LanceNamespace:
        ns = _table(root, _UNDER_THE_THRESHOLD)
        open_dataset(ns, {}, _TABLE_ID).create_scalar_index("pii", kind)
        return ns

    return build


def _created(root: Path, table: Callable[[Path], pa.Table], *, external: bool) -> LanceNamespace:
    """``subjects`` created through the catalog's create path with one base beside it, outside its location,
    recorded on the control root the test's base judge reads ([[LH-279]], [[LH-209]]): its external blob base,
    or else its data base."""
    ns = lance_namespace.connect("dir", {"root": str(root)})
    base = root / "bases" / "payloads"
    base.mkdir(parents=True)
    body = table(base)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, body.schema) as writer:
        writer.write_table(body)
    payload = read_arrow_body(sink.getvalue().to_pybytes(), max_bytes=_BODY_LIMIT)
    bases = [str(base)]
    create_table(
        ns,
        {},
        _TABLE_ID,
        payload,
        mode="create",
        registry=base_registry.BaseRegistry(control_root=str(_control(root))),
        external_blob_bases=bases if external else None,
        data_bases=None if external else bases,
    )
    return ns


def _control(root: Path) -> Path:
    return root.parent / "control"


def _payloads(base: Path) -> pa.Table:
    names = [*(f"person-{i}" for i in range(19)), _IDENTIFIER]
    for name in names:
        (base / f"{name}.bin").write_bytes(name.encode() * 4)
    return pa.table({"pii": pa.array(names), "scan": lance.blob_array([str(base / f"{name}.bin") for name in names])})


def _external_payloads(root: Path) -> LanceNamespace:
    """Each row's blob-v2 value names a payload under the table's registered external base."""
    return _created(root, _payloads, external=True)


def _data_base(root: Path) -> LanceNamespace:
    """The table's data files land under an approved data base."""
    return _created(root, lambda _: _UNDER_THE_THRESHOLD, external=False)


def _external_payloads_deleted_first(compacted: bool) -> Callable[[Path], LanceNamespace]:
    """The delete door removed the subject before the erasure, and a materialising compaction too when
    ``compacted``: the head no longer points at the subject's payload, a retained version does."""

    def build(root: Path) -> LanceNamespace:
        ns = _external_payloads(root)
        dataset = open_dataset(ns, {}, _TABLE_ID)
        dataset.delete(f"pii = '{_IDENTIFIER}'")
        if compacted:
            dataset.optimize.compact_files(materialize_deletions_threshold=0.0)
        return ns

    return build


def _tagged_before_the_subject_arrived(root: Path) -> LanceNamespace:
    """A run's tag names a BTREE-indexed version the subject never touched; it arrives after, and the index
    is optimized over it."""
    ns = _table(root, _rows(*(f"person-{i}" for i in range(20))))
    dataset = open_dataset(ns, {}, _TABLE_ID)
    dataset.create_scalar_index("pii", "BTREE")
    dataset.tags.create("trained", dataset.version)
    dataset.insert(_rows(_IDENTIFIER))
    dataset.optimize.optimize_indices()
    return ns


def _through_a_mem_wal_shard(root: Path) -> LanceNamespace:
    """The subject arrives through a MemWAL shard writer, so the base table never holds it."""
    ns = _table(root, _rows(*(f"person-{i}" for i in range(19))))
    dataset = open_dataset(ns, {}, _TABLE_ID)
    dataset.initialize_mem_wal(unsharded=True)
    writer = open_dataset(ns, {}, _TABLE_ID).mem_wal_writer("6f1d9a52-3c1e-4c5e-9a43-0b6f3c2d7e10")
    writer.put(_rows(_IDENTIFIER))
    writer.close()
    return ns


def _tagged_over_a_stale_index_segment(root: Path) -> LanceNamespace:
    """The delete door and a materialising compaction removed the subject's row and kept the BTREE segment
    holding its key, cleanup reclaimed every earlier version, and a run's tag names the result."""
    ns = _indexed("BTREE")(root)
    dataset = open_dataset(ns, {}, _TABLE_ID)
    dataset.delete(f"pii = '{_IDENTIFIER}'")
    dataset.optimize.compact_files(materialize_deletions_threshold=0.0)
    dataset.cleanup_old_versions(timedelta(0))
    dataset.tags.create("trained", dataset.version)
    return ns


def _tagged_after_a_plain_delete(root: Path) -> LanceNamespace:
    """The delete door removed the subject at v2, behind a deletion vector, and a run's tag names v2."""
    ns = _table(root, _UNDER_THE_THRESHOLD)
    open_dataset(ns, {}, _TABLE_ID).delete(f"pii = '{_IDENTIFIER}'")
    open_dataset(ns, {}, _TABLE_ID).tags.create("trained", 2)
    return ns


@pytest.mark.parametrize(
    ("build", "bound", "expected"),
    [
        pytest.param(lambda root: _table(root, _UNDER_THE_THRESHOLD), None, (True, True, [], []), id="under-the-rewrite-threshold"),
        pytest.param(
            lambda root: _table(root, _rows(*(f"person-{i}" for i in range(10))), _rows(_IDENTIFIER, _IDENTIFIER)),
            None,
            (True, True, [], []),
            id="a-whole-fragment",
        ),
        pytest.param(
            lambda root: _table(root, _rows(*(f"x-{i}" for i in range(60_000))), _rows(_IDENTIFIER, "carol")),
            64 * 1024,
            (True, True, [], []),
            id="beside-a-fragment-over-the-bound",
        ),
        pytest.param(
            lambda root: _table(root, _rows(*(f"x-{i}" for i in range(20_000)), _IDENTIFIER)),
            64 * 1024,
            (True, True, [], []),
            id="in-a-fragment-over-the-bound",
        ),
        pytest.param(_indexed("BTREE"), None, (True, True, [], []), id="a-btree-indexed-column"),
        pytest.param(_indexed("BITMAP"), None, (True, True, [], []), id="a-bitmap-indexed-column"),
        pytest.param(_tagged_before_the_subject_arrived, None, (True, True, [], []), id="a-tag-over-an-indexed-version-the-subject-never-touched"),
        pytest.param(_tagged_after_a_plain_delete, None, (False, False, ["pinned_by"], ["verify"]), id="a-tag-over-an-earlier-plain-delete"),
        pytest.param(_tagged_over_a_stale_index_segment, None, (False, False, ["pinned_by"], ["verify"]), id="a-tag-over-a-stale-index-segment"),
        pytest.param(_external_payloads, None, (False, False, ["external_base"], []), id="an-external-payload"),
        pytest.param(_external_payloads_deleted_first(False), None, (False, False, ["external_base"], []), id="an-external-payload-deleted-first"),
        pytest.param(_external_payloads_deleted_first(True), None, (False, False, ["external_base"], []), id="an-external-payload-deleted-and-compacted-first"),
        pytest.param(_data_base, None, (False, False, ["data_base"], ["compact:main", "history:main", "verify"]), id="a-data-base"),
        pytest.param(_through_a_mem_wal_shard, None, (False, False, ["mem_wal"], []), id="a-mem-wal-shard"),
    ],
)
def test_no_object_under_the_table_root_holds_the_identifier_or_the_report_names_what_keeps_it(
    tmp_path: Path,
    build: Callable[[Path], LanceNamespace],
    bound: int | None,
    expected: tuple[bool, bool, list[str], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lance records a delete's filter in its transaction and the head manifest, its default rewrite threshold
    leaves a fragment holding one subject row as it was, and the compact door's byte bound caps a whole run,
    so a small fragment beside a large one was never rewritten ([[LH-281]]); each kept the identifier on
    storage under `complete: true`. So did a fragment larger than that bound, and an index segment, which
    compaction keeps with the subject's key ([[LH-263]]). A tag over a version whose subject row a deletion
    vector hides keeps those bytes, and so do an external payload, a data file under a data base and a
    MemWAL shard, none of which the erasure rewrites, deleted before the erasure or not: the report says so and
    names what keeps them, beside surfaces that say why nothing else failed. A tag over an indexed version the
    subject never touched keeps nothing of it.

    The bases sit outside the table's location and are judged by the catalog's own base judge over the record
    the create wrote, as the deployed lifespan installs it."""
    root = tmp_path / "data"
    if bound is not None:
        monkeypatch.setattr(erasure_module, "COMPACTION_BOUND", {**COMPACTION_BOUND, "max_source_bytes": bound})
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_CONTROL_ROOT=str(_control(root)))
    base_judge.install(base_judge.BaseJudge.from_settings(settings))
    try:
        with _door(build(root), settings) as client:
            response = client.post("/management/v1/table/subjects/erasure", json={"predicate": f"pii = '{_IDENTIFIER}'"})
    finally:
        base_judge.install(None)

    assert response.status_code == 200, response.text
    report = response.json()
    holders = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and _IDENTIFIER.encode() in path.read_bytes())
    named = [field for field in ("held_by_retention", "pinned_by") if report[field]]
    named += sorted({s["surface"].split(":", 1)[0] for s in report["surfaces"] if s["outcome"] == "held"})
    unclean = sorted(s["surface"] for s in report["surfaces"] if s["outcome"] in ("failed", "skipped"))
    assert (holders == [], report["complete"], named, unclean) == expected, (holders, report)


def _history(location: str) -> tuple[dict[str, Any], list[int], list[int], int]:
    """What an erasure can destroy: the tags, each ref's versions, and the subject's rows on main."""
    dataset = lance.dataset(location)
    return (
        dict(dataset.tags.list()),
        [entry["version"] for entry in dataset.versions()],
        [entry["version"] for entry in dataset.checkout_version(("work", None)).versions()],
        dataset.count_rows(filter=_PREDICATE),
    )


@pytest.mark.parametrize(
    "predicate",
    [
        pytest.param("pii_typo = 'alice'", id="an-unknown-column"),
        pytest.param("CAST(pii AS INT) = 1", id="a-cast-that-fails"),
        pytest.param("count(*) > 0", id="an-aggregate"),
    ],
)
def test_a_predicate_lance_refuses_answers_400_and_touches_nothing(tmp_path: Path, predicate: str) -> None:
    """A column typed wrong made every delete fail while the erasure still dropped the tag it could not
    probe and reclaimed history ([[LH-210]]), and a predicate Lance parses but cannot evaluate answered
    as a store fault."""
    ns = _namespace(tmp_path / "data")
    location = open_dataset(ns, {}, _TABLE_ID).uri
    before = _history(location)
    with _door(ns) as client:
        response = client.post("/management/v1/table/subjects/erasure", json={"predicate": predicate})

    assert (response.status_code, response.json()["title"]) == (400, "InvalidInputError"), response.text
    assert _history(location) == before
