"""An erasure's verification judges, reclaims and verifies EVERY ref's history, and names what is left per ref ([[LH-263]]).

The bring-your-own job rewrites every ref (the conftest's ``ErasureJob``, [[LH-493]]); the claims here are the
catalog's: what it judges, what it reclaims, and what the report says keeps the rest.

A branch keeps its own history under ``tree/<name>/`` (``lance_docs/lance/format/table/branch_tag.md`` "Branch Dataset
Layout"), numbered in its own line (``lance_docs/lance/guide/tags_and_branches.md`` Branches: "version numbers may overlap across
branches"). So a subject written ON a branch lives in versions that main's cleanup never lists and main's
verification never probes: after the erasure, ``checkout_version(("work", 3))`` still returns it.

The oracle here, ``_surviving``, reads every ref's every version itself rather than trusting the report,
because the claim under test is that the report and the table agree.

``pinned_by`` is only actionable if following it succeeds, and only honest if it names nothing that holds
nothing: a branch is named only when its head stands on a residual's files. Measured on pylance 12.0.0,
Lance refuses to delete a branch while another branch is cut from it ("Branch work is referenced by
[("deeper", 3)] versions, can not delete") or while a tag names it ("Branch work is referenced by tags
[("t", 2)]"), so a branch that must go brings those first.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest

from catalog.services.erasure import ErasureReport, ref_heads, verify


_SUBJECT = "alice"
_PREDICATE = "pii = 'alice'"
_NOW = timedelta(seconds=0)


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


type _Erase = Callable[..., ErasureReport]


def _verify(uri: str, retention: timedelta = _NOW, trusted: dict[str, int] | None = None) -> ErasureReport:
    """The verification of an obligation recorded at ``trusted``'s heads, by default the table's heads now."""
    return verify(
        lance.dataset(uri),
        storage_options={},
        protected=None,
        reopen=lambda: lance.dataset(uri),
        table="acme-bronze$subjects",
        predicate=_PREDICATE,
        retention=retention,
        trusted=trusted if trusted is not None else ref_heads(lance.dataset(uri)),
        history=None,
        derived=list,
    )


@pytest.fixture
def erase(erasure_job: Any) -> _Erase:  # noqa: ANN401 — conftest.ErasureJob
    """The job rewrites every ref, then the catalog verifies."""

    def _erase(uri: str, retention: timedelta = _NOW) -> ErasureReport:
        recorded = ref_heads(lance.dataset(uri))
        erasure_job(uri, _PREDICATE).run()
        return _verify(uri, retention, recorded)

    return _erase


def _surviving(uri: str) -> set[str]:
    """Every ``<ref>@<version>`` that still answers the predicate, read from the table rather than the report."""
    root = lance.dataset(uri)
    found: set[str] = set()
    for ref in [None, *root.branches.list()]:
        handle = root if ref is None else root.checkout_version((ref, None))
        for entry in handle.versions():
            version = int(entry["version"])
            if root.checkout_version((ref, version)).count_rows(filter=_PREDICATE):
                found.add(f"{ref or 'main'}@{version}")
    return found


def _follow(uri: str, report: ErasureReport) -> None:
    """Delete every ref ``pinned_by`` names, in the order it names them."""
    dataset = lance.dataset(uri)
    for pin in report.pinned_by:
        kind, name = pin.ref.split(":", 1)
        (dataset.branches if kind == "branch" else dataset.tags).delete(name)


def _subject_on_a_branch(tmp_path: Path) -> str:
    """main v1 bob, v2 carol; ``work`` cut from main v2 gains the subject at v3 and dan at v4."""
    uri = str(tmp_path / "subjects")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows("carol"), uri, mode="append")
    work = lance.dataset(uri).create_branch("work", 2)
    lance.write_dataset(_rows(_SUBJECT), work, mode="append")
    lance.write_dataset(_rows("dan"), lance.dataset(uri).checkout_version(("work", None)), mode="append")
    assert _surviving(uri) == {"work@3", "work@4"}, "the subject must live in work's own history alone for this to be the right test"
    return uri


def test_no_version_of_any_ref_answers_after_the_erasure(tmp_path: Path, erase: _Erase) -> None:
    uri = _subject_on_a_branch(tmp_path)

    report = erase(uri)

    assert _surviving(uri) == set(), [(s.surface, s.outcome) for s in report.surfaces]
    assert (report.complete, report.settled) == (True, True)


@pytest.mark.parametrize("retention", [timedelta(days=1)], ids=["inside-the-window"])
def test_the_report_names_exactly_what_survives_on_every_ref(tmp_path: Path, retention: timedelta, erase: _Erase) -> None:
    """Inside the retention window work v3 and v4 legitimately survive; the report must say so, per ref."""
    uri = _subject_on_a_branch(tmp_path)

    report = erase(uri, retention)

    surviving = _surviving(uri)
    assert set(report.residual_versions) == surviving
    assert report.complete is (not surviving)


@pytest.mark.parametrize("ref", ["work"], ids=["branch"])
def test_a_write_staged_while_the_erasure_runs_still_commits_readable(tmp_path: Path, ref: str | None, erase: _Erase) -> None:
    """Every ref's reclaim leaves another writer's uncommitted files alone.

    Lance keeps an unreferenced file younger than 7 days because such files "are indistinguishable from files being
    written by an in-progress operation" (``lance_docs/lance/guide/read_and_write.md`` cleanup_old_versions), and says
    of ``delete_unverified=True``: "Any concurrent write may have its files deleted out from under it, leaving the
    dataset corrupted" (``lance_docs/lance/guide/read_and_write.md`` Cleanup). Measured on pylance 12.0.0 with the flag: the
    staged file is deleted, the writer's commit still succeeds, and the ref's head no longer reads.
    """
    uri = _subject_on_a_branch(tmp_path)
    writer = lance.dataset(uri) if ref is None else lance.dataset(uri).checkout_version((ref, None))
    staged = lance.fragment.write_fragments(_rows("zed"), writer)

    erase(uri)
    lance.LanceDataset.commit(writer, lance.LanceOperation.Append(staged), read_version=writer.version)

    head = lance.dataset(uri) if ref is None else lance.dataset(uri).checkout_version((ref, None))
    assert "zed" in head.to_table().column("pii").to_pylist()


def test_the_subjects_bytes_leave_the_branchs_own_data_files(tmp_path: Path, erase: _Erase) -> None:
    """A delete leaves the subject's bytes in the branch's live data file behind a deletion vector.

    Measured on pylance 12.0.0: after the delete and the branch's own cleanup the file under
    ``tree/work/data`` still holds them; once the job has compacted through the branch handle, the
    verification's reclaim of the branch removes the original.
    """
    uri = str(tmp_path / "bytes")
    lance.write_dataset(_rows("bob"), uri)
    work = lance.dataset(uri).create_branch("work", 1)
    lance.write_dataset(_rows(_SUBJECT, "eve"), work, mode="append")
    branch_data = Path(uri) / "tree" / "work" / "data"

    def holding() -> list[str]:
        return [p.name for p in branch_data.rglob("*.lance") if _SUBJECT.encode() in p.read_bytes()]

    assert holding(), "the subject's bytes must sit in a branch-owned data file for this to be the right test"

    erase(uri)

    assert holding() == []


#: A parent and the branch cut from it. Both orders, because the reclaim and `pinned_by` must run a child
#: before its parent by DEPTH: with `work`/`deeper` plain name order happens to be deepest first too.
_NAMES = pytest.mark.parametrize("names", [("work", "deeper"), ("alpha", "zulu")], ids=["child-sorts-first", "child-sorts-last"])


def _chain(tmp_path: Path, names: tuple[str, str]) -> str:
    """main v2 holds the subject; ``parent`` cut from main v2 gains eve at v3; ``child`` is cut from (parent, 3)."""
    parent, child = names
    uri = str(tmp_path / "chain")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT, "carol"), uri, mode="append")
    lance.write_dataset(_rows("eve"), lance.dataset(uri).create_branch(parent, 2), mode="append")
    lance.dataset(uri).create_branch(child, (parent, 3))
    return uri


@_NAMES
def test_a_chain_every_ref_rewrites_is_erased_without_deleting_a_branch(tmp_path: Path, names: tuple[str, str], erase: _Erase) -> None:
    """A fork pin lasts while a retained branch version references the fork version's files.

    Every fragment here is small enough to be rewritten, so once the child, then the parent, have been
    compacted and reclaimed, nothing references main v2 or the parent's v3 and main's cleanup takes them.
    Reclaiming a parent before its child keeps both, holding the subject (measured on pylance 12.0.0).
    """
    uri = _chain(tmp_path, names)

    report = erase(uri)

    assert _surviving(uri) == set(), report.residual_versions
    assert report.complete is True
    assert set(lance.dataset(uri).branches.list()) == set(names)


def _pinned_chain(tmp_path: Path, names: tuple[str, str]) -> str:
    """The chain, plus a clean tag on the child's v4 — a retained version that still stands on the
    parent's v3 and main v2."""
    uri = _chain(tmp_path, names)
    lance.dataset(uri).checkout_version((names[1], None)).delete(_PREDICATE)
    lance.dataset(uri).tags.create("trained", (names[1], 4))
    assert not lance.dataset(uri).checkout_version("trained").count_rows(filter=_PREDICATE), "the tag must be clean for this to be the right test"
    return uri


