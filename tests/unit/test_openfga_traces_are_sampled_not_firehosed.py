"""OpenFGA traces reach the Collector, and they are SAMPLED before they get there.

[[XC-047]]'s remaining half. The metrics scrape answers "is authz slow" in aggregate; a trace answers
"what did THIS request do", and until now OpenFGA emitted none — so a Check that was slow inside a
catalog request showed up as unexplained latency in the parent span and nothing below it.

SIZE THE RATIO IN SPANS, NOT REQUESTS — the mistake this gate now pins. Sizing off the request rate
(9.4/s, measured) says 0.05 is modest. It is not: OpenFGA emits a span PER GRAPH EDGE and PER
DATASTORE READ, so one Check is ~1,900 spans. Enabled at 0.05 on the live estate this component
produced **532,192 spans in ten minutes — 25x the whole rest of the estate combined** (lineage 21.9k,
catalog 20.2k, maintenance 5.6k). That is the `ray_data_*` firehose again, one signal over. It was
created and reverted inside the hour, and the chart default is now OFF.

SO TRACING IS A DEBUGGING TOOL HERE, not a standing signal. The metrics scrape answers "is authz slow"
continuously and completely; a trace answers "what did THIS Check walk", which is worth enabling
deliberately and at a ratio picked against spans — 0.001 lands near 18 spans/s, the catalog's own
order.

THE HEADER THE ROW ASKED FOR IS NOT OPENFGA'S TO SEND. XC-047 says to point the endpoint at the
Collector "with the `x-greptime-pipeline-name=greptime_trace_v1` header". Measured: the Collector's own
`otlphttp/greptime_traces` exporter already sets that header (`otel-collector.yaml:486`), because it is
a GreptimeDB INGESTION requirement, not an OTLP one. OpenFGA speaks plain OTLP to the Collector and the
Collector adds it downstream — which is the whole point of the Collector being the single seam.
"""

from __future__ import annotations

import yaml

from tests.unit.test_invariants import _helm_template


def _openfga_env() -> dict[str, str]:
    """The OpenFGA Deployment's env, as {name: value}."""
    for doc in yaml.safe_load_all(_helm_template()):
        if not doc or doc.get("kind") != "Deployment":
            continue
        if "openfga" not in doc["metadata"]["name"]:
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            env = {e["name"]: str(e.get("value", "")) for e in container.get("env") or [] if "value" in e}
            if any(k.startswith("OPENFGA_") for k in env):
                return env
    return {}


def test_the_openfga_deployment_renders_at_all() -> None:
    """Without this the assertions below pass by reading an empty env."""
    assert _openfga_env(), "no OpenFGA Deployment with OPENFGA_* env rendered — this gate checks nothing"


def test_tracing_is_OFF_by_default() -> None:
    """The measured default. At 0.05 this one component outran the whole estate 25 to 1, so the chart
    must not ship it on — an operator turns it on while diagnosing and turns it back off."""
    # ABSENT, not "false": the subchart wraps the whole var in `{{- if .Values.telemetry.trace.enabled }}`
    # (openfga/templates/…:599-602), so OFF renders no env at all. Asserting `== "false"` would be a gate
    # that can only pass in a state the chart cannot produce.
    enabled = _openfga_env().get("OPENFGA_TRACE_ENABLED")

    assert enabled != "true", "OpenFGA tracing ships ENABLED; one Check is ~1,900 spans and the default must be off"


def test_the_endpoint_is_the_COLLECTOR_so_enabling_it_needs_no_other_edit() -> None:
    """Off by default is only useful if turning it on is one flag. The endpoint must already be right,
    and it must be the Collector — a component exporting straight to the store bypasses every
    processor and the estate's single telemetry seam with it."""
    endpoint = _openfga_env().get("OPENFGA_TRACE_OTLP_ENDPOINT", "")

    assert "otel-collector" in endpoint, f"traces would go to {endpoint!r} rather than the Collector"


def test_the_sample_ratio_is_SIZED_IN_SPANS() -> None:
    """The number that was wrong the first time. 0.05 measured 532,192 spans/10min live — 25x the rest
    of the estate — because a Check is ~1,900 spans, not one. Anything at or above that is the
    firehose, so the ceiling is set an order below it rather than merely under 1.0."""
    ratio = _openfga_env().get("OPENFGA_TRACE_SAMPLE_RATIO", "")
    assert ratio, "no sample ratio set — OpenFGA traces everything it is asked to"

    assert 0.0 < float(ratio) <= 0.005, (
        f"sample ratio is {ratio!r}; 0.05 was measured at 532,192 spans per ten minutes, so this must "
        "be sized against SPANS (~1,900 per Check) rather than against the 9.4 req/s rate"
    )


def test_openfga_does_not_send_the_greptime_pipeline_header() -> None:
    """The row asked for it and it belongs to the Collector, which already sets it on its own exporter.
    A component setting a STORE's ingestion header is a component that has bypassed the seam."""
    env = _openfga_env()

    assert not [k for k, v in env.items() if "greptime" in k.lower() or "greptime" in v.lower()], (
        f"OpenFGA carries a GreptimeDB-specific setting: {[(k, v) for k, v in env.items() if 'greptime' in (k + v).lower()]}"
    )
