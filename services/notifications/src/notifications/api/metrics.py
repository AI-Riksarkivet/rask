"""OpenTelemetry domain metrics for the notification plane.

`service_kit.setup_otel` owns the SDK; this module only names counters and increments them. The label
sets are CLOSED vocabularies — `Lane` and `Outcome` are `StrEnum`s rather than free strings, so a
caller cannot mint a new series by passing a value off a payload.

**The bounded-cardinality rule is a security rule here, not a cost rule.** The unit this plane counts
is a person's notification, so the SUBJECT is per-user data: it belongs on spans and log lines, where
access is already governed, and NEVER on a metric label. A counter labelled by subject would publish
the estate's user list into a metrics store that nothing authorizes reads against, and would grow one
series per person while doing it. Every function below takes only enum values for exactly that reason.

Dot-namespaced under the project's `lance.*` attribute convention (the medallion's `record_*` set is
the precedent); in PromQL the dots become underscores.
"""

from enum import StrEnum

from opentelemetry import metrics


class Lane(StrEnum):
    """Which ingress an event arrived on. Two, because the bus provably does not carry every producer:
    the ingest service emits lineage over HTTP only, as do Ray TRAIN and any external OpenLineage
    producer, and those lanes reach lineage's durable feed without ever touching the topic."""

    BUS = "bus"
    FEED = "feed"


class Outcome(StrEnum):
    """What became of one event at the ingress. A closed set, and each member is a decision the
    handler actually makes — `DROPPED` and `RETRIED` are the two Dapr statuses that are not an
    ordinary ack, and `IGNORED` is the ack that means "this was not notification-worthy"."""

    DELIVERED = "delivered"
    #: The natural key was already in the inbox — an at-least-once redelivery, or the same run
    #: arriving on the other lane. Counted rather than silent: it is the evidence the dedupe works.
    DUPLICATE = "duplicate"
    #: The candidate recipient may not see the run's outputs, so they are not told about it.
    HIDDEN = "hidden"
    #: A terminal-state event nobody needed: no verified author, no outputs, or not a terminal state.
    IGNORED = "ignored"
    #: Permanent — an unparseable payload. Redelivery cannot fix it, so the handler DROPs.
    DROPPED = "dropped"
    #: Transient — the actor plane or OpenFGA was unreachable. The sidecar redelivers.
    RETRIED = "retried"


_meter = metrics.get_meter("lance.notifications")

_ingress_events = _meter.create_counter(
    "notifications.ingress.events",
    unit="{event}",
    description="Lineage events seen at an ingress, by lane and outcome.",
)
_fanout_recipients = _meter.create_counter(
    "notifications.fanout.recipients",
    unit="{recipient}",
    description="Per-recipient delivery attempts, by outcome. Never labelled by subject.",
)
_dlq_parked = _meter.create_counter(
    "notifications.dlq.parked",
    unit="{delivery}",
    description="Deliveries DEAD-LETTERED — parked after the sidecar's retry schedule was exhausted.",
)


def record_ingress(lane: Lane, outcome: Outcome) -> None:
    """Count one event at the ingress. Both labels are closed enums — see the module docstring."""
    _ingress_events.add(1, {"lance.notifications.lane": lane.value, "lance.notifications.outcome": outcome.value})


def record_recipient(outcome: Outcome) -> None:
    """Count one recipient's delivery attempt.

    Deliberately NOT labelled by lane: a fan-out is per-recipient and the lane is a property of the
    event, so crossing them would multiply the series for a distinction the event counter already
    carries. And deliberately not by subject — the module docstring says why that one is a rule.
    """
    _fanout_recipients.add(1, {"lance.notifications.outcome": outcome.value})


def record_dead_letter(app_label: str) -> None:
    """Count one parked delivery, by the app that parked it.

    Labelled by THIS app's id, taken from its own DLQ topic. A shared literal made two movers' parks
    indistinguishable in the cascade's counter, and a shared per-subTopic DLQ had two apps subscribed
    to each other's dead letters double-counting every park — per-app naming is what avoids both.
    """
    _dlq_parked.add(1, {"lance.notifications.app": app_label})