def _tagged_branch(tmp_path: Path, names: tuple[str, str]) -> str:
    """main v2 holds the subject; ``parent`` cut from it drops the subject at v3, which a clean tag names."""
    uri = str(tmp_path / "tagged")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT, "carol"), uri, mode="append")
    lance.dataset(uri).create_branch(names[0], 2).delete(_PREDICATE)
    lance.dataset(uri).tags.create("trained", (names[0], 3))
    assert not lance.dataset(uri).checkout_version("trained").count_rows(filter=_PREDICATE), "the tag must be clean for this to be the right test"
    return uri


def _clean_descendant(tmp_path: Path, names: tuple[str, str]) -> str:
    """``_tagged_branch`` plus ``child`` cut from the clean parent v3: it holds nothing."""
    uri = _tagged_branch(tmp_path, names)
    lance.dataset(uri).create_branch(names[1], (names[0], 3))
    return uri


@_NAMES
def test_a_tag_on_a_descendant_is_named_and_no_branch_is(tmp_path: Path, names: tuple[str, str], erase: _Erase) -> None:
    """``trained`` keeps the child's v4, which stands on the parent's v3 and main v2, both holding the subject.

    The branches' heads were rewritten, so neither stands on those files: deleting the tag alone lets
    the next erasure reclaim both. Naming a branch would send someone to destroy a working ref for
    nothing ([[LH-178]]). The tagged child v4 is a residual too: the subject's row sits behind its
    deletion vector.
    """
    uri = _pinned_chain(tmp_path, names)

    report = erase(uri)

    assert "trained" in lance.dataset(uri).tags.list(), "the clean tag must survive the erasure itself"
    assert report.residual_versions == ["main@2", *sorted([f"{names[0]}@3", f"{names[1]}@4"])]
    assert report.model_dump()["pinned_by"] == [{"ref": "tag:trained", "holds": f"{names[1]}@4"}]


