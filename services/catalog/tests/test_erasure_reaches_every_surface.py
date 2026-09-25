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
from typing import Any, cast

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


@pytest.fixture
def pinned(tmp_path: Path) -> str:
    """A branch the erasure cannot release: ``work`` drops the subject at v2, and a clean tag keeps v2.

    Work v2 still stands on main v1's data file, so main v1 — which holds the subject — stays referenced
    after every ref is rewritten and reclaimed.
    """
    uri = str(tmp_path / "pinned")
    dataset = lance.write_dataset(pa.table({"id": pa.array([1, 2, 3, 4]), "pii": pa.array([_SUBJECT, "bob", "carol", "dan"])}), uri)
    dataset.create_branch("work").delete(_PREDICATE)
    lance.dataset(uri).tags.create("trained", ("work", 2))
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


def test_a_version_a_BRANCH_PINS_still_answers_and_the_report_says_so(pinned: str) -> None:
    """THE LIMIT, found by building this and worth more than the capability.

    A branch pins the version it was cut from for as long as a retained branch version references that
    version's files. Here a tag the erasure rightly keeps holds such a version, so the parent's
    pre-delete version survives cleanup still holding the subject. Every step can report success and the
    row stays readable — the one outcome an erasure must never call done. The verify step catches it,
    and `complete` is False.

    Making it True means DELETING the branch, which destroys someone's working ref. That is an owner
    decision, not a side effect of an erasure call ([[LH-178]]).
    """
    report = _erase(pinned)

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

    assert {s.surface for s in report.surfaces} == {
        "branch:work",
        "branch:review",
        "tag:pinned",
        "main",
        *(f"{step}:{ref}" for step in ("compact", "history") for ref in ("main", "work", "review")),
        "verify",
    }


def test_complete_is_EVIDENCE_not_the_absence_of_an_error(pinned: str) -> None:
    """Every step on this table reports success and the erasure is still incomplete, because a branch
    pins a version holding the subject. A caller reading "no exception" would have told the data
    subject the wrong thing."""
    report = _erase(pinned)

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


def test_the_report_names_the_BRANCH_that_pins_the_residual(pinned: str) -> None:
    """`residual_versions` says the erasure is incomplete; only this says what to delete to finish it.

    Lance records the fork point in `_refs/branches/<name>.json` (`parentVersion`), so the pin is read
    rather than guessed. Without it an operator holding a legal deadline knows a branch is responsible
    and has to go find which one — on a table that may carry dozens. The tag comes first because Lance
    will not delete a branch a tag names.
    """
    report = _erase(pinned)

    assert report.residual_versions == ["main@1"]
    assert [(pin.ref, pin.holds) for pin in report.pinned_by] == [("tag:trained", "work@2"), ("branch:work", "main@1")]


def test_a_clean_erasure_names_no_pin(tmp_path: Path) -> None:
    """The control: `pinned_by` is evidence about a FAILURE, so a successful erasure must leave it empty
    rather than listing every branch that merely exists."""
    uri = str(tmp_path / "unpinned")
    lance.write_dataset(pa.table({"id": pa.array([1, 2]), "pii": pa.array([_SUBJECT, "bob"])}), uri)

    assert _erase(uri).pinned_by == []


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

    assert [s.outcome for s in report.surfaces if s.surface == "history:main"] == ["reclaimed"]
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


class _Optimize:
    """Records the bound the erasure hands pylance, then does the real compaction."""

    def __init__(self, inner: Any, log: list[tuple[str, Any]]) -> None:
        self._inner, self._log = inner, log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def compact_files(self, **kwargs: Any) -> Any:
        self._log.append(("compact_files", dict(kwargs)))
        return self._inner.compact_files(**kwargs)


class _Probe:
    """A REAL dataset that also records HOW it was asked.

    Wrapping rather than faking, for the reason this whole file gives: the claims here are about
    Lance's behaviour, and a stand-in that answers from a dict would restate the belief under test.
    `checkout_version` re-wraps so the per-version residual check is visible too — a probe that
    stopped at the first handle would see none of the calls this exists to count.
    """

    def __init__(self, inner: Any, log: list[tuple[str, Any]]) -> None:
        self._inner, self._log = inner, log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def optimize(self) -> _Optimize:
        return _Optimize(self._inner.optimize, self._log)

    def checkout_version(self, version: Any) -> _Probe:
        return _Probe(self._inner.checkout_version(version), self._log)

    def count_rows(self, *args: Any, **kwargs: Any) -> Any:
        self._log.append(("count_rows", kwargs.get("filter", args[0] if args else None)))
        return self._inner.count_rows(*args, **kwargs)

    def to_table(self, *args: Any, **kwargs: Any) -> Any:
        self._log.append(("to_table", kwargs.get("filter")))
        return self._inner.to_table(*args, **kwargs)


def _erase_watching(uri: str) -> list[tuple[str, Any]]:
    log: list[tuple[str, Any]] = []
    # CAST, not a suppression: `_Probe.__getattr__` forwards every member of the protocol to a real
    # dataset, which no static check can see. Declaring each forwarded member instead would be a
    # second copy of `_Dataset` that drifts from it silently — the opposite of what the probe is for.
    erase(cast("Any", _Probe(lance.dataset(uri), log)), table="acme-bronze$subjects", predicate=_PREDICATE, retention=_NOW)
    return log


def test_the_erasure_COMPACTION_carries_the_bound_the_compact_door_already_applies(table: str) -> None:
    """`compact_now` in `services/maintenance.py` pins `batch_size=64, num_threads=2` and says why:
    "rows are not a unit of memory, and the default batch size on a blob tier read ~15 GB/thread — the
    OOM measured on the maintenance pod is just as available to the catalog pod through this button".

    Erasure is a second such button on the same pod, and it reached pylance unbounded. An erasure runs
    over exactly the tables most likely to carry a blob column, so the unbounded default is not the
    cheaper path here — it is the same hazard through a door nobody had counted.
    """
    log = _erase_watching(table)

    compactions = [kwargs for name, kwargs in log if name == "compact_files"]
    assert compactions, "the erasure did not compact at all — the subject's bytes stay in a live data file"
    for kwargs in compactions:
        assert kwargs.get("batch_size") == 64, f"unbounded read: {kwargs}"
        assert kwargs.get("num_threads") == 2, f"unbounded parallelism: {kwargs}"


def test_the_RESIDUAL_CHECK_counts_rather_than_materialising_every_matching_row(table: str) -> None:
    """The verify step asks one question per retained version — "does this version still hold the
    subject" — and the answer is a number.

    Materialising the matching rows to read `.num_rows` sizes that check by the ERASURE: a subject
    with many rows is exactly the case where the proof costs the most, and it is paid once per
    version. `count_rows` answers the same question without building the rows.
    """
    log = _erase_watching(table)

    materialised = [f for name, f in log if name == "to_table" and f == _PREDICATE]
    assert not materialised, f"the residual check built {len(materialised)} row set(s) to ask for a count"
    assert any(name == "count_rows" and f == _PREDICATE for name, f in log), "no version was probed for residual rows at all"
