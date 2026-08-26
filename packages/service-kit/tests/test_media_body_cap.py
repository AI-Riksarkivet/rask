"""The media apps had no body ceiling, and one of them documented a protection it does not provide.

open_fastapi-audit. Three separate things, all the same shape — a cap in the wrong place:

1. **No ceiling on the media trio.** `service_kit.middleware.register_middleware` grew a body cap,
   but viewer / search / annotator do not use it — they build through
   `service_kit.media.middleware.register_middleware`, which registered CORS and nothing else. The
   apps that actually accept multipart uploads were the ones without a cap.

2. **The existing cap is in the handler, not at the door.** `await file.read(_MAX_UPLOAD_BYTES + 1)`
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
from service_kit.media.middleware import register_middleware


def test_the_media_factory_applies_a_body_cap() -> None:
    """The apps that accept uploads were the ones without a ceiling."""
    app = FastAPI()
    register_middleware(app, MediaSettings())

    names = [getattr(m.cls, "__name__", repr(m.cls)) for m in app.user_middleware]
    assert "BodySizeLimitMiddleware" in names, (
        f"media register_middleware installs {names} and no body cap — viewer, search and annotator "
        f"accept multipart uploads and starlette spools each file part whole before the handler runs"
    )


def test_the_media_ceiling_is_declared_and_TIGHTER_than_the_catalogs() -> None:
    """A media upload is not an Arrow-IPC write.

    The catalog's 256 MiB exists for bulk table writes; sizing the media apps the same would make the
    ceiling meaningless for a voice snippet. The audit asks for ~32 MiB here.
    """
    from catalog.core.config import Settings as CatalogSettings

    assert "max_body_bytes" in MediaSettings.model_fields
    assert MediaSettings().max_body_bytes < CatalogSettings.model_fields["max_body_bytes"].default


def test_the_ceiling_is_above_the_handlers_own_upload_cap() -> None:
    """The two bounds must not invert.

    If the door were tighter than `_MAX_UPLOAD_BYTES`, the handler's own 400 — which names the upload
    limit in the caller's terms — would be unreachable, and every oversize snippet would get a bare
    413 instead. Defence in depth means the outer bound is the looser one.
    """
    from viewer.services import voice_service

    assert MediaSettings().max_body_bytes > voice_service._MAX_UPLOAD_BYTES


def test_the_voice_docstring_no_longer_claims_it_avoids_buffering() -> None:
    """A docstring asserting a protection the code does not provide is what a reader trusts instead
    of the code. This is the specific false sentence the audit names."""
    from pathlib import Path

    import viewer

    source = Path(viewer.__file__).parent / "api" / "v1" / "endpoints" / "voice.py"
    # Whitespace-normalized: the sentence wraps a line break in the source, so a raw substring match
    # passes vacuously — which is exactly what this assertion did on its first run.
    text = " ".join(source.read_text().split())
    assert "without ever being buffered whole" not in text, (
        "voice.py still claims the read-cap avoids buffering the upload. Starlette spools the whole "
        "file part to disk before the handler is entered; the read-cap bounds the handler's memory only"
    )
