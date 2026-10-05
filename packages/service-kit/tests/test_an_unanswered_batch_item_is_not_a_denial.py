"""A batch item OpenFGA could not answer is an outage, never a denial.

OpenFGA's BatchCheck answers each item with EITHER a verdict OR an error (a per-check timeout, a
resolution too complex, a relation the pinned model lacks), and the SDK hands an errored item back as
``allowed=False`` beside a ``CheckError``. A gate that reads only ``allowed`` turns every such item into
a deny: a catalog batch route answers 403, a notification row is hidden, a train trigger is dropped,
and each is recorded as a decision. :func:`fga.check` answers the same faults with a 503.

The doubles below speak the SDK's own response classes, and the last test drives a real
``OpenFgaClient`` against a server answering OpenFGA's wire shape, so the double cannot drift from
what the SDK actually returns.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientTuple
from openfga_sdk.client.models.batch_check_response import ClientBatchCheckResponse
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models.check_error import CheckError

from service_kit.governed import fga


_NO_WAIT: dict[str, Any] = {"retry_backoff_seconds": 0.0, "retry_max_backoff_seconds": 0.0}

#: Two per-item faults, each as OpenFGA reports it.
_MISSING_RELATION = CheckError(input_error="validation_error", message="relation 'table#can_nope' not found")
_TIMED_OUT = CheckError(internal_error="deadline_exceeded", message="context deadline exceeded")


class _BatchOpenFga:
    """An OpenFGA whose BatchCheck answers from a script, one scripted round per call.

    Each round maps object -> a verdict or a ``CheckError``, returned through the SDK's own
    ``ClientBatchCheckResponse``: an errored item carries ``allowed=False`` exactly as the SDK sets it.
    """

    def __init__(self, *rounds: dict[str, bool | CheckError]) -> None:
        self._rounds = list(rounds)
        self.calls = 0
        self.asked: list[str] = []

    async def batch_check(self, body: ClientBatchCheckRequest, options: dict[str, Any] | None = None) -> ClientBatchCheckResponse:
        del options
        answers = self._rounds[min(self.calls, len(self._rounds) - 1)]
        self.calls += 1
        result = []
        for index, item in enumerate(body.checks):
            self.asked.append(item.object)
            answer = answers[item.object]
            error = answer if isinstance(answer, CheckError) else None
            # The SDK hands the batch ITEM back as `request` (client.py `map_response`); its annotation says ClientTuple.
            result.append(ClientBatchCheckSingleResponse(allowed=answer is True, request=cast("ClientTuple", item), correlation_id=f"c{index}", error=error))
        return ClientBatchCheckResponse(result)


def _ask(client: _BatchOpenFga, objects: list[str], *, attempts: int = 1) -> dict[str, bool]:
    return asyncio.run(
        fga.batch_check(cast("OpenFgaClient", client), user="alice", relation="can_read_data", objects=objects, retry_attempts=attempts, **_NO_WAIT)
    )


@pytest.mark.parametrize("fault", [_MISSING_RELATION, _TIMED_OUT], ids=["missing-relation", "timed-out"])
def test_an_item_openfga_could_not_answer_is_a_503_not_a_deny(fault: CheckError, caplog: pytest.LogCaptureFixture) -> None:
    """One errored item fails the call closed even when its sibling was answered cleanly, and the log
    line names the item and the code, since the 503's body is constant by design."""
    caplog.set_level(logging.ERROR, logger=fga.log.name)
    client = _BatchOpenFga({"table:answered": True, "table:unanswered": fault})

    with pytest.raises(ServiceUnavailableError):
        _ask(client, ["table:answered", "table:unanswered"])

    [record] = [r for r in caplog.records if r.getMessage() == "openfga_batch_check_unavailable"]
    assert record.exc_info is not None
    logged = str(record.exc_info[1])
    code = fault.input_error or fault.internal_error
    assert "table:unanswered" in logged and str(code) in logged, logged
    assert "table:answered" not in logged, logged


def test_a_clean_batch_still_answers_every_object() -> None:
    """The contract the fix keeps: a genuine deny is still ``False``, not an error."""
    client = _BatchOpenFga({"table:a": True, "table:b": False})
    assert _ask(client, ["table:a", "table:b"]) == {"table:a": True, "table:b": False}


