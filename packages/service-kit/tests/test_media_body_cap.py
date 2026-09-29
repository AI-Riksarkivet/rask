"""The media apps had no body ceiling, and one of them documented a protection it does not provide.

open_fastapi-audit. Three separate things, all the same shape — a cap in the wrong place:

1. **No ceiling on the media trio.** `service_kit.middleware.register_middleware` grew a body cap,
   but viewer / search / annotator do not use it — they build through
   `service_kit.media.middleware.register_media_middleware`, which registered CORS and nothing else. The
   apps that actually accept multipart uploads were the ones without a cap.

2. **The existing cap is in the handler, not at the door.** `await file.read(MAX_UPLOAD_BYTES + 1)`
   caps what the ENDPOINT holds. It cannot cap what starlette already did: a multipart file part is
   spooled to `SpooledTemporaryFile` in full BEFORE the handler is entered. So the read-cap protects
   memory — which is real — and nothing else.

3. **`voice.py`'s docstring claims otherwise**, in as many words: "the body read stops one byte past
   the size cap, so an oversize upload 400s ... without ever being buffered whole". It IS buffered
   whole, on disk. A docstring asserting a protection the code does not provide is worse than no
   docstring: it is what a reader checks instead of the code.

The cap belongs where the bytes ARRIVE. `BodySizeLimitMiddleware` is pure-ASGI and already handles
both the declared-Content-Length fast reject and the streaming counter for a chunked or lying client,
so it refuses before starlette spools anything. The handler's `read(cap+1)` stays as defence in depth.
"""

from __future__ import annotations

from fastapi import FastAPI

from service_kit.media.config import Settings as MediaSettings
from service_kit.media.middleware import register_media_middleware


def test_the_media_factory_applies_a_body_cap() -> None:
    """The apps that accept uploads were the ones without a ceiling."""
    app = FastAPI()
    register_media_middleware(app, MediaSettings())

    names = [getattr(m.cls, "__name__", repr(m.cls)) for m in app.user_middleware]
    assert "BodySizeLimitMiddleware" in names, (
        f"media register_middleware installs {names} and no body cap — viewer, search and annotator "
        f"accept multipart uploads and starlette spools each file part whole before the handler runs"
    )
