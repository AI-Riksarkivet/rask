"""A deterministically refused trigger's payload is published where it can be replayed ([[LH-151]]).

**THE ACK IS DELIBERATELY UNCHANGED, and this file is the reason it can change later.** All eleven
pre-flight refusals still return DROP, which Dapr routes to the dead-letter topic — so today nothing
is lost and `MedallionCascadeDeadLettering` still reads every one as an exhausted delivery. The fix is
to ack SUCCESS instead, and that cannot land first: the cascade's subscribers are `deliverPolicy: new`
(`chart/templates/dapr-component.yaml`), so a SUCCESS there genuinely discards the payload rather than
leaving it replayable the way lineage's `all` subscriber does. Retention first, ack second.

RETENTION HOOKS AT ONE SITE, NOT ELEVEN. `_preflight` returns its verdict rather than acking itself,
so `handle_stage` retains every refusal it produces by construction — a twelfth refusal added inside
`_preflight` cannot forget to retain, which a per-site call would leave to memory.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from dapr.aio.clients import DaprClient

from medallion.core.config import MedallionSettings
from medallion.services.transform import handle_stage


def _settings(**over: Any) -> MedallionSettings:
    base: dict[str, Any] = {
        "MEDALLION_FROM_NAMESPACE": "bronze",
        "MEDALLION_TO_NAMESPACE": "silver",
        "MEDALLION_REFUSED_TOPIC": "refused.bronze-to-silver",
        "MEDALLION_PUBSUB": "lineage-pubsub",
    }
    return MedallionSettings(**{**base, **over})


def _dapr() -> Any:
    d = MagicMock(spec=DaprClient)
    d.publish_event = AsyncMock()
    return d


#: A payload `parse_stage_trigger` cannot read at all — the FIRST refusal in `_preflight`, and the one
#: no redelivery can repair. `{"data": {...}}` with unreadable FIELDS is not this: a trigger that parses
#: goes on to run the stage, so it would prove nothing about the refusal path.
_MALFORMED = {"nope": 1}


@pytest.mark.asyncio
async def test_a_refused_trigger_is_published_to_the_retention_topic() -> None:
    dapr = _dapr()

    verdict = await handle_stage(cast(DaprClient, dapr), _settings(), _MALFORMED)

    assert verdict["status"] == "DROP", "the ack must NOT change until the topic is proven live"
    topics = [c.kwargs.get("topic_name") for c in dapr.publish_event.await_args_list]
    assert "refused.bronze-to-silver" in topics, f"the refusal was not retained; published to {topics}"


@pytest.mark.asyncio
async def test_the_retained_payload_carries_the_reason_and_the_event() -> None:
    """A retained payload that does not say WHY is a replay nobody can triage — the reason is the same
    string `record_refused` counts, so the stream and the metric cannot describe one refusal two ways."""
    dapr = _dapr()

    await handle_stage(cast(DaprClient, dapr), _settings(), _MALFORMED)

    call = next(c for c in dapr.publish_event.await_args_list if c.kwargs.get("topic_name") == "refused.bronze-to-silver")
    body = call.kwargs["data"]
    assert "malformed" in body, body
    assert "bronze->silver" in body, body
    assert "nope" in body, "the original event must be replayable from the retained payload"


@pytest.mark.asyncio
async def test_retention_is_OFF_when_no_topic_is_configured() -> None:
    """Dapr does not auto-create streams, so a publish to an unprovisioned subject FAILS. A deployment
    that has not run the stream job must retain nowhere rather than fail every refusal."""
    dapr = _dapr()

    verdict = await handle_stage(cast(DaprClient, dapr), _settings(MEDALLION_REFUSED_TOPIC=""), _MALFORMED)

    assert verdict["status"] == "DROP"
    assert not [c for c in dapr.publish_event.await_args_list if "refused" in str(c.kwargs.get("topic_name"))]


@pytest.mark.asyncio
async def test_a_FAILED_retention_publish_does_not_change_the_ack() -> None:
    """THE ONE THAT PROTECTS THE CASCADE. A refusal is already decided; letting the retention publish
    raise would hand the sidecar a handler error and therefore a RETRY, turning a NATS hiccup into a
    redelivery storm over an event that can never be accepted. Losing one replay is the cost."""
    dapr = _dapr()
    dapr.publish_event = AsyncMock(side_effect=RuntimeError("jetstream unavailable"))

    verdict = await handle_stage(cast(DaprClient, dapr), _settings(), _MALFORMED)

    assert verdict["status"] == "DROP", "a retention failure must not become a RETRY"
