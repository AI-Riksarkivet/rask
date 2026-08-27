"""`/produce` can vary its volume, because a fixed-volume producer cannot exercise the review band.

`seed_bronze` has carried `rows: int = 8` since it was written, and nothing could reach it: `produce`
called it without the argument, so every cascade this estate has ever run wrote exactly eight rows.
That is a parameter with no door — the mirror of the dead-config defect this plane keeps finding, and
it has a consequence beyond tidiness.

The promotion review band asks "is this promotion unusual?", comparing a stage's row count against its
predecessor's. With every promotion writing the same eight rows the delta is always ZERO, so no legal
band can ever breach — `abs(0) > band * previous` is false for every `band >= 0`, and `ge=0` forbids
the rest. Measured 2026-08-23: the band was enabled, correct, deployed, and unfalsifiable, because the
only producer available could not produce an unusual promotion.

§9.1 also says the 0.25 default is "ASSUMED, not measured — nobody has looked at what a normal
silver->gold delta is on a live corpus" and asks for a measurement of real promotions. That
measurement needs promotions of different sizes.
"""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest
from medallion.core.config import MedallionSettings
from medallion.services import produce as produce_module
from medallion.services.compute import seed_bronze


def test_seed_bronze_still_takes_a_row_count() -> None:
    """The knob this exposes. If it goes, the door below is pointing at nothing."""
    assert "rows" in inspect.signature(seed_bronze).parameters


def test_produce_accepts_a_row_count() -> None:
    parameters = inspect.signature(produce_module.produce).parameters
    assert "rows" in parameters, "produce cannot vary its volume, so the review band cannot be exercised"


def test_the_row_count_is_optional_and_defaults_to_the_seeders_own() -> None:
    """Absent means byte-identical to today: `produce` must not restate seed_bronze's default, because
    two copies of one number drift and the drift is invisible (both still "work")."""
    assert inspect.signature(produce_module.produce).parameters["rows"].default is None


class _Stop(Exception):
    """Ends `produce` at the seeder, so the assertion is about the CALL and not about lineage, Dapr or S3."""


def _capturing_seeder(seen: dict[str, object]):
    def seeder(uri: str, storage_options: dict[str, str], **kwargs: object) -> object:
        seen.update(kwargs)
        raise _Stop

    return seeder


@pytest.mark.asyncio
async def test_produce_forwards_the_row_count_to_the_seeder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plumbing, asserted on the CALL rather than on the source text.

    A knob that is accepted and dropped looks exactly like one that works, so this watches what
    `seed_bronze` actually receives. An earlier version of this test grepped the source for
    `rows=rows`; that passes or fails on how the forwarding is SPELLED, which is not the property.
    """
    seen: dict[str, object] = {}
    monkeypatch.setattr(produce_module, "seed_bronze", _capturing_seeder(seen))
    settings = MedallionSettings.model_validate({"MEDALLION_COMPUTE_ENABLED": "true", "MEDALLION_BRONZE_URI": "memory://bronze"})
    with pytest.raises(_Stop):
        await produce_module.produce(cast("Any", None), settings, token="idem-test", rows=137)
    assert seen.get("rows") == 137, f"seed_bronze saw {seen!r}"


@pytest.mark.asyncio
async def test_absent_rows_lets_the_seeder_choose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte-identical to before: `produce` must not pass a number it invented."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(produce_module, "seed_bronze", _capturing_seeder(seen))
    settings = MedallionSettings.model_validate({"MEDALLION_COMPUTE_ENABLED": "true", "MEDALLION_BRONZE_URI": "memory://bronze"})
    with pytest.raises(_Stop):
        await produce_module.produce(cast("Any", None), settings, token="idem-test")
    assert "rows" not in seen, f"produce restated a default the seeder owns: {seen!r}"
