"""A project that DECLARES `review_enabled` must actually get its promotions held.

THE DEFECT, and it is worse than the feature being inert. `_review_reasons` gated the whole band
evaluation on `promotion_hold.review_enabled(settings)` — the CHART-WIDE flag — while the same
function LOGS `gate.review_enabled`, the value the declaration supplied. So an admin declares "hold
unusual promotions for a person" through the catalog's admin-gated, audited door, the door answers
200, `gate/describe` reads the record back, the mover emits `review_enabled: true` in its structured
log, and not one promotion is ever held.

An operator checking whether their declaration took effect sees exactly what they declared. The log
reports the policy; the decision ignored it.

The composition rule is already settled and this just applies it: `effective_gate` returns the declared
record WHOLE or the chart's settings WHOLE, never merged, and it names which won. `review_band` and
`key_column` already flow from there. `review_enabled` is the one field the caller went around.
"""

from __future__ import annotations

from types import SimpleNamespace

from medallion.services import transform


class _Gate:
    """An effective gate as `effective_gate` composes it — the DECLARED record having won."""

    def __init__(self, *, review_enabled: bool) -> None:
        self.review_band = 0.25
        self.review_enabled = review_enabled
        self.key_column = "id"
        self.required_columns: tuple[str, ...] = ()
        self.gate_source = "declared"


def _drive(*, review_enabled: bool, chart_flag: bool) -> list[str]:
    """Run `_review_reasons` with the gate resolving to a DECLARED record, and the chart flag opposed.

    The chart flag is set to the opposite of the declaration on purpose: a test where both agree
    cannot tell which one the code consulted, which is exactly how the defect survived.
    """
    import asyncio
    from unittest.mock import patch

    settings = SimpleNamespace(quality_review_enabled=chart_flag)
    trigger = SimpleNamespace(pre_row_count=8)
    result = SimpleNamespace(row_count=1000, previous_row_count=8)
    gate = _Gate(review_enabled=review_enabled)

    async def _resolved(*_a: object, **_k: object) -> object:
        return None

    with (
        patch.object(transform.gate_svc, "effective_gate", lambda *_a, **_k: gate),
        patch.object(transform.gate_svc, "resolve_gate_async", _resolved),
    ):
        return asyncio.run(transform._review_reasons(settings, trigger, result=result, project="acme", transition="silver->gold"))


def test_a_declared_review_is_evaluated_even_when_the_chart_flag_is_OFF() -> None:
    """The headline: the declaration is the policy, not the chart-wide default."""
    reasons = _drive(review_enabled=True, chart_flag=False)

    assert reasons, "a declared review evaluated nothing — the project's policy was ignored in favour of the chart flag"


def test_a_declaration_that_says_NO_review_is_also_honoured() -> None:
    """The other direction, so the fix is 'the declaration decides' and not 'always review'."""
    reasons = _drive(review_enabled=False, chart_flag=True)

    assert not reasons, "a declaration disabling review was overridden by the chart flag"
