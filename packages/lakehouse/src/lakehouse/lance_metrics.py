"""Bridge Lance's NATIVE IO metrics into OpenTelemetry.

``lance.otel.instrument_lance_metrics()`` (the ``pylance[otel]`` extra the library declares) registers Lance's Rust-side
object-store/IO metrics as observable instruments on the global ``MeterProvider``, the one ``opentelemetry-instrument``
already installs in every deployed service, so the metrics flow through the existing OTLP export to the platform's OTLP
gateway with no new config. The catalog and maintenance call it once per process, from the lifespan, after the
auto-instrumentation wrapper has set the provider.
"""

from __future__ import annotations

import logging

from lance.otel import instrument_lance_metrics


log = logging.getLogger(__name__)


def register_lance_metrics() -> bool:
    """Register Lance-native metrics on the global MeterProvider, and answer whether the bridge activated.

    Never raises: metrics are an enhancement, and a bridge that fails may not touch service startup.
    """
    try:
        instrument_lance_metrics()
    except Exception as exc:
        log.warning("lance_metrics_instrument_failed", extra={"error": str(exc)}, exc_info=exc)
        return False
    log.info("lance_metrics_instrumented")
    return True


__all__ = ["register_lance_metrics"]
