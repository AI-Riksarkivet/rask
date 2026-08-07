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
