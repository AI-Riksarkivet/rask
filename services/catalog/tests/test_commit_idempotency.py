"""A replayed commit must find its own version, not append the same rows twice.

THE REPLAY. Ingest's `finalize` activity commits through this door, then can die BEFORE Dapr records
the activity's result — a pod eviction in the window between the catalog's 200 and the runtime's
checkpoint. Dapr then re-runs the activity (that is its contract: activities re-execute, decisions
replay), the retry re-reads `read_version` fresh, and the door — which had no memory of the first
attempt — appended the same fragments again. The format cannot refuse it: Append never conflicts
with Append (`lance_docs/file_format.md:4828-4834`), which is a FEATURE for concurrent writers and
a trap for a replayed one. Nine runs of one fixture prefix measured today's bronze at nine copies
per file through the RE-RUN flavour of the same hole; this is the CRASH flavour.

THE MECHANISM. `run_id` rides pylance's `commit_message`, stored as the `__lance_commit_message`
transaction property (round-trip verified on pylance 9.0.0 before this was built). A commit
carrying a `run_id` first scans versions after its `read_version` for its own marker and, on a hit,
returns THAT version — same wire shape, no new version, no duplicate rows.

These tests run against a REAL local Lance dataset — the property under test is Lance's own
transaction storage, and a mock of it would prove nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from lance.fragment import write_fragments

from catalog.services import dataplane


SCHEMA = pa.schema([("id", pa.int64()), ("partition_key", pa.string())])


@pytest.fixture
def dataset_uri(tmp_path: Path) -> str:
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(SCHEMA.empty_table(), uri, data_storage_version="2.2", enable_stable_row_ids=True)
    return uri


def _staged_fragments(uri: str, ids: list[int]) -> list[dict]:
    batch = pa.table({"id": pa.array(ids, pa.int64()), "partition_key": pa.array(["p"] * len(ids))})
    written = write_fragments(batch, uri, data_storage_version="2.2", enable_stable_row_ids=True)
    return [json.loads(json.dumps(f.to_json())) for f in written]


def test_a_replayed_commit_returns_the_SAME_version_and_adds_NO_rows(dataset_uri: str) -> None:
    """The regression, end to end: same fragments, same read_version, same run_id — exactly the
    state a died-after-commit retry arrives in."""
    fragments = _staged_fragments(dataset_uri, [1, 2, 3])

    first = dataplane.commit_appended_fragments(dataset_uri, {}, fragments, read_version=1, run_id="run-abc")
    replay = dataplane.commit_appended_fragments(dataset_uri, {}, fragments, read_version=1, run_id="run-abc")

    assert replay == first, "the replay must be answered with the original commit, not a new one"
    ds = lance.dataset(dataset_uri)
    assert ds.count_rows() == 3, "the replay appended — the exact duplication this exists to prevent"
    assert ds.version == first[0]


def test_DIFFERENT_runs_still_append_independently(dataset_uri: str) -> None:
    """The marker must not make the door sticky: a second, distinct run against the same table is a
    legitimate append and lands its own version."""
    v1, _ = dataplane.commit_appended_fragments(dataset_uri, {}, _staged_fragments(dataset_uri, [1, 2]), read_version=1, run_id="run-a")
    v2, rows = dataplane.commit_appended_fragments(dataset_uri, {}, _staged_fragments(dataset_uri, [3]), read_version=v1, run_id="run-b")

    assert v2 > v1
    assert rows == 3


def test_a_commit_WITHOUT_a_run_id_behaves_exactly_as_before(dataset_uri: str) -> None:
    """run_id is optional on purpose — other callers of this door owe nothing to ingest's replay
    story, and their commits must be byte-identical to the pre-change behaviour (no marker scan, no
    commit message)."""
    version, rows = dataplane.commit_appended_fragments(dataset_uri, {}, _staged_fragments(dataset_uri, [7]), read_version=1)

    assert (version, rows) == (2, 1)
    props = getattr(lance.dataset(dataset_uri).read_transaction(2), "transaction_properties", None) or {}
    assert "__lance_commit_message" not in props


def test_the_replay_check_looks_only_AFTER_read_version(dataset_uri: str) -> None:
    """A run id reused for a NEW run against a later read_version must not be mistaken for a replay
    of the old one: the scan starts strictly after the retry's own read_version, so an earlier
    commit that happens to carry the same marker is out of scope by construction."""
    v1, _ = dataplane.commit_appended_fragments(dataset_uri, {}, _staged_fragments(dataset_uri, [1]), read_version=1, run_id="run-x")

    # Same marker, but read_version = v1: the earlier commit is at v1 itself, not after it.
    v2, rows = dataplane.commit_appended_fragments(dataset_uri, {}, _staged_fragments(dataset_uri, [2]), read_version=v1, run_id="run-x")

    assert v2 > v1
    assert rows == 2


def test_an_interleaved_FOREIGN_commit_does_not_hide_the_replay(dataset_uri: str) -> None:
    """The realistic race: this run commits, ANOTHER writer appends, then the retry arrives. The
    scan must walk past the stranger's version and still find our marker."""
    fragments = _staged_fragments(dataset_uri, [1, 2])
    first = dataplane.commit_appended_fragments(dataset_uri, {}, fragments, read_version=1, run_id="run-abc")

    # A concurrent writer lands an unrelated append on top.
    dataplane.commit_appended_fragments(dataset_uri, {}, _staged_fragments(dataset_uri, [9]), read_version=first[0], run_id="other-run")

    replay = dataplane.commit_appended_fragments(dataset_uri, {}, fragments, read_version=1, run_id="run-abc")

    assert replay == first
    assert lance.dataset(dataset_uri).count_rows() == 3, "replay after an interleaved commit still duplicated"


