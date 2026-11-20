"""OpenTelemetry domain metrics for the #4 transactional lineage outbox (OTLP-direct to GreptimeDB).

WHY (docs/adr/0003-p1-1-outbox-observability-the-four-signals.md P1.1, the four signals): the outbox is an external boundary (S3 + pub/sub) that
carried ZERO of the four golden signals — a direct violation of
`python-infrastructure/references/observability.md`, which requires
them "for every external boundary (HTTP, DB, queue, cache)". Concretely: every durability property #4
claims was **unobservable**. An outbox that silently stops draining — the one failure that loses lineage
forever — looked exactly like an outbox that is working. The audit ranked this the highest-ROI fix.

The signals, mapped to the four:
  · Traffic  — staged / published / dropped counters (the happy path's rate)
  · Errors   — publish_failed (event left staged = the crash window doing its job) + poison_dropped
  · Saturation — DEPTH and OLDEST-AGE gauges: the alertable pair. Depth > 0 sustained means the relay is
    not draining; oldest-age bounds how much lineage is at risk. These are what a human is actually paged on.
  · Latency  — the publish itself is timed where it is made (`lakehouse.events.bus`: `messaging.client.operation.duration`).

Bounded cardinality (observability.md): NO run_id / dataset / token labels — those are unbounded and belong
on spans and logs, not metric attributes. Every series carries one attribute, its lane.

EACH OUTBOX IS A LANE OF ITS OWN (`OutboxLane`): the lineage outbox, which the catalog and maintenance stage into and
lineage's relay drains, and the control outbox, which each service's control emitter stages into and the catalog's
control relay drains. One process can work both (the catalog stages into each), so the resource's `service.name` cannot
say which outbox a point is about; the `lance.outbox.lane` attribute does, and an alert about one lane selects it.
"""

from __future__ import annotations

from typing import Final, Literal

from opentelemetry import metrics


#: The outboxes the lakehouse keeps, and the closed set of the `lance.outbox.lane` attribute.
type OutboxLane = Literal["lineage", "control"]

_LANE: Final = "lance.outbox.lane"

_meter = metrics.get_meter("lance.outbox")

_staged = _meter.create_counter(
    "outbox.events.staged",
    unit="{event}",
    description="Lineage events written to the object-store outbox BEFORE the publish (the durable copy).",
)
_published = _meter.create_counter(
    "outbox.events.published",
    unit="{event}",
    description="Lineage events whose publish was acked (the staged copy is then dropped).",
)
_publish_failed = _meter.create_counter(
    "outbox.publish.failed",
    unit="{event}",
    description=(
        "Publishes that failed AFTER staging — the event survives on object storage for the reconcile "
        "relay. Non-zero is the crash window working as designed, NOT data loss; sustained non-zero with a "
        "rising depth is the real alarm."
    ),
)
_stage_failed = _meter.create_counter(
    "outbox.stage.failed",
    unit="{event}",
    description=(
        "Stages that failed, after which the event was published ANYWAY without a durable copy. This is "
        "the only path that delivers with the crash window open: `staged` never counts it, `publish_failed` "
        "is gated on having staged, and depth/oldest-age describe objects that exist. Non-zero means the "
        "object store refused a write the lineage lane did not need in order to deliver."
    ),
)
_drop_failed = _meter.create_counter(
    "outbox.drop.failed",
    unit="{event}",
    description=(
        "Published events whose staged copy could not be deleted: by the publisher on ack, or by the catalog's control "
        "relay after it republished one. Delivered, so nothing is lost: the copy is delivered again and dropped by a "
        "later relay tick. Sustained non-zero with a rising depth means the store refuses the deletes, and the relay "
        "is re-delivering every event left staged."
    ),
)
_drained = _meter.create_counter(
    "outbox.events.drained",
    unit="{event}",
    description="Staged survivors the relay tick re-ingested into the lineage feed.",
)
_stranded = _meter.create_counter(
    "outbox.events.stranded",
    unit="{event}",
    description=(
        "Staged events a tick FAILED on and deliberately LEFT staged — a store outage, an expired "
        "credential, a catalog that did not answer. Distinct from poison_dropped: the object is intact and "
        "is retried next tick. Distinct from refused: that one is a settled governance answer rather than "
        "a failure, and counting the two together made this number report an outage that was not happening."
    ),
)
_refused = _meter.create_counter(
    "outbox.events.refused",
    unit="{event}",
    description=(
        "Staged events lineage refuses to record — a producer staged provenance it is not authorized to "
        "write. Deterministic, so every tick re-refuses the same events and the count is a floor rather "
        "than a rate: it means N events need a human, NOT that the relay is failing. Split out because "
        "`drained=0 stranded=6` held for 2.1 days on a healthy relay and read as a wedged one."
    ),
)
_poison_dropped = _meter.create_counter(
    "outbox.events.poison_dropped",
    unit="{event}",
    description=(
        "Staged objects discarded so they cannot wedge the drain: an unparseable object, or on the catalog's "
        "control lane one that does not verify as the catalog's own. Any non-zero value warrants investigation: "
        "a malformed event was written upstream, or something other than the catalog wrote under its outbox."
    ),
)

