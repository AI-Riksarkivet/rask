"""Trace continuity across the Ray boundary (prod-readiness P3) — infra-free unit tier.

The estate's distributed trace went dark at ``ray job submit``: neither submission site propagated W3C
context, so the Ray-side spans were orphans or absent. Covers both halves + the degradation contract:

- SUBMITTER (``medallion.services.stage_submit.trace_env``, carried by the work order): the span's traceparent is injected
  into the submitted ``runtime_env`` at both sites (stage + train), and nothing is injected when no span
  is active — the trace is continued, never fabricated.
- JOB (``scripts/ray_stage_job.py`` + ``ray_train_job.py``): the inlined ``_traced_root`` extracts the
  env context and starts the job's root span as a child of the submitting span; a missing/garbage
  TRACEPARENT runs the work untraced without crashing. The two inlined copies are pinned identical
  (the self-contained-ray-job convention's drift-pin, like ``test_ray_stage_job``'s deriver pins).

Job scripts are standalone files baked into the ray image, so they load here by path (repo convention).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from medallion.core.config import MedallionSettings
from medallion.services import stage_submit, train_plans


_SCRIPTS = Path(__file__).parents[2] / "scripts"


def _load_job(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage_job = _load_job("ray_stage_job")
train_job = _load_job("ray_train_job")

#: A fixed, valid W3C traceparent a "submitter" could have injected (trace 11…1, parent span 22…2).
_PARENT = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"


# --------------------------------------------------------------------------- #
# submitter side — inject at both submission sites
# --------------------------------------------------------------------------- #


def test_trace_env_is_empty_without_an_active_span() -> None:
    # No active span → inject writes nothing → the job runs untraced (never a fabricated context).
    assert stage_submit.trace_env() == {}


def test_stage_order_carries_the_active_spans_traceparent() -> None:
    """The stage run's order, which the engine posts as the job's env through `to_env` (CP-056)."""
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("stage-runner") as span:
        order = stage_submit.build_work_order(MedallionSettings(), engine="ray", spec=None, from_uri="a", to_uri="b", stage="bronze", token="t")
    ctx = span.get_span_context()
    assert order.to_env()["TRACEPARENT"].startswith(f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-")


def test_train_order_carries_the_active_spans_traceparent() -> None:
    """The training run's order, which the engine posts as the job's env through `to_env` (CP-044)."""
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("trainer") as span:
        order = train_plans.train_order(
            MedallionSettings.model_validate({}),
            token="tok1",
            model="churn",
            features=[],
            config={},
            registry_uri="s3://lake/models/churn",
            artifact_base="s3://lake/artifacts/churn",
            originator="",
            project="",
        )
    assert f"{span.get_span_context().trace_id:032x}" in order.to_env()["TRACEPARENT"]


# --------------------------------------------------------------------------- #
# job side — extract + parent, degrade gracefully
# --------------------------------------------------------------------------- #


def test_job_root_span_parents_on_the_env_traceparent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPARENT", _PARENT)
    exporter = InMemorySpanExporter()
    with stage_job._traced_root("ray.stage_job", {"lance.medallion.stage": "bronze"}, span_processor=SimpleSpanProcessor(exporter)):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.parent is not None and span.parent.is_remote  # a child of the submitter's span
    assert f"{span.parent.trace_id:032x}" == "1" * 32
    assert f"{span.parent.span_id:016x}" == "2" * 16
    assert f"{span.context.trace_id:032x}" == "1" * 32  # same trace — continuity, not a new root
    assert span.attributes is not None and span.attributes["lance.medallion.stage"] == "bronze"


def test_job_failure_is_recorded_on_the_continued_span(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPARENT", _PARENT)
    exporter = InMemorySpanExporter()
    with (
        pytest.raises(RuntimeError, match="boom"),  # the failure still propagates (the job fails honestly)
        train_job._traced_root("ray.train_job", {"lance.model": "churn"}, span_processor=SimpleSpanProcessor(exporter)),
    ):
        raise RuntimeError("boom")
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    # Narrowed, not suppressed: a span with no parent is a BROKEN continuity, which is the thing
    # under test — so the absence has to fail here rather than at attribute access.
    assert span.parent is not None, "the span lost its remote parent — trace continuity is what this asserts"
    assert f"{span.parent.trace_id:032x}" == "1" * 32


def test_job_system_exit_is_recorded_on_the_continued_span(monkeypatch: pytest.MonkeyPatch) -> None:
    # The jobs' own verification failures exit via SystemExit (BaseException) — the SDK's use_span
    # records only Exception, so without the helper's own handling a FAILED job exports a green span.
    monkeypatch.setenv("TRACEPARENT", _PARENT)
    exporter = InMemorySpanExporter()
    with (
        pytest.raises(SystemExit, match="lost stable row ids"),
        stage_job._traced_root("ray.stage_job", {"lance.medallion.stage": "bronze"}, span_processor=SimpleSpanProcessor(exporter)),
    ):
        raise SystemExit("stage transform produced wrong row count or lost stable row ids")
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.events and span.events[0].name == "exception"


def test_job_runs_untraced_without_a_traceparent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACEPARENT", raising=False)
    exporter = InMemorySpanExporter()
    ran = False
    with stage_job._traced_root("ray.stage_job", {}, span_processor=SimpleSpanProcessor(exporter)):
        ran = True
    assert ran and not exporter.get_finished_spans()


@pytest.mark.parametrize(
    "garbage",
    [
        "not-a-traceparent",
        "00-" + "0" * 32 + "-" + "0" * 16 + "-01",  # syntactically valid but the invalid all-zero ids
    ],
)
def test_job_tolerates_a_garbage_traceparent(monkeypatch: pytest.MonkeyPatch, garbage: str) -> None:
    monkeypatch.setenv("TRACEPARENT", garbage)
    exporter = InMemorySpanExporter()
    ran = False
    with stage_job._traced_root("ray.stage_job", {}, span_processor=SimpleSpanProcessor(exporter)):
        ran = True  # no crash, and no span under a fabricated parent
    assert ran and not exporter.get_finished_spans()


def test_job_no_ops_without_an_otlp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real-export path (no injected processor): no endpoint → nowhere to export → run untraced,
    # the same no-op contract as the train job's emit_metrics.
    monkeypatch.setenv("TRACEPARENT", _PARENT)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    ran = False
    with stage_job._traced_root("ray.stage_job", {}):
        ran = True
    assert ran
