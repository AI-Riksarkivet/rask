"""The delivery vectors of ``gen_port_vectors.py``: how the Python lane settles a delivery.

Every case is put to the real ``lakehouse.bus_delivery.judged`` with a handler and a park that do what the case says
(answer, fail, or outlast the handling time), and the settlement and the reason a park was given are recorded.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any


#: The ack wait the cases run under: the lane keeps 10 s of it for the settle, so a handling has 50 ms.
_ACK_WAIT_SECONDS = 10.05
_RETRY_SECONDS = 7.0


async def _settled(attempts: int, attempt: int, handling: str, parking: str) -> dict[str, Any]:
    from lakehouse.bus_delivery import Delivery, Lane, Verdict, judged

    lane = Lane(stream="S", consumer="c", attempts=attempts, retry_seconds=_RETRY_SECONDS, ack_wait_seconds=_ACK_WAIT_SECONDS)
    reasons: list[str] = []

    async def handle(_: Delivery) -> Verdict:
        if handling == "fails":
            raise ConnectionError("the catalog did not answer")
        if handling == "outlasts":
            await asyncio.sleep(1)
        return Verdict(handling) if handling in {"taken", "again", "park"} else Verdict.TAKEN

    async def park(_: Delivery, reason: str) -> None:
        reasons.append(reason)
        if parking == "fails":
            raise ConnectionError("the refusals table did not answer")
        if parking == "outlasts":
            await asyncio.sleep(1)

    settlement = await judged(Delivery(subject="s", body=b"{}", sequence=1, attempt=attempt), lane, handle, park)
    return {
        "attempts": attempts,
        "attempt": attempt,
        "handling": handling,
        "parking": parking,
        "outcome": settlement.outcome.value,
        "delay_seconds": settlement.delay_seconds,
        "park_reason": reasons[0] if reasons else None,
    }


async def _all() -> list[dict[str, Any]]:
    cases = [
        (attempts, attempt, handling, parking)
        for attempts in (1, 3)
        for attempt in range(1, attempts + 2)
        for handling, parking in itertools.product(("taken", "again", "park", "fails", "outlasts"), ("keeps", "fails", "outlasts"))
    ]
    return [await _settled(*case) for case in cases]


#: What authorizing an event comes to, by name, and the refusal the Python raises for it.
_AUTHORIZING = ("no check", "allowed", "denied", "unverified", "unauthored", "foreign", "ungoverned", "unavailable")
_RECORDING = ("lands", "foreign", "fails")
_EVENT = {"eventType": "COMPLETE", "eventTime": "t", "run": {"runId": "r"}, "job": {"namespace": "j", "name": "n"}}


async def _handled(body: object, authorizing: str, recording: str) -> dict[str, Any]:
    from unittest.mock import patch

    from lance_namespace import PermissionDeniedError

    from lineage.core.metrics import Door
    from lineage.models import ForeignRunError, UnauthoredRunError, UngovernedOutputError, UnverifiedEventError
    from lineage.services import consumer

    refusals = {
        "denied": PermissionDeniedError("can_write_data required on outputs"),
        "unverified": UnverifiedEventError("no listed signer verifies it", "unsigned"),
        "unauthored": UnauthoredRunError("the event names no author"),
        "foreign": ForeignRunError("the run is another subject's"),
        "ungoverned": UngovernedOutputError("the output is no table of this catalog"),
    }

    async def authorize(*_: object) -> frozenset[str]:
        if authorizing in refusals:
            raise refusals[authorizing]
        if authorizing == "unavailable":
            raise ConnectionError("OpenFGA did not answer")
        return frozenset()

    class Store:
        async def record(self, *_: object, **__: object) -> None:
            if recording == "foreign":
                raise ForeignRunError("run r was founded by another subject")
            if recording == "fails":
                raise ConnectionError("the catalog did not answer")

    counted: list[str] = []
    with patch.object(consumer, "record_outcome", lambda outcome, *, door: counted.append(outcome.value)):
        store: Any = Store()
        verdict = await consumer.handle_cloud_event(store, body, None if authorizing == "no check" else authorize, door=Door.SUBSCRIBER)
    return {"body": body, "authorizing": authorizing, "recording": recording, "verdict": verdict.value, "counted": counted}


async def _all_handled() -> list[dict[str, Any]]:
    unread: list[object] = [{"data": {"eventTime": "t"}}, {"data": None}, {}, None, "text"]
    cases = [await _handled(body, "allowed", "lands") for body in unread]
    cases += [await _handled({"data": _EVENT}, authorizing, recording) for authorizing in _AUTHORIZING for recording in _RECORDING]
    return cases


def delivery_vectors() -> dict[str, list[Any]]:
    """Each delivery with how the lane settles it, and each envelope with whether the lane reads it."""
    from lakehouse.bus_delivery import envelope_of

    def nested(levels: int) -> str:
        return '{"a": ' * (levels - 1) + '{"s": "]}\\"{["}' + "}" * (levels - 1)

    bodies = ['{"id": "1", "data": {}}', "[1, 2]", '"text"', "null", "{", "", nested(2), nested(300), nested(301), "[" * 300 + "]" * 300]
    return {
        "limits": [{"ack_wait_seconds": _ACK_WAIT_SECONDS, "retry_seconds": _RETRY_SECONDS}],
        "settlements": asyncio.run(_all()),
        "handlings": asyncio.run(_all_handled()),
        "envelopes": [{"body": body, "read": envelope_of(body.encode()) is not None} for body in bodies],
    }
