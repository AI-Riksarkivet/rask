"""An erasure decides a tag's fate by the version the tag ACTUALLY pins — on the branch it names.

Tags are stored once at the dataset root and ``tags.list()`` answers every branch's from any handle,
each carrying the ``branch`` it names (``lance_docs/file_format.md`` "Tag Storage" / "Tag File Format";
``null`` is main). Branch versions are numbered in the branch's own history, so ``work`` v3 and main v3
are different snapshots. Reading a tag as ``checkout_version(<bare int>)`` resolves the number against
the HANDLE's branch (``lance_docs/guide.md`` Tags: "An integer: version number in the current branch"),
and the erasure runs on a main handle — so probing a branch tag by its bare number judges it by main's
same-numbered version.

That misreads in both directions, and each is its own harm:

* a branch tag pinning the subject is RETAINED because main's version is clean, and the subject stays
  one ``checkout_version("<tag>")`` away after an erasure that reported the tag as clean;
* a branch tag that never held the subject is UNTAGGED because main's version holds it, destroying a
  reproducibility pointer the module keeps on purpose.

Driven on real Lance for the reason ``test_erasure_reaches_every_surface.py`` gives: the claim is about
how the format numbers refs, and a fake would restate the belief under test.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import lance
import pyarrow as pa

from catalog.services.erasure import ErasureReport, erase


_SUBJECT = "alice"
_PREDICATE = "pii = 'alice'"
_NOW = timedelta(seconds=0)


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def _erase(uri: str) -> ErasureReport:
    return erase(lance.dataset(uri), table="acme-bronze$subjects", predicate=_PREDICATE, retention=_NOW)


def _holds_subject(uri: str, reference: str | tuple[str | None, int]) -> bool:
    return bool(lance.dataset(uri).checkout_version(reference).count_rows(filter=_PREDICATE))


def test_a_BRANCH_tag_pinning_the_subject_is_not_kept_on_mains_evidence(tmp_path: Path) -> None:
    """``work`` v3 holds the subject and main v3 does not; the tag names ``work`` v3."""
    uri = str(tmp_path / "subjects")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows("carol"), uri, mode="append")
    branch = lance.dataset(uri).create_branch("work", 2)
    lance.write_dataset(_rows(_SUBJECT), branch.uri, mode="append")
    lance.dataset(uri).tags.create("pinned", ("work", 3))
    lance.write_dataset(_rows("dan"), uri, mode="append")
    assert _holds_subject(uri, ("work", 3)), "the tagged branch version must hold the subject for this to be the right test"
    assert not _holds_subject(uri, (None, 3)), "main's same-numbered version must be clean for this to be the right test"

    report = _erase(uri)

    survivors = lance.dataset(uri).tags.list()
    assert "pinned" not in survivors, f"the tag still pins a version holding the subject: {survivors.get('pinned')}"
    assert any(s.surface == "tag:pinned" and s.outcome == "untagged" for s in report.surfaces), report.surfaces


def _clean_branch_tag_beside_a_pinning_branch(tmp_path: Path) -> str:
    """``clean`` v2 never held the subject and is tagged; main v2 holds it and ``work`` stands on main v2."""
    uri = str(tmp_path / "provenance")
    lance.write_dataset(_rows("bob"), uri)
    clean = lance.dataset(uri).create_branch("clean", 1)
    lance.write_dataset(_rows("eve"), clean.uri, mode="append")
    lance.dataset(uri).tags.create("trained", ("clean", 2))
    lance.write_dataset(_rows(_SUBJECT), uri, mode="append")
    lance.dataset(uri).create_branch("work", 2)
    assert not _holds_subject(uri, ("clean", 2)), "the tagged branch version must be clean for this to be the right test"
    assert _holds_subject(uri, (None, 2)), "main's same-numbered version must hold the subject for this to be the right test"
    return uri


def test_a_BRANCH_tag_that_never_held_the_subject_is_not_dropped_on_mains_evidence(tmp_path: Path) -> None:
    uri = _clean_branch_tag_beside_a_pinning_branch(tmp_path)

    report = _erase(uri)

    assert "trained" in lance.dataset(uri).tags.list(), "a tag pinning a version without the subject was destroyed"
    assert any(s.surface == "tag:trained" and s.outcome == "retained" for s in report.surfaces), report.surfaces


def test_pinned_by_names_only_the_refs_that_pin_a_residual_MAIN_version(tmp_path: Path) -> None:
    """``residual_versions`` are main's; a retained tag on another branch pins that branch's history, not them.

    Main v2 survives holding the subject because ``work`` stands on it. ``trained`` names ``clean`` v2 —
    the same number, a different snapshot — so listing it would send an operator with a deadline to
    delete a clean reproducibility pointer that frees nothing.
    """
    uri = _clean_branch_tag_beside_a_pinning_branch(tmp_path)

    report = _erase(uri)

    assert report.residual_versions == [2], report.residual_versions
    assert report.pinned_by == {"branch:work": 2}


def test_pinned_by_does_not_name_a_branch_cut_from_ANOTHER_branch(tmp_path: Path) -> None:
    """``deeper`` stands on ``work`` v3, not main v3; main v2 and v3 survive by the retention window alone.

    A fork point is a ``(parent branch, version)`` reference (``parent_branch`` in ``branches.list()``,
    ``parentBranch`` in ``lance_docs/file_format.md`` "Branch Metadata File Format"), so a branch cut from
    another branch pins that branch's history whatever its number. Naming ``deeper`` would send an
    operator with a deadline to delete a branch that frees nothing on main.
    """
    uri = str(tmp_path / "grandchild")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT), uri, mode="append")
    lance.write_dataset(_rows("carol"), uri, mode="append")
    work = lance.dataset(uri).create_branch("work", 1)
    lance.write_dataset(_rows("eve"), work, mode="append")
    lance.write_dataset(_rows("fred"), lance.dataset(uri).checkout_version(("work", None)), mode="append")
    lance.dataset(uri).create_branch("deeper", ("work", 3))
    fork = lance.dataset(uri).branches.list()["deeper"]
    assert (fork["parent_branch"], fork["parent_version"]) == ("work", 3), f"deeper must stand on work v3 for this to be the right test: {fork}"

    report = erase(lance.dataset(uri), table="acme-bronze$subjects", predicate=_PREDICATE, retention=timedelta(days=1))

    assert report.residual_versions == [2, 3], "main v2 and v3 must survive holding the subject for this to be the right test"
    assert report.pinned_by == {}
