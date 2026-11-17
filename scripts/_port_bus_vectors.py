"""The bus vectors of ``gen_port_vectors.py``: the CloudEvents envelope each event rides the bus in, and a stream page.

Every case is put to the real Python: ``lakehouse.cloud_events.lineage_envelope`` and ``CloudEvent.as_wire`` for an
OpenLineage event, ``CloudEvent.model_validate_json`` for a staged envelope (the outbox's ``staged_envelope``), the
bus's own ``_body`` for the bytes a publish sends under a given trace context, and ``StreamPage`` for a read by
sequence. Inputs and wire forms are recorded as JSON text, so the Rust reads the bytes Python read and compares the bytes
Python wrote. Nothing here is generated: every id, time and trace context is an input.
"""

from __future__ import annotations

import json
from typing import Any


_T = "2026-11-17T10:00:00Z"
_UUID = "0190a1b2-0000-7000-8000-00000000000a"
_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
_TRACESTATE = "rojo=00f067aa0ba902b7,congo=t61rcWkgMzE"
_TOPIC = "lineage.events.v1"


def _run(state: object = "COMPLETE", **members: Any) -> dict[str, Any]:  # noqa: ANN401 - an event is arbitrary JSON
    return {"eventType": state, "eventTime": _T, "run": {"runId": "run-1"}, "producer": "rask/catalog"} | members


def _output(uuid: object = _UUID) -> dict[str, Any]:
    return {"namespace": "ns", "name": "wh$t", "facets": {"rask_lance": {"tableUuid": uuid}}}


def _lineage_events() -> list[Any]:
    """OpenLineage events as their producers send them, the shapes lineage's doors parse and some they do not."""
    states: list[object] = ["COMPLETE", "FAIL", "ABORT", "START", "RUNNING", "OTHER", "complete", "", None, 7]
    times: list[object] = [
        _T,
        "2026-11-17T10:00:00.5+02:00",
        "2026-11-17T10:00:00.123456789Z",
        "2026-11-17 10:00:00+00:00",
        "2026-11-17T10:00:00",
        "2026-11-17",
        "2026-11-17T10Z",
        "2026-11-17T10:00Z",
        "2026-11-17T1000Z",
        "2026-11-17T10:00:00,5Z",
        "2026-11-17T10:00:00+02",
        "2026-11-17T10:00:00+0230",
        "2026-11-17T10:00:00-00:30",
        "2026-11-17T10:00:00+02:00:30",
        "2026-11-17T10:00:00.000Z",
        "2026-11-17T10:00:00.120Z",
        "2026-02-29T00:00:00Z",
        "2024-02-29T23:59:59.999999Z",
        "2026-11-17T24:00:00Z",
        "2026-11-17T10:00:60Z",
        "2026-11-17t10:00:00z",
        "2026-11-17T10:00:00+24:00",
        "0001-01-01T00:00:00Z",
        "2026-11-17T10:00:00-02:00:30",
        "2026-11-17T10:00:00+02:00:59",
        "2026-11-17T10:00:00+02:00:59.999",
        "2026-12-31T24:00:00+01:00",
        "2026-11-17t10:00:00Z",
        "2026-11-17T10:00:00.12345678x",
        "2026-11-17T10:00:00+02:00x",
        "2026-11-17T10:00:00-0000",
        "2026-11-17T10:0:00Z",
        "2026-11-17T10:00:00.5",
        "2026-11-17T10:00:00.1",
        "2026-11-17T10:00:00.1Z",
        "2026-11-17T10:00:00+05:30",
        "not a time",
        "",
        5,
        None,
    ]
    events: list[Any] = [_run(state) for state in states]
    events += [_run("START", eventTime=time) for time in times]
    events += [_run(eventTime=time) for time in times[:4]]
    events += [
        _run(outputs=[_output()]),
        _run(outputs=[_output(), _output()]),
        _run(outputs=[_output("")]),
        _run(outputs=[_output(5)]),
        _run(outputs=[]),
        _run(outputs={"a": 1}),
        _run(outputs=["text"]),
        _run(producer=""),
        _run(producer=5),
        _run(schemaURL="https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"),
        _run(schemaURL=""),
        _run(run={"runId": 42}),
        _run(run={"runId": True}),
        _run(run={"runId": 1.5}),
        _run(run={"runId": ""}),
        _run(run={"runId": 0}),
        _run(run="r"),
        _run(run={}),
        _run(data={"nested": [1, 1.0, 1e20, 1e-7, 0.0001, -0.0, 123456789012345678, True, None, '\u00e9 \u2028 \U0001f600 "q" \\']}),
        {"run": {"runId": "r"}},
        {"dataset": {"namespace": "ns", "name": "wh$t", "facets": {"rask_lance": {"tableUuid": _UUID, "operation": "append"}}}, "eventTime": _T},
        {"dataset": {"namespace": "ns", "name": "wh$t"}, "eventTime": _T, "producer": "rask/maintenance", "schemaURL": "s"},
        {"dataset": "text", "eventTime": 5},
        {"dataset": {"facets": "x"}},
        {"dataset": {"namespace": 5, "name": None}, "producer": True},
        {"dataset": {}, "run": {"runId": "r"}, "eventType": "COMPLETE"},
        {},
    ]
    return events


