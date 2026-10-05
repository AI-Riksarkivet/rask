"""The Ray stage job is told WHERE to read and write, and never WHAT it is moving.

`runners/dummy/src/dummy_runner/job.py` reads three identity variables the platform sets nowhere:

  1. `RASK_DEST_TABLE` / `RASK_SOURCE_TABLE` — the CATALOG identifiers (`silver$features`), which the lineage graph and
     the FGA objects are keyed by. The runner falls back to the URI's stem, and its own comment says
     what that costs: "emitting the URI would name a node no grant matches, hiding the run from every
     recipient". A hidden run acks SUCCESS, so nothing anywhere reports the loss.
  2. `RASK_RUN_ID` — the run the job's own OpenLineage events are keyed on. Unset, the runner emits an
     empty run id, so the job's COMPLETE/FAIL cannot MERGE onto the run the stage runner already emitted for
     the same hop; the graph holds two half-runs instead of one.

The stage runner HAS all three at the dispatch site (`resolve_stage_identity` names the tables,
`lineage_doc.run_id` is the run) and drops them one layer down, exactly as `BASE_VERSION` was dropped
before `test_delta_boundary_reaches_the_job.py` — same chain, same three files, same shape of test:
drive the whole chain rather than any one link, because each link looks correct alone.

THE VERSION WALL is why this travels as env: a sealed runner pins >=3.10,<3.13 and cannot import a
platform package, so the only contract between them is `runtime_env.env_vars` (plus Ray's `metadata`
for the post-mortem read, which already carries the originator).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import ray_submit, stage_submit, transform


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The submitted Ray job body, without a cluster."""
    seen: dict[str, Any] = {}

    async def _capture(_client: Any, submission_id: str, body: dict[str, Any], **_policy: Any) -> str:
        seen["submission_id"] = submission_id
        seen["body"] = body
        return "submitted"  # the real `submit_or_reattach` answers what happened; its caller maps it

    monkeypatch.setattr(ray_submit.rk, "submit_or_reattach", _capture)

    async def _resolve(_settings: Any, *, project: str = "") -> None:
        return None

    monkeypatch.setattr(stage_submit, "resolve_transform_async", _resolve)
    return seen


def _settings(**over: object) -> MedallionSettings:
    """The REAL settings object — the submit path reads more config than this feature cares about, so
    a hand-rolled fake would test the fake (`test_delta_boundary_reaches_the_job`'s reasoning)."""
    return MedallionSettings().model_copy(update=over)


async def _submit(settings: MedallionSettings, **kwargs: Any) -> None:
    """The stage lane's submission: the order it builds, posted through the port."""
    order, registration = await stage_submit.build_stage_order(settings, **kwargs)
    await stage_submit.submit_stage_order(order, registration)


@pytest.mark.asyncio
async def test_the_submitted_job_is_told_the_catalog_identifiers_it_moves(captured: dict[str, Any]) -> None:
    """The submission is the only place these can enter the job's environment."""
    await _submit(
        _settings(),
        from_uri="s3://acme-wh/abc_bronze$events",
        to_uri="s3://acme-wh/def_silver$features",
        stage="silver",
        token="tok-1",
        from_id="acme-bronze$events",
        to_id="acme-silver$features",
        run_id="0f9f1f1e-0000-4000-8000-000000000001",
    )

    env = captured["body"]["runtime_env"]["env_vars"]
    assert env.get("RASK_SOURCE_TABLE") == "acme-bronze$events", f"the job cannot name its input table: {sorted(env)}"
    assert env.get("RASK_DEST_TABLE") == "acme-silver$features", f"the job cannot name its output table: {sorted(env)}"
    assert env.get("RASK_RUN_ID") == "0f9f1f1e-0000-4000-8000-000000000001", (
        f"the job's own lineage events would key on a run nothing else knows: {sorted(env)}"
    )


@pytest.mark.asyncio
async def test_an_unwired_identity_is_OMITTED_rather_than_sent_blank(captured: dict[str, Any]) -> None:
    """Same rule as `RASK_ORIGINATOR`/`RASK_PROJECT`: an empty value is not an identity.

    ABSENT AND BLANK MUST STAY DISTINGUISHABLE, and what absent COSTS is worth naming because it is not
    free. `ray_stage_job.py:464` reads `os.environ.get("RASK_DEST_TABLE", "").strip()` and nothing else —
    there is no fallback deriving the id from the URI stem, and no `_identifier_from` anywhere in this
    repository. So an unwired lane stamps `dataset_id=""`, which DROPS the tier's declared name rather
    than inheriting its upstream's: the runner's own comment prefers that to "publishing a name that
    describes another dataset".

    Sending `""` instead of omitting would pin a value the platform does not know, and the moment a
    runner tests for the key's PRESENCE — the natural way to ask "was I wired?" — a blank answers yes.
    """
    await _submit(
        _settings(),
        from_uri="s3://acme-wh/bronze",
        to_uri="s3://acme-wh/silver",
        stage="silver",
        token="tok-1",
    )

    env = captured["body"]["runtime_env"]["env_vars"]
    assert "RASK_SOURCE_TABLE" not in env and "RASK_DEST_TABLE" not in env and "RASK_RUN_ID" not in env, (
        f"an unwired lane sent blank identities instead of none: {sorted(env)}"
    )


@pytest.mark.asyncio
async def test_the_handler_dispatches_with_the_identity_IT_resolved(captured: dict[str, Any], tmp_path: Any) -> None:
    """The whole chain, and the one the finding turns on: the stage runner holds these names and must hand them on.

    Driven through `handle_stage`, its real plan dispatch and the real submission, because each link looks correct
    alone: the defect is a name the stage runner resolved and the job's environment never received.
    """
    import lance
    import pyarrow as pa

    lance.write_dataset(pa.table({"id": [1, 2, 3]}), str(tmp_path / "bronze.lance"))
    settings = MedallionSettings(
        MEDALLION_FROM_NAMESPACE="bronze",
        MEDALLION_FROM_DATASET="bronze$events",
        MEDALLION_TO_NAMESPACE="silver",
        MEDALLION_TO_DATASET="silver$features",
        MEDALLION_PUB_TOPIC="medallion.silver",
        MEDALLION_COMPUTE_ENABLED="true",
        MEDALLION_RAY_ENABLED="true",
        MEDALLION_FROM_URI=str(tmp_path / "bronze.lance"),
        MEDALLION_TO_URI=str(tmp_path / "silver.lance"),
        MEDALLION_CONTROL_ROOT=str(tmp_path / "control"),
    )

    class _Dapr:
        async def publish_event(self, **_kwargs: Any) -> None:
            return None

    status = await transform.handle_stage(cast(Any, _Dapr()), settings, {"data": {"token": "tok-1"}})

    assert status == {"status": "SUCCESS"}
    env = captured["body"]["runtime_env"]["env_vars"]
    assert env.get("RASK_SOURCE_TABLE") == "bronze$events", f"the stage runner kept its resolved input id to itself: {sorted(env)}"
    assert env.get("RASK_DEST_TABLE") == "silver$features"
    assert env.get("RASK_RUN_ID"), "the run the job will emit under was never handed over"
