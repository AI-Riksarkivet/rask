"""OpenFGA's answers as the SDK hands them to rask: the server's JSON decoded by the SDK's own deserializer.

A client double that serves an answer builds it here, from the JSON the server sends, so what rask reads is the SDK's
model of that answer: its field names, its types (a timestamp arrives as a ``datetime``), and its refusal of a body that
leaves out a field the API requires. An object holding only the attributes one reader looks at agrees with whatever
that reader assumes; this one is the shape the SDK hands over on the estate.

The synchronous client's deserializer is the same generated code as the asynchronous one's, and needs no running loop.
"""

from __future__ import annotations

import json
from typing import Any

from openfga_sdk.sync.api_client import ApiClient


__all__ = ["TIMESTAMP", "answer", "tuple_json"]

#: When every tuple a double serves was written: the API requires one, and no reader here judges it.
TIMESTAMP = "2026-07-29T09:37:21Z"

_DECODER = ApiClient()


class _Wire:
    """The response body the deserializer reads, as the HTTP layer hands it over."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.data = json.dumps(payload)


def answer[Answer](payload: dict[str, Any], kind: type[Answer]) -> Answer:
    """``payload``, the JSON body of an OpenFGA answer, as the SDK's model ``kind`` of it."""
    return _DECODER.deserialize(_Wire(payload), kind)


def tuple_json(user: str, relation: str, obj: str) -> dict[str, Any]:
    """One stored tuple as a ``Read`` answer lists it."""
    return {"key": {"user": user, "relation": relation, "object": obj}, "timestamp": TIMESTAMP}
