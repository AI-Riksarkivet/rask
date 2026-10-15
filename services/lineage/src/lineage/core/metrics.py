"""OpenTelemetry domain metrics for the lineage ingest path.

Auto-instrumentation gives generic HTTP server/client metrics; these are the *business* golden
signals you'd actually alert on — how many lineage events we ingest vs. drop vs. retry, and how long
ingest takes. They go out via the OTel SDK (activated by ``opentelemetry-instrument``) **OTLP-direct to
GreptimeDB** (no Collector — mirrors rask), queryable in PromQL / Perses.

Cardinality is bounded on purpose (the otel skill's #1 cost driver): the processed-events counter carries only the bounded,
namespaced ``lance.lineage.outcome`` and the equally bounded ``lance.lineage.door``, and the signature counters carry the
signing identity (a member of the chart's signer set, so as many series as the estate has signers) or a refusal reason (a
closed set) — per-run /
per-table identifiers belong on spans and logs, never on metric attributes. (Custom attributes use the project's dot-namespaced `lance.*` convention —
deliberately NOT the otel skill's reverse-DNS letter, pinned in todo_fable; in PromQL the dots become
underscores → ``lance_lineage_outcome``.)
``metrics.get_meter`` returns a proxy that binds lazily, so creating the instruments at import time
(before ``opentelemetry-instrument`` installs the real MeterProvider) is safe.
"""

from __future__ import annotations

from enum import StrEnum
from typing import get_args

from opentelemetry import metrics

from lineage_kit.signing import RefusalReason


_meter = metrics.get_meter("lance.lineage")

_events_processed = _meter.create_counter(
    "lineage.events.processed",
    unit="{event}",
    description="Lineage events processed by the Dapr subscriber, by outcome.",
)
_ingest_duration = _meter.create_histogram(
    "lineage.ingest.duration",
    unit="s",
    description="Wall-clock seconds to record one event in the feed and the edge list (successful records only).",
    # Second-scale bucket boundaries. WITHOUT this the SDK default explicit buckets apply, and those are
    # millisecond-tuned (0, 5, 10, 25, … 10000) — a real ~10ms–2s ingest lands entirely in the first [0,5s]
    # bucket, so histogram_quantile() (the p95 panel) reads a flat ~5 for every quantile (obs audit
    # 2026-07-13). The advisory is honoured by the SDK when no View overrides the instrument.
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)


_forwarded = _meter.create_counter(
    "lineage.forward.events",
    unit="{event}",
    description="Events the forwarder POSTed to the configured graph backend, by outcome: taken, or refused by the backend.",
)
_forward_stalled = _meter.create_counter(
    "lineage.forward.stalled",
    unit="{tick}",
    description="Relay ticks whose forward stopped short of the feed's head: the backend was down or refused the forwarder.",
)


def record_forwarded(*, forwarded: int, refused: int, stalled: bool) -> None:
    """Count one tick's forward. Every series is added to, at 0 too, so each exists from the first tick a backend is set."""
    _forwarded.add(forwarded, {"lance.lineage.outcome": "forwarded"})
    _forwarded.add(refused, {"lance.lineage.outcome": "refused"})
    _forward_stalled.add(1 if stalled else 0)


_signature_verified = _meter.create_counter(
    "lineage.signature.verified",
    unit="{event}",
    description="Events whose rask_signature a listed signer's published Ed25519 key verified, by the signing identity.",
)
_signature_refused = _meter.create_counter(
    "lineage.signature.refused",
    unit="{event}",
    description=(
        "Events refused because no listed signer's published key verifies their signature, by reason. A refusal is acked, "
        "so this count is the only trace it leaves."
    ),
)


def record_signature_verified(identity: str) -> None:
    """Count one event verified for ``identity``. Bounded: only an identity the chart lists is ever verified."""
    _signature_verified.add(1, {"lance.lineage.identity": identity})


def create_signature_refused_series() -> None:
    """Create every reason series of `lineage.signature.refused` at 0.

    The alert over this counter is `rate(...) > 0`, and a cumulative counter exports nothing for a series before its first
    add: a series born at 1 has no 0 before it, so `rate()` sees no increase and the first refusal never pages. Called
    when the bus door is registered, at import, after `opentelemetry-instrument` installed the MeterProvider at
    interpreter start: an add through the proxy meter before then is dropped. Both measured on opentelemetry-sdk 1.44.0.
    """
    for reason in get_args(RefusalReason.__value__):
        _signature_refused.add(0, {"lance.lineage.reason": reason})


def record_signature_refused(reason: str) -> None:
    """Count one refused event. ``reason`` is `lineage_kit.signing.RefusalReason`, a closed set, never the event's own text."""
    _signature_refused.add(1, {"lance.lineage.reason": reason})


