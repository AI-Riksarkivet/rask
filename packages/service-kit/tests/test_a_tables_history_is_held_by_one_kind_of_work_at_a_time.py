"""[[LH-308]] A table's history fence is held by reclaims or by ref writes, never both, and every hold ends.

The doors' side, a ref write refused while a reclaim holds the fence and every reclaim refused while a ref write does,
is pinned in ``services/catalog/tests/test_tag_and_branch_failures_carry_their_spec_code.py`` and
``tests/integration/test_no_reclaim_deletes_a_version_while_a_ref_is_written.py``. Here, on a local control root, the
fence's own rules: holders of one kind share it, and a hold that lapsed because its holder crashed no longer refuses
the other kind. That a released hold frees it is what every door test writing a ref and then reclaiming relies on.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from service_kit.lakehouse import history_fences, records
from service_kit.lakehouse.history_fences import HistoryFence, HistoryWork, TableHistory
from service_kit.lakehouse.objectfs import StorageOptions


@pytest.mark.parametrize(
    ("first", "lapsed", "then", "held_by"),
    [
        pytest.param(HistoryWork.REFS, False, HistoryWork.REFS, [HistoryWork.REFS, HistoryWork.REFS], id="ref-writes-share-it"),
        pytest.param(HistoryWork.RECLAIM, True, HistoryWork.REFS, [HistoryWork.REFS], id="a-lapsed-hold-frees-it"),
    ],
)
def test_a_take_is_refused_only_by_a_live_hold_of_the_other_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: HistoryWork, lapsed: bool, then: HistoryWork, held_by: list[HistoryWork]
) -> None:
    history = TableHistory(control_root=str(tmp_path), storage_options={}, table_uuid="5f0e9c3a-8b1d-4e2f-a6c7-0d9b8e7f6a5c")
    with monkeypatch.context() as hold:
        if lapsed:
            hold.setattr(history_fences, "HOLD", timedelta(0))
        history.take(first)

    history.take(then)

    fence = records.read_json(history.control_root, StorageOptions(), history.key)
    assert fence is not None
    assert [holder.work for holder in HistoryFence.model_validate(fence[0]).holders] == held_by
