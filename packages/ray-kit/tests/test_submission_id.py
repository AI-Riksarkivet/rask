"""The submission id is the idempotency key — these pin what it must and must not collapse.

The bug this file exists for: ``token or 'notoken'`` meant every token-less submission of a stage
landed on ONE id, and ``submit_or_reattach`` read the duplicate as a successful re-attach — the
second transform's work silently never ran.
"""

from ray_kit.submit import submission_id


def test_tokenless_submissions_of_DIFFERENT_work_get_DIFFERENT_ids() -> None:
    """The collapse: two distinct transforms, no token — they must not share a job id."""
    a = submission_id("silver", None, work="s3://wh/bronze$a\x00s3://wh/silver$a")
    b = submission_id("silver", None, work="s3://wh/bronze$b\x00s3://wh/silver$b")
    assert a != b


def test_one_token_fanning_out_to_two_tables_gets_two_ids() -> None:
    """The sibling collapse hid WITH a token: same trigger, same stage, two tables."""
    a = submission_id("silver", "tok-1", work="s3://wh/bronze$a\x00s3://wh/silver$a")
    b = submission_id("silver", "tok-1", work="s3://wh/bronze$b\x00s3://wh/silver$b")
    assert a != b


def test_redelivery_reattaches_same_stage_token_work_is_same_id() -> None:
    """A redelivered trigger carries the same (stage, token, work) — it must re-attach."""
    kwargs = {"work": "s3://wh/bronze$a\x00s3://wh/silver$a"}
    assert submission_id("silver", "tok-1", **kwargs) == submission_id("silver", "tok-1", **kwargs)
    assert submission_id("silver", None, **kwargs) == submission_id("silver", None, **kwargs)


def test_token_stays_visible_in_the_id() -> None:
    """Operators grep the Ray dashboard by token — the digest must not replace it."""
    sid = submission_id("silver", "arrival-42", work="from\x00to")
    assert "arrival-42" in sid
    assert sid.startswith("ray-silver-arrival-42-")


def test_no_work_is_byte_identical_to_the_historic_shape() -> None:
    """The train path passes no work; its ids (and any running jobs) must not move."""
    assert submission_id("train", "tok-9") == "ray-train-tok-9"
    assert submission_id("silver", None) == "ray-silver-notoken"


def test_id_is_ray_safe_regardless_of_work_length() -> None:
    """Arbitrarily long URIs ride as a fixed-width digest; the charset stays [A-Za-z0-9_-]."""
    sid = submission_id("silver", "tok/with:odd chars", work="x" * 10_000)
    assert len(sid) <= 200
    assert all(c.isalnum() or c in "_-" for c in sid)


def test_a_redelivered_trigger_reattaches_but_a_DEPLOY_does_not() -> None:
    """B3's second axis. Re-attach is only correct while the program is the same one.

    During a rolling deploy a redelivered trigger landing on the NEW pod re-attached to a job the OLD
    pod submitted — old entrypoint, old runtime_env, old transform — and `submit_or_reattach` read the
    collision as success. The run then carried the new build's provenance over the old build's output,
    which is worse than a failure because nothing anywhere is red.
    """
    work = "s3://wh/bronze$a\x00s3://wh/silver$a"

    # Same build, same work: a redelivery must land on the SAME job.
    assert submission_id("silver", "tok", work=work, code="main-aaaa") == submission_id("silver", "tok", work=work, code="main-aaaa")
    # A DEPLOY changes the program, so the same work must become a NEW job.
    assert submission_id("silver", "tok", work=work, code="main-aaaa") != submission_id("silver", "tok", work=work, code="main-bbbb")


def test_an_unset_code_version_reproduces_the_previous_id_exactly() -> None:
    """Backwards compatibility is the reason the axis is opt-in rather than always-on.

    A deployment that has not wired the code version must keep re-attaching exactly as before, rather
    than silently adopting a new id scheme — which would orphan every in-flight job at the moment of
    upgrade, each one still running and no longer watched by anything.
    """
    work = "s3://wh/bronze$a\x00s3://wh/silver$a"
    assert submission_id("silver", "tok", work=work, code="") == submission_id("silver", "tok", work=work)


def test_the_id_stays_within_rays_length_limit_under_a_long_tag() -> None:
    """Both axes ride as digests precisely so a long URI or a long tag cannot push the id past the
    limit — the failure there is a submit rejection at the far end of a cascade, for a reason that
    reads as nothing to do with naming."""
    got = submission_id("silver", "t" * 300, work="s3://" + "x" * 500, code="main-" + "y" * 300)
    assert len(got) <= 200
