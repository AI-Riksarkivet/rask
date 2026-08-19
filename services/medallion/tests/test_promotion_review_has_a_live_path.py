"""A tripwire on the promotion review's only dispatch path, so removing it is a decision, not a diff.

The design deletes the mover's local quality gate once movers publish, on the grounds that the
catalog's publish gate then runs the identical assertions at the identical seam. That reasoning is
sound and predates the review: `promotion_hold.publish_hold` has exactly ONE production call site,
`transform.py`, inside the `if quality_blocked:` branch that deletion removes. Taking the branch out
orphans the workflow, the door, the FGA rung and the chart wiring in one edit, with nothing red.

There is a second, sharper half. An approved hold resumes today by publishing the next-stage trigger
(`workflow.publish_promotion` -> `spec.pub_topic`). Under a tag-driven cascade the resume must instead
move the `published` tag — and NO DOOR DOES THAT past a failed gate: `publish` re-runs the assertions
and refuses, `tags/update` moves the tag and emits nothing. So "a validator accepted data the gate
refused" is currently unexpressible, and the review cannot be ported by moving its call site.

This test does not defend the current shape. It defends the decision being made explicitly.
"""

from __future__ import annotations

import inspect

from medallion.services import promotion_hold, transform


def test_the_hold_dispatch_is_still_reachable_from_the_stage_handler() -> None:
    source = inspect.getsource(transform)

    assert "promotion_hold.publish_hold" in source, (
        "the promotion review's only dispatch path is gone. If the mover's quality gate was deleted "
        "for the catalog's publish gate, that is the intended direction — but the review must move "
        "with it, and 'a validator accepted data the gate refused' has no door yet: publish re-gates "
        "and refuses, tags/update emits nothing. Decide where the review attaches before removing this."
    )


def test_the_dispatch_is_opt_in_and_fails_closed_without_a_reviewer() -> None:
    """The other half of why deleting the branch is not a like-for-like move: the hold is only a
    QUESTION where a deployment opted in. Everywhere else it is still the permanent block, and that
    behaviour has to survive the port too."""
    from medallion.core.config import MedallionSettings

    assert promotion_hold.review_enabled(MedallionSettings()) is False, (
        "review must stay opt-in: an estate with nobody to ask keeps the permanent block rather than parking promotions on an event no one will raise"
    )
    assert "promotion_hold.review_enabled" in inspect.getsource(transform), "the hold is dispatched unconditionally — the permanent-block default is gone"
