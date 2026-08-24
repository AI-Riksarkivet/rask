"""A declared gate governs; an undeclared project keeps the chart's settings.

This is the read half of the door built for item 4. Without it the record is something an admin
edits and the medallion ignores — two sources of truth for whether a promotion is held, with the
governed one losing, which is worse than having only the ungoverned one because it LOOKS governed.

Three behaviours, and the default matters as much as the feature — the same three the lane
resolution test pins, for the same reasons.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from medallion.services import gate as gate_svc

from service_kit.lakehouse import gate_specs
from service_kit.lakehouse.gate_specs import GateSpec


def _settings(tmp_path: Path, **over: object) -> Any:
    base: dict[str, Any] = {
        "control_root": str(tmp_path),
        "storage_options": lambda: {},
        "quality_key_column": "id",
        "required_column_list": ["id"],
        "promotion_review_band": 0.25,
        "quality_review_enabled": False,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_an_undeclared_project_keeps_the_chart_settings(tmp_path: Path) -> None:
    """Byte-for-byte the old behaviour — an estate that opted into nothing changes nothing."""
    settings = _settings(tmp_path)

    gate = gate_svc.effective_gate(settings, gate_svc.resolve_gate(settings, project="acme"))

    assert gate.review_band == 0.25
    assert gate.key_column == "id"


def test_a_declared_band_wins(tmp_path: Path) -> None:
    """The whole point: a threshold changes through the door, not through `helm upgrade`."""
    gate_specs.put_spec(str(tmp_path), {}, GateSpec(project="acme", review_band=0.9, review_enabled=True))
    settings = _settings(tmp_path)

    gate = gate_svc.effective_gate(settings, gate_svc.resolve_gate(settings, project="acme"))

    assert gate.review_band == 0.9
    assert gate.review_enabled is True


def test_one_project_does_not_govern_another(tmp_path: Path) -> None:
    """A declaration is per-tenant; a neighbour's record must not leak into this one."""
    gate_specs.put_spec(str(tmp_path), {}, GateSpec(project="other", review_band=0.9))
    settings = _settings(tmp_path)

    gate = gate_svc.effective_gate(settings, gate_svc.resolve_gate(settings, project="acme"))

    assert gate.review_band == 0.25


def test_an_unresolvable_gate_falls_back_rather_than_raising(tmp_path: Path) -> None:
    """A config lookup must never take down a cascade — see the module note on why a gate falls
    back where a lane refuses."""
    settings = _settings(tmp_path, control_root="")

    gate = gate_svc.effective_gate(settings, gate_svc.resolve_gate(settings, project="acme"))

    assert gate.review_band == 0.25