# --------------------------------------------------------------------------------------------------
# The replay that arrives with NOTHING to commit
# --------------------------------------------------------------------------------------------------


def test_an_EMPTY_replay_is_answered_with_the_runs_own_commit(dataset_uri: str) -> None:
    """The door was unreachable for the case that needs it most, and the ORDER of two guards is why.

    `if not fragments: raise` sat ABOVE the `if run_id:` marker check, so a retry carrying an empty
    list could never reach the dedupe -- it was refused 400 before the catalog ever looked.

    That is exactly the state an ingest retry arrives in. `finalize_run` purges the staged manifests
    immediately after committing, so a replay finds staging empty AND its carried fallback empty, and
    asks with nothing. Refused, it reported `committed_version: None, rows: 0` for a run that had
    committed -- false lineage for work that landed.

    Empty-plus-a-known-marker is not a meaningless commit. It is the question "what did I commit?",
    and the catalog is the only thing that can answer it.
    """
    fragments = _staged_fragments(dataset_uri, [1, 2, 3])
    first = dataplane.commit_appended_fragments(dataset_uri, {}, fragments, read_version=1, run_id="run-purged")

    answered = dataplane.commit_appended_fragments(dataset_uri, {}, [], read_version=1, run_id="run-purged")

    assert answered == first, "the catalog could not name the version this run committed"
    assert lance.dataset(dataset_uri).count_rows() == 3, "answering the question must not write anything"


def test_an_EMPTY_commit_from_an_UNKNOWN_run_is_still_refused(dataset_uri: str) -> None:
    """The guard the reorder must not remove. Nothing staged and no prior commit is a genuinely
    meaningless request, and answering it with the dataset's current version is the very thing the
    ingest defect did -- reporting a version this run did not produce."""
    from lance_namespace import InvalidInputError

    with pytest.raises(InvalidInputError):
        dataplane.commit_appended_fragments(dataset_uri, {}, [], read_version=1, run_id="run-never-ran")


def test_an_EMPTY_commit_with_NO_run_id_is_still_refused(dataset_uri: str) -> None:
    """No marker means no question to answer, so the empty guard applies unchanged."""
    from lance_namespace import InvalidInputError

    with pytest.raises(InvalidInputError):
        dataplane.commit_appended_fragments(dataset_uri, {}, [], read_version=1)