# --- Saturation: the alertable pair. An ObservableGauge is the right instrument (observability.md: "pool/
# queue depth ... ObservableGauge for resource usage") — the relay OBSERVES the prefix on each tick rather
# than the outbox trying to track a distributed count itself. Registered LAZILY on the first backlog
# observation: the publishing services import this module too (for the counters), and import-time gauges made
# every one of those pods export a constant depth=0/oldest_age=0 forever, diluting any unfiltered
# aggregation over the one series that carries truth — the relay's (audit 2026-07-15).
_gauges_registered = False
#: Each lane's backlog as this process measured it on the relay's last window, as (depth, oldest age in seconds). A
#: lane is absent while this process holds no measurement of it: only the replica that ran the tick does, and only
#: while its last tick could measure.
_backlogs: dict[OutboxLane, tuple[float, float]] = {}


def _observe_depth(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [metrics.Observation(depth, {_LANE: lane}) for lane, (depth, _age) in _backlogs.items()]


def _observe_oldest_age(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [metrics.Observation(age, {_LANE: lane}) for lane, (_depth, age) in _backlogs.items()]


def _register_gauges() -> None:
    global _gauges_registered
    if _gauges_registered:
        return
    _gauges_registered = True
    _meter.create_observable_gauge(
        "outbox.depth",
        callbacks=[_observe_depth],
        unit="{event}",
        description=(
            "Events currently staged and undrained, by lane. THE alert signal: sustained > 0 means the lane's "
            "relay is not draining and its events are accumulating at risk."
        ),
    )
    _meter.create_observable_gauge(
        "outbox.oldest_age",
        callbacks=[_observe_oldest_age],
        unit="s",
        description=(
            "Age of the OLDEST staged event, by lane. Bounds how much is at risk and how long the relay has "
            "been failing — depth alone cannot distinguish a brief spike from a stuck relay."
        ),
    )


def create_series(lane: OutboxLane) -> None:
    """Create every counter's series of ``lane`` at 0.

    An alert over a counter reads its increase, and a cumulative counter exports nothing for a series before its first
    add: a series born at 1 has no 0 before it, so the first failure shows no increase and never pages. Called by what
    stages into a lane or drains it, when it starts.
    """
    for counter in (_staged, _published, _publish_failed, _stage_failed, _drop_failed, _drained, _stranded, _refused, _poison_dropped):
        counter.add(0, {_LANE: lane})


def record_staged(lane: OutboxLane) -> None:
    _staged.add(1, {_LANE: lane})


def record_published(lane: OutboxLane) -> None:
    _published.add(1, {_LANE: lane})


def record_publish_failed(lane: OutboxLane) -> None:
    _publish_failed.add(1, {_LANE: lane})


def record_stage_failed(lane: OutboxLane) -> None:
    _stage_failed.add(1, {_LANE: lane})


def record_drop_failed(lane: OutboxLane) -> None:
    _drop_failed.add(1, {_LANE: lane})


def record_drained(lane: OutboxLane, count: int) -> None:
    """Count what one tick delivered out of ``lane``'s outbox; a tick that delivered nothing adds 0."""
    _drained.add(count, {_LANE: lane})


def record_stranded(lane: OutboxLane, count: int) -> None:
    """Count what one tick failed on and left staged in ``lane``'s outbox; a tick that stranded nothing adds 0."""
    _stranded.add(count, {_LANE: lane})


def record_refused(lane: OutboxLane, count: int) -> None:
    """Count what one tick found refused in ``lane``'s outbox; a tick that refused nothing adds 0.

    The alert that guards on this series must be able to read a zero: a rule written as "depth > 0 and no refusals"
    evaluates to nothing at all while the refused series does not exist.
    """
    _refused.add(count, {_LANE: lane})


def record_poison_dropped(lane: OutboxLane) -> None:
    _poison_dropped.add(1, {_LANE: lane})


def observe_backlog(lane: OutboxLane, depth: int, oldest_age_seconds: float) -> None:
    """Publish the saturation snapshot ``lane``'s relay measured this tick.

    Called on EVERY relay tick — including a tick that finds an EMPTY outbox, which is what makes
    ``depth`` fall back to 0 instead of going stale at its last non-zero reading. A gauge that only reports
    when non-zero would alert forever after a single transient spike.
    """
    _register_gauges()  # first observation = the measuring process; only it exports the series
    _backlogs[lane] = (float(depth), float(oldest_age_seconds))


def forget_backlog(lane: OutboxLane) -> None:
    """Report no backlog of ``lane`` from this process until it measures one again.

    The relay's timer runs each tick on one replica, so the replica that ran the last one holds the truth and every
    other replica's snapshot is from an older window: one that measured a backlog during an outage would report it for
    as long as others won the windows since, over a prefix long drained. A replica that finds the window another's
    calls this, and so does a tick that could not measure (the store did not answer the listing): its last reading says
    nothing about the outbox now, and an empty one read before an outage would report 0 throughout it. The series it
    exported ends.
    """
    _backlogs.pop(lane, None)


__all__ = [
    "OutboxLane",
    "create_series",
    "forget_backlog",
    "observe_backlog",
    "record_drained",
    "record_drop_failed",
    "record_poison_dropped",
    "record_publish_failed",
    "record_published",
    "record_refused",
    "record_stage_failed",
    "record_staged",
    "record_stranded",
]
