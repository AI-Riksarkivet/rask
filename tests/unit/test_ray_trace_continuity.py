"""Trace continuity across the Ray boundary (prod-readiness P3) — infra-free unit tier.

The estate's distributed trace went dark at ``ray job submit``: neither submission site propagated W3C
context, so the Ray-side spans were orphans or absent. Covers both halves + the degradation contract:

- SUBMITTER (``ray_kit.submit.trace_env``, used by medallion's wrappers): the span's traceparent is injected
  into the submitted ``runtime_env`` at both sites (stage + train), and nothing is injected when no span
  is active — the trace is continued, never fabricated.
- JOB (``scripts/ray_stage_job.py`` + ``ray_train_job.py``): the inlined ``_traced_root`` extracts the
  env context and starts the job's root span as a child of the submitting span; a missing/garbage
  TRACEPARENT runs the work untraced without crashing. The two inlined copies are pinned identical
  (the self-contained-ray-job convention's drift-pin, like ``test_ray_stage_job``'s deriver pins).

Job scripts are standalone files baked into the ray image, so they load here by path (repo convention).
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from medallion.core.config import MedallionSettings
from medallion.services import ray_submit
from ray_kit import submit as ray_submit_kit


_SCRIPTS = Path(__file__).parents[2] / "scripts"


def _load_job(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage_job = _load_job("ray_stage_job")
train_job = _load_job("ray_train_job")

_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
#: A fixed, valid W3C traceparent a "submitter" could have injected (trace 11…1, parent span 22…2).
_PARENT = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"


# --------------------------------------------------------------------------- #
# submitter side — inject at both submission sites
# --------------------------------------------------------------------------- #


def test_trace_env_injects_a_valid_traceparent_from_the_active_span() -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("submitting") as span:
        env = ray_submit_kit.trace_env()
    ctx = span.get_span_context()
    assert _TRACEPARENT_RE.match(env["TRACEPARENT"])
    # Exact ids from the active span; the trailing flags byte is the SDK's to choose (sampled et al).
    assert env["TRACEPARENT"].startswith(f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-")


def test_trace_env_is_empty_without_an_active_span() -> None:
    # No active span → inject writes nothing → the job runs untraced (never a fabricated context).
    assert ray_submit_kit.trace_env() == {}


def _capture_submits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub the Ray Jobs API (the test_ray_submit MockTransport shape) and capture POSTed bodies."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"submission_id": "s"})
        return httpx.Response(200, json={"status": "SUCCEEDED"})

    real = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr(ray_submit.httpx, "AsyncClient", factory)
    return captured


def _stage_settings() -> MedallionSettings:
    """The submit-path settings every test here uses — Ray on, timings short."""
    return MedallionSettings.model_validate({"ray_enabled": True, "compute_enabled": True, "ray_poll_interval_seconds": 0.001, "ray_job_timeout_seconds": 0.05})


def test_stage_submission_carries_the_active_spans_traceparent(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_submits(monkeypatch)
    settings = MedallionSettings.model_validate(
        {
            "ray_enabled": True,
            "compute_enabled": True,
            "ray_poll_interval_seconds": 0.001,
            "ray_job_timeout_seconds": 0.05,
        }
    )
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("mover") as span:
        asyncio.run(ray_submit.submit_stage_job(settings, from_uri="a", to_uri="b", stage="bronze", token="t"))
    env = captured[0]["runtime_env"]["env_vars"]
    ctx = span.get_span_context()
    assert env["TRACEPARENT"].startswith(f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-")


def test_train_submission_carries_the_active_spans_traceparent(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_submits(monkeypatch)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("trainer") as span:
        asyncio.run(
            ray_submit.submit_train_job(
                MedallionSettings.model_validate({}),
                model="churn",
                features_json="[]",
                token="tok1",
                registry_uri="s3://lake/models/churn",
                artifact_base="s3://lake/artifacts/churn",
            )
        )
    env = captured[0]["runtime_env"]["env_vars"]
    assert f"{span.get_span_context().trace_id:032x}" in env["TRACEPARENT"]


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
        "00-zz" + "1" * 30 + "-" + "2" * 16 + "-01",  # non-hex trace id
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


def _trace_helper_source(script: str) -> str:
    """The two inlined trace helpers, extracted BY NAME from the file with `ast`.

    Not `inspect.getsource` on an imported module: `ray_dummy_job` cannot be imported outside the Ray
    image at all — it does `from dummy_runner.job import main`, and `runners/dummy` is a SEALED
    environment matched by no workspace glob on purpose. Executing it to inspect it would make this
    pin depend on the seal the repo deliberately maintains.

    Not string-slicing between markers either, which was the first attempt and was wrong: the scripts
    do not carry the same functions AFTER the helpers, so "up to the next def main" grabbed 8469
    characters from one file and 3959 from another and reported a drift that did not exist. `ast`
    addresses the definitions themselves and cannot overshoot.
    """
    body = (_SCRIPTS / f"{script}.py").read_text(encoding="utf-8")
    tree = ast.parse(body)
    wanted = {"_extract_trace_parent", "_traced_root"}
    found = {n.name: ast.get_source_segment(body, n) or "" for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in wanted}
    missing = wanted - set(found)
    assert not missing, f"{script} does not inline {sorted(missing)} — the submitter's TRACEPARENT is discarded there"
    return "\n".join(found[name] for name in sorted(wanted))


def test_traced_root_is_pinned_identical_across_ALL_FOUR_jobs() -> None:
    """The self-contained-job convention's drift-pin, over every baked entrypoint.

    All four scripts INLINE the same helpers rather than importing them, because a job is an ARTEFACT
    baked into an image and must not depend on the fleet's import graph. Duplication is the accepted
    cost and this pin is what makes it safe — so it has to cover every copy, not the two it happened
    to start with. `ray_dummy_job` and `ray_lance_job` had NO copy: the submitter injected TRACEPARENT
    into the dummy lane and the script discarded it, so the estate's own GPU-free end-to-end prover
    ran untraced and could not demonstrate the property the production lanes depend on.
    """
    reference = _trace_helper_source("ray_stage_job")
    assert "def _traced_root(" in reference, "the reference block was sliced wrong"

    for script in ("ray_train_job", "ray_dummy_job", "ray_lance_job"):
        assert _trace_helper_source(script) == reference, f"{script} has drifted from ray_stage_job"


def test_the_dummy_lane_CONTINUES_the_trace_it_is_handed() -> None:
    """The dummy lane is the GPU-free end-to-end prover — declare -> baked entrypoint -> silver. A
    smoke lane that runs untraced cannot demonstrate the one property the production lanes rely on."""
    body = (_SCRIPTS / "ray_dummy_job.py").read_text(encoding="utf-8")
    assert "_traced_root" in body, "the dummy job never opens a root span, so the submitter's TRACEPARENT is discarded"


def test_a_submitted_job_carries_WHO_it_is_for_in_ray_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Ray job carried no identity, so nothing in the compute plane could name a person.

    THE FIELD IS `metadata`, NOT `runtime_env.env_vars`, and the distinction decides the feature. The
    identity has to be readable from OUTSIDE the job, AFTER it fails — `metadata` is returned by
    `GET /api/jobs/<id>` and shown in the dashboard, which is exactly the read a failure path makes.
    `runtime_env.env_vars` configures the PROCESS: recovering it means introspecting the runtime env,
    and it hands the job code an identity it has no reason to hold.

    The originator rides the trigger already (added with the cascade's ORIGINATOR lane), so this is a
    field on the submission rather than a new identity to thread.
    """
    captured = _capture_submits(monkeypatch)

    asyncio.run(ray_submit.submit_stage_job(_stage_settings(), from_uri="a", to_uri="b", stage="silver", token="tok", originator="alice", project="acme"))

    metadata = captured[0]["metadata"]
    assert metadata["rask.originator"] == "alice"
    assert metadata["rask.project"] == "acme"
    assert metadata["rask.token"] == "tok"
    assert "rask.originator" not in captured[0]["runtime_env"]["env_vars"], "identity is not process config"


def test_a_submission_with_no_human_behind_it_carries_no_originator(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service-triggered cascade has no person to name, and an empty string is not an identity — the
    key is omitted so a reader never mistakes '' for someone."""
    captured = _capture_submits(monkeypatch)

    asyncio.run(ray_submit.submit_stage_job(_stage_settings(), from_uri="a", to_uri="b", stage="silver", token="tok"))

    assert "rask.originator" not in captured[0].get("metadata", {})
