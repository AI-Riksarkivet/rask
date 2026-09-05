"""The media CORS layer advertises the write methods its apps actually serve.

docs/DECISIONS.md "The Python estate audit" `ANN-04` (cross-service — the fix travels with the shared middleware). The media
factory `service_kit.media.middleware.register_media_middleware` — used by viewer, search and annotator —
set `allow_methods=["GET", "POST", "OPTIONS"]`, while the annotator alone serves seven PUT/PATCH/DELETE
routes (member put/delete, draft put, ontology patch, project-event put/delete). With `cors_origins`
defaulting to `["*"]`, a cross-origin browser preflight for those methods is answered without them in
`Access-Control-Allow-Methods`, so the real request is blocked. This guards the shared seam for all
three media services at once.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.media.config import Settings as MediaSettings
from service_kit.media.middleware import register_media_middleware


def _preflight_allowed_methods(method: str) -> str:
    app = FastAPI()
    register_media_middleware(app, MediaSettings())

    @app.api_route("/thing", methods=[method])
    async def _thing() -> dict[str, str]:  # pragma: no cover - never invoked; only the preflight is
        return {"ok": "yes"}

    resp = TestClient(app).options(
        "/thing",
        headers={"Origin": "https://example.test", "Access-Control-Request-Method": method},
    )
    return resp.headers.get("access-control-allow-methods", "")


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_media_preflight_advertises_write_methods(method: str) -> None:
    advertised = _preflight_allowed_methods(method)
    assert method in advertised, (
        f"media CORS preflight advertises '{advertised}' — {method} routes (the annotator's members/drafts/ontology) are blocked cross-origin"
    )
