"""What a stage owes its tier: every row names a parent, and only a 1:1 lane is held to its count.

`tier_write.assert_stage_contract` is a pure function both engines call after their write, so its branches are pinned
here directly. A fan-out lane (a video into frames, a recording into speaker turns) legitimately grows; provenance is
the property the old row-count equality stood in for, and it is checked on every lane.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.tier_write import StageContractError, assert_stage_contract


@pytest.mark.parametrize(
    ("rows_in", "rows_out", "cardinality", "parentless", "refusal"),
    [
        pytest.param(3, 6, "1:N", 0, None, id="a-fanout-grows"),
        pytest.param(3, 4, "1:N", 1, "no parent", id="a-parentless-row"),
        pytest.param(3, 6, "1:1", 0, "wrong row count", id="a-1to1-count-moved"),
        pytest.param(3, 3, "1:1", 0, None, id="a-1to1-count-held"),
        pytest.param(3, 3, "one-to-many", 0, "unknown stage cardinality", id="an-unknown-cardinality"),
    ],
)
def test_the_contract_refuses_only_what_a_lane_does_not_owe(rows_in: int, rows_out: int, cardinality: str, parentless: int, refusal: str | None) -> None:
    if refusal is None:
        assert_stage_contract(rows_in=rows_in, rows_out=rows_out, cardinality=cardinality, parentless=parentless)
        return
    with pytest.raises(StageContractError, match=refusal):
        assert_stage_contract(rows_in=rows_in, rows_out=rows_out, cardinality=cardinality, parentless=parentless)
