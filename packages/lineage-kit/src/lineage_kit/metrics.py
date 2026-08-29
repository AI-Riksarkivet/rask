"""The series a DROPPED lineage event leaves behind.

This package makes one hard promise — emission never crashes compute — and pays for it by swallowing
every failure. Until this module the whole price of that promise was a `log.warning`: a deployment
whose lineage endpoint had been 401ing for an hour looked, to every dashboard and every alerting
rule, exactly like a deployment that simply had no runs. That is not hypothetical, it is the
2026-07-13 incident recorded in ``LineageSettings.app_token``: every training RunEvent 401'd and the
training provenance vanished, one warning at a time.

CARDINALITY. Two closed label keys and nothing else: ``lance.lineage.reason`` (author | transport)
and ``lance.lineage.state`` (the six OpenLineage run states). A job name, a namespace or an exception
string must NEVER become a label — job names are producer-chosen and unbounded, which is the estate's
own rule (``notifications/api/metrics.py``). Per-event detail belongs on the log line, where it is
indexed rather than multiplied.

API ONLY, never the SDK. ``get_meter`` returns a proxy that binds if and when the host process
installs a ``MeterProvider``, so a Ray driver, a sealed runner and a FastAPI service can all import
this and only the instrumented ones pay anything.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from opentelemetry import metrics as _otel_metrics


if TYPE_CHECKING:  # pragma: no cover — typing only
    from opentelemetry.sdk.metrics import MeterProvider


class DropReason(StrEnum):
    """WHY an event was thrown away. CLOSED — the label vocabulary is the cardinality bound."""

    #: The event could not be turned into the client's wire model: a producer bug, ours to fix.
    AUTHOR = "author"
    #: The client refused or could not deliver it: the endpoint, the credential, or the network.
    TRANSPORT = "transport"


_METRIC_NAME = "lineage.events.dropped"
_METRIC_DESCRIPTION = "Lineage events this process authored and then threw away, by reason — the series a silent lineage outage shows up in."

_meter = _otel_metrics.get_meter("lance.lineage")
_dropped = _meter.create_counter(_METRIC_NAME, unit="{event}", description=_METRIC_DESCRIPTION)


def bind_meter_provider(provider: MeterProvider | None) -> None:
    """Rebind this module's instrument to ``provider`` — for tests, which need an in-memory reader.

    ``None`` restores the module-level proxy meter, so a test cannot leak its provider into the next.
    """
    global _meter, _dropped
    _meter = provider.get_meter("lance.lineage") if provider is not None else _otel_metrics.get_meter("lance.lineage")
    _dropped = _meter.create_counter(_METRIC_NAME, unit="{event}", description=_METRIC_DESCRIPTION)


def record_drop(reason: DropReason, state: str) -> None:
    """Record that one event was dropped. ``state`` is the run state, a closed six-value set."""
    _dropped.add(1, {"lance.lineage.reason": reason.value, "lance.lineage.state": state})