class Outcome(StrEnum):
    """The bounded set of terminal outcomes for one delivered event (the only metric attribute)."""

    INGESTED = "ingested"  # graph write committed → Dapr SUCCESS
    # A bus event the stamped subject was not authorized to record (§ E2). Its OWN value rather than
    # `DROPPED`, though both ack the same way: the two send an operator to different places — a refusal
    # is an authorization question about a producer, a drop is a schema question about its payload — and
    # a refusal is the one that can be a silent, deliberate loss, so it gets its own alert.
    REFUSED = "refused"  # authorization denied → Dapr DROP (redelivery cannot grant a permission)
    RETRIED = "retried"  # transient failure → Dapr RETRY (sidecar redelivers)
    DEAD_LETTERED = "dead_lettered"  # exhausted the resiliency schedule → parked, and the feed lacks the run
    # Parked, but the feed ALREADY holds the run — visibility, not loss. Its own value because
    # `DEAD_LETTERED` is read as the terminal-loss signal and a re-park of a recorded run is not loss:
    # a replay of the deliverPolicy=all ingest consumer re-reads up to the stream's retention and
    # re-parks whatever still fails, while deterministic run ids mean a stage that runs
    # again heals the gap. Measured against the live estate 2026-09-13: 8,515 parked deliveries of
    # `lineage.events.v1` against a stream whose last sequence was 5,860, and 17 of 21 distinct parked
    # run ids already recorded.
    PARKED_ALREADY_RECORDED = "parked_already_recorded"
    # UNREPAIRABLE, and therefore the one outcome that ACKS a run the feed does not hold: a payload
    # that does not parse, or a run carrying no author at all. No tuple, redelivery or restart can
    # change that answer, so parking it writes a dead-letter duplicate on every roll — measured
    # 2026-09-18, 37 of 44 refusals in one hour were one unauthored run id re-presented at pod start.
    #
    # ITS OWN VALUE BECAUSE THE ACK IS A DELIBERATE, SILENT DISCARD. `REFUSED` still means a named
    # person who lacks a grant, which parks and can be repaired by writing the tuple; this one cannot be
    # repaired at all and leaves nothing behind but this count, which is what makes the count the safety
    # argument for acking rather than a statistic. A producer regression shows up here as a rising line
    # and nowhere else.
    UNREPAIRABLE = "unrepairable"
    # RECOVERED FROM THE DEAD-LETTER STREAM ([[LH-148]]): a parked delivery the feed did NOT hold, which
    # the parking route re-presented to the ingest path and the feed then did. Its own value because it
    # is the exact inverse of `DEAD_LETTERED` — the same population, measured after a second attempt
    # rather than before one — and reading them as one number would hide the only thing an operator wants
    # to know, which is whether the parking lane is losing provenance or repairing it.
    #
    # MEASURED, NEVER INFERRED FROM AN ACK. `handle_cloud_event` answers SUCCESS both for a committed
    # write and for an unrepairable discard, so the route asks the feed a second time instead: a run it
    # lacked and now holds was recovered. Anything else stays `DEAD_LETTERED`.
    RECOVERED = "recovered"
    # AN EVENT NO LISTED SIGNER'S PUBLISHED KEY VERIFIES ([[LH-064]]): unsigned, forged, or signed by an
    # identity the chart does not list. ACKED rather than parked, like `UNREPAIRABLE` and for the same
    # reason: a redelivery cannot add a signature to bytes already published, so a DROP would write a
    # dead-letter copy per restart about an event that can never be recorded. Its own value because the
    # question it sends an operator to answer is different (who is publishing to the topic without a key
    # the estate provisions?) and `lineage.signature.refused` carries the reason that count cannot.
    SIGNATURE_REFUSED = "signature_refused"


class Door(StrEnum):
    """WHICH ingress an event arrived through — the counter's second and last attribute.

    All three write one counter from ONE pod, so no resource or instance attribute separates them, and
    each fails for a different reason and is repaired in a different place. Without this an operator
    woken by a `refused` rate learned that provenance was lost and nothing about where to look.
    """

    #: The Dapr subscription off `lineage.events.v1` — an in-cluster producer with a sidecar.
    SUBSCRIBER = "subscriber"
    #: `POST /api/v1/lineage`, the OpenLineage HTTP transport. The ONLY door for a producer with no
    #: sidecar: the whole Ray lane, every runner, and any external OpenLineage producer.
    HTTP = "http"
    #: The dead-letter handler — not an arrival but a giving-up, and an operator reads it as neither of
    #: the above.
    DEAD_LETTER = "dead_letter"


def record_outcome(outcome: Outcome, *, door: Door) -> None:
    """Increment the processed-events counter for ``outcome`` at ``door``.

    ``door`` is keyword-only WITH NO DEFAULT on purpose: a default would let a new call site silently
    inherit another door's label, which is the exact confusion this attribute exists to remove, and a
    wrong label is worse than a missing one because it reads as an answer.
    """
    _events_processed.add(1, {"lance.lineage.outcome": outcome.value, "lance.lineage.door": door.value})


def record_ingest_duration(seconds: float) -> None:
    """Record one successful-ingest latency sample."""
    _ingest_duration.record(seconds)