@_NAMES
def test_a_descendant_that_holds_nothing_is_not_named(tmp_path: Path, names: tuple[str, str], erase: _Erase) -> None:
    uri = _clean_descendant(tmp_path, names)

    report = erase(uri)

    assert report.residual_versions == ["main@2", f"{names[0]}@3"]
    assert report.model_dump()["pinned_by"] == [{"ref": "tag:trained", "holds": f"{names[0]}@3"}]


@pytest.mark.parametrize(
    ("build", "names"),
    [
        pytest.param(_pinned_chain, ("work", "deeper"), id="pinning-descendant-child-sorts-first"),
        pytest.param(_pinned_chain, ("alpha", "zulu"), id="pinning-descendant-child-sorts-last"),
        pytest.param(_clean_descendant, ("work", "deeper"), id="clean-descendant-child-sorts-first"),
    ],
)
def test_following_pinned_by_finishes_the_erasure_and_keeps_every_branch(
    tmp_path: Path, build: Callable[[Path, tuple[str, str]], str], names: tuple[str, str], erase: _Erase
) -> None:
    uri = build(tmp_path, names)
    branches = set(lance.dataset(uri).branches.list())

    _follow(uri, erase(uri))
    report = erase(uri)

    assert report.complete is True, [(s.surface, s.outcome, s.detail) for s in report.surfaces]
    assert _surviving(uri) == set()
    assert set(lance.dataset(uri).branches.list()) == branches


@pytest.mark.parametrize("names", [("work", "deeper")], ids=["child-sorts-first"])
def test_inside_the_retention_window_no_ref_is_named(tmp_path: Path, names: tuple[str, str], erase: _Erase) -> None:
    """Every version here is minutes old, so a 1-day window keeps them all — the branches hold nothing
    the window does not. The report says so, and an erasure at retention 0 finishes with both intact."""
    uri = _chain(tmp_path, names)

    report = erase(uri, timedelta(days=1))

    assert report.residual_versions, "the window must keep a residual for this to be the right test"
    assert (report.pinned_by, report.held_by_retention) == ([], report.residual_versions)
    assert erase(uri).complete is True
    assert set(lance.dataset(uri).branches.list()) == set(names)


