"""`max_source_bytes` has ONE range on every model that carries it to a compaction.

The value travels settings -> policy -> work item -> plan door, and each hop validates it. A value one
hop accepts and the next refuses is a configuration that passes review and then fails the dataset: the
plan door's refusal is a 4xx, which the executor treats as its own bug. So the four models share the
floor and the ceiling, the same way `scan_batch_size`/`batch_size` share 1..8192.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from catalog.schemas import CompactionPlanRequest, PolicyRequest
from maintenance.core.config import MaintenanceSettings
from service_kit.lakehouse.work_items import DatasetPlan


MIB = 1024 * 1024
FLOOR = MIB
CEILING = 1024 * MIB

_CARRIERS: dict[str, Callable[[int], BaseModel]] = {
    "MaintenanceSettings": lambda value: MaintenanceSettings.model_validate({"s3_bucket": "lake", "max_source_bytes": value}),
    "PolicyRequest": lambda value: PolicyRequest.model_validate({"max_source_bytes": value}),
    "DatasetPlan": lambda value: DatasetPlan.model_validate({"max_source_bytes": value}),
    "CompactionPlanRequest": lambda value: CompactionPlanRequest.model_validate({"batch_size": 64, "num_threads": 2, "max_source_bytes": value}),
}


@pytest.mark.parametrize("carrier", sorted(_CARRIERS))
@pytest.mark.parametrize("value", [FLOOR, CEILING])
def test_the_range_holds_its_ends(carrier: str, value: int) -> None:
    built: Any = _CARRIERS[carrier](value)
    assert built.max_source_bytes == value


@pytest.mark.parametrize("carrier", sorted(_CARRIERS))
@pytest.mark.parametrize("value", [FLOOR - 1, CEILING + 1])
def test_a_value_outside_the_range_is_refused_by_every_carrier(carrier: str, value: int) -> None:
    with pytest.raises(ValidationError):
        _CARRIERS[carrier](value)
