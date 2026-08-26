"""The `<verb>_activity` suffix is not used, and that is a DECISION, not an oversight (DWF-ACT-008).

`register_activity` is called with no explicit name, so the Dapr runtime registers each activity by
its `__name__` — these function names ARE the wire names. Renaming them breaks replay for every
in-flight instance, because history holds the old name and the replay produces the new one; this
estate has no versioning seam to bridge that.

Owner ruling 2026-08-25: record the deviation rather than rename. The convention exists to ease
cross-language invocation, and nothing outside these modules calls these activities; medallion's
names additionally appear in daprd's `activity||<name>` spans, which `report_stage_outcome` reads.

This test exists because the previous state was SILENCE: two independent review sweeps raised the
same finding, found no recorded reasoning, and filed it again. A decision nobody can find is
indistinguishable from an accident.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from ingest import workflow as ingest_wf
from medallion import workflow as medallion_wf


MODULES = pytest.mark.parametrize("module", [pytest.param(ingest_wf, id="ingest"), pytest.param(medallion_wf, id="medallion")])


def _attr(module: ModuleType, name: str) -> Any:
    """Reach a module attribute without a suppression comment.

    The parameters are modules, so a static reader cannot narrow `module.register`; `getattr` states
    the dynamism the test actually relies on instead of asking a checker to look away.
    """
    return getattr(module, name)


@MODULES
def test_the_registered_wire_names_are_the_function_names(module: ModuleType) -> None:
    """The premise the whole ruling rests on. If `register` ever passed an explicit name, renaming
    would become free and this deviation would need revisiting rather than defending."""
    import inspect

    source = inspect.getsource(_attr(module, "register"))

    assert "runtime.register_activity(a)" in source, f"register() no longer registers by __name__; the DWF-ACT-008 ruling assumed it did:\n{source}"
    assert "name=" not in source, "register() now passes an explicit wire name — the rename cost this ruling weighed no longer applies"


@MODULES
def test_the_deviation_is_RECORDED_where_a_reviewer_will_look(module: ModuleType) -> None:
    """Beside the registration itself, not in a commit message a sweep will never read."""
    doc = _attr(module, "register").__doc__ or ""

    assert "DWF-ACT-008" in doc, "the ruling is not recorded where the next sweep will find it"
    assert "replay" in doc, "the recorded reasoning does not say WHY renaming is refused"


@MODULES
def test_no_activity_carries_the_suffix_so_the_deviation_stays_uniform(module: ModuleType) -> None:
    """Half-applied is worse than either answer: a reader seeing three of twenty suffixed cannot tell
    the convention from the exception."""
    suffixed = [a.__name__ for a in _attr(module, "ACTIVITIES") if a.__name__.endswith("_activity")]

    assert suffixed == [], f"the deviation is now half-applied, which reads as an accident: {suffixed}"