def _staged_texts() -> list[str]:
    """Staged envelopes as an outbox holds them, and texts that hold no envelope."""
    whole = {"specversion": "1.0", "datacontenttype": "application/json", "id": "e-1", "source": "rask/catalog", "type": "rask.lineage.run.complete.v1"}
    texts = [json.dumps(whole | {"data": {"a": 1}})]
    for member, values in {
        "id": ["", 5, None],
        "source": ["", None],
        "type": ["", []],
        "subject": ["table:x", "", None, 5],
        "dataschema": ["s", "", None],
        "time": [
            _T,
            "2026-11-17T10:00:00+00:00",
            "2026-11-17T10:00:00.5+02:00",
            "2026-11-17T10:00:00.123456789Z",
            "2026-11-17 10:00Z",
            "2026-11-17t10:00:00z",
            "2026-11-17T10:00:00-00:30",
            "2026-11-17T10:00:00+0200",
            "2026-11-17T10:00:00",
            "2026-11-17",
            "20261117T100000Z",
            "2026-02-29T00:00:00Z",
            "2026-11-17T24:00:00Z",
            "2026-11-17T10:00:60Z",
            "2026-11-17T10:00:00+02:00:59",
            "2026-11-17T10:00:00.5+02",
            "2026-11-17T10:00+02:00",
            "2026-11-17T10Z",
            "2026-11-17T10:00:00.Z",
            "2026-11-17_10:00Z",
            "2026-11-17T10:00:00,5Z",
            "2026-11-17T10:00:00.1234567890123Z",
            "2026-11-17T10:00:00+00:00:00",
            "2026-11-17T10:00:00-00:00",
            "2026-11-17T10:00:00+23:59",
            "2026-11-17T10:00:00+24:00",
            "2026-11-17T10:00:00+2400",
            "2026-11-17T10:00:00+0",
            "0001-01-01T00:00:00Z",
            "9999-12-31T23:59:59Z",
            " 2026-11-17T10:00:00Z",
            "2026-11-17T10:00:00Z ",
            "2026-1-17T10:00:00Z",
            "2026-11-17T1:00:00Z",
            "2026-11-17T10:00:00.5",
            "1e3",
            "0x10",
            "1_000",
            1e20,
            "",
            True,
            None,
        ],
        "traceparent": [
            _TRACEPARENT,
            _TRACEPARENT.upper(),
            "00-00000000000000000000000000000000-b7ad6b7169203331-01",
            "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
            "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            _TRACEPARENT + "-x",
            "",
            None,
        ],
        "tracestate": [
            _TRACESTATE,
            " rojo=1 ,\tcongo=2\t",
            "a@b=c",
            "0a@b=c",
            "0a=c",
            "a=",
            "a= b",
            "a=b ",
            "a=b=c",
            "a=b,",
            "",
            ",".join(f"k{i}=v" for i in range(32)),
            ",".join(f"k{i}=v" for i in range(33)),
            "k=" + "v" * 256,
            "k=" + "v" * 257,
            "k=café",
            None,
        ],
        "data": [{}, {"b": [1.5, None]}, [], None, "x"],
    }.items():
        texts += [json.dumps(whole | {"data": {"a": 1}} | {member: value}) for value in values]
    texts += [
        json.dumps({key: value for key, value in whole.items() if key != "id"} | {"data": {}}),
        json.dumps(whole),
        '{"id": "e", "source": "s", "type": "t", "data": {"x": 1}, "data": {"y": 2}}',
        '{"id": "e", "source": "s", "type": "t", "data": {"x": 1, "y": 2, "x": 3}}',
        '{"id": "e", "source": "s", "type": "t", "data": {}, "extra": 1}',
        '{"id": "e", "source": "s", "type": "t", "data": {"n": 1.0, "m": 1E3, "k": -0.0, "j": 12}}',
        '[{"id": "e"}]',
        '"text"',
        "null",
        "{",
        "",
    ]
    return texts


