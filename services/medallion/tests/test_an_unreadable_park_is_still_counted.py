"""A dead-letter park is counted whether or not its bytes are a CloudEvent ([[LH-151]] clause 2).

**THE CLASS THAT WAS INVISIBLE.** A body the sidecar cannot read as a CloudEvent fails deserialization
on the SOURCE topic, parks, and then fails deserialization AGAIN on the dead-letter topic — so the
handler never ran, `record_dead_letter` never fired, and the DLQ stream and `medallion_dlq_parked_total`
disagreed by exactly the messages nobody could see.

MEASURED on the deployed bronze-to-silver stage runner 2026-09-19, before this: publishing
`this-is-not-a-cloudevent-at-all` to `medallion.bronze` moved DLQ 2,522 -> 2,523 while the app logged
nothing but health probes, and the sidecar logged `error deserializing cloud event` TWICE — once for
`medallion.bronze` and once for `dlq.bronze-to-silver`.

`rawPayload` is what makes the bytes arrive whatever they are. The trade is that a normal park now
arrives as the raw CloudEvent, so the envelope is parsed here — best-effort, because those fields are
an enrichment and failing to read them must not re-create the silence.
"""

from __future__ import annotations

import base64
import json

from medallion.api.dlq import _envelope


_PARKED = {"id": "evt-1", "topic": "medallion.bronze", "data": {"token": "tok-1"}}


def test_a_plain_cloudevent_is_read() -> None:
    """The control: the normal park must keep every field it had before rawPayload."""
    env = _envelope(json.dumps(_PARKED).encode())

    assert env is not None
    assert env["id"] == "evt-1"
    assert env["data"]["token"] == "tok-1"


def test_a_cloudevent_wrapped_by_dapr_is_unwrapped() -> None:
    """Dapr's rawPayload delivery carries the original bytes one level in, as `data`."""
    env = _envelope(json.dumps({"id": "outer", "data": json.dumps(_PARKED)}).encode())

    assert env is not None and env["id"] == "evt-1"


def test_a_base64_wrapped_cloudevent_is_unwrapped() -> None:
    """The binary spelling of the same delivery — Dapr uses `data_base64` for non-text payloads."""
    outer = {"id": "outer", "data_base64": base64.b64encode(json.dumps(_PARKED).encode()).decode()}

    env = _envelope(json.dumps(outer).encode())

    assert env is not None and env["id"] == "evt-1"


def test_BYTES_THAT_ARE_NOT_A_CLOUDEVENT_return_None_rather_than_raising() -> None:
    """THE ONE THE ROW IS ABOUT. `None` is what the route turns into `undeserializable: true` — the
    field that makes this class visible. Raising here would abort the handler and restore the silence,
    which is the whole defect."""
    assert _envelope(b"this-is-not-a-cloudevent-at-all") is None


def test_INVALID_UTF8_returns_None_rather_than_raising() -> None:
    """A truly binary park — the case a JSON-only guard misses, because the decode fails before the
    parse does."""
    assert _envelope(b"\xff\xfe\x00\x01") is None


def test_a_json_scalar_is_not_mistaken_for_an_envelope() -> None:
    """Valid JSON is not the same as a CloudEvent: a bare number parses and has no fields to read, so
    it must be reported as unreadable rather than as a park with every field empty."""
    assert _envelope(b"42") is None
    assert _envelope(b'"a string"') is None


def test_a_json_object_without_an_id_is_not_claimed_as_an_envelope() -> None:
    """The guard against the quiet wrong answer: an object that happens to parse but carries no `id`
    is not a CloudEvent, and reporting it as one would log a park with a null id that an operator
    cannot tell from a real one."""
    assert _envelope(b'{"unrelated": true}') is None