def test_a_server_side_item_fault_is_retried_like_a_5xx() -> None:
    """``check`` retries a 5xx, so a batch whose only faults are server-side is asked again, and a
    clean second answer is the answer."""
    client = _BatchOpenFga({"table:a": True, "table:b": _TIMED_OUT}, {"table:a": True, "table:b": True})

    assert _ask(client, ["table:a", "table:b"], attempts=3) == {"table:a": True, "table:b": True}
    assert client.calls == 2


@pytest.mark.parametrize("fault", [_MISSING_RELATION], ids=["missing-relation"])
def test_an_input_item_fault_is_not_retried_like_a_4xx(fault: CheckError) -> None:
    """An input fault recurs on every attempt, as a 400 does for ``check``: one call, then the 503."""
    client = _BatchOpenFga({"table:a": True, "table:b": fault, "table:c": _TIMED_OUT})

    with pytest.raises(ServiceUnavailableError):
        _ask(client, ["table:a", "table:b", "table:c"], attempts=3)
    assert client.calls == 1


_STORE_ID = "01HVMMBCMGZNT3SED4Z17ECXCA"
_MODEL_ID = "01HVMMBD0B0BN8ZX5A9EZFYRWQ"


async def _wire_batch_check(request: web.Request) -> web.Response:
    """OpenFGA's BatchCheck wire answer: ``check_result`` is a oneof, so an errored item has no ``allowed``."""
    assert request.match_info["store_id"] == _STORE_ID
    body = await request.json()
    result: dict[str, Any] = {}
    for check in body["checks"]:
        if check["tuple_key"]["object"] == "table:unanswered":
            result[check["correlation_id"]] = {"error": {"input_error": "validation_error", "message": "relation 'table#can_read_data' not found"}}
        else:
            result[check["correlation_id"]] = {"allowed": True}
    return web.json_response({"result": result})


def _over_the_wire(objects: list[str]) -> dict[str, bool]:
    async def drive() -> dict[str, bool]:
        app = web.Application()
        app.router.add_post("/stores/{store_id}/batch-check", _wire_batch_check)
        async with TestServer(app) as server:
            client = fga.make_client(str(server.make_url("")).rstrip("/"), _STORE_ID, _MODEL_ID, token_file=None)
            try:
                return await fga.batch_check(client, user="alice", relation="can_read_data", objects=objects, retry_attempts=1, **_NO_WAIT)
            finally:
                await client.close()

    return asyncio.run(drive())


def test_the_sdk_hands_a_wire_item_error_to_the_gate() -> None:
    """The real SDK, from the real wire shape: the double above is only as good as this agreement."""
    with pytest.raises(ServiceUnavailableError):
        _over_the_wire(["table:answered", "table:unanswered"])


#: Object ids OpenFGA v1.18.3 refuses, measured through Dagger: the first five per item, the rest for
#: the whole request.
_NOT_OBJECT_IDS = {
    "second-colon": "table:s3://images/run1",
    "hash": "table:a#b",
    "control-char": "table:file//scan\x0b01",
    "empty-id": "table:",
    "empty-type": ":db1$a",
    "space": "table:file//my scans",
    "257-runes": "table:" + "d" * 251,
}


@pytest.mark.parametrize("obj", list(_NOT_OBJECT_IDS.values()), ids=list(_NOT_OBJECT_IDS))
def test_an_object_openfga_cannot_parse_is_not_granted_and_never_sent(obj: str) -> None:
    """No tuple can name such an object, so False is its answer; sent, it would fail its item, or the
    whole request, for every object beside it."""
    client = _BatchOpenFga({"table:db1$a": True})

    assert _ask(client, ["table:db1$a", obj]) == {"table:db1$a": True, obj: False}
    assert client.asked == ["table:db1$a"]


def test_a_batch_of_only_unparseable_objects_asks_nothing() -> None:
    client = _BatchOpenFga({})

    assert _ask(client, [_NOT_OBJECT_IDS["hash"]]) == {_NOT_OBJECT_IDS["hash"]: False}
    assert client.calls == 0


def test_the_longest_object_openfga_accepts_is_still_asked() -> None:
    longest = "table:" + "d" * 250
    client = _BatchOpenFga({longest: True})

    assert _ask(client, [longest]) == {longest: True}