def test_a_residual_whose_reclaim_failed_names_no_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erase: _Erase) -> None:
    """``trained`` stands on main v2's files, and would be named had the reclaim run. It did not, so
    deleting the tag cannot finish the erasure: the report says the reclaim is what is missing."""
    uri = _tagged_branch(tmp_path, ("work", "deeper"))

    def _boom(*_: object, **__: object) -> None:
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr(lance.LanceDataset, "cleanup_old_versions", _boom)
    report = erase(uri)

    verify = next(s.detail for s in report.surfaces if s.surface == "verify")
    assert [s.outcome for s in report.surfaces if s.surface.startswith("history:")] == ["failed", "failed"]
    held = ["main@2", "main@3", "main@4", "work@2", "work@3", "work@4"]
    assert (report.residual_versions, report.pinned_by, report.held_by_retention) == (held, [], [])
    assert f"{held} remain because the reclaim of ['main', 'work'] did not run" in verify
    assert "accounts for" not in verify


def _heads_on_the_subjects_file(tmp_path: Path, names: tuple[str, str]) -> str:
    """The subject is 1 row of a 20,001-row fragment of 152 KB that ``parent`` and ``child`` both stand on;
    the child dropped the subject itself and a clean tag keeps that version. ``<child>-rewrite``, cut from
    it, overwrote everything, so it stands on nothing — it only blocks deleting the child."""
    parent, child = names
    uri = str(tmp_path / "heads")
    lance.write_dataset(_rows(_SUBJECT, *(f"x-{i}" for i in range(20_000))), uri)
    lance.dataset(uri).create_branch(parent, 1)
    lance.dataset(uri).create_branch(child, (parent, 1)).delete(_PREDICATE)
    lance.dataset(uri).tags.create("kept", (child, 2))
    lance.write_dataset(_rows("zed"), lance.dataset(uri).create_branch(f"{child}-rewrite", (child, 2)), mode="overwrite")
    return uri


@_NAMES
def test_pinned_by_names_what_stands_on_the_residual(tmp_path: Path, names: tuple[str, str], erase: _Erase) -> None:
    """Every head is rewritten off main v1's file, so ``parent``'s fork version is reclaimed and the tag on
    ``child``'s version, which still reads main v1's file, is the one thing keeping it: the obligation settles
    on that pin, and following it finishes the erasure."""
    _, child = names
    uri = _heads_on_the_subjects_file(tmp_path, names)

    report = erase(uri)

    assert report.residual_versions == ["main@1", f"{child}@2"]
    assert ([(pin.ref, pin.holds) for pin in report.pinned_by], report.settled) == ([("tag:kept", f"{child}@2")], True)
    _follow(uri, report)
    again = erase(uri)
    assert (again.residual_versions, again.complete) == ([], True)


def test_a_branch_whose_fork_point_cannot_be_read_is_still_erased(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erase: _Erase) -> None:
    uri = _subject_on_a_branch(tmp_path)
    monkeypatch.setattr(type(lance.dataset(uri).branches), "list", lambda self: {"work": {}})

    report = erase(uri)

    assert _surviving(uri) == set(), [(s.surface, s.outcome, s.detail) for s in report.surfaces]
    assert report.complete is True


def test_an_unlistable_branch_set_is_not_verified_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    """Verification covers the refs it can list, so a table whose branches cannot be listed is not proved clean."""
    uri = str(tmp_path / "unlisted")
    dataset = lance.write_dataset(_rows(_SUBJECT, "bob"), uri)
    job = erasure_job(uri, _PREDICATE)
    job.delete()
    job.compact()

    def _boom(self: Any) -> None:
        raise RuntimeError("branch store unavailable")

    monkeypatch.setattr(type(dataset.branches), "list", _boom)
    report = _verify(uri)

    assert [s.outcome for s in report.surfaces if s.surface == "branches"] == ["failed"]
    assert (report.complete, report.settled) == (False, False)


def test_an_unreadable_fork_point_is_named_when_something_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erase: _Erase) -> None:
    """What keeps a residual is walked through fork points, so one that cannot be read may hide a holder."""
    uri = _tagged_branch(tmp_path, ("work", "deeper"))
    monkeypatch.setattr(type(lance.dataset(uri).branches), "list", lambda self: {"work": {}})

    report = erase(uri)

    assert report.residual_versions == ["main@2", "work@3"], "the tag must keep main v2 for this to be the right test"
    assert "the fork point of ['work'] could not be read" in next(s.detail for s in report.surfaces if s.surface == "verify")


