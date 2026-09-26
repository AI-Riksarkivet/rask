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
import pytest
from lance.dataset import Tags

from catalog.services.erasure import ErasureReport, erase


_SUBJECT = "alice"
_PREDICATE = "pii = 'alice'"
_NOW = timedelta(seconds=0)


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def _erase(uri: str) -> ErasureReport:
    return erase(
        lance.dataset(uri),
        storage_options={},
        protected=None,
        reopen=lambda: lance.dataset(uri),
        table="acme-bronze$subjects",
        predicate=_PREDICATE,
        retention=_NOW,
    )


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
    """``clean`` v2 never held the subject and is tagged; main v2 holds it and ``work`` stands on main v2.

    ``work`` drops the subject at v3 and a clean tag keeps v3, so work keeps standing on main v2's files
    after the erasure rewrites and reclaims every ref.
    """
    uri = str(tmp_path / "provenance")
    lance.write_dataset(_rows("bob"), uri)
    clean = lance.dataset(uri).create_branch("clean", 1)
    lance.write_dataset(_rows("eve"), clean.uri, mode="append")
    lance.dataset(uri).tags.create("trained", ("clean", 2))
    lance.write_dataset(_rows(_SUBJECT, "carol"), uri, mode="append")
    lance.dataset(uri).create_branch("work", 2).delete(_PREDICATE)
    lance.dataset(uri).tags.create("kept", ("work", 3))
    assert not _holds_subject(uri, ("clean", 2)), "the tagged branch version must be clean for this to be the right test"
    assert _holds_subject(uri, (None, 2)), "main's same-numbered version must hold the subject for this to be the right test"
    return uri


def test_a_BRANCH_tag_that_never_held_the_subject_is_not_dropped_on_mains_evidence(tmp_path: Path) -> None:
    uri = _clean_branch_tag_beside_a_pinning_branch(tmp_path)

    report = _erase(uri)

    assert "trained" in lance.dataset(uri).tags.list(), "a tag pinning a version without the subject was destroyed"
    assert any(s.surface == "tag:trained" and s.outcome == "retained" for s in report.surfaces), report.surfaces


def test_pinned_by_names_only_the_refs_that_pin_a_residual(tmp_path: Path) -> None:
    """A retained tag names a version of ITS branch, so ``trained`` on ``clean`` v2 pins nothing on main v2.

    Main v2 survives holding the subject because ``kept`` keeps work v3, which stands on its files.
    ``trained`` names ``clean`` v2 — the same number, a different snapshot — so listing it would send an
    operator with a deadline to delete a clean reproducibility pointer that frees nothing; and work's
    head was rewritten, so naming ``work`` would destroy a working ref for nothing too.
    """
    uri = _clean_branch_tag_beside_a_pinning_branch(tmp_path)

    report = _erase(uri)

    assert report.residual_versions == ["main@2"], report.residual_versions
    assert [(pin.ref, pin.holds) for pin in report.pinned_by] == [("tag:kept", "work@3")]


@pytest.mark.parametrize("names", [("work", "deeper"), ("alpha", "zulu")], ids=["child-sorts-first", "child-sorts-last"])
def test_pinned_by_does_not_name_a_branch_cut_from_ANOTHER_branch(tmp_path: Path, names: tuple[str, str]) -> None:
    """The child stands on the parent's v3, not main v3; main v2 and v3 survive by the retention window alone.

    A fork point is a ``(parent branch, version)`` reference (``parent_branch`` in ``branches.list()``,
    ``parentBranch`` in ``lance_docs/file_format.md`` "Branch Metadata File Format"), so a branch cut from
    another branch pins that branch's history whatever its number. Naming the child would send an
    operator with a deadline to delete a branch that frees nothing on main.
    """
    parent, child = names
    uri = str(tmp_path / "grandchild")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT), uri, mode="append")
    lance.write_dataset(_rows("carol"), uri, mode="append")
    lance.write_dataset(_rows("eve"), lance.dataset(uri).create_branch(parent, 1), mode="append")
    lance.write_dataset(_rows("fred"), lance.dataset(uri).checkout_version((parent, None)), mode="append")
    lance.dataset(uri).create_branch(child, (parent, 3))
    fork = lance.dataset(uri).branches.list()[child]
    assert (fork["parent_branch"], fork["parent_version"]) == (parent, 3), f"the child must stand on the parent's v3 for this to be the right test: {fork}"

    report = erase(
        lance.dataset(uri),
        storage_options={},
        protected=None,
        reopen=lambda: lance.dataset(uri),
        table="acme-bronze$subjects",
        predicate=_PREDICATE,
        retention=timedelta(days=1),
    )

    assert report.residual_versions == ["main@2", "main@3"], "main v2 and v3 must survive holding the subject for this to be the right test"
    assert (report.pinned_by, report.held_by_retention) == ([], ["main@2", "main@3"])


def test_a_tag_whose_delete_FAILED_is_named_as_the_pin_on_its_own_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``kept`` names main v2, the one version holding the subject, and step 2 cannot delete it.

    No branch stands on main v2, so the surviving tag alone keeps it from cleanup, and it is the whole
    of what an operator must delete to finish. Only ``tags.delete("kept")`` fails; the rest is real Lance.
    """
    uri = str(tmp_path / "stuck")
    lance.write_dataset(_rows("bob"), uri)
    lance.write_dataset(_rows(_SUBJECT), uri, mode="append")
    lance.write_dataset(_rows("carol"), uri, mode="append")
    lance.dataset(uri).tags.create("kept", 2)
    assert _holds_subject(uri, (None, 2)), "the tagged version must hold the subject for this to be the right test"
    delete = Tags.delete

    def _refuse_kept(self: Tags, tag: str) -> None:
        if tag == "kept":
            raise OSError(f"object store refused deleting tag {tag!r}")
        delete(self, tag)

    monkeypatch.setattr(Tags, "delete", _refuse_kept)

    report = _erase(uri)

    assert any(s.surface == "tag:kept" and s.outcome == "failed" for s in report.surfaces), report.surfaces
    assert report.residual_versions == ["main@2"], "only the tagged version may survive for this to be the right test"
    assert not report.complete
    assert [(pin.ref, pin.holds) for pin in report.pinned_by] == [("tag:kept", "main@2")], report.pinned_by
