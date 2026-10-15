"""The planner hands out datasets in a shuffled order, never in the store's listing order.

Discovery returns object-store listing order, which is the same on every tick. A tick that runs its datasets
in one serial pass and dies at dataset N would then never reach the datasets behind N, on any tick.
Shuffling the planned order rotates which datasets sit behind a recurring failure point.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from maintenance.services import sweep as sweep_module
from maintenance.services.due import TableIds
from service_kit.lakehouse.base_refs import BaseRefs


_LISTED = [f"s3://wh/t{i:02d}" for i in range(12)]


@pytest.fixture
def planner(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`plan_sweep` over twelve maintainable datasets, discovered in a fixed listing order."""
    monkeypatch.setattr(sweep_module, "load_policies", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep_module, "_trash_exclusions", lambda *_a, **_k: {})
    monkeypatch.setattr(sweep_module, "_s3fs", lambda *_a, **_k: object())
    monkeypatch.setattr(sweep_module, "buckets_to_sweep", lambda *_a, **_k: ["wh"])
    monkeypatch.setattr(sweep_module, "_discover_all", lambda *_a, **_k: list(_LISTED))
    monkeypatch.setattr(sweep_module, "_protected_roots", lambda *_a, **_k: BaseRefs())
    monkeypatch.setattr(sweep_module, "table_ids", lambda *_a, **_k: TableIds())
    return sweep_module


def test_the_planned_order_is_a_shuffle_of_the_listing(planner: Any) -> None:
    from maintenance.core.config import MaintenanceSettings

    random.seed(0)
    planned = planner.plan_sweep(MaintenanceSettings.model_validate({"MAINTENANCE_S3_BUCKET": "wh"})).uris

    assert sorted(planned) == _LISTED, "the planner dropped or duplicated a dataset"
    assert planned != _LISTED, "the planner hands datasets out in listing order, so a recurring failure starves the same successors every tick"
