"""A table's history fence is held by an erasure's scheduling or by ref writes, never both, and every hold ends (ADR 0233).

That many ref writes share it through the tag door, on a real S3 store, is
``tests/integration/test_a_tables_ref_writes_share_its_history.py``'s claim. Here the fence's own rules: holders of one
kind share it, each holding an object of its own (ADR 0233 §9), a hold that lapsed because its holder crashed does not
refuse the other kind, a released hold frees it, what is planted below it holds no more than its key names, and two
takes that exclude each other never both hold it, the last on a local root and, in the lane that names an S3 store
(``scripts/lifecycle_on_rustfs_check.sh``), on that store.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lakehouse import history_fences, records, stored_objects
from lakehouse.history_fences import HistoryFencedError, HistoryHolder, HistoryWork, TableHistory
from lakehouse.storage_options import StorageOptions


_UUID = "5f0e9c3a-8b1d-4e2f-a6c7-0d9b8e7f6a5c"
_STORE = os.environ.get("RASK_LIFECYCLE_STORE_ENDPOINT", "")

type Before = Callable[[TableHistory, pytest.MonkeyPatch], None]


def _taken(work: HistoryWork, hold: timedelta) -> Before:
    def before(history: TableHistory, monkeypatch: pytest.MonkeyPatch) -> None:
        with monkeypatch.context() as taken:
            taken.setattr(history_fences, "HOLD", hold)
            history.take(work)

    return before


def _released(history: TableHistory, _monkeypatch: pytest.MonkeyPatch) -> None:
    history.release(history.take(HistoryWork.SCHEDULE))


@pytest.mark.parametrize(
    ("before", "then", "held_by"),
    [
        pytest.param(_taken(HistoryWork.REFS, history_fences.HOLD), HistoryWork.REFS, [HistoryWork.REFS, HistoryWork.REFS], id="ref-writes-share-it"),
        pytest.param(_taken(HistoryWork.SCHEDULE, timedelta(0)), HistoryWork.REFS, [HistoryWork.REFS], id="a-lapsed-hold-frees-it"),
        pytest.param(_released, HistoryWork.REFS, [HistoryWork.REFS], id="a-released-hold-frees-it"),
    ],
)
def test_a_take_joins_a_hold_of_its_own_kind_or_replaces_a_lapsed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: Before, then: HistoryWork, held_by: list[HistoryWork]
) -> None:
    history = TableHistory(control_root=str(tmp_path), storage_options={}, table_uuid=_UUID)
    before(history, monkeypatch)

    history.take(then)

    # Each live hold, as the fence's directory holds one object per holder.
    held: list[str] = []
    for listed in stored_objects.list_under(f"{tmp_path}/{history.directory}", StorageOptions()):
        stored = records.read_json(history.control_root, StorageOptions(), f"{history.directory}/{listed.path.rsplit('/', 1)[-1]}")
        assert stored is not None
        holder = HistoryHolder.model_validate(stored[0])
        assert holder.uuid == _UUID, f"the holder names {holder.uuid}"
        if holder.holder is not None and holder.holder.expires_at > datetime.now(UTC):
            held.append(holder.holder.work)
    assert sorted(held) == held_by


#: What a writer could plant below a table's fence directory, and what a ref write's take reads of it: (where, body, the
#: kind the take is refused by, the marks it is handed). Only the catalog's identity writes there (ADR 0246); a take judges
#: what it finds as though anything could (ADR 0233 §9).
_ID = "0" * 32
_PLANTED = {
    "a-schedule-name-whose-body-is-no-json": (f"schedule-{_ID}.json", b"not json", "schedule", frozenset()),
    "a-schedule-name-claiming-a-release-until-2099": (
        f"schedule-{_ID}.json",
        json.dumps(
            {"uuid": _UUID, "holder": {"token": "x", "work": "release", "taken_at": "2026-01-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z"}}
        ).encode(),
        "schedule",
        frozenset(),
    ),
    "a-mark-below-a-subdirectory": (f"schedule-x/erasure-release.{'1' * 32}.json", b"{}", None, frozenset()),
    "a-release-hold-below-a-subdirectory": (f"schedule-x/release-{_ID}.json", b"{}", None, frozenset()),
    "a-name-the-fence-does-not-define": ("schedule-zz.json", b"{}", None, frozenset()),
}


@pytest.mark.parametrize("planted", list(_PLANTED), ids=list(_PLANTED))
def test_what_is_planted_below_the_fence_holds_at_most_what_its_key_names_until_it_lapses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planted: str
) -> None:
    # A take judges only the names the fence defines, directly in its directory, a holder's kind by its key whatever its
    # body says (F3 of specs/catalog/Fence.tla): a body that does not parse, or claims another kind and a later end, holds
    # a scheduling's hold, refusing a ref write 409 and never 500, and a mark or a holder's name below a subdirectory is
    # nothing. Whatever was planted is swept once it has stood a hold on the store's clock.
    history = TableHistory(control_root=str(tmp_path), storage_options={}, table_uuid=_UUID)
    name, body, refused_by, marks = _PLANTED[planted]
    path = tmp_path / history.directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)

    try:
        with history.hold_for(HistoryWork.REFS, renewed=False) as hold:
            seen: tuple[str | None, frozenset[tuple[str, str]]] = (None, hold.marks)
    except HistoryFencedError as exc:
        seen = (str(exc).split()[1], frozenset())
    with monkeypatch.context() as lapsed:
        lapsed.setattr(history_fences, "HOLD", timedelta(0))
        history.release(history.take(HistoryWork.SCHEDULE))
    left = [listed.path for listed in stored_objects.list_under(f"{tmp_path}/{history.directory}", StorageOptions())]

    assert (seen, left) == ((refused_by, marks), []), f"a ref write's take read {seen}, and once it lapsed {left} still stood"


@pytest.fixture(params=["dir", "s3"] if _STORE else ["dir"])
def control_root(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[str, StorageOptions]:
    """A control root on the local root and, where a lane names an S3 store (``scripts/lifecycle_on_rustfs_check.sh``), in a
    bucket of the test's own there, whose conditional writes and listings arbitrate the fence."""
    if request.param == "dir":
        return str(tmp_path), StorageOptions()
    import boto3

    bucket = f"fence-{uuid.uuid4().hex[:12]}"
    access, secret = os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"]
    boto3.client("s3", endpoint_url=_STORE, region_name="us-east-1", aws_access_key_id=access, aws_secret_access_key=secret).create_bucket(Bucket=bucket)
    options = StorageOptions.from_flat({"aws_access_key_id": access, "aws_secret_access_key": secret, "aws_region": "us-east-1"}).with_endpoint(_STORE)
    return f"s3://{bucket}", options


