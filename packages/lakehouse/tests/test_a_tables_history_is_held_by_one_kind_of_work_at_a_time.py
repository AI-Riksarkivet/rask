"""[[LH-308]] A table's history fence is held by reclaims or by ref writes, never both, and every hold ends.

The doors' side, a ref write refused while a reclaim holds the fence and every reclaim refused while a ref write does,
is pinned in ``services/catalog/tests/test_tag_and_branch_failures_carry_their_spec_code.py`` and
``tests/integration/test_no_reclaim_deletes_a_version_while_a_ref_is_written.py``. Here, on a local control root, the
fence's own rules: holders of one kind share it, a hold that lapsed because its holder crashed does not refuse the
other kind, and the record a release leaves, its holders emptied, is taken again (ADR 0233). That a released hold frees
it is what every door test writing a ref and then reclaiming relies on.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from lakehouse import history_fences, records
from lakehouse.fences import FenceUnreadableError
from lakehouse.history_fences import HistoryFence, HistoryWork, TableHistory
from lakehouse.storage_options import StorageOptions


_UUID = "5f0e9c3a-8b1d-4e2f-a6c7-0d9b8e7f6a5c"

type Before = Callable[[TableHistory, pytest.MonkeyPatch], None]


def _taken(work: HistoryWork, hold: timedelta) -> Before:
    def before(history: TableHistory, monkeypatch: pytest.MonkeyPatch) -> None:
        with monkeypatch.context() as taken:
            taken.setattr(history_fences, "HOLD", hold)
            history.take(work)

    return before


def _released(history: TableHistory, _monkeypatch: pytest.MonkeyPatch) -> None:
    # What a release writes once its holder is gone: the record stays, naming the table, its holders emptied.
    history.release(history.take(HistoryWork.RECLAIM))


@pytest.mark.parametrize(
    ("before", "then", "held_by"),
    [
        pytest.param(_taken(HistoryWork.REFS, history_fences.HOLD), HistoryWork.REFS, [HistoryWork.REFS, HistoryWork.REFS], id="ref-writes-share-it"),
        pytest.param(_taken(HistoryWork.RECLAIM, timedelta(0)), HistoryWork.REFS, [HistoryWork.REFS], id="a-lapsed-hold-frees-it"),
        pytest.param(_released, HistoryWork.REFS, [HistoryWork.REFS], id="the-record-a-release-left-is-taken-again"),
    ],
)
def test_a_take_joins_a_hold_of_its_own_kind_or_replaces_a_lapsed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: Before, then: HistoryWork, held_by: list[HistoryWork]
) -> None:
    history = TableHistory(control_root=str(tmp_path), storage_options={}, table_uuid=_UUID)
    before(history, monkeypatch)

    history.take(then)

    fence = records.read_json(history.control_root, StorageOptions(), history.key)
    assert fence is not None
    assert fence[0]["uuid"] == _UUID, f"the record names its table under {sorted(fence[0])}"
    assert [holder.work for holder in HistoryFence.model_validate(fence[0]).holders] == held_by


def test_a_record_that_does_not_parse_is_refused_by_name(tmp_path: Path) -> None:
    history = TableHistory(control_root=str(tmp_path), storage_options={}, table_uuid=_UUID)
    records.create_json(history.control_root, StorageOptions(), history.key, {"uuid": _UUID, "holders": "every one"})

    with pytest.raises(FenceUnreadableError) as refused:
        history.take(HistoryWork.REFS)

    assert history.key in str(refused.value), f"the refusal does not name the record: {refused.value}"
