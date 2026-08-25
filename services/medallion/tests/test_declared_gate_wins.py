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

import pytest
from medallion.services import gate as gate_svc
from pydantic import ValidationError

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


class TestTheGateNamesItsSource:
    """§8 change 6. Two sources, one shape — so the result has to say which one answered.

    The catalog's policy ruling states the rule this implements: "Any surface showing an effective
    policy must say which record won; an inherited value rendered identically to a set one is how
    nobody can tell what is governing their data." A declared `review_band` of 0.25 and the chart's
    default of 0.25 were byte-identical here, so a lane author who declared a gate had no way to
    confirm theirs was the one applied — and nobody investigating a promotion that held could tell
    which record to go and edit.

    Change 6 asked for the chart fallback to be DROPPED instead. That is not the right fix and the
    reason is measured, not argued: a `GateSpec` is scoped per PROJECT (`project: str`,
    `extra="forbid"`) while `chart/values.yaml` carries `requiredColumns` per MOVER — `"id"` for
    bronze-to-silver against `"id,thumbnail,embedding"` for media-to-silver, because one derives
    artifacts the other does not. Dropping the fallback would either un-gate those columns or force
    one list across movers with different outputs. See `docs/architecture/medallion-data-flow.md` item 6.
    """

    def test_the_chart_gate_says_chart(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        assert gate_svc.effective_gate(settings, None).gate_source == "chart"

    def test_a_declared_gate_says_declared(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        gate_specs.put_spec(str(tmp_path), {}, GateSpec(project="acme", review_band=0.5))
        gate = gate_svc.effective_gate(settings, gate_svc.resolve_gate(settings, project="acme"))
        assert gate.gate_source == "declared"

    def test_the_source_is_distinguishable_when_the_VALUES_are_identical(self, tmp_path: Path) -> None:
        """The case the field exists for, and the one a value comparison cannot answer.

        A project declares exactly the chart's defaults — a legitimate thing to do, and the only way
        to say "I have reviewed these and they are what I want". Every value matches; only the source
        differs, and before this there was nothing to read it off.
        """
        settings = _settings(tmp_path)
        chart = gate_svc.effective_gate(settings, None)
        gate_specs.put_spec(str(tmp_path), {}, GateSpec(project="acme", key_column=chart.key_column, review_band=chart.review_band))
        declared = gate_svc.effective_gate(settings, gate_svc.resolve_gate(settings, project="acme"))

        assert declared.review_band == chart.review_band and declared.key_column == chart.key_column
        assert (declared.gate_source, chart.gate_source) == ("declared", "chart")

    def test_the_source_cannot_be_set_by_a_caller(self) -> None:
        """A source a writer can assign is a source a writer can lie about.

        `GateSpec` is `extra="forbid"`, so a stored record naming its own source is refused at parse
        — the field is a property of the TYPE, derived, never carried in the JSON.
        """
        with pytest.raises(ValidationError):
            GateSpec.model_validate({"project": "acme", "gate_source": "declared"})
