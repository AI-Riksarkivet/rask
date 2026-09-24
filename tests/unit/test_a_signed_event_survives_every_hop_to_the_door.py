"""A signature is verified against the bytes that arrived, and EVERY hop has to hand them over.

[[LH-064]]. The door's own suite proves the gate; it cannot prove that its callers reach it with the
right document. Both hops that do were measured ungated: replacing the arrived mapping with
``event.model_dump(by_alias=True)`` in ``services/consumer.py`` and in the outbox relay left the whole
estate green, because every signature test above them drives the gate directly.

WHY A DUMP IS A DIFFERENT DOCUMENT. ``RunEvent``'s facet bags are ``Field(default_factory=dict)``, so a
model built from an event that carried no ``job.facets`` dumps one anyway. The canonical body then
differs from the one the producer signed and the HMAC fails — for an honest producer. The cost is not
symmetric between the two hops: on the bus a refusal is a ``_DROP`` onto the dead-letter topic, which
``deliverPolicy: all`` re-parks on every roll; on the relay it is a refusal that leaves the event
staged forever, and the drain is the only durable copy a crashed publish has.

The fixtures here deliberately OMIT the facet bags. An event that happens to carry them round-trips
losslessly and would pass either way, so a fixture built by a helper that fills them in would assert
nothing — the shape a double must carry is the shape that can fail.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from lineage.api import fga_deps, reconcile_cron
from lineage.models import DatasetEvent, RunEvent
from lineage.services.consumer import handle_cloud_event
from lineage_kit.signing import attach_signature, signature_of
from service_kit.lakehouse import outbox


KEY = "FGjbWnUx1oRd7TqYpE4sLvZc0MhA6iK2eB9wQn3t"
IDENT = "service-medallion"


def _wire(run_id: str) -> dict[str, Any]:
    """An event as a producer puts it on the wire — with NO facet bags on the job or the outputs.

    That omission is the whole fixture: those are the three fields the model defaults, so an event
    carrying them cannot tell a door reading the arrived bytes from one reading a re-serialisation.
    """
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-09-24T06:00:00Z",
        "producer": "https://rask/medallion",
        "run": {"runId": run_id, "facets": {"author": {"sub": IDENT}}},
        "job": {"namespace": "lance", "name": "stage.silver"},
        "outputs": [{"namespace": "lance", "name": "silver$features"}],
    }


def _signed(run_id: str) -> dict[str, Any]:
    event = attach_signature(_wire(run_id), key=KEY, identity=IDENT)
    # ANTI-VACUITY. Both tests below assert that an event is ADMITTED, and an UNSIGNED event is admitted
    # too — so a fixture that quietly stopped signing would leave them green while testing nothing.
    # Measured: replacing this call with the bare payload passed both.
    assert signature_of(event) is not None, "the fixture is unsigned, so neither test below discriminates"
    return event


def _arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the door this identity's real key, and stop it at the output check.

    The output check is a different question with its own tests; leaving it live here would let an
    FGA-shaped refusal masquerade as a signature failure and make this file pass for the wrong reason.
    """

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(fga_deps, "dedicated_token_from_store", lambda _store: lambda identity: KEY if identity == IDENT else None)
    monkeypatch.setattr(fga_deps, "enforce_output_authz", _allow)


class _Repo:
    def __init__(self) -> None:
        self.ingested: list[str] = []

    async def record_refusal(self, **_k: Any) -> None:
        return None

    async def ingest_event(self, ev: Any) -> None:  # noqa: ANN401 — the drain's and consumer's own shape
        self.ingested.append(ev.run.run_id)


def _request() -> Any:  # noqa: ANN401 — the drain takes a Request and reads only app.state
    return cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))


# --------------------------------------------------------------------------- #
# HOP 1 — the bus subscriber
# --------------------------------------------------------------------------- #


def test_the_CONSUMER_hands_the_door_the_bytes_that_arrived(monkeypatch: pytest.MonkeyPatch) -> None:
    """An honest producer's event must reach the graph, not the dead-letter topic.

    IF THIS IS RED the consumer is authorizing a reconstruction of the event instead of the event, and
    every signing producer is being parked. The ack is the assertion that matters: a DROP here is the
    sidecar being asked to route the message to `deadLetterTopic`, once per delivery and again on every
    roll, while the log line blames a producer that did nothing wrong.
    """
    _arm(monkeypatch)
    settings = cast("Any", SimpleNamespace(fga_enabled=True, dapr_secret_store="lance-secrets"))
    repo = _Repo()

    async def authorize(parsed: RunEvent | DatasetEvent, arrived: Mapping[str, Any]) -> None:
        await fga_deps.enforce_bus_authz(parsed, _request(), settings, arrived)

    status = asyncio.run(handle_cloud_event(cast("Any", repo), {"data": _signed("0198e0f2-1b2c-7a3d-8e4f-000000000001")}, authorize))

    assert status == {"status": "SUCCESS"}, f"an honestly signed event was refused at the bus door: {status}"
    assert len(repo.ingested) == 1, "the event never reached the graph"


# --------------------------------------------------------------------------- #
# HOP 2 — the outbox relay
# --------------------------------------------------------------------------- #


def _settings(uri: str) -> Any:  # noqa: ANN401 — a stand-in for the drain's settings protocol
    class _S:
        outbox_uri = uri
        outbox_drain_limit = 500
        dapr_pubsub = "lineage-pubsub"
        dapr_topic = "lineage.events.v1"
        dapr_publish_timeout_seconds = 5.0
        dapr_secret_store = "lance-secrets"
        fga_enabled = True

    return _S()


def test_the_OUTBOX_RELAY_verifies_the_STAGED_BYTES(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A staged event must drain, and the relay is where getting this wrong costs the most.

    A refusal here does not retry and does not park: it leaves the object staged and counts it, forever,
    on an answer that is deterministic. The outbox exists so a committed write's provenance survives a
    crashed publish, so the one path built to recover provenance would be the one that stops recovering
    it the moment producers start signing.
    """
    _arm(monkeypatch)
    uri = f"file://{tmp_path}/outbox"
    event = _signed("0198e0f2-1b2c-7a3d-8e4f-000000000002")
    outbox.stage_event(uri, {}, event["run"]["runId"], json.dumps(event))

    repo = _Repo()
    outcome = asyncio.run(reconcile_cron._drain_outbox(_request(), cast("Any", repo), _settings(uri), {}))

    assert outcome.refused == 0, "the relay refused an honestly signed staged event"
    assert outcome.drained == 1, f"the staged event never drained: {outcome}"
    assert len(repo.ingested) == 1
    assert list(outbox.list_events(uri, {})) == [], "a drained event must not stay staged"
