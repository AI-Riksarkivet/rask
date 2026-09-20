"""An erasure request removes the subject from every ref the catalog serves ([[LH-073]]).

The counterpart to `tests/unit/test_an_erased_row_survives_in_three_places.py`, which characterises
the defect: today a `delete_from_table` reaches ONE of four surfaces and the other three stay live.

THESE DRIVE REAL LANCE, not a fake, because the claim is about the format's behaviour — that a branch
pins the parent's history, that a tag pins a version against reclamation — and a fake would restate
the belief under test rather than check it.

THE ORDER IS THE POINT. `test_reclamation_is_a_NO_OP_when_a_branch_still_pins_the_history` is the one
that justifies the whole shape: run the cleanup before the branches are handled and it removes
nothing, so an estate could report an erasure that reclaimed exactly zero bytes and looked successful.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from catalog.services.erasure import erase


_SUBJECT = "alice"
_PREDICATE = "id = 1"
_NOW = timedelta(seconds=0)


@pytest.fixture
def table(tmp_path: Path) -> str:
    """A table with two branches, a tag and history — every surface the catalog serves at once."""
    uri = str(tmp_path / "subjects")
    dataset = lance.write_dataset(pa.table({"id": pa.array([1, 2, 3, 4]), "pii": pa.array([_SUBJECT, "bob", "carol", "dan"])}), uri)
    dataset.create_branch("work")
    dataset.create_branch("review")
    dataset.tags.create("pinned", dataset.version)
    return uri


def _pii(handle: Any) -> list[str]:
    return list(handle.to_table().to_pydict()["pii"])


def _erase(uri: str, retention: timedelta = _NOW) -> Any:
    return erase(lance.dataset(uri), table="acme-bronze$subjects", predicate=_PREDICATE, retention=retention)


def test_the_subject_is_gone_from_main(table: str) -> None:
    _erase(table)

    assert _SUBJECT not in _pii(lance.dataset(table))


@pytest.mark.parametrize("branch", ["work", "review"])
def test_the_subject_is_gone_from_every_branch(table: str, branch: str) -> None:
    """EVERY branch, not the one the request named — an erasure is about the subject, not a ref."""
    _erase(table)

    assert _SUBJECT not in _pii(lance.dataset(table).checkout_version((branch, None)))


def test_the_pinning_tag_is_removed(table: str) -> None:
    """A tag holds a VERSION, so there is nothing to delete from it — the row becomes unreachable only
    when the tag stops pinning the version that still contains it."""
    _erase(table)

    assert "pinned" not in lance.dataset(table).tags.list()


def test_a_version_a_BRANCH_PINS_still_answers_and_the_report_says_so(table: str) -> None:
    """THE LIMIT, found by building this and worth more than the capability.

    Deleting rows ON a branch does not remove that branch's pin on the parent's history: the branch
    reads clean, and the parent's pre-delete version survives cleanup still holding the subject. So
    every step can report success and the row stays readable — the one outcome an erasure must never
    call done. The verify step catches it, and `complete` is False.

    Making it True means DELETING the branch, which destroys someone's working ref. That is an owner
    decision, not a side effect of an erasure call ([[LH-178]]).
    """
    report = _erase(table)

    assert report.residual_versions, "a branch pinned a version holding the subject and verify missed it"
    assert report.complete is False
    assert any(s.surface == "verify" and s.outcome == "failed" for s in report.surfaces)


def test_with_no_branch_pinning_it_nothing_surviving_answers(tmp_path: Path) -> None:
    """The control, and the proof the capability works: the same erasure on a table with a tag and
    history but no branch reaches every surface, and verify comes back clean."""
    uri = str(tmp_path / "unpinned")
    dataset = lance.write_dataset(pa.table({"id": pa.array([1, 2]), "pii": pa.array([_SUBJECT, "bob"])}), uri)
    dataset.tags.create("pinned", dataset.version)

    report = _erase(uri)

    surviving = {v["version"]: _pii(lance.dataset(uri).checkout_version(v["version"])) for v in lance.dataset(uri).versions()}
    assert report.residual_versions == [], surviving
    assert report.complete is True


def test_reclamation_is_a_NO_OP_when_a_branch_still_pins_the_history(tmp_path: Path) -> None:
    """WHY THE ORDER IS FORCED, measured rather than asserted.

    A branch pins the parent's history at the branch point. Reclaim first and nothing goes — so an
    estate could run an erasure, report it complete, and have freed zero bytes with the subject still
    readable at the pre-delete version.
    """
    uri = str(tmp_path / "pinned")
    dataset = lance.write_dataset(pa.table({"id": pa.array([1, 2]), "pii": pa.array([_SUBJECT, "bob"])}), uri)
    dataset.create_branch("work")
    dataset.delete(_PREDICATE)  # main only — the door's behaviour today

    with_branch = lance.dataset(uri).cleanup_old_versions(_NOW, delete_unverified=True)
    still_there = any(_SUBJECT in _pii(lance.dataset(uri).checkout_version(v["version"])) for v in lance.dataset(uri).versions())

    assert with_branch.old_versions == 0, "the branch stopped pinning the history without being touched"
    assert still_there, "the subject was reclaimed while a branch pinned the version holding it"


def test_the_report_names_every_surface_it_touched(table: str) -> None:
    """An erasure that half-succeeded has to say which half: the remainder is a legal obligation, not a
    retry the caller can shrug at."""
    report = _erase(table)

    assert {s.surface for s in report.surfaces} == {"branch:work", "branch:review", "tag:pinned", "main", "compact", "history", "verify"}


def test_complete_is_EVIDENCE_not_the_absence_of_an_error(table: str) -> None:
    """Every step on this table reports success and the erasure is still incomplete, because a branch
    pins a version holding the subject. A caller reading "no exception" would have told the data
    subject the wrong thing."""
    report = _erase(table)

    assert all(s.outcome != "failed" for s in report.surfaces if s.surface != "verify")
    assert report.complete is False


def test_one_failed_surface_does_not_stop_the_others(table: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping at the first error leaves the rest both un-erased AND unreported, so the caller cannot
    say what is left — which, with a deadline attached, is the worst of the three outcomes."""
    dataset = lance.dataset(table)
    broken = type(dataset.tags)

    def _boom(self: Any, name: str) -> None:
        raise RuntimeError("tag store unavailable")

    monkeypatch.setattr(broken, "delete", _boom)
    report = erase(dataset, table="t", predicate=_PREDICATE, retention=_NOW)

    assert report.complete is False
    assert [s.outcome for s in report.surfaces if s.surface == "main"] == ["deleted"], "main was skipped because a tag failed"
    assert _SUBJECT not in _pii(lance.dataset(table))


