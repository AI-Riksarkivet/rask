"""An erasure's verification reaches every surface the catalog serves ([[LH-073]], [[LH-493]]).

A plain `delete_from_table` reaches ONE of the four surfaces a table serves (main) and leaves the other
three live: a branch, a tag and the version history. The bring-your-own job rewrites every ref (the
conftest's ``ErasureJob``); the catalog's verification judges every head, drops the tags pinning the
subject, reclaims the history and proves what survives.

THESE DRIVE REAL LANCE, not a fake, because the claim is about the format's behaviour — that a branch
pins the parent's history, that a tag pins a version against reclamation — and a fake would restate
the belief under test rather than check it.

THE ORDER IS THE POINT: run the cleanup before the branches are handled and it removes nothing, so an
estate could report an erasure that reclaimed exactly zero bytes and looked successful.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from catalog.services.erasure import ErasureReport, Pin, ref_heads, verify


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


@pytest.fixture
def pinned(tmp_path: Path) -> str:
    """A version the erasure cannot release: ``work`` drops the subject at v2, and a clean tag keeps v2.

    Work v2 still stands on main v1's data file, so main v1 — which holds the subject — stays referenced
    after every ref is rewritten and reclaimed, for as long as the tag keeps work v2.
    """
    uri = str(tmp_path / "pinned")
    dataset = lance.write_dataset(pa.table({"id": pa.array([1, 2, 3, 4]), "pii": pa.array([_SUBJECT, "bob", "carol", "dan"])}), uri)
    dataset.create_branch("work").delete(_PREDICATE)
    lance.dataset(uri).tags.create("trained", ("work", 2))
    return uri


def _pii(handle: Any) -> list[str]:
    return list(handle.to_table().to_pydict()["pii"])


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
    )


@pytest.fixture
def erase(erasure_job: Any) -> _Erase:  # noqa: ANN401 — conftest.ErasureJob
    """The job rewrites every ref, then the catalog verifies."""

    def _erase(uri: str, retention: timedelta = _NOW) -> ErasureReport:
        recorded = ref_heads(lance.dataset(uri))
        erasure_job(uri, _PREDICATE).run()
        return _verify(uri, retention, recorded)

    return _erase


def _follow(uri: str, report: ErasureReport) -> None:
    """Delete every ref ``pinned_by`` names, in the order it names them."""
    dataset = lance.dataset(uri)
    for pin in report.pinned_by:
        kind, name = pin.ref.split(":", 1)
        (dataset.branches if kind == "branch" else dataset.tags).delete(name)


@pytest.mark.parametrize("branch", ["work", "review"])
def test_a_branch_the_job_leaves_is_named_and_the_others_are_clean(table: str, branch: str, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    """EVERY branch is judged, not the ones a job chose to reach: an erasure is about the subject, not a ref."""
    job = erasure_job(table, _PREDICATE)
    for ref in ("work", "review", None):
        if ref != branch:
            job.delete(ref)
            job.compact(ref)

    report = _verify(table)

    owed = sorted(s.surface for s in report.surfaces if s.outcome == "failed" and s.surface.startswith(("main", "branch:", "compact:", "index:")))
    assert (owed, report.settled) == ([f"branch:{branch}"], False), [(s.surface, s.outcome) for s in report.surfaces]


def test_with_no_branch_pinning_it_nothing_surviving_answers(tmp_path: Path, erase: _Erase) -> None:
    """The control, and the proof the capability works: the same erasure on a table with a tag and
    history but no branch reaches every surface, and verify comes back clean."""
    uri = str(tmp_path / "unpinned")
    dataset = lance.write_dataset(pa.table({"id": pa.array([1, 2]), "pii": pa.array([_SUBJECT, "bob"])}), uri)
    dataset.tags.create("pinned", dataset.version)

    report = erase(uri)

    surviving = {v["version"]: _pii(lance.dataset(uri).checkout_version(v["version"])) for v in lance.dataset(uri).versions()}
    assert report.residual_versions == [], surviving
    assert (report.complete, report.settled) == (True, True)


def test_one_failed_surface_does_not_stop_the_others(table: str, monkeypatch: pytest.MonkeyPatch, erase: _Erase) -> None:
    """Stopping at the first error leaves the rest both unjudged AND unreported, so the caller cannot
    say what is left — which, with a deadline attached, is the worst of the three outcomes."""
    broken = type(lance.dataset(table).tags)

    def _boom(self: Any, name: str) -> None:
        raise RuntimeError("tag store unavailable")

    monkeypatch.setattr(broken, "delete", _boom)
    report = erase(table)

    assert (report.complete, report.settled) == (False, False)
    assert [s.outcome for s in report.surfaces if s.surface in ("main", "history:main")] == ["clean", "reclaimed"], "main was skipped because a tag failed"
    assert Pin(ref="tag:pinned", holds="main@1") in report.pinned_by, "the tag whose delete failed still holds main v1"


def test_retention_is_honoured_rather_than_forced_to_zero(table: str, erase: _Erase) -> None:
    """An erasure is not a licence to destroy unrelated history. A requester that means to reclaim
    everything passes zero; one that passes a window keeps it, and the obligation stays open while it does."""
    report = erase(table, retention=timedelta(days=3650))

    assert (report.versions_reclaimed, report.settled) == (0, False)
    assert "main@1" in report.held_by_retention


def test_the_window_is_judged_against_the_cutoff_the_cleanup_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erase: _Erase) -> None:
    """`cleanup_old_versions` measures its cutoff when it is called (measured on pylance 12.0.0), so an
    instant taken before the call can place inside the window a version the cleanup was free to take.
    The cleanup here is slowed until main v1 is older than its cutoff: the tag whose delete failed keeps
    v1, and the report must not say the window does too."""
    uri = str(tmp_path / "window")
    lance.write_dataset(pa.table({"id": pa.array([1, 2]), "pii": pa.array([_SUBJECT, "bob"])}), uri)
    lance.dataset(uri).tags.create("pinned", 1)
    stamp = lance.dataset(uri).versions()[0]["timestamp"]
    assert isinstance(stamp, datetime)
    committed = stamp.astimezone(UTC)
    cleanup = lance.LanceDataset.cleanup_old_versions

    def _slow(self: lance.LanceDataset, *args: Any, **kwargs: Any) -> Any:
        time.sleep(2)
        return cleanup(self, *args, **kwargs)

    def _boom(self: Any, name: str) -> None:
        raise RuntimeError("tag store unavailable")

    monkeypatch.setattr(lance.LanceDataset, "cleanup_old_versions", _slow)
    monkeypatch.setattr(type(lance.dataset(uri).tags), "delete", _boom)
    report = erase(uri, retention=datetime.now(UTC) - committed + timedelta(seconds=1))

    assert report.residual_versions == ["main@1"], "the failed tag delete must keep v1 for this to be the right test"
    assert (report.pinned_by, report.held_by_retention) == ([Pin(ref="tag:pinned", holds="main@1")], [])


def test_the_report_names_the_TAG_that_keeps_the_residual(pinned: str, erase: _Erase) -> None:
    """`residual_versions` says the erasure is incomplete; only this says what to delete to finish it.

    Lance records the fork point in `_refs/branches/<name>.json` (`parentVersion`), so which branch
    stands on main v1 is read rather than guessed — and its head was rewritten, so it is the tag keeping
    work v2 that holds main v1, not the branch. Naming the branch would send an operator holding a legal
    deadline to destroy a working ref that frees nothing. Work v2 is a residual too: the subject's row sits
    in its data file behind the deletion vector work's delete wrote.
    """
    report = erase(pinned)

    assert report.residual_versions == ["main@1", "work@2"]
    assert [(pin.ref, pin.holds) for pin in report.pinned_by] == [("tag:trained", "work@2")]
    assert report.settled is True, "every head is clean and only the tag keeps the residual, so its scheduled release finishes the erasure"
    _follow(pinned, report)
    assert erase(pinned).complete is True
    assert "work" in lance.dataset(pinned).branches.list()


def test_a_tag_the_erasure_already_removed_is_not_named(pinned: str, erase: _Erase) -> None:
    """Tags are read AFTER step 2: naming one step 2 dropped would make following `pinned_by` fail."""
    lance.dataset(pinned).tags.create("snapshot", 1)

    report = erase(pinned)

    assert "snapshot" not in lance.dataset(pinned).tags.list(), "step 2 must drop the tag for this to be the right test"
    assert [pin.ref for pin in report.pinned_by] == ["tag:trained"]


def test_a_tag_that_never_held_the_subject_is_RETAINED(tmp_path: Path, erase: _Erase) -> None:
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

    report = erase(uri)

    tags = lance.dataset(uri).tags.list()
    assert "trained-on-this" in tags, "a tag pinning a version without the subject was destroyed"
    assert "holds-the-subject" not in tags, "a tag pinning the subject survived and keeps the version alive"
    assert any(s.surface == "tag:trained-on-this" and s.outcome == "retained" for s in report.surfaces)


def test_an_unreadable_tag_is_dropped_rather_than_assumed_clean(table: str, monkeypatch: pytest.MonkeyPatch, erase: _Erase) -> None:
    """Unreadable is not evidence of absence. A probe that fails must resolve AGAINST the tag, or an
    erasure quietly keeps alive exactly the versions it could not inspect."""
    import catalog.services.erasure as module

    monkeypatch.setattr(module, "_answers", lambda *_args, **_kwargs: None)
    report = erase(table)

    assert any(s.surface == "tag:pinned" and s.outcome == "untagged" for s in report.surfaces)


def test_a_RETAINED_tag_does_not_break_reclamation(tmp_path: Path, erase: _Erase) -> None:
    """FOUND BY DRIVING THE DEPLOYED DOOR, not by a unit test.

    Keeping a tag over a clean version (the step above) made `cleanup_old_versions` refuse the ENTIRE
    call — pylance errors by default when any tagged version falls in range. So doing the right thing
    in step 2 cost the estate every byte of reclamation in step 3, and the erasure answered
    `history: failed` as a direct consequence of preserving model provenance. Observed live before the
    fix: `Cleanup error: 1 tagged version(s) have been marked for cleanup`.
    """
    uri = str(tmp_path / "mixed")
    dataset = lance.write_dataset(pa.table({"id": pa.array([2]), "pii": pa.array(["bob"])}), uri)
    dataset.tags.create("trained-on-v1", dataset.version)  # clean: the subject is not here yet
    lance.write_dataset(pa.table({"id": pa.array([1]), "pii": pa.array([_SUBJECT])}), uri, mode="append")

    report = erase(uri)

    assert [s.outcome for s in report.surfaces if s.surface == "history:main"] == ["reclaimed"]
    assert "trained-on-v1" in lance.dataset(uri).tags.list(), "the clean tag had to survive for this to be the right test"
    assert report.complete is True, [(s.surface, s.outcome, s.detail) for s in report.surfaces]


def test_erasure_reclaims_a_subjects_BLOB_SIDECAR_with_its_version(tmp_path: Path, erase: _Erase) -> None:
    # A row delete leaves the payload bytes in `data/<stem>/*.blob`. Measured on pylance 11.0.0:
    # `cleanup_old_versions` takes the sidecar with its data file, so step 3 reclaims them once the job's
    # rewrite has left them unreferenced — reclaiming only the .lance would leave the payload on storage.
    from lance import blob_array

    uri = str(tmp_path / "blobs")
    big = b"A" * (5 * 1024 * 1024)
    lance.write_dataset(pa.table({"id": pa.array([1, 2]), "payload": blob_array([big, b"B" * (5 * 1024 * 1024)])}), uri, data_storage_version="2.2")
    assert list(Path(uri).rglob("*.blob")), "the fixture produced no sidecar — raise the payload past the dedicated threshold"

    report = erase(uri)

    assert report.complete is True, [(s.surface, s.outcome, s.detail) for s in report.surfaces]
    assert report.bytes_reclaimed > 1024 * 1024, f"only {report.bytes_reclaimed} bytes freed — the sidecar was left behind"
