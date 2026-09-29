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

import pytest

from medallion.services import stage_submit


@pytest.fixture
def _with_otlp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "bronze-to-silver")


@pytest.mark.usefixtures("_with_otlp")
def test_a_value_the_lane_HAS_is_still_forwarded() -> None:
    """The fix must not become "stop forwarding" — a configured lane has to behave exactly as before."""
    forwarded = stage_submit.otlp_env()

    assert forwarded["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4318"
    assert forwarded["OTEL_SERVICE_NAME"] == "bronze-to-silver"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in forwarded, "an unset name must stay absent even when its siblings are set"
