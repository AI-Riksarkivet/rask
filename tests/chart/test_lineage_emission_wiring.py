"""Emission must be REAL in a rendered production config — not merely configured somewhere.

A lineage plane fails silently by construction: every emitter in this repo logs and swallows on the
error path, so a misrouted event produces a green deployment, a healthy pod, and an empty graph.
Nothing about the running system distinguishes "no lineage was emitted" from "nothing has happened
yet". That is why the wiring has to be asserted at render time rather than observed at runtime.

The lakehouse's producers publish on the bus, each on a connection of its own: the catalog and maintenance on the topic
their ``<APP>_LINEAGE_TOPIC`` names, and lineage republishes a recovered event on ``RASK_LINEAGE_TOPIC``. What takes
them is a durable consumer the stream Job declares, filtered to one subject: lineage's ingest lane, and maintenance's
write-event lane. A publisher's topic and a consumer's filter are each rendered from ``pubsub.topic``, and a template
that stopped deriving one of them from it would publish where no consumer reads while every pod stays healthy. A
producer outside the estate posts OpenLineage to lineage's HTTP door with its own transport (any OpenLineage client,
configured by that producer), which this chart renders nothing for.

Asserted against the render rather than values.yaml, because the rendered manifest is what runs, and with the topic
moved off its default, so two equal defaults cannot pass for one derivation.
"""

from __future__ import annotations

from lineage.api import ingest_lane
from maintenance.services import queue
from tests.chart.chart_render import DEFAULT_ARGS, bus_consumers, containers, env_of, render


#: Every env var that names the topic a service publishes lineage on: the catalog's (each committed write), maintenance's
#: (its passes) and lineage's (a recovered event, published again).
_PUBLISHER_TOPICS = ("RASK_LINEAGE_TOPIC", "RASK_LINEAGE_TOPIC", "RASK_LINEAGE_TOPIC")

#: The consumers that take the topic: lineage's ingest lane and maintenance's write-event lane.
_CONSUMERS = ((ingest_lane.STREAM, ingest_lane.CONSUMER), (queue.ARRIVALS_STREAM, queue.ARRIVALS_CONSUMER))


def test_every_lineage_publisher_renders_the_topic_the_declared_consumers_take() -> None:
    docs = render(*DEFAULT_ARGS, "--set", "pubsub.topic=lineage.events.moved")

    published = {name: {env[name] for _, _, container in containers(docs) if name in (env := env_of(container))} for name in _PUBLISHER_TOPICS}
    taken = {key: bus_consumers(docs)[key]["filter_subject"] for key in _CONSUMERS}

    assert published == {name: {"lineage.events.moved"} for name in _PUBLISHER_TOPICS}
    assert taken == dict.fromkeys(_CONSUMERS, "lineage.events.moved")