# ── the guard must fail CLOSED ──────────────────────────────────────────────────────────────────
#
# docs/DECISIONS.md "The Python estate audit" (E3, P1/high) — "Commit idempotency guard fails OPEN on any storage error,
# re-enabling the duplicate-append it exists to prevent". Confirmed at HEAD by the independent
# re-audit, which also noted the gap these tests close: eight tests existed and NONE injected a
# raising storage layer, so the failure mode was entirely uncovered.
#
# TWO BLANKET HANDLERS, both pointing the same wrong way:
#   * `lance.dataset(...)` wrapped in `except Exception: return None`, commented "no dataset yet ->
#     certainly no prior commit by this run" — true for ABSENCE, false for a transient S3 error, a
#     timeout, or expired credentials. All of them answered "no prior commit".
#   * `read_transaction(version)` wrapped in `except Exception: continue`, whose docstring defends
#     skipping a STRANGER'S unreadable version — and says nothing about the case that matters: a
#     transient failure reading OUR OWN marker version, which skips the very evidence the scan exists
#     to find.
#
# Either one turns the replay into a silent duplicate append, and nothing downstream can refuse it
# (Append never conflicts with Append). The guard's whole purpose is to fail CLOSED: when it cannot
# prove the run has not committed, it must raise so the activity retries — not assume innocence.


class _RaisingDataset:
    """A dataset whose versions can be listed but whose transactions cannot be read.

    The wrapped handle is typed precisely; the `*a: Any` forwards below are NOT lazy typing but a
    genuine dynamic boundary — `lance.dataset` is a heavily overloaded extension function, and typing
    the pass-through as `object` makes the forward itself a type error while proving nothing about it.
    """

    def __init__(self, inner: lance.LanceDataset, boom: Exception) -> None:
        self._inner = inner
        self._boom = boom

    def versions(self) -> list[dict]:
        return self._inner.versions()

    def read_transaction(self, _version: int) -> object:
        raise self._boom


def test_an_UNREADABLE_STORE_refuses_rather_than_assuming_no_prior_commit(dataset_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The open door: `lance.dataset` failing meant "certainly no prior commit", so the replay
    appended again. A store that cannot be READ has told us nothing about what is in it."""

    def _boom(*_a: Any, **_kw: Any) -> object:
        raise OSError("connection reset by peer talking to the object store")

    monkeypatch.setattr(dataplane.lance, "dataset", _boom)

    with pytest.raises(Exception) as caught:
        dataplane._find_run_commit(dataset_uri, {}, "run-1", 0)
    assert not isinstance(caught.value, AssertionError)
    assert "run-1" in str(caught.value) or "unavailable" in str(caught.value).lower(), str(caught.value)


def test_a_MISSING_dataset_still_reads_as_no_prior_commit(dataset_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half, and why this is a discrimination rather than a blanket raise: a table that
    genuinely does not exist yet HAS no prior commit, and a first commit must not be refused."""

    def _absent(*_a: Any, **_kw: Any) -> object:
        raise OSError("Dataset at path /nope/t.lance was not found")

    monkeypatch.setattr(dataplane.lance, "dataset", _absent)

    assert dataplane._find_run_commit(dataset_uri, {}, "run-1", 0) is None


def test_an_UNREADABLE_TRANSACTION_refuses_rather_than_skipping_our_own_marker(dataset_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`except Exception: continue` skipped ANY unreadable version — including the run's own marker
    on a transient error, which is precisely the evidence the scan exists to find."""
    fragments = _staged_fragments(dataset_uri, [1, 2])
    dataplane.commit_appended_fragments(dataset_uri, {}, fragments, read_version=1, run_id="run-1")

    real = dataplane.lance.dataset

    def _wrap(*a: Any, **kw: Any) -> object:
        return _RaisingDataset(real(*a, **kw), OSError("read timed out fetching the transaction file"))

    monkeypatch.setattr(dataplane.lance, "dataset", _wrap)

    with pytest.raises(Exception) as caught:
        dataplane._find_run_commit(dataset_uri, {}, "run-1", 1)
    assert not isinstance(caught.value, AssertionError)


def test_a_GENUINELY_unreadable_STRANGER_version_is_still_skipped(dataset_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The docstring's own case must survive: pre-transaction-file history and GC'd versions are
    ABSENT, not broken, and an absent stranger must not fail a legitimate first commit."""
    fragments = _staged_fragments(dataset_uri, [1, 2])
    dataplane.commit_appended_fragments(dataset_uri, {}, fragments, read_version=1, run_id="other-run")

    real = dataplane.lance.dataset

    def _wrap(*a: Any, **kw: Any) -> object:
        return _RaisingDataset(real(*a, **kw), FileNotFoundError("_transactions/3.txn was not found"))

    monkeypatch.setattr(dataplane.lance, "dataset", _wrap)

    assert dataplane._find_run_commit(dataset_uri, {}, "run-1", 1) is None
