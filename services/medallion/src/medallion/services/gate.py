"""Resolve the DECLARED quality gate — the read half of the gate-spec contract.

The catalog writes a :class:`~service_kit.lakehouse.gate_specs.GateSpec` through an admin-gated
door; this is where the medallion reads one back. Object-store-backed for the same reason
``lane.resolve_transform`` is: the gate runs on a path that holds no catalog client, and a mover pod that
has never met the catalog must still be able to resolve the record from the control root alone.

**Opt-in, and the default is load-bearing.** No declared record means ``None``, and the chart's
``quality_key_column`` / ``required_columns`` / ``promotion_review_band`` govern exactly as before.
An estate that has declared nothing behaves byte-for-byte as it did — the stance ``lane`` and
``ray_code_version`` already take.

**A DECLARED GATE IS NOT A MERGE.** When a record exists it supplies all four values, including the
ones left at their model defaults. Falling back per-field would make a UI that cleared a list
indistinguishable from one that never set it, so "declare" means "these are the settings" and
"delete" means "the chart governs again" — which is exactly why the door's clear is a DELETE and
never a write of zeros.

**UNLIKE A LANE, AN UNRESOLVABLE GATE DOES NOT REFUSE.** A named-but-undeclared lane must refuse,
because running the wrong program silently is worse than not running. A gate is the opposite: the
chart's settings are a real, safe configuration, and failing a whole cascade because a settings
record could not be read would take an estate down over a config lookup. So an unreadable record
falls back, loudly.
"""

from __future__ import annotations

import logging
from typing import Protocol

from fastapi.concurrency import run_in_threadpool

from service_kit.lakehouse import gate_specs
from service_kit.lakehouse.gate_specs import GateSpec


log = logging.getLogger(__name__)


class _GateSettings(Protocol):
    """The two fields resolution needs — a Protocol so tests need no full Settings object."""

    control_root: str

    def storage_options(self) -> dict[str, str]: ...


def resolve_gate(settings: _GateSettings, *, project: str) -> GateSpec | None:
    """This project's declared gate, or ``None`` when the chart's settings still govern.

    Never raises: see the module note on why a gate falls back where a lane refuses.
    """
    control_root = getattr(settings, "control_root", "")
    if not project or not control_root:
        return None
    try:
        return gate_specs.get_spec(control_root, settings.storage_options(), project)
    except Exception:  # noqa: BLE001 — a config lookup must not take down a cascade
        log.exception("gate_spec_unresolvable", extra={"project": project})
        return None


async def resolve_gate_async(settings: _GateSettings, *, project: str) -> GateSpec | None:
    """`resolve_gate` off the event loop — the object-store read is blocking."""
    return await run_in_threadpool(resolve_gate, settings, project=project)


class EffectiveGate(Protocol):
    """What a caller actually needs: the values, already resolved — AND where they came from."""

    key_column: str
    required_columns: list[str]
    review_band: float
    review_enabled: bool

    @property
    def gate_source(self) -> str:
        """``"declared"`` or ``"chart"``. See :func:`effective_gate` for why it is not optional.

        READ-ONLY on purpose, and ty is what enforces it: declared as a plain attribute the protocol
        would accept writes, and a source a caller can assign is a source a caller can lie about. The
        two implementations answer it structurally — `GateSpec` as a property, `_ChartGate` as a class
        attribute — and neither takes it as an argument.
        """
        ...


def effective_gate(settings: object, spec: GateSpec | None) -> EffectiveGate:
    """The gate that governs this run — the declared record whole, or the chart's settings whole.

    Whole, never merged, for the reason in the module note: a per-field fallback makes "cleared" and
    "never set" indistinguishable.

    **THE RESULT NAMES ITS SOURCE, and that is the substance of §8 change 6.** The catalog's own
    policy ruling states the rule this implements: *"Any surface showing an effective policy must say
    which record won; an inherited value rendered identically to a set one is how nobody can tell what
    is governing their data."* The gate had exactly that problem — two sources, one shape, and a
    caller that could not tell a declared `review_band` of 0.25 from the chart's default of 0.25.

    Change 6 asked for the fallback to be DROPPED instead, and that is not the right fix; the
    measurement is recorded in `docs/architecture/medallion-data-flow.md`. A `GateSpec` is scoped per PROJECT
    (`project: str`, `extra="forbid"`) while the chart carries `requiredColumns` per MOVER — `"id"`
    for bronze-to-silver against `"id,thumbnail,embedding"` for media-to-silver, because one derives
    artifacts the other does not. Dropping the fallback would either un-gate those columns or force
    one list across movers with different outputs. The drift it feared is also already prevented by
    the whole-not-merged rule above. What was genuinely missing was ATTRIBUTION, so that is what this
    adds.
    """
    if spec is not None:
        return spec
    return _ChartGate(
        key_column=getattr(settings, "quality_key_column", "id"),
        required_columns=list(getattr(settings, "required_column_list", [])),
        review_band=getattr(settings, "promotion_review_band", 0.25),
        review_enabled=getattr(settings, "quality_review_enabled", False),
    )


class _ChartGate:
    """The chart's settings, presented in the shape a declared record has.

    A tiny adapter rather than threading two shapes through every call site: the caller asks one
    object for `review_band` and never learns which source answered.
    """

    __slots__ = ("key_column", "required_columns", "review_band", "review_enabled")

    #: This gate came from the chart, not from a declared record. A plain class attribute rather than
    #: an `__init__` argument: it is a property of the TYPE, and letting a caller pass it would make
    #: the one thing this field exists to state forgeable.
    gate_source = "chart"

    def __init__(self, *, key_column: str, required_columns: list[str], review_band: float, review_enabled: bool) -> None:
        self.key_column = key_column
        self.required_columns = required_columns
        self.review_band = review_band
        self.review_enabled = review_enabled
