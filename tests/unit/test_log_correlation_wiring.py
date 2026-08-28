"""Only a pod with the OTel LAUNCHER may tell `LoggingInstrumentor` to skip its handler.

open_fastapi-audit — "`setup_otel` passes `logger_provider=` to `LoggingInstrumentor`, which the
installed 0.65b0 ignores — and `set_logging_format=True` is a no-op, so fleet stdout carries no trace
id" (its third leg).

The finding asks for an invariant that `rask.otelEnv` never renders
`OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED`, "so the skip branch cannot be reached by a copy
from the sibling helper". This gate states the RULE instead of the absence, because the rule is what
makes the absence correct — and it then catches the copy in both directions.

Read in the installed `LoggingInstrumentor._instrument`: when that variable is `"true"` the
instrumentor logs a warning and installs NO `LoggingHandler`, on the assumption that the SDK's own
launcher-installed handler is already active. So the variable is not a preference, it is an assertion
about how the process was started:

* **`opentelemetry-instrument` in the command** — the lance plane. The launcher installs the SDK
  handler, so the instrumentor must stand down or every record ships TWICE.
* **`command: ["uvicorn"]`** — the fleet. Nothing else installs a handler, so telling the instrumentor
  to skip means the OTLP log tier goes silent: records are built and delivered nowhere, with no error.

Both failures are silent, which is why this is a render gate and not a comment. `rask.otelEnv` (fleet)
and `lance.otelEnv` (lakehouse) are sibling helpers that already share a byte-for-byte exclusion list,
so a copy between them is the specific accident worth guarding.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _first_party_deployments, _rendered_docs  # noqa: E402


SKIP_VAR = "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED"
LAUNCHER = "opentelemetry-instrument"


def _otel_containers(docs: list[dict]) -> list[tuple[str, dict, dict]]:
    """(name, container, env) for every first-party container wired to an OTLP endpoint."""
    found = []
    for doc in _first_party_deployments(docs):
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            env = {e["name"]: e.get("value") for e in (container.get("env") or []) if isinstance(e, dict)}
            if "OTEL_EXPORTER_OTLP_ENDPOINT" in env:
                found.append((f"{doc['metadata']['name']}/{container['name']}", container, env))
    return found


_CONTAINERS = _otel_containers(_rendered_docs())

assert _CONTAINERS, "no OTel-wired first-party container rendered — this gate would pass vacuously"


@pytest.mark.parametrize(("name", "container", "env"), _CONTAINERS, ids=[c[0] for c in _CONTAINERS])
def test_the_handler_skip_is_set_EXACTLY_when_a_launcher_installs_one(name: str, container: dict, env: dict) -> None:
    """Iff, not merely if: both directions of this are a silent telemetry fault."""
    launched = LAUNCHER in (container.get("command") or [])
    skipping = (env.get(SKIP_VAR) or "").strip().lower() == "true"

    if launched and not skipping:
        pytest.fail(
            f"{name} runs under {LAUNCHER} but does not set {SKIP_VAR}, so the SDK's handler and "
            "LoggingInstrumentor's handler are both active — every log record ships twice"
        )
    if skipping and not launched:
        pytest.fail(
            f"{name} sets {SKIP_VAR} without running under {LAUNCHER}, so LoggingInstrumentor installs "
            "no handler and nothing else installs one either — the OTLP log tier goes silent with no error"
        )


def test_both_kinds_of_pod_are_actually_represented() -> None:
    """The rule above is vacuous if the render only contains one shape."""
    launched = [n for n, c, _ in _CONTAINERS if LAUNCHER in (c.get("command") or [])]
    bare = [n for n, c, _ in _CONTAINERS if LAUNCHER not in (c.get("command") or [])]
    assert launched, "no launcher-run pod rendered, so the ships-twice direction is untested"
    assert bare, "no bare-uvicorn pod rendered, so the goes-silent direction is untested"