def test_retention_is_honoured_rather_than_forced_to_zero(table: str) -> None:
    """An erasure is not a licence to destroy unrelated history. A caller that means to reclaim
    everything passes zero; one that passes a window keeps it."""
    report = _erase(table, retention=timedelta(days=3650))

    assert report.versions_reclaimed == 0
    assert _SUBJECT not in _pii(lance.dataset(table)), "the rows must still be deleted even when nothing is reclaimed"


def test_the_report_names_the_BRANCH_that_pins_the_residual(table: str) -> None:
    """`residual_versions` says the erasure is incomplete; only this says what to delete to finish it.

    Lance records the fork point in `_refs/branches/<name>.json` (`parentVersion`), so the pin is read
    rather than guessed. Without it an operator holding a legal deadline knows a branch is responsible
    and has to go find which one — on a table that may carry dozens.
    """
    report = _erase(table)

    assert set(report.pinned_by) == {"branch:work", "branch:review"}
    assert all(v in report.residual_versions for v in report.pinned_by.values())


def test_a_clean_erasure_names_no_pin(tmp_path: Path) -> None:
    """The control: `pinned_by` is evidence about a FAILURE, so a successful erasure must leave it empty
    rather than listing every branch that merely exists."""
    uri = str(tmp_path / "unpinned")
    lance.write_dataset(pa.table({"id": pa.array([1, 2]), "pii": pa.array([_SUBJECT, "bob"])}), uri)

    assert _erase(uri).pinned_by == {}


