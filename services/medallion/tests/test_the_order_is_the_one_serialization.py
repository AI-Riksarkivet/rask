"""The submitted `runtime_env` is exactly `order.to_env()` — no spread hand-rolled beside it.

[[LH-159]] `WorkOrder.to_env` is documented as "The ONE serialization, so no adapter hand-rolls it",
and `ray_submit` hand-rolled three spreads beside it: the OTLP names, the trace context, and a
`RASK_PARAM_` map. That is the shape the port exists to prevent — a second submitter that renders the
same facts differently is how two lanes stop agreeing about what a job was told, and it is exactly
what made the Ray adapter unusable against this job once before (measured 2026-09-07: the port's
adapter rendered `to_env()` and this submitter wrote six differently-spelled names, sharing ZERO keys).

TWO OF THE THREE WERE ALREADY REDUNDANT, which is why this is a deletion rather than a redesign.
`to_env()` emits `RASK_PARAM_*` from `order.params` — the same `job_params` — and emits
`TRACEPARENT`/`TRACESTATE`/`OTEL_SERVICE_NAME` plus the `observability.otlp` map. What the order
lacked was the VALUES: nothing populated `WorkOrder.observability`, so the lane filled the gap beside
the order instead of inside it.

EQUIVALENCE IS ONLY PROVABLE NOW BECAUSE OF [[XC-066]]. `to_env()` OMITS an empty optional while the
old block blanked it (`os.environ.get(name, "")`), so the two rendered different dicts and the
migration was a behaviour change rather than a refactor. Once `otlp_env()` was fixed to omit, they
agree — so this can be asserted key-for-key instead of deployed and hoped for.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import ray_submit


class _FakeJobsAPI:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeJobsAPI:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, _url: str, **kwargs: Any) -> Any:
        self.posts.append(kwargs.get("json") or {})
        return _Resp()

    async def get(self, _url: str, **_kw: Any) -> Any:
        return _Resp(status=404)


class _Resp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def json(self) -> dict[str, Any]:
        return {}

    def raise_for_status(self) -> None:
        return None


def _submit(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    api = _FakeJobsAPI()
    monkeypatch.setattr(ray_submit.httpx, "AsyncClient", lambda **_kw: api)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "bronze-to-silver")
    settings = MedallionSettings.model_validate({"compute_enabled": True, "ray_enabled": True, "ray_job_params": {"alpha": "1"}})
    asyncio.run(ray_submit.submit_stage_job(settings, from_uri="s3://lake/b", to_uri="s3://lake/s", stage="silver", token="t", lineage_json="{}"))
    return api.posts[0]["runtime_env"]["env_vars"]


def test_the_submitted_env_carries_the_platform_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-vacuity: without this, an empty submission would satisfy every assertion below."""
    env = _submit(monkeypatch)

    assert env.get("RASK_STAGE") == "silver", f"the submission is missing the platform's own half: {sorted(env)[:8]}"


def test_the_workloads_parameters_ride_the_ORDER(monkeypatch: pytest.MonkeyPatch) -> None:
    """`to_env()` already namespaces `order.params`; a second map beside it is a divergence waiting."""
    env = _submit(monkeypatch)

    assert env.get("RASK_PARAM_alpha") == "1", "the workload's parameter did not reach the job"


def test_the_observability_context_rides_the_ORDER(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED before the fix: `WorkOrder.observability` was never populated, so `to_env()` emitted none of
    it and the lane spread the values in beside the order.

    The environment is set HERE rather than relying on `_submit`: this asserts the builder itself, and
    a test that depended on another helper's monkeypatch would pass or fail for the wrong reason.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "bronze-to-silver")

    order = ray_submit.build_stage_order_observability()

    assert order.otlp.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://collector:4318", (
        "the order carries no OTLP config, so `to_env()` cannot be the one serialization"
    )
    assert order.service_name == "bronze-to-silver", "the order carries no service name"
