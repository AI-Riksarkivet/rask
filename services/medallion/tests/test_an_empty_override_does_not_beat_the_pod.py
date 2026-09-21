"""An OTLP name the stage runner does not have is OMITTED from `runtime_env`, never blanked.

[[XC-066]] THE FILE ALREADY RECORDS THE MECHANISM, for a different pair of keys. `ray_submit.py` states,
measured twice on the live estate, that "Ray merges `runtime_env` OVER the process env, so a key sent
here BEATS the pod's" — written about an S3 credential pair whose double ownership gave every job
`SignatureDoesNotMatch`, and whose halves were moved to the pod for exactly that reason.

The OTLP block was left forwarding `os.environ.get("OTEL_...", "")`. An empty value is not an absence
in an override layer: it makes the SUBMITTER the owner of that key, so a stage runner with
observability off silently disables tracing on a Ray cluster that has its own working configuration.
`""` and absent are different claims, and only one of them defers.

LATENT TODAY, AND THE DISTINCTION IS THE RISK. `observability.enabled` is one global toggle, so the
stage runner and the Ray pods are on or off together and the empty override lands on an already-empty
value. It becomes live the moment a Ray cluster is configured independently — which is precisely what
"bring your own distributed engine" invites, and this platform's own design does.

WHY THIS IS NOT MERELY TIDINESS: the fix can only ever REMOVE an override. When the stage runner HAS a
value it is forwarded unchanged, so a configured lane behaves identically; when it does not, the job
falls back to its own pod's configuration instead of being forced to empty. There is no input for
which omitting is worse than blanking.
"""

from __future__ import annotations

import os

import pytest

from medallion.services import stage_submit


#: The names the lane forwards from its own process. `TRACEPARENT`/`TRACESTATE` come from `rk.trace_env()`
#: and are the span's business, not this block's.
_OTLP = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
)


@pytest.fixture
def _no_otlp(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OTLP:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def _with_otlp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "bronze-to-silver")


def test_the_forwarder_exists_and_is_addressable() -> None:
    """Without this the assertions below could pass against a helper that no longer exists."""
    assert hasattr(stage_submit, "otlp_env"), "the OTLP forwarding must be its own function to be testable at all"


@pytest.mark.usefixtures("_no_otlp")
def test_an_unset_name_is_absent_rather_than_empty() -> None:
    """RED before the fix: every name was present with `""`, and on Ray that OVERRIDES the pod."""
    forwarded = stage_submit.otlp_env()

    blanked = sorted(k for k, v in forwarded.items() if v == "")
    assert not blanked, f"these names are forwarded blank and will override the Ray pod's own values: {blanked}"


@pytest.mark.usefixtures("_with_otlp")
def test_a_value_the_lane_HAS_is_still_forwarded() -> None:
    """The fix must not become "stop forwarding" — a configured lane has to behave exactly as before."""
    forwarded = stage_submit.otlp_env()

    assert forwarded["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4318"
    assert forwarded["OTEL_SERVICE_NAME"] == "bronze-to-silver"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in forwarded, "an unset name must stay absent even when its siblings are set"


@pytest.mark.usefixtures("_with_otlp")
def test_the_forwarder_reads_the_process_rather_than_a_snapshot() -> None:
    """Captured at import, the value would be as old as the worker and a restart-time change invisible."""
    os.environ["OTEL_SERVICE_NAME"] = "silver-to-gold"
    try:
        assert stage_submit.otlp_env()["OTEL_SERVICE_NAME"] == "silver-to-gold"
    finally:
        os.environ["OTEL_SERVICE_NAME"] = "bronze-to-silver"