def test_two_takes_that_exclude_each_other_never_both_hold_the_fence(control_root: tuple[str, StorageOptions], monkeypatch: pytest.MonkeyPatch) -> None:
    # F1 of specs/catalog/Fence.tla: a ref write and a scheduling each list the directory empty, each write their own object,
    # and only then list again. The second listing shows each the other, so at most one holds the fence; without it both
    # would, and a tag could land unseen between a release's mark and its record.
    root, options = control_root
    history = TableHistory(control_root=root, storage_options=options, table_uuid=_UUID)
    listed, written = threading.Barrier(2, timeout=30), threading.Barrier(2, timeout=30)
    real = TableHistory._excluding

    def _in_step(fence: TableHistory, work: str, *, mine: str | None) -> Any:
        if mine is not None:
            written.wait()
        found = real(fence, work, mine=mine)
        if mine is None:
            listed.wait()
        return found

    monkeypatch.setattr(TableHistory, "_excluding", _in_step)
    held: list[HistoryWork] = []

    def _take(work: HistoryWork) -> None:
        try:
            history.take(work)
        except HistoryFencedError:
            return
        held.append(work)

    takers = [threading.Thread(target=_take, args=(work,)) for work in (HistoryWork.REFS, HistoryWork.SCHEDULE)]
    for taker in takers:
        taker.start()
    for taker in takers:
        taker.join(timeout=60)

    assert len(held) < 2, f"a ref write and a scheduling both hold the table's history: {held}"