def test_a_tag_that_never_held_the_subject_is_RETAINED(tmp_path: Path) -> None:
    """A TAG IS A REPRODUCIBILITY POINTER, and erasing one is not part of erasing a person.

    A tagged version is exempt from cleanup by design, which is how a training run records the exact
    data it saw. Dropping every tag would destroy that record for versions the subject never appeared
    in — model provenance lost as a side effect of a request about one person, and unrecoverable.
    """
    uri = str(tmp_path / "tagged")
    dataset = lance.write_dataset(pa.table({"id": pa.array([1, 2]), "pii": pa.array([_SUBJECT, "bob"])}), uri)
    dataset.tags.create("holds-the-subject", dataset.version)
    dataset.delete(_PREDICATE)
    clean = lance.dataset(uri)
    clean.tags.create("trained-on-this", clean.version)

    report = _erase(uri)

    tags = lance.dataset(uri).tags.list()
    assert "trained-on-this" in tags, "a tag pinning a version without the subject was destroyed"
    assert "holds-the-subject" not in tags, "a tag pinning the subject survived and keeps the version alive"
    assert any(s.surface == "tag:trained-on-this" and s.outcome == "retained" for s in report.surfaces)


def test_an_unreadable_tag_is_dropped_rather_than_assumed_clean(table: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreadable is not evidence of absence. A probe that fails must resolve AGAINST the tag, or an
    erasure quietly keeps alive exactly the versions it could not inspect."""
    import catalog.services.erasure as module

    monkeypatch.setattr(module, "_answers", lambda *_args, **_kwargs: None)
    report = _erase(table)

    assert any(s.surface == "tag:pinned" and s.outcome == "untagged" for s in report.surfaces)


def test_a_RETAINED_tag_does_not_break_reclamation(tmp_path: Path) -> None:
    """FOUND BY DRIVING THE DEPLOYED DOOR, not by a unit test.

    Keeping a tag over a clean version (the step above) made `cleanup_old_versions` refuse the ENTIRE
    call — pylance errors by default when any tagged version falls in range. So doing the right thing
    in step 2 cost the estate every byte of reclamation in step 4, and the erasure answered
    `history: failed` as a direct consequence of preserving model provenance. Observed live before the
    fix: `Cleanup error: 1 tagged version(s) have been marked for cleanup`.
    """
    uri = str(tmp_path / "mixed")
    dataset = lance.write_dataset(pa.table({"id": pa.array([2]), "pii": pa.array(["bob"])}), uri)
    dataset.tags.create("trained-on-v1", dataset.version)  # clean: the subject is not here yet
    lance.write_dataset(pa.table({"id": pa.array([1]), "pii": pa.array([_SUBJECT])}), uri, mode="append")

    report = _erase(uri)

    assert [s.outcome for s in report.surfaces if s.surface == "history"] == ["reclaimed"]
    assert "trained-on-v1" in lance.dataset(uri).tags.list(), "the clean tag had to survive for this to be the right test"
    assert report.complete is True, [(s.surface, s.outcome, s.detail) for s in report.surfaces]


def test_erasure_reclaims_a_subjects_BLOB_SIDECAR_with_its_version(tmp_path: Path) -> None:
    # A row delete leaves the payload bytes in `data/<stem>/*.blob`. Measured on pylance 11.0.0:
    # `cleanup_old_versions` takes the sidecar with its data file, so step 4 already reclaims them —
    # an erasure that reclaimed only the .lance would leave the subject's payload on storage.
    from lance import blob_array

    uri = str(tmp_path / "blobs")
    big = b"A" * (5 * 1024 * 1024)
    lance.write_dataset(pa.table({"id": pa.array([1, 2]), "payload": blob_array([big, b"B" * (5 * 1024 * 1024)])}), uri, data_storage_version="2.2")
    assert list(Path(uri).rglob("*.blob")), "the fixture produced no sidecar — raise the payload past the dedicated threshold"

    report = _erase(uri)

    assert report.complete is True, [(s.surface, s.outcome, s.detail) for s in report.surfaces]
    assert report.bytes_reclaimed > 1024 * 1024, f"only {report.bytes_reclaimed} bytes freed — the sidecar was left behind"


def test_erase_compaction_leaves_a_branch_readable(table: str) -> None:
    # Compaction rewrites main's fragments, and a branch resolves inherited fragments through the
    # parent — so a rewrite that stranded them would make the branch unreadable.
    _erase(table)

    assert _pii(lance.dataset(table).checkout_version(("work", None))) == ["bob", "carol", "dan"]
