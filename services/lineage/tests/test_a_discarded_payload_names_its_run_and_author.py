"""A payload lineage parks or discards names the run it was about and the verified author it belonged to.

Both readers are tolerant rather than a `RunEvent` parse: they fire on the paths a payload reaches BECAUSE it failed
validation or was refused, so the strict model is unavailable exactly where the answer is needed.
"""

from __future__ import annotations

from lineage.models import author_sub_from_payload, run_id_from_payload


def test_the_verified_author_is_read_off_a_payload_even_when_it_will_not_parse() -> None:
    """`sub` and nothing else, deliberately — the same rule a notifier targets by. This model's `author` property prefers
    `name` and falls back to the standard `ownership` facet, which is right for ATTRIBUTION on a board and wrong here:
    those are producer-supplied and unverified, so a loss log built on them could name someone who is not the author of
    the run that was lost.
    """
    assert author_sub_from_payload({"run": {"facets": {"rask_author": {"name": "Alice A", "sub": "alice"}}}}) == "alice"
    # A name without a verified sub names nobody: the loss is anonymous rather than misattributed.
    assert author_sub_from_payload({"run": {"facets": {"rask_author": {"name": "Alice A"}}}}) is None
    assert author_sub_from_payload({"job": {"facets": {"ownership": {"owners": [{"name": "claimed"}]}}}}) is None
    # Shapes a discard path really meets: truncated, wrong-typed, absent.
    for junk in ({}, {"run": {}}, {"run": {"facets": None}}, {"run": {"facets": {"author": "alice"}}}, "not-a-dict", None):
        assert author_sub_from_payload(junk) is None


def test_the_run_id_is_read_off_a_payload_that_will_not_parse() -> None:
    assert run_id_from_payload({"run": {"runId": "r-1"}}) == "r-1"
    assert run_id_from_payload({"run": {"runId": "  "}}) is None  # blank names no run
    for junk in ({}, {"run": {}}, {"run": None}, {"run": {"runId": 7}}, "not-a-dict", None):
        assert run_id_from_payload(junk) is None
