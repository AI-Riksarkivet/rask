"""An erasure rewrites, reclaims and verifies EVERY ref's history, and names what is left per ref ([[LH-263]]).

A branch keeps its own history under ``tree/<name>/`` (``lance_docs/file_format.md`` "Branch Dataset
Layout"), numbered in its own line (``lance_docs/guide.md`` Branches: "version numbers may overlap across
branches"). So a subject written ON a branch lives in versions that main's cleanup never lists and main's
verification never probes: after the erasure, ``checkout_version(("work", 3))`` still returns it.

The oracle here, ``_surviving``, reads every ref's every version itself rather than trusting the report,
because the claim under test is that the report and the table agree.

``pinned_by`` is only actionable if following it succeeds. Measured on pylance 12.0.0, Lance refuses to
delete a branch while another branch is cut from it ("Branch work is referenced by [("deeper", 3)]
versions, can not delete") or while a tag names it ("Branch work is referenced by tags [("t", 2)]"), so
the report must name those first.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from catalog.services.erasure import ErasureReport, erase


_SUBJECT = "alice"
_PREDICATE = "pii = 'alice'"
_NOW = timedelta(seconds=0)


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def _erase(uri: str, retention: timedelta = _NOW) -> ErasureReport:
    return erase(lance.dataset(uri), table="acme-bronze$subjects", predicate=_PREDICATE, retention=retention)


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


def test_no_version_of_any_ref_answers_after_the_erasure(tmp_path: Path) -> None:
    uri = _subject_on_a_branch(tmp_path)

    report = _erase(uri)

    assert _surviving(uri) == set(), [(s.surface, s.outcome) for s in report.surfaces]
    assert report.complete is True


@pytest.mark.parametrize("retention", [_NOW, timedelta(days=1)], ids=["reclaimed", "inside-the-window"])
def test_the_report_names_exactly_what_survives_on_every_ref(tmp_path: Path, retention: timedelta) -> None:
    """Inside the retention window work v3 and v4 legitimately survive; the report must say so, per ref."""
    uri = _subject_on_a_branch(tmp_path)

    report = _erase(uri, retention)

    surviving = _surviving(uri)
    assert set(report.residual_versions) == surviving
    assert report.complete is (not surviving)


def test_the_subjects_bytes_leave_the_branchs_own_data_files(tmp_path: Path) -> None:
    """A delete leaves the subject's bytes in the branch's live data file behind a deletion vector.

    Measured on pylance 12.0.0: after the delete and the branch's own cleanup the file under
    ``tree/work/data`` still holds them; compacting through the branch handle first rewrites it, and the
    same cleanup then removes the original.
    """
    uri = str(tmp_path / "bytes")
    lance.write_dataset(_rows("bob"), uri)
    work = lance.dataset(uri).create_branch("work", 1)
    lance.write_dataset(_rows(_SUBJECT, "eve"), work, mode="append")
    branch_data = Path(uri) / "tree" / "work" / "data"

    def holding() -> list[str]:
        return [p.name for p in branch_data.rglob("*.lance") if _SUBJECT.encode() in p.read_bytes()]

    assert holding(), "the subject's bytes must sit in a branch-owned data file for this to be the right test"

    _erase(uri)

    assert holding() == []


def _chain(tmp_path: Path) -> str:
    """main v2 holds the subject; ``work`` cut from main v2 gains eve at v3; ``deeper`` is cut from (work, 3)."""
    uri = str(tmp_path / "chain")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT, "carol"), uri, mode="append")
    work = lance.dataset(uri).create_branch("work", 2)
    lance.write_dataset(_rows("eve"), work, mode="append")
    lance.dataset(uri).create_branch("deeper", ("work", 3))
    return uri


def test_a_chain_every_ref_rewrites_is_erased_without_deleting_a_branch(tmp_path: Path) -> None:
    """A fork pin lasts while a retained branch version references the fork version's files.

    Every fragment here is small enough to be rewritten, so once deeper, then work, have been compacted
    and reclaimed, nothing references main v2 or work v3 and main's cleanup takes them. Reclaiming main
    first keeps both, holding the subject (measured on pylance 12.0.0).
    """
    uri = _chain(tmp_path)

    report = _erase(uri)

    assert _surviving(uri) == set(), report.residual_versions
    assert report.complete is True
    assert set(lance.dataset(uri).branches.list()) == {"work", "deeper"}


def _pinned_chain(tmp_path: Path) -> str:
    """The chain, plus a clean tag on deeper v4 — a retained version that still stands on work v3 and main v2."""
    uri = _chain(tmp_path)
    lance.dataset(uri).checkout_version(("deeper", None)).delete(_PREDICATE)
    lance.dataset(uri).tags.create("trained", ("deeper", 4))
    assert not lance.dataset(uri).checkout_version("trained").count_rows(filter=_PREDICATE), "the tag must be clean for this to be the right test"
    return uri


def _tagged_branch(tmp_path: Path) -> str:
    """main v2 holds the subject; ``work`` cut from it drops the subject at v3, which a clean tag names."""
    uri = str(tmp_path / "tagged")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT, "carol"), uri, mode="append")
    lance.dataset(uri).create_branch("work", 2).delete(_PREDICATE)
    lance.dataset(uri).tags.create("trained", ("work", 3))
    assert not lance.dataset(uri).checkout_version("trained").count_rows(filter=_PREDICATE), "the tag must be clean for this to be the right test"
    return uri


def test_pinned_by_names_descendants_and_their_tags_before_the_branch(tmp_path: Path) -> None:
    """``trained`` keeps deeper v4, which keeps work v3 and main v2 holding the subject.

    Deleting ``work`` needs ``deeper`` gone, and deleting ``deeper`` needs ``trained`` gone — a clean
    reproducibility pointer the erasure itself keeps, named here because nothing else finishes the job.
    """
    uri = _pinned_chain(tmp_path)

    report = _erase(uri)

    assert "trained" in lance.dataset(uri).tags.list(), "the clean tag must survive the erasure itself"
    assert report.residual_versions == ["main@2", "work@3"]
    assert report.model_dump()["pinned_by"] == [
        {"ref": "tag:trained", "holds": "deeper@4"},
        {"ref": "branch:deeper", "holds": "work@3"},
        {"ref": "branch:work", "holds": "main@2"},
    ]


def test_pinned_by_names_a_tag_on_a_branch_that_must_go(tmp_path: Path) -> None:
    uri = _tagged_branch(tmp_path)

    report = _erase(uri)

    assert report.residual_versions == ["main@2"]
    assert report.model_dump()["pinned_by"] == [{"ref": "tag:trained", "holds": "work@3"}, {"ref": "branch:work", "holds": "main@2"}]


def _clean_descendant(tmp_path: Path) -> str:
    """``_tagged_branch`` plus ``deeper`` cut from the clean work v3: it pins nothing, and still blocks ``work``."""
    uri = _tagged_branch(tmp_path)
    lance.dataset(uri).create_branch("deeper", ("work", 3))
    return uri


def test_pinned_by_names_a_descendant_that_pins_nothing_itself(tmp_path: Path) -> None:
    uri = _clean_descendant(tmp_path)

    report = _erase(uri)

    assert report.residual_versions == ["main@2"]
    assert report.model_dump()["pinned_by"] == [
        {"ref": "tag:trained", "holds": "work@3"},
        {"ref": "branch:deeper", "holds": "work@3"},
        {"ref": "branch:work", "holds": "main@2"},
    ]


@pytest.mark.parametrize("build", [_pinned_chain, _tagged_branch, _clean_descendant], ids=["pinning-descendant", "tag", "clean-descendant"])
def test_following_pinned_by_in_order_lets_the_erasure_finish(tmp_path: Path, build: Callable[[Path], str]) -> None:
    uri = build(tmp_path)

    _follow(uri, _erase(uri))
    report = _erase(uri)

    assert report.complete is True, [(s.surface, s.outcome, s.detail) for s in report.surfaces]
    assert _surviving(uri) == set()


def test_a_branch_whose_fork_point_cannot_be_read_is_still_erased(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uri = _subject_on_a_branch(tmp_path)
    monkeypatch.setattr(type(lance.dataset(uri).branches), "list", lambda self: {"work": {}})

    report = _erase(uri)

    assert _surviving(uri) == set(), [(s.surface, s.outcome, s.detail) for s in report.surfaces]
    assert report.complete is True


def test_an_unlistable_branch_set_is_not_verified_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification covers the refs it can list, so a table whose branches cannot be listed is not proved clean."""
    uri = str(tmp_path / "unlisted")
    dataset = lance.write_dataset(_rows(_SUBJECT, "bob"), uri)

    def _boom(self: Any) -> None:
        raise RuntimeError("branch store unavailable")

    monkeypatch.setattr(type(dataset.branches), "list", _boom)
    report = erase(dataset, table="t", predicate=_PREDICATE, retention=_NOW)

    assert [s.outcome for s in report.surfaces if s.surface == "branches"] == ["failed"]
    assert report.complete is False
