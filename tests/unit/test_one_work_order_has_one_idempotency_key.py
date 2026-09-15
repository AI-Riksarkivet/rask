"""A `WorkOrder`'s idempotency key is derived in ONE place, and it honours what the field promises.

[[LH-157]]. `WorkOrder.idempotency_key` documents itself as "deterministic in (stage, token, from->to,
code_version)", and the key IS the re-attach handle: `inprocess_executor` states that "the order's
`idempotency_key` IS the handle, which makes a redelivered order re-attach to the same" outcome.

TWO CALLERS BUILT IT TWO WAYS, and one of them dropped an axis. `ray_submit` derived it through
`stage_submission_id(stage, token, from_uri, to_uri, code=code_version)` — all four. `transform`
hand-rolled `f"{stage}:{token}:{from}->{to}"` — no `code_version`. So on the RAY lane a build bump
minted a new identity and the work re-ran, while on the IN-PROCESS lane the key was unchanged and the
run re-attached to the PREVIOUS BUILD's outcome: a stage reporting COMPLETE against an artifact the
current code never produced, with no counter moving and nothing in the log.

THE ESTATE ALREADY LEARNED THIS ONCE, one lane over. `stage_submission_id`'s own docstring says it was
extracted because "a second inline copy of this expression is how the poller ends up watching an id the
submitter never used", and that `code` "must therefore reach BOTH calls". The hand-rolled key in
`transform` is precisely that second inline copy, reintroduced on the other lane.

So the derivation moves onto the order itself. A caller that cannot spell the key cannot spell it
differently.
"""

from __future__ import annotations

from service_kit.lakehouse.work_order import derive_idempotency_key


def key(
    *,
    stage: str = "silver",
    token: str | None = "tok-1",
    from_uri: str = "s3://b/bronze",
    to_uri: str = "s3://b/silver",
    code_version: str = "build-1",
) -> str:
    """One call shape with named defaults, so each test varies exactly the axis it is about.

    Explicit rather than a splatted dict: only `token` is optional, so a dict wide enough to hold
    `None` is the wrong type for the other four — and a test that has to loosen a type to call the
    function is testing a signature nobody else can use.
    """
    return derive_idempotency_key(stage=stage, token=token, from_uri=from_uri, to_uri=to_uri, code_version=code_version)


def test_a_code_version_change_changes_the_key() -> None:
    """The axis that was missing, and the whole of this row.

    Without it a rebuilt stage re-attaches to the previous build's recorded outcome and reports success
    for work this code never did.
    """
    before = key()

    after = key(code_version="build-2")

    assert before != after, "a build bump leaves the key unchanged, so the run re-attaches to the old outcome"


def test_the_same_inputs_give_the_same_key() -> None:
    """Determinism is the other half: a REDELIVERED order must re-attach rather than run twice."""
    assert key() == key()


def test_every_documented_axis_actually_moves_the_key() -> None:
    """The field promises four axes. A promise nothing tests is how the fourth went missing.

    Asserted per axis rather than in aggregate so a failure names the one that stopped counting.
    """
    baseline = key()

    for axis, changed in (
        ("stage", "gold"),
        ("token", "tok-2"),
        ("from_uri", "s3://b/other-bronze"),
        ("to_uri", "s3://b/other-silver"),
        ("code_version", "build-9"),
    ):
        assert key(**{axis: changed}) != baseline, f"{axis} does not affect the key, but the field says it is deterministic in it"


def test_an_absent_token_is_not_the_same_as_the_string_none() -> None:
    """`token` is optional, and a caller spelling its absence differently is how two lanes diverge.

    Pinned because the hand-rolled version used `token or 'notoken'` — a spelling invented at one call
    site, which the other never knew about.
    """
    absent = key(token=None)
    literal = key(token="None")

    assert absent != literal
    assert absent == key(token=None)


def test_neither_lane_hand_rolls_the_key() -> None:
    """The structural half: one derivation means no call site can spell it differently.

    Source-read rather than behavioural, because the defect was never a wrong VALUE at either site —
    each was self-consistent. It was two sites at all.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for path in (
        repo / "services" / "medallion" / "src" / "medallion" / "services" / "transform.py",
        repo / "services" / "medallion" / "src" / "medallion" / "services" / "ray_submit.py",
    ):
        source = path.read_text()
        assigned = [line.strip() for line in source.splitlines() if "idempotency_key=" in line]
        for line in assigned:
            assert "derive_idempotency_key" in line, f"{path.name} spells the key itself: {line!r}"
