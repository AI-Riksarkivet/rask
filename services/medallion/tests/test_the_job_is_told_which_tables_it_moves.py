"""The Ray stage job is told WHERE to read and write, and never WHAT it is moving.

`runners/dummy/src/dummy_runner/job.py` reads three identity variables the platform sets nowhere:

  1. `TO_ID` / `FROM_ID` — the CATALOG identifiers (`silver$features`), which the lineage graph and
     the FGA objects are keyed by. The runner falls back to the URI's stem, and its own comment says
     what that costs: "emitting the URI would name a node no grant matches, hiding the run from every
     recipient". A hidden run acks SUCCESS, so nothing anywhere reports the loss.
  2. `RUN_ID` — the run the job's own OpenLineage events are keyed on. Unset, the runner emits an
     empty run id, so the job's COMPLETE/FAIL cannot MERGE onto the run the mover already emitted for
     the same hop; the graph holds two half-runs instead of one.

The mover HAS all three at the dispatch site (`resolve_stage_identity` names the tables,
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
from medallion.core.config import MedallionSettings, get_settings
from medallion.services import ray_submit, transform
from medallion.services.trigger_guards import StageTrigger


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The submitted Ray job body, without a cluster."""
    seen: dict[str, Any] = {}

    async def _capture(_client: Any, submission_id: str, body: dict[str, Any]) -> None:
        seen["submission_id"] = submission_id
        seen["body"] = body

    monkeypatch.setattr(ray_submit.rk, "submit_or_reattach", _capture)

    async def _resolve(_settings: Any, *, project: str = "") -> None:
        return None

    monkeypatch.setattr(ray_submit, "resolve_transform_async", _resolve)
    return seen


def _settings(**over: object) -> MedallionSettings:
    """The REAL settings object — the submit path reads more config than this feature cares about, so
    a hand-rolled fake would test the fake (`test_delta_boundary_reaches_the_job`'s reasoning)."""
    return MedallionSettings().model_copy(update=over)


@pytest.mark.asyncio
async def test_the_submitted_job_is_told_the_catalog_identifiers_it_moves(captured: dict[str, Any]) -> None:
    """The submission is the only place these can enter the job's environment."""
    await ray_submit.submit_stage_job(
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
    assert env.get("FROM_ID") == "acme-bronze$events", f"the job cannot name its input table: {sorted(env)}"
    assert env.get("TO_ID") == "acme-silver$features", f"the job cannot name its output table: {sorted(env)}"
    assert env.get("RUN_ID") == "0f9f1f1e-0000-4000-8000-000000000001", f"the job's own lineage events would key on a run nothing else knows: {sorted(env)}"


@pytest.mark.asyncio
async def test_an_unwired_identity_is_OMITTED_rather_than_sent_blank(captured: dict[str, Any]) -> None:
    """Same rule as `ORIGINATOR`/`PROJECT`: an empty value is not an identity.

    The runner reads `e.get("TO_ID", "") or _identifier_from(to_uri)`, so an absent key takes the
    documented stem fallback. Sending `""` would take the same branch today and pin a value the
    platform does not know — and the moment a runner tests for the key's PRESENCE (the natural way to
    ask "was I wired?"), a blank would answer yes.
    """
    await ray_submit.submit_stage_job(
        _settings(),
        from_uri="s3://acme-wh/bronze",
        to_uri="s3://acme-wh/silver",
        stage="silver",
        token="tok-1",
    )

    env = captured["body"]["runtime_env"]["env_vars"]
    assert "FROM_ID" not in env and "TO_ID" not in env and "RUN_ID" not in env, f"an unwired lane sent blank identities instead of none: {sorted(env)}"


def test_the_dispatch_hands_the_WORKFLOW_the_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Link 2 of the chain. The workflow input is what survives to the submit activity.

    Carried on the SPEC rather than read off the round-tripped trigger, because the trigger does not
    carry it: `from_id`/`to_id` are resolved by `resolve_stage_identity` (env, or the declared
    transform record) and the run id is minted by the mover, so neither exists on the payload the
    publisher sent.
    """
    scheduled: dict[str, Any] = {}

    class _Client:
        def schedule_new_workflow(self, **kwargs: Any) -> None:
            scheduled.update(kwargs)

        def get_workflow_state(self, _instance_id: str) -> object:
            return None

    import dapr.ext.workflow as wf

    monkeypatch.setattr(wf, "DaprWorkflowClient", lambda *a, **k: _Client())

    transform._dispatch_stage_workflow(
        get_settings(),
        from_uri="s3://wh/p-bronze/pages.lance",
        to_uri="s3://wh/p-silver/pages.lance",
        token="tok-1",
        lineage_json="{}",
        trigger=StageTrigger(token="tok-1"),
        from_id="acme-bronze$events",
        to_id="acme-silver$features",
        run_id="0f9f1f1e-0000-4000-8000-000000000002",
    )

    spec = scheduled["input"]
    assert spec.get("from_id") == "acme-bronze$events", f"the identity never reached the workflow input: {sorted(spec)}"
    assert spec.get("to_id") == "acme-silver$features"
    assert spec.get("run_id") == "0f9f1f1e-0000-4000-8000-000000000002"


@pytest.mark.asyncio
async def test_the_handler_dispatches_with_the_identity_IT_resolved(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Link 1, and the one the whole finding turns on: the mover holds these names and dropped them.

    Driven through `handle_stage` rather than asserted on `resolve_stage_identity`, because the defect
    is not that the names are wrong — it is that the dispatch never passed them on.
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
    )
    dispatched: dict[str, Any] = {}

    def _fake_dispatch(_settings: Any, **kwargs: Any) -> str:
        dispatched.update(kwargs)
        return "stage-instance"

    monkeypatch.setattr(transform, "_dispatch_stage_workflow", _fake_dispatch)

    class _Dapr:
        async def publish_event(self, **_kwargs: Any) -> None:
            return None

    status = await transform.handle_stage(cast(Any, _Dapr()), settings, {"data": {"token": "tok-1"}})

    assert status == {"status": "SUCCESS"}
    assert dispatched.get("from_id") == "bronze$events", f"the mover kept its resolved input id to itself: {sorted(dispatched)}"
    assert dispatched.get("to_id") == "silver$features"
    assert dispatched.get("run_id"), "the run the job will emit under was never handed over"


def test_the_submit_ACTIVITY_forwards_what_the_spec_carries(captured: dict[str, Any]) -> None:
    """Link 3, the one a carried field is silently dropped at.

    `submit_stage` reads named fields off the spec — it does not splat it — so a field added to
    `StageJobSpec` and not read here reaches the state store, survives every checkpoint, and never
    reaches the job. That is exactly how `originator` and `project` had to be wired one by one.
    """
    from medallion import workflow

    spec = workflow.StageJobSpec(
        from_uri="s3://acme-wh/abc_bronze$events",
        to_uri="s3://acme-wh/def_silver$features",
        stage="silver",
        token="tok-1",
        from_id="acme-bronze$events",
        to_id="acme-silver$features",
        run_id="0f9f1f1e-0000-4000-8000-000000000003",
    )

    workflow.submit_stage(cast(Any, None), cast(Any, spec.model_dump()))

    env = captured["body"]["runtime_env"]["env_vars"]
    assert (env.get("FROM_ID"), env.get("TO_ID"), env.get("RUN_ID")) == (
        "acme-bronze$events",
        "acme-silver$features",
        "0f9f1f1e-0000-4000-8000-000000000003",
    ), f"the durable spec carried the identity and the submission dropped it: {sorted(env)}"
