"""Unit tests for HTRFlowViaServeBytes.

The actor calls `serve.get_app_handle("htrflow").transcribe_bytes.remote(...)`
in production. To test in isolation we pass a stub handle through the
constructor — `handle=None` (the default) is what Ray Data uses at runtime
and triggers the real `get_app_handle` lookup; tests pass a fake.
"""

from __future__ import annotations

import typing

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


def test_the_http_ingress_transcribes_the_posted_bytes() -> None:
    """#88 step 3: POST raw bytes → ALTO. Before this the route existed and answered Serve's
    default 405 — a door with no handler — so the medallion's Ray-free lane could not reach the
    warm weights at all. The heavy pipeline is faked; the ingress CONTRACT is what's under test."""
    import asyncio

    from runner.htrflow_service import HTRFlowDeployment

    cls = HTRFlowDeployment.func_or_class
    deployment = cls.__new__(cls)  # skip __init__ — no model load in a unit test
    seen: dict[str, object] = {}

    def fake_transcribe(data: bytes, name: str = "page") -> str:
        seen.update(data=data, name=name)
        return "<alto/>"

    deployment.transcribe_bytes = fake_transcribe  # type: ignore[method-assign]

    class _Request:
        query_params: typing.ClassVar[dict[str, str]] = {"name": "vol/00012.jpg"}

        @staticmethod
        async def body() -> bytes:
            return b"JPEGBYTES"

    response = asyncio.run(deployment(_Request()))

    assert response.status_code == 200
    assert response.body == b"<alto/>"
    assert response.media_type == "application/xml"
    assert seen == {"data": b"JPEGBYTES", "name": "vol/00012.jpg"}


def test_the_http_ingress_refuses_an_empty_body() -> None:
    """An empty POST is a caller bug, answered 400 BEFORE the thread hop — never a minutes-long
    pipeline run over zero bytes that fails somewhere deep in PIL."""
    import asyncio

    from runner.htrflow_service import HTRFlowDeployment

    cls = HTRFlowDeployment.func_or_class
    deployment = cls.__new__(cls)

    class _Request:
        query_params: typing.ClassVar[dict[str, str]] = {}

        @staticmethod
        async def body() -> bytes:
            return b""

    response = asyncio.run(deployment(_Request()))

    assert response.status_code == 400
