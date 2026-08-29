"""ffmpeg's source URL must come from CONFIGURATION, never from the request's Host header (VS-09).

open_python-audit VS-09. `media_clip` built ffmpeg's input as
``f"{request.base_url}/api/explorer/{doc_id}"``, and ``base_url`` is derived from the ``Host`` /
``X-Forwarded-Host`` headers. So ``Host: internal-metadata.local`` made the viewer's own ffmpeg
fetch that host over the pod network and transcode whatever came back into an MP4 the caller then
downloaded — a read primitive against anything the pod can reach, with a 120 s ffmpeg timeout
attached to every attempt.

AND THAT URL NAMED NOTHING REAL. The viewer serves media bytes at ``/api/media/{doc_id}``; it has
no ``/api/explorer/{doc_id}`` route, and the gateway row ``("/api/explorer", "/api", *viewer)``
rewrites the external form to ``/api/{doc_id}``, which it has no route for either (asserted below
against the app's own OpenAPI paths, so this cannot rot into a comment). The request-derived host
was therefore buying nothing except the SSRF.

The three properties pinned here: the origin is the configured loopback whatever the caller claims;
the path is a route this service actually serves; and the caller's own bearer rides along, because
``/api/media/{doc_id}`` is `REQUIRE_MEDIA_BYTES`-gated and an unauthenticated loopback would 403 —
the same `can_read_data` grant the clip route has already checked for this very caller.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from viewer.api.v1 import router as viewer_router
from viewer.api.v1.endpoints import media as media_ep
from viewer.core.config import ViewerSettings

from service_kit.media.state import AppState


HOSTILE_HOST = "internal-metadata.local"


class _Binding:
    table = "chunks"
    media_blob = "media"
    mime = "mime"
    thumbnail = None
    thumbnail_mime = None


class _Identity:
    doc_key = "doc_id"


class _Declared:
    document = _Binding()
    identity = _Identity()


class _Descriptor:
    declared = _Declared()


class _Handle:
    id = "corpus"
    descriptor = _Descriptor()


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("127.0.0.1", 8101),
            "path": "/api/media-clip/doc1",
            "root_path": "",
            "query_string": b"",
            "headers": headers,
        }
    )


@pytest.fixture
def clip_call(monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401 — returns the recorded build args
    """Drive `media_clip` with the Lance/authz layers stubbed, recording what ffmpeg was told."""
    recorded: dict[str, Any] = {}
    clip = Path(tempfile.mkdtemp()) / "clip.mp4"
    clip.write_bytes(b"mp4")

    def _build(source_url: str, cache_key: str, lo: float, hi: float, **kwargs: Any) -> Path:
        recorded["source"] = source_url
        recorded["cache_key"] = cache_key
        recorded.update(kwargs)
        return clip

    monkeypatch.setattr(media_ep, "build_clip", _build)
    monkeypatch.setattr(media_ep, "dataset_handle", lambda *_a, **_k: _Handle())
    monkeypatch.setattr(media_ep, "validate_doc_key", lambda _declared, doc_id: doc_id)
    monkeypatch.setattr(media_ep, "table_dataset", lambda *_a, **_k: object())
    monkeypatch.setattr(media_ep, "rowid_for_doc", lambda *_a, **_k: 1)
    monkeypatch.setattr(media_ep, "corpus_object", lambda *_a, **_k: "table:corpus")

    async def _allow(**_kw: object) -> bool:
        return True

    def _call(headers: list[tuple[bytes, bytes]], dataset: str | None = None) -> dict[str, Any]:
        asyncio.run(
            media_ep.media_clip(
                doc_id="doc1",
                request=_request(headers),
                state=cast("AppState", object()),
                subject="gina",
                checker=_allow,
                settings=ViewerSettings(),
                lo=0.0,
                hi=5.0,
                dataset=dataset,
            )
        )
        return recorded

    return _call


def test_a_hostile_host_header_never_reaches_ffmpeg(clip_call: Any) -> None:
    recorded = clip_call([(b"host", HOSTILE_HOST.encode())])
    assert HOSTILE_HOST not in recorded["source"], (
        f"ffmpeg was pointed at {recorded['source']!r} — the caller's Host header chose the origin, so any host the pod can reach is fetchable through this route"
    )


def test_the_origin_is_the_configured_loopback(clip_call: Any) -> None:
    settings = ViewerSettings()
    recorded = clip_call([(b"host", HOSTILE_HOST.encode())])
    assert recorded["source"].startswith(settings.clip_source_origin), (
        f"{recorded['source']!r} does not start with the configured origin {settings.clip_source_origin!r}"
    )


def test_the_source_path_is_a_route_this_service_serves(clip_call: Any) -> None:
    """The old URL named a path no viewer route and no gateway rewrite resolves to."""
    recorded = clip_call([(b"host", b"viewer:8101")])
    path = recorded["source"].split("://", 1)[1].partition("/")[2].partition("?")[0]
    app = FastAPI()
    app.include_router(viewer_router.router)
    served = set(app.openapi()["paths"])
    assert f"/{path}".replace("/doc1", "/{doc_id}") in served, f"ffmpeg is pointed at /{path}, which is not one of this service's routes: {sorted(served)}"


def test_the_dataset_selector_still_rides_along(clip_call: Any) -> None:
    recorded = clip_call([(b"host", b"viewer:8101")], dataset="other corpus")
    assert "dataset=other%20corpus" in recorded["source"], recorded["source"]


def test_the_callers_own_bearer_is_forwarded(clip_call: Any) -> None:
    """`/api/media/{doc_id}` is REQUIRE_MEDIA_BYTES-gated: a tokenless loopback transcodes a 403."""
    recorded = clip_call([(b"host", b"viewer:8101"), (b"authorization", b"Bearer tok-123")])
    assert recorded.get("authorization") == "Bearer tok-123", (
        f"the caller's credential was not handed to the fetch ({recorded!r}) — the media door refuses an anonymous loopback, so every clip would fail once FGA is on"
    )
