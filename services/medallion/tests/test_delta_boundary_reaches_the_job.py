"""The CDF delta boundary is published and then dropped three times over, so it never reaches Ray.

The publication event carries `{from_version, to_version}` and `publication_trigger` propagates both
onto the stage trigger. From there the range dies quietly:

  1. `StageTrigger` declares neither field and sets `extra="ignore"`, so parsing DISCARDS them --
     while that model's own docstring cites `from_version`/`to_version` as its example of the
     additive fields a consumer must tolerate;
  2. `submit_stage` reads only `originator`/`project` off the trigger;
  3. `submit_stage_job` never exports `BASE_VERSION`.

`runners/dummy/job.py` reads `BASE_VERSION` as "the delta boundary … from the publication event,
which carries the exact range". It is never set, so the job reads the whole tier every time.

What this is and is not: the Ray stage-job entrypoint is per-mover OPT-IN and no mover row in
`chart/values.yaml` declares one, so nothing pays the O(tier) cost in the shipped estate today. It is
dead config on an opt-in lane, not a live regression -- but D1's advertised "O(delta), not a tier
rescan" property is unobtainable by any lane that switches the Ray stage job on, and two docstrings
assert a wiring that does not exist.

The gap sits BETWEEN three files that each look correct alone, which is why this test drives the
whole chain rather than any one of them.
"""

from __future__ import annotations

from typing import Any

import pytest
from medallion.services import ray_submit
from medallion.services.trigger_guards import StageTrigger


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def _capture(_client: Any, submission_id: str, body: dict[str, Any]) -> None:
        seen["submission_id"] = submission_id
        seen["body"] = body

    monkeypatch.setattr(ray_submit.rk, "submit_or_reattach", _capture)
    return seen


def _settings(**over: object) -> Any:
    """The REAL settings object, not a namespace — the same reasoning `test_ray_job_names_its_transform`
    records: the submit path reads more of the config than the feature under test cares about, so a
    hand-rolled fake here would test the fake. Constructed from defaults, then `model_copy`, which
    also side-steps the cross-field guard that refuses `ray_enabled` without the compute config.
    """
    from medallion.core.config import MedallionSettings

    return MedallionSettings().model_copy(update=over)


def test_the_trigger_model_KEEPS_the_range_it_documents_as_its_own_example() -> None:
    """Step 1. `extra="ignore"` silently dropped the two fields the docstring names."""
    trigger = StageTrigger.model_validate({"token": "tok-1", "dataset": "silver$features", "from_version": 7, "to_version": 9})

    assert trigger.from_version == 7, "the publication range was discarded at parse"
    assert trigger.to_version == 9


def test_a_FIRST_publication_carries_no_floor_and_that_is_not_an_error() -> None:
    """`from_version` is None on a dataset's first publication -- 'everything', not 'missing'."""
    trigger = StageTrigger.model_validate({"token": "tok-1", "to_version": 3})

    assert trigger.from_version is None
    assert trigger.to_version == 3


@pytest.mark.asyncio
async def test_the_range_REACHES_the_submitted_job_as_BASE_VERSION(captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Steps 2 and 3, and the assertion the whole finding turns on."""

    async def _resolve(_settings: Any, *, project: str = "") -> None:
        return None

    monkeypatch.setattr(ray_submit, "resolve_transform_async", _resolve)

    await ray_submit.submit_stage_job(
        _settings(),
        from_uri="s3://acme/bronze",
        to_uri="s3://acme/silver",
        stage="silver",
        token="tok-1",
        from_version=7,
    )

    env = captured["body"]["runtime_env"]["env_vars"]
    assert env.get("BASE_VERSION") == "7", f"the delta boundary never reached the job: {sorted(env)}"


@pytest.mark.asyncio
async def test_NO_floor_is_an_EMPTY_string_which_the_runner_reads_as_everything(captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Not omitted, and not a sentinel number. `job.py` strips the value and treats empty as a full
    read, so an empty string is the one spelling that already has a defined meaning downstream."""

    async def _resolve(_settings: Any, *, project: str = "") -> None:
        return None

    monkeypatch.setattr(ray_submit, "resolve_transform_async", _resolve)

    await ray_submit.submit_stage_job(
        _settings(),
        from_uri="s3://acme/bronze",
        to_uri="s3://acme/silver",
        stage="silver",
        token="tok-1",
    )

    env = captured["body"]["runtime_env"]["env_vars"]
    assert env.get("BASE_VERSION") == "", f"a first publication must say 'everything', not omit the key: {env.get('BASE_VERSION')!r}"
