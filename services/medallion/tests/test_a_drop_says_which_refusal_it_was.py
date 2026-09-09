"""A DROP is the last word a caller gets, so it must say which refusal it was.

§ Q16-7. Dapr neither redelivers nor dead-letters a DROP — the ack IS the outcome — and it was the
same four bytes for a routing drop, an unresolvable lane, an FGA denial and a held promotion. The
counters and logs distinguished them; the wire did not, so an operator could not tell governance from
misrouting. That opacity is how § Q16-6's assertion passed on a trigger that never reached the gate.

THE REASON WAS NEVER MISSING. Every one of these sites already passed it to `record_refused`, so the
wire was discarding a value the code had in hand — which is why `_drop` takes the same string the
counter records rather than a second, hand-kept one.
"""

from __future__ import annotations

from medallion.services.transform import _drop


def test_the_ack_still_carries_the_status_dapr_reads() -> None:
    """The contract is unchanged: the sidecar keys on `status` and ignores the extra key. A reason
    that cost the DROP its meaning would be a worse defect than the opacity it fixes."""
    assert _drop("unresolvable_lane")["status"] == "DROP"


def test_two_refusals_are_distinguishable_on_the_wire() -> None:
    """The whole point. Before this, these two were byte-identical."""
    assert _drop("fga_denied") != _drop("routing_disabled")
    assert _drop("fga_denied")["reason"] == "fga_denied"


def test_every_drop_in_the_transform_names_a_reason() -> None:
    """A roster, not a per-site test: the failure to guard is a drop added LATER without a reason, and
    a per-site test cannot notice a site nobody wrote a test for."""
    import inspect

    from medallion.services import transform

    source = inspect.getsource(transform)
    assert "return _DROP" not in source, "a bare _DROP returned an ack that says nothing about which refusal it was"
    assert 'return _drop("' in source
