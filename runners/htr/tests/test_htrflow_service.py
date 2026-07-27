"""Unit tests for HTRFlowViaServeBytes.

The actor calls `serve.get_app_handle("htrflow").transcribe_bytes.remote(...)`
in production. To test in isolation we pass a stub handle through the
constructor — `handle=None` (the default) is what Ray Data uses at runtime
and triggers the real `get_app_handle` lookup; tests pass a fake.
"""

from __future__ import annotations

import numpy as np
import pytest


class _FakeRef:
    def __init__(self, value: str | Exception) -> None:
        self._value = value

    def result(self) -> str:
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


class _FakeRemote:
    def __init__(self, mapping: dict[str, str | Exception]) -> None:
        self._mapping = mapping
        self.calls: list[tuple[bytes, str]] = []

    def remote(self, data: bytes, name: str = "page") -> _FakeRef:
        self.calls.append((data, name))
        return _FakeRef(self._mapping[name])


class _FakeHandle:
    def __init__(self, mapping: dict[str, str | Exception]) -> None:
        self.transcribe_bytes = _FakeRemote(mapping)


def test_emits_output_key_and_alto_xml_for_each_input_row():
    from runner.htrflow_service import HTRFlowViaServeBytes

    handle = _FakeHandle(
        {
            "A0060198/00001.jpg": "<alto>page1</alto>",
            "A0060198/00002.jpg": "<alto>page2</alto>",
        }
    )
    actor = HTRFlowViaServeBytes(handle=handle)

    batch = {
        "key": np.array(["A0060198/00001.jpg", "A0060198/00002.jpg"], dtype=object),
        "image_bytes": np.array([b"JPEG1", b"JPEG2"], dtype=object),
    }
    out = actor(batch)

    assert set(out.keys()) == {"output_key", "alto_xml"}
    assert list(out["output_key"]) == ["A0060198/00001.xml", "A0060198/00002.xml"]
    assert list(out["alto_xml"]) == [b"<alto>page1</alto>", b"<alto>page2</alto>"]


def test_passes_image_bytes_and_key_to_transcribe_bytes():
    from runner.htrflow_service import HTRFlowViaServeBytes

    handle = _FakeHandle({"A/01.jpg": "<alto/>"})
    actor = HTRFlowViaServeBytes(handle=handle)

    actor(
        {
            "key": np.array(["A/01.jpg"], dtype=object),
            "image_bytes": np.array([b"BYTES"], dtype=object),
        }
    )

    assert handle.transcribe_bytes.calls == [(b"BYTES", "A/01.jpg")]


def test_per_row_failure_yields_empty_alto_and_does_not_raise():
    from runner.htrflow_service import HTRFlowViaServeBytes

    handle = _FakeHandle(
        {
            "ok.jpg": "<alto>ok</alto>",
            "bad.jpg": RuntimeError("serve replica blew up"),
        }
    )
    actor = HTRFlowViaServeBytes(handle=handle)

    out = actor(
        {
            "key": np.array(["ok.jpg", "bad.jpg"], dtype=object),
            "image_bytes": np.array([b"A", b"B"], dtype=object),
        }
    )

    assert list(out["output_key"]) == ["ok.xml", "bad.xml"]
    assert list(out["alto_xml"]) == [b"<alto>ok</alto>", b""]


def test_handles_keys_without_extension():
    """`AltoExportActor` does `key.rsplit('.', 1)[0] + '.xml'`. For a key with
    no dot, rsplit returns the whole string — same behavior preserved here."""
    from runner.htrflow_service import HTRFlowViaServeBytes

    handle = _FakeHandle({"plain": "<alto/>"})
    actor = HTRFlowViaServeBytes(handle=handle)

    out = actor(
        {
            "key": np.array(["plain"], dtype=object),
            "image_bytes": np.array([b"X"], dtype=object),
        }
    )

    assert list(out["output_key"]) == ["plain.xml"]


def test_default_constructor_calls_serve_get_app_handle(monkeypatch: pytest.MonkeyPatch):
    """When constructed with handle=None (Ray Data's call path), the actor
    must resolve the handle via `serve.get_app_handle('htrflow')`."""
    from runner import htrflow_service

    captured = {}

    class _StubServe:
        @staticmethod
        def get_app_handle(name: str) -> _FakeHandle:
            captured["name"] = name
            return _FakeHandle({})

    # The actor does `from ray import serve as _serve` inside __init__; we
    # patch the module attribute on ray so the import picks up our stub.
    import ray

    monkeypatch.setattr(ray, "serve", _StubServe, raising=False)

    actor = htrflow_service.HTRFlowViaServeBytes()
    assert captured == {"name": "htrflow"}
    assert isinstance(actor._handle, _FakeHandle)
