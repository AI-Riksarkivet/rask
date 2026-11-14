"""Lineage's relay timer claims each window under a control root, so a period needs a root, and a root on the object
store is one more thing lineage signs requests for."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lineage.core.config import LineageSettings


def test_a_relay_period_with_no_root_to_claim_under_is_refused() -> None:
    with pytest.raises(ValidationError, match="LINEAGE_TIMER_ROOT is empty"):
        LineageSettings(LINEAGE_RELAY_PERIOD_SECONDS=30)


@pytest.mark.parametrize(
    ("outbox", "root", "signs"),
    [
        pytest.param("s3://control/_lineage_outbox", "", True, id="an-outbox-on-the-store"),
        pytest.param("/var/outbox", "s3://control", True, id="a-timer-root-on-the-store"),
        pytest.param("/var/outbox", "/var/control", False, id="neither-on-the-store"),
    ],
)
def test_lineage_signs_store_requests_for_an_outbox_or_a_timer_root_on_the_store(outbox: str, root: str, signs: bool) -> None:
    settings = LineageSettings(LINEAGE_OUTBOX_URI=outbox, LINEAGE_TIMER_ROOT=root)

    assert settings.signs_store_requests is signs
