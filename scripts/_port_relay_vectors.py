"""The relay vectors of ``gen_port_vectors.py``: what the Python relay does with one staged event.

Every case is put to the real ``lineage.api.relay._drain_outbox`` with one event staged. Listing the outbox, asking who
may write, recording, publishing again, dropping and keeping a refusal are replaced by stand-ins that do what the case
says and note that they were called, so a vector is the order of the effects and what the drain counted.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


_EVENT = {"eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z", "run": {"runId": "r"}, "job": {"namespace": "j", "name": "n"}}
_KEY = "staged-1"


async def _drained(staged: str, authorizing: str, recording: str, publishing: str, dropping: str, keeping: str) -> dict[str, Any]:
    from lance_namespace import PermissionDeniedError

    from lakehouse.cloud_events import lineage_envelope
    from lineage.api import relay
    from lineage.models import ForeignRunError, UnverifiedEventError

    texts = {
        "an event": json.dumps(lineage_envelope(_EVENT).as_wire()),
        "no envelope": "not json",
        "no event": json.dumps(lineage_envelope(_EVENT).as_wire() | {"data": {"eventTime": "t"}}),
    }
    effects: list[str] = []
    counted: list[str] = []

    async def authorize(*_: object) -> frozenset[str]:
        effects.append("authorize")
        if authorizing == "denied":
            raise PermissionDeniedError("can_write_data required on outputs")
        if authorizing == "unverified":
            raise UnverifiedEventError("no listed signer verifies it", "unsigned")
        if authorizing == "unavailable":
            raise ConnectionError("OpenFGA did not answer")
        return frozenset()

    class Store:
        async def record(self, *_: object, **__: object) -> None:
            effects.append("record")
            if recording == "foreign":
                raise ForeignRunError("run r was founded by another subject")
            if recording == "fails":
                raise ConnectionError("the catalog did not answer")

        async def record_refusal(self, *, outbox_key: str, **_: object) -> None:
            effects.append(f"keep {outbox_key}")
            if keeping == "fails":
                raise ConnectionError("the refusals table did not answer")

    async def republish(*_: object) -> None:
        effects.append("republish")
        if publishing == "fails":
            raise ConnectionError("the bus did not answer")

    def drop(_uri: str, _opts: object, key: str) -> None:
        effects.append(f"drop {key}")
        if dropping == "fails":
            raise ConnectionError("the store did not answer")

    settings: Any = SimpleNamespace(outbox_uri="s3://bucket/outbox", outbox_drain_limit=500)
    store: Any = Store()
    app: Any = SimpleNamespace()
    options: Any = {}
    with (
        patch.object(relay.outbox, "measured_backlog", lambda *_: (1, 0.0)),
        patch.object(relay.outbox, "list_events", lambda *_, **__: iter([(_KEY, texts[staged])])),
        patch.object(relay.outbox, "drop_event", drop),
        patch.object(relay, "enforce_bus_authz", authorize),
        patch.object(relay, "republish_staged", republish),
        patch.object(relay, "record_outcome", lambda outcome, *, door: counted.append(outcome.value)),
    ):
        outcome = await relay._drain_outbox(app, store, settings, options, None)
    return {
        "staged": staged,
        "authorizing": authorizing,
        "recording": recording,
        "publishing": publishing,
        "dropping": dropping,
        "keeping": keeping,
        "effects": effects,
        "outcome": outcome.model_dump(),
        "counted": counted,
    }


def relay_vectors() -> dict[str, list[Any]]:
    """Each staged event with the effects the drain made, in order, and what it counted."""
    ways = itertools.product(
        ("allowed", "denied", "unverified", "unavailable"), ("lands", "foreign", "fails"), ("sent", "fails"), ("dropped", "fails"), ("kept", "fails")
    )
    cases = [("an event", *way) for way in ways]
    cases += [(staged, "allowed", "lands", "sent", dropping, "kept") for staged in ("no envelope", "no event") for dropping in ("dropped", "fails")]

    async def every() -> list[dict[str, Any]]:
        return [await _drained(*case) for case in cases]

    return {"key": [_KEY], "drains": asyncio.run(every())}
