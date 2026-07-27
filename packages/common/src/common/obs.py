"""Shared observability init — raise the APP loggers to INFO so the audit/lifecycle log tier reaches OTLP.

``opentelemetry-instrument`` attaches an SDK ``LoggingHandler(NOTSET)`` to the ROOT logger at process start,
but nothing raises the root/app level from Python's default WARNING — so ``log.info()`` / ``log.debug()`` are
rejected by ``isEnabledFor()`` and the LogRecord is never created, never reaching the OTLP exporter. The
result (found by the 2026-07-13 observability audit): the entire INFO **audit** (``access_denied``,
``openfga_provisioned``, ``fga_tuples_revoked``) + **request-lifecycle** (``medallion_produced``,
``train_requested``, ``ray_stage_job_submitted``, ``compaction_sweep`` …) tier the otel skill mandates at
severity 9 is silently lost; only WARNING/ERROR survive.

Fix: each service calls :func:`configure_app_logging` once at import time (the SDK handler is already on root
by then, since the launcher runs before app import). It raises ONLY the app package loggers to INFO — so the
INFO tier flows to GreptimeDB WITHOUT dragging every dependency's INFO chatter along.
"""

from __future__ import annotations

import logging


#: The top-level app packages whose module loggers (``logging.getLogger(__name__)``) must emit INFO. Their
#: effective level inherits from these package loggers, so raising the package raises the whole tree.
#: ``viewer``/``search``/``annotator`` are the folded lance-media services; ``ratch`` covers its
#: pipeline runs that log through the same process (e.g. ``ratch serve``).
_APP_LOGGERS = (
    "catalog",
    "lineage",
    "medallion",
    "compaction",
    "common",
    "viewer",
    "search",
    "annotator",
    "ratch",
)


def configure_app_logging(level: int = logging.INFO) -> None:
    """Raise the app package loggers to ``level`` (default INFO) so their records reach the OTLP handler."""
    for name in _APP_LOGGERS:
        logging.getLogger(name).setLevel(level)
