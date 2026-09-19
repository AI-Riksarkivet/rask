"""A deterministically refused trigger's payload is published where it can be replayed ([[LH-151]]).

**THE ACK FOLLOWS THE EVIDENCE, NOT THE CONFIGURATION.** A deterministic refusal acks SUCCESS only
when its payload is provably on the retention topic; otherwise it keeps the DROP that parks it. The
cascade's subscribers are `deliverPolicy: new` (`chart/templates/dapr-component.yaml`), so SUCCESS
genuinely DISCARDS — unlike lineage's `all` subscriber, where the retained stream re-presents it. An
estate with no `MEDALLION_REFUSED_TOPIC`, or a broker that refused the publish, must behave exactly as
before: park, and be paged. An unnecessary park costs a duplicate; a premature SUCCESS costs the
payload, and only one of those is recoverable.

RETENTION HOOKS AT ONE SITE, NOT ELEVEN. `_preflight` returns its verdict rather than acking itself,
so `handle_stage` retains every refusal it produces by construction — a twelfth refusal added inside
`_preflight` cannot forget to retain, which a per-site call would leave to memory.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dapr.aio.clients import DaprClient

from medallion.core.config import MedallionSettings
from medallion.services import transform
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

    topics = [c.kwargs.get("topic_name") for c in dapr.publish_event.await_args_list]
    assert "refused.bronze-to-silver" in topics, f"the refusal was not retained; published to {topics}"
    assert verdict["status"] == "SUCCESS", "a retained refusal must stop parking — that is the whole row"
    assert verdict.get("reason") == "malformed", "the wire must still say WHICH refusal it was"


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

    assert not [c for c in dapr.publish_event.await_args_list if "refused" in str(c.kwargs.get("topic_name"))]
    assert verdict["status"] == "DROP", "with nowhere to retain, SUCCESS would DISCARD the payload on a deliverPolicy=new subscriber"


@pytest.mark.asyncio
async def test_a_FAILED_retention_publish_falls_back_to_parking() -> None:
    """THE ONE THAT PROTECTS THE PAYLOAD. A broker that refused the publish leaves nothing to replay,
    so SUCCESS would discard the event outright — the ack falls back to the DROP that parks it. And it
    must not become a RETRY either: the refusal is deterministic, so redelivery is a storm over an
    event that can never be accepted."""
    dapr = _dapr()
    dapr.publish_event = AsyncMock(side_effect=RuntimeError("jetstream unavailable"))

    verdict = await handle_stage(cast(DaprClient, dapr), _settings(), _MALFORMED)

    assert verdict["status"] == "DROP", "a failed retention publish must fall back to parking, never SUCCESS or RETRY"


@pytest.mark.asyncio
async def test_a_TRANSIENT_failure_is_never_acked_as_a_refusal() -> None:
    """THE GUARD THAT PROTECTS RECOVERABLE WORK, and the bug it exists for was real.

    `_preflight` does not only answer refusals. `_authorize` returns RETRY when FGA is UNREACHABLE —
    transient, and the whole point is that the sidecar redelivers. An ack rule keyed on "the verdict
    is not a StagePreflight" flips that to SUCCESS too, and on a `deliverPolicy: new` subscriber
    SUCCESS discards: an FGA blip would silently drop triggers instead of retrying them.

    So the flip is keyed on DROP specifically, and a RETRY is neither retained nor re-acked — the
    event is coming back, so a retention copy would duplicate something nothing lost.
    """
    dapr = _dapr()

    with patch.object(transform, "_preflight", AsyncMock(return_value={"status": "RETRY"})):
        verdict = await handle_stage(cast(DaprClient, dapr), _settings(), _MALFORMED)

    assert verdict["status"] == "RETRY", "a transient authorization outage must still redeliver"
    assert not [c for c in dapr.publish_event.await_args_list if "refused" in str(c.kwargs.get("topic_name"))], (
        "a RETRY is not a refusal — retaining it duplicates an event that is coming back"
    )
