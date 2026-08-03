"""Bridge Lance's NATIVE IO metrics into OpenTelemetry.

pylance ≥ 9.0 ships ``lance.otel.instrument_lance_metrics()`` (the ``pylance[otel]`` extra), which
registers Lance's Rust-side object-store/IO metrics as observable instruments on the global
``MeterProvider`` — the one ``opentelemetry-instrument`` already installs in every deployed service,
so the metrics flow through the existing OTLP → Vector → GreptimeDB pipeline with zero new config.

The lock pins pylance 9.0.0, so the bridge is live in catalog / lineage / medallion / compaction; the
import guard stays because a consumer resolving pylance < 9 must degrade to a logged no-op, not fail
startup. Call it once per process, from the lifespan, after the auto-instrumentation wrapper has set
the provider.
"""

from __future__ import annotations

import logging


log = logging.getLogger(__name__)


def instrument_lance_if_available() -> bool:
    """Register Lance-native metrics on the global MeterProvider when pylance ships the bridge.

    Returns whether instrumentation activated. Never raises: metrics are an enhancement — neither a
    missing module (pylance < 9) nor a bridge failure may touch service startup.
    """
    try:
        from lance.otel import instrument_lance_metrics
    except ImportError:
        log.info("lance_metrics_unavailable", extra={"reason": "pylance<9 has no lance.otel bridge"})
        return False
    try:
        instrument_lance_metrics()
    except Exception as exc:
        log.warning("lance_metrics_instrument_failed", extra={"error": str(exc)})
        return False
    log.info("lance_metrics_instrumented")
    return True