def _lineage_cases() -> list[dict[str, Any]]:
    from lakehouse.cloud_events import lineage_envelope

    cases = []
    for event in _lineage_events():
        text = json.dumps(event)
        envelope = lineage_envelope(json.loads(text))
        cases.append({"event": text, "wire": json.dumps(envelope.as_wire())})
    return cases


def _parse_cases() -> list[dict[str, Any]]:
    from pydantic import ValidationError

    from lakehouse.cloud_events import CloudEvent

    cases = []
    for text in _staged_texts():
        try:
            envelope = CloudEvent.model_validate_json(text)
        except ValidationError:
            cases.append({"text": text, "wire": None})
            continue
        cases.append({"text": text, "wire": json.dumps(envelope.as_wire())})
    return cases


def _body_cases() -> list[dict[str, Any]]:
    """The bytes a publish sends: the envelope's own trace dropped, and the publish's trace context, where it runs in one."""
    from lakehouse.bus import _body
    from lakehouse.cloud_events import CloudEvent
    from lakehouse.context import continuing

    staged = '{"id": "e", "source": "s", "type": "t", "subject": "table:x", "time": "2026-11-17T10:00:00Z", "data": {"a": "\\u00e9"}}'
    traced = json.dumps(json.loads(staged) | {"traceparent": "00-11111111111111111111111111111111-2222222222222222-00", "tracestate": "x=y"})
    cases = []
    for text in (staged, traced, '{"id": "e", "source": "s", "type": "t", "data": {}}'):
        envelope = CloudEvent.model_validate_json(text)
        for traceparent, tracestate in ((None, None), (_TRACEPARENT, None), (_TRACEPARENT, _TRACESTATE), (_TRACEPARENT, "a= b ,")):
            with continuing(traceparent, tracestate):
                body = _body(envelope, _TOPIC)
            cases.append({"envelope": text, "traceparent": traceparent, "tracestate": tracestate, "body": body.decode()})
    return cases


def _trace_of_cases() -> list[dict[str, Any]]:
    from lakehouse.cloud_events import CloudEvent

    fresh = CloudEvent.model_validate_json('{"id": "e", "source": "s", "type": "t", "data": {}, "tracestate": "a=b"}')
    staged = CloudEvent.model_validate_json(f'{{"id": "f", "source": "s", "type": "t", "data": {{}}, "traceparent": "{_TRACEPARENT}"}}')
    return [{"envelope": fresh.model_dump_json(), "staged": staged.model_dump_json(), "wire": json.dumps(fresh.with_trace_of(staged).as_wire())}]


def _page_cases() -> dict[str, list[dict[str, Any]]]:
    from lakehouse.bus import StreamPage
    from lakehouse.bus_delivery import Delivery

    places = [
        {"after": after, "end": end, "limit": limit, "first": StreamPage.first_place(after, end, limit)}
        for after in (0, 1, 5, 9, 10, 11)
        for end in (0, 1, 10)
        for limit in (1, 3, 20)
    ]
    found: list[list[int]] = [[], [2], [6], [7, 8], [10], [11]]
    pages = [
        {
            "after": after,
            "end": end,
            "sequences": sequences,
            "gap": StreamPage.of(after, end, [Delivery(subject="s", body=b"", sequence=s, attempt=1) for s in sequences]).gap,
        }
        for after in (0, 1, 5, 10, 12)
        for end in (0, 5, 10)
        for sequences in found
    ]
    return {"places": places, "pages": pages}


def bus_vectors() -> dict[str, list[Any]]:
    """Each event with its envelope, each staged text with the envelope read from it, each publish's bytes, and pages."""
    from lakehouse.bus import MAX_PAYLOAD_BYTES, WARN_PAYLOAD_BYTES

    return {
        "limits": [{"max_payload_bytes": MAX_PAYLOAD_BYTES, "warn_payload_bytes": WARN_PAYLOAD_BYTES}],
        "lineage": _lineage_cases(),
        "parses": _parse_cases(),
        "bodies": _body_cases(),
        "trace_of": _trace_of_cases(),
    } | _page_cases()