def test_a_branch_cut_while_the_verification_runs_is_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erase: _Erase) -> None:
    """Branches are listed again for the verification: one cut from a pre-delete version after step 1
    listed them holds the subject at its head, and the report must say so rather than miss it. Its head is
    a residual the job never reached, so the job and a verification after it take it and release main v2 with
    it: the branch is not named in ``pinned_by``, since that would send its owners a notice to lose a working
    ref for nothing ([[LH-178]])."""
    uri = str(tmp_path / "late")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT), uri, mode="append")
    lance.dataset(uri).tags.create("snap", 2)
    tags = type(lance.dataset(uri).tags)
    delete = tags.delete

    def _someone_branches_first(self: Any, name: str) -> None:
        lance.dataset(uri).create_branch("late", 2)
        delete(self, name)

    monkeypatch.setattr(tags, "delete", _someone_branches_first)
    report = erase(uri)

    verify = next(s.detail for s in report.surfaces if s.surface == "verify")
    assert report.residual_versions == ["main@2", "late@2"]
    assert "['late'] appeared during the verification: verify again" in verify
    assert "['late@2'] are heads whose rows still answer" in verify
    assert (report.pinned_by, report.complete, report.settled) == ([], False, False)


def test_a_ref_the_job_leaves_keeps_its_tags_and_history_and_names_no_branch(tmp_path: Path, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    """``work`` is cut from main v1, which a reproducibility tag names. The job erases ``work`` and not main, so
    main v1 is main's own head: no cleanup takes it and deleting ``work`` frees nothing, so the report must name
    main's head rather than the branch. Dropping the tag or reclaiming main's history would each destroy something
    while the subject stays ([[LH-210]])."""
    uri = str(tmp_path / "head")
    lance.write_dataset(_rows(_SUBJECT, *(f"x-{i}" for i in range(20_000))), uri)
    lance.dataset(uri).create_branch("work", 1)
    lance.dataset(uri).tags.create("snap", 1)
    job = erasure_job(uri, _PREDICATE)
    job.delete("work")
    job.compact("work")

    report = _verify(uri)

    assert report.residual_versions == ["main@1"]
    assert report.pinned_by == []
    assert "['main@1'] are heads whose rows still answer" in next(s.detail for s in report.surfaces if s.surface == "verify")
    kept = {s.surface: s.outcome for s in report.surfaces if s.surface in ("main", "tag:snap", "compact:main", "history:main")}
    expected = {"main": "failed", "tag:snap": "skipped", "compact:main": "skipped", "history:main": "skipped"}
    assert (kept, "snap" in lance.dataset(uri).tags.list()) == (expected, True)


@pytest.mark.timeout(10)
def test_the_holder_walk_ends_on_a_cyclic_fork_map() -> None:
    """Each version is walked once: a malformed ``_refs`` whose fork points loop must not recurse until
    the interpreter gives up, which would escape the erasure after every destructive step had run."""
    import catalog.services.erasure as module

    class _Everywhere:
        """Every version of every ref retained, outside the window, and standing on one shared file."""

        def reclaimed(self, ref: str | None) -> bool:
            return True

        def inside_window(self, reference: tuple[str | None, int]) -> bool:
            return False

        def files(self, reference: tuple[str | None, int]) -> frozenset[str]:
            return frozenset({"data/shared.lance"})

        def versions(self, ref: str | None) -> dict[int, None]:
            return {1: None, 2: None, 3: None}

    held = module._holders(("x", 2), cast("Any", _Everywhere()), {"x": ("y", 2), "y": ("x", 2)}, {})

    assert held.branches == {"x", "y"}


def test_a_child_sorts_before_a_parent_whose_fork_point_cannot_be_read() -> None:
    """``x``'s own fork point is unreadable; ``y`` is cut from it and must still be reclaimed first."""
    import catalog.services.erasure as module

    forks: dict[str, tuple[str | None, int] | None] = {"work": (None, 2), "x": None, "y": ("x", 4)}

    order = sorted(forks, key=lambda name: (-module._depth(name, forks), name))

    assert order.index("y") < order.index("x"), order


@pytest.mark.timeout(10)
def test_the_depth_walk_ends_on_a_cyclic_fork_map() -> None:
    """Lance's fork points form a tree; a malformed ``_refs`` that loops must not hang the erasure door."""
    import catalog.services.erasure as module

    assert module._depth("a", {"a": ("b", 1), "b": ("a", 1)}) >= 1
