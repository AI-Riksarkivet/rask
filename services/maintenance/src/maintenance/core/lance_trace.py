"""Bridge Lance's INTERNAL trace events onto OpenTelemetry counters (#62).

``instrument_lance_if_available()`` already ships Lance's OBJECT-STORE metrics (requests, bytes,
errors, throttles, in-flight). What it does not ship is what Lance sees INSIDE a query — the
``lance::execution`` plan counters (``iops``, ``bytes_read``, ``indices_loaded``, ``parts_loaded``)
and the ``lance::io_events`` index loads. Those are exactly the numbers that JUDGE the #102 shared
session in production: if the session is engaging, a warm dataset's second read loads no index parts
and issues fewer IOPS. Without them the cache fix is unfalsifiable in prod.

**Why a callback and not ``LANCE_LOG``.** Lance's Rust log records go to stderr, not Python
``logging``. The maintenance pod carries the ``lance.dev/logs: otlp`` label, and the collector's
``filter/drop_app_file_logs`` processor drops the file-tailed lines that the Python SDK also exported.
Lance's Rust lines are not among them, but they arrive with no scope, no severity and no trace
correlation — a raw stderr string. The honest route to the
warehouse is this one: take the events in-process and re-emit them through the OTLP metrics pipeline
the service already has.

Empirical facts this module is built on (pylance 9.0.0, all measured, not read):

* ``capture_trace_events(cb)`` hands events to ``cb`` on a DEDICATED Lance thread, asynchronously —
  it is a reporting hook, never a synchronization point. Assertions on it must poll with a deadline.
* ``TraceEvent`` carries exactly ``.target`` and ``.args``; every ``.args`` VALUE is a string
  (``{'iops': '21', 'bytes_read': '41425', …}``), so every number is parsed, never read.
* The callback is **NOT** filtered by ``LANCE_LOG``'s target filters — with a narrow ``LANCE_LOG`` the
  callback still receives ``lance::file_audit``. :data:`_TARGETS` is therefore the ONLY filter, and it
  is load-bearing: ``file_audit`` fires once per file created or deleted, i.e. per compacted fragment,
  across every dataset in every bucket every 120s.
* ``lance::io_events`` fires only on an index-cache MISS (measured: a cold filtered read emitted
  ``open_scalar_index`` + ``load_scalar_part``; the same read repeated on the warm handle emitted
  NOTHING). That is why the counter is named for the miss rather than for the event.
* A SECOND ``capture_trace_events`` call **panics** — ``PanicException: Callback already set`` — and
  that panic also poisons the Rust tracing mutex, so the ``atexit shutdown_tracing()`` Lance
  registers then panics too. Worse, ``pyo3_runtime.PanicException`` derives from ``BaseException``,
  NOT ``Exception``: a routine ``except Exception`` would let it escape. Hence :data:`_INSTALLED`
  (one registration per process, ever) and the ``BaseException`` catches below. A lifespan CAN run
  twice in one process (a ``TestClient``, a reload), so "call it once" cannot be left to the caller.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from opentelemetry import metrics


log = logging.getLogger(__name__)

#: Lance's per-plan execution counters — one event per completed plan run.
_EXECUTION = "lance::execution"
#: Index loads. Emitted ONLY on an index-cache miss (measured), which is what makes it the #102 judge.
_IO_EVENTS = "lance::io_events"
#: The allowlist. Matched on ``TraceEvent.target``; everything else returns without touching a counter.
#: Not decorative — see the module docstring on ``lance::file_audit`` volume during a sweep.
_TARGETS = frozenset({_EXECUTION, _IO_EVENTS})

#: ``lance::execution`` args that are counters, in the order they are read. ``index_comparisons``,
#: ``output_rows`` and ``requests`` are deliberately NOT metered: the first two are query-shape
#: numbers rather than IO, and ``requests`` duplicates ``lance_object_store_requests_total``, which
#: ``instrument_lance_if_available()`` already ships. Two series for one quantity is worse than none.
_EXECUTION_ARGS = ("iops", "bytes_read", "indices_loaded", "parts_loaded")

#: The io-event kind (``open_scalar_index``, ``load_scalar_part``, …) — a bounded Rust enum, so it is
#: safe as a metric attribute. The event's ``index_uuid`` and ``part_id`` are NOT: one series per
#: index or per part is precisely the unbounded-attribute failure the otel skill names.
_IO_EVENT_TYPE_ATTR = "lance.io_event.type"


class LanceTraceRecorder:
    """The callback body: filter, parse, count. Never raises — see :meth:`__call__`.

    Split from :func:`install_lance_trace_bridge` on purpose. Registration with Lance is a
    once-per-process, panic-on-repeat global; the recording is pure and takes its meter by argument,
    so it is testable against an in-memory reader without touching a global MeterProvider.
    """

    def __init__(self, meter: metrics.Meter | None = None) -> None:
        # Default = the process meter `setup_otel` wired to OTLP, exactly like `core/metrics.py`.
        m = meter if meter is not None else metrics.get_meter("lance.trace")
        self._execution: dict[str, metrics.Counter] = {
            "iops": m.create_counter(
                "lance.execution.iops",
                unit="{operation}",
                description="IO operations Lance issued while running a query plan.",
            ),
            "bytes_read": m.create_counter(
                "lance.execution.bytes_read",
                unit="By",
                description="Bytes Lance read while running a query plan.",
            ),
            "indices_loaded": m.create_counter(
                "lance.execution.indices_loaded",
                unit="{index}",
                description="Secondary indices a query plan had to LOAD (a warm session loads none).",
            ),
            "parts_loaded": m.create_counter(
                "lance.execution.parts_loaded",
                unit="{part}",
                description="Index parts a query plan had to page in (a warm session pages in none).",
            ),
        }
        #: The #102 judge. One per ``lance::io_events`` event, which Lance emits only when the index
        #: cache MISSED — so a rising rate against a flat sweep cadence means the shared session is
        #: not holding, and a falling one is the fix working.
        self._index_cache_misses = m.create_counter(
            "lance.index_cache.misses",
            unit="{miss}",
            description="Index loads Lance performed because the session's index cache missed.",
        )
        #: Events the callback FAILED on. It runs on Lance's own thread, where a raised exception is
        #: swallowed by design — so without this series a bridge that broke on its first event would
        #: look identical to a service doing no Lance IO at all.
        self._dropped = m.create_counter(
            "lance.trace.events.dropped",
            unit="{event}",
            description="Lance trace events this bridge failed to record (its callback raised).",
        )

    def __call__(self, event: Any) -> None:
        """Record one ``TraceEvent``. MUST NOT RAISE — Lance calls this on its own tracing thread.

        The catch is ``BaseException``, not ``Exception``, and that is not paranoia: every attribute
        read here crosses into pyo3, whose ``PanicException`` derives from ``BaseException``. Swallowing
        ``KeyboardInterrupt``/``SystemExit`` costs nothing on a non-main thread — the signal is
        delivered to the main thread, which this is not.
        """
        try:
            if event.target not in _TARGETS:
                return
            args = event.args
            if event.target == _EXECUTION:
                for name in _EXECUTION_ARGS:
                    _add(self._execution[name], args, name)
            else:
                self._index_cache_misses.add(1, {_IO_EVENT_TYPE_ATTR: str(args.get("type", "unknown"))})
        except BaseException:
            self._drop()

    def _drop(self) -> None:
        """Count a failed event — itself guarded, so the error path cannot resurrect the exception.

        ``BaseException`` and not ``Exception``: this runs on a Lance-owned thread, and letting
        anything at all escape a reporting hook would take the sweep down to record that a metric
        failed to record. ``contextlib.suppress`` rather than ``try``/``except``/``pass`` because
        ruff's SIM105 is in the selected set and the two are equivalent here (no ``else``, no
        ``finally``, one statement in the body).
        """
        with contextlib.suppress(BaseException):
            self._dropped.add(1)


def _add(counter: metrics.Counter, args: Any, key: str) -> None:
    """Add one string-valued Lance arg to ``counter``.

    Zero IS added rather than skipped: adding 0 creates the series, and on a dashboard "this sweep
    loaded no indices" and "the bridge never ran" must not look the same (the ``record_reclaimed``
    doctrine in ``core/metrics.py``). A negative or unparseable value is dropped silently — a counter
    rejects negatives, and a malformed arg is Lance's business, not an outage of ours.
    """
    raw = args.get(key)
    if raw is None:
        return
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return
    if value >= 0:
        counter.add(value)


#: Whether Lance's ONE callback slot has been filled by this process. Never reset — refilling it panics.
_INSTALLED = False
#: The recorder ``_on_trace_event`` currently forwards to; ``None`` means the bridge is off.
_RECORDER: LanceTraceRecorder | None = None


def _on_trace_event(event: Any) -> None:
    """The one and only callback ever handed to Lance.

    A level of indirection, because Lance's slot can be filled exactly once but the service's
    lifespan can run more than once. Rebinding :data:`_RECORDER` is how a second install (or a
    disable) takes effect without a second — panicking — registration.
    """
    recorder = _RECORDER
    if recorder is not None:
        recorder(event)


def install_lance_trace_bridge(*, enabled: bool = True, meter: metrics.Meter | None = None) -> bool:
    """Route Lance's ``execution``/``io_events`` traces into OTel counters. Returns whether it is live.

    Never raises. Telemetry is an enhancement; neither an old pylance without ``lance.tracing`` nor a
    foreign library that already claimed the callback slot may touch service startup.

    ``enabled=False`` unbinds the recorder rather than unregistering — Lance offers no way to release
    the slot, so "off" means the installed dispatcher becomes a no-op. That is a genuine off: the
    events are still produced on Lance's thread, but nothing is parsed and no series are created.
    """
    global _INSTALLED, _RECORDER  # noqa: PLW0603 — Lance's callback slot is process-global; so is this
    if not enabled:
        _RECORDER = None
        log.info("lance_trace_bridge_disabled")
        return False
    # Bound BEFORE registering, so no event can arrive between the two and be dropped on a None recorder.
    _RECORDER = LanceTraceRecorder(meter)
    if _INSTALLED:
        log.info("lance_trace_bridge_rebound")
        return True
    try:
        from lance.tracing import capture_trace_events
    except ImportError:
        _RECORDER = None
        log.info("lance_trace_bridge_unavailable", extra={"reason": "this pylance ships no lance.tracing"})
        return False
    try:
        capture_trace_events(_on_trace_event)
    except BaseException as exc:  # incl. pyo3's PanicException ("Callback already set"), which is NOT an Exception
        _RECORDER = None
        log.warning("lance_trace_bridge_install_failed", extra={"error": str(exc)})
        return False
    _INSTALLED = True
    log.info("lance_trace_bridge_installed", extra={"targets": sorted(_TARGETS)})
    return True
