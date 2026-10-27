"""Every subscriber on the bus holds a DURABLE JetStream consumer, so a NATS restart does not unsubscribe it.

[[LH-303]] An ephemeral consumer lives only as long as the server that holds it and the client that
attached it. Measured 2026-09-26 on Dapr 1.18.1: NATS scaled to 0 and back left the three durable
consumers on LINEAGE delivering and lineage's ephemeral one gone, and the sidecar never re-created it,
so lineage recorded nothing from the bus until its pod was restarted. A durable consumer persists in the
stream's own storage and resumes where it stopped, on a NATS restart as on a pod restart.

A subscriber is a component carrying a queue group or a deliver policy: the catalog's broadcast takes no queue group,
since every replica hears every event, and each replica holds a durable named for its pod ([[LH-305]]). A
publisher-only component holds no consumer and is outside the rule.
"""

from __future__ import annotations

from tests.unit import chart_render


def _subscriber_components(docs: tuple[dict, ...]) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for doc in docs:
        if doc.get("kind") != "Component" or doc.get("spec", {}).get("type") != "pubsub.jetstream":
            continue
        metadata = {item["name"]: str(item.get("value")) for item in doc["spec"].get("metadata", [])}
        if "queueGroupName" in metadata or "deliverPolicy" in metadata:
            found[doc["metadata"]["name"]] = metadata
    return found


def test_every_subscriber_holds_a_durable_consumer() -> None:
    subscribers = _subscriber_components(chart_render.render(*chart_render.DEFAULT_ARGS))

    assert {"lineage-pubsub-lineage", "catalog-control-pubsub"} <= subscribers.keys(), "the render no longer carries the subscriptions this gate checks"
    ephemeral = sorted(name for name, metadata in subscribers.items() if not metadata.get("durableName"))
    assert not ephemeral, f"these subscribers lose their consumer on a NATS restart: {ephemeral}"
