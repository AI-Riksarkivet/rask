"""Whether an object-store endpoint is plaintext: the one rule every storage-options builder applies.

Its own module, importing only the stdlib, because the catalog's and the media plane's config modules
apply it and neither loads pyarrow; `objectfs` does, and importing it from a settings module would.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def allow_http_for(endpoint: str) -> str:
    """The ``allow_http`` storage option ``endpoint`` warrants: ``"true"`` exactly when its scheme is ``http``.

    Case-insensitive, as object_store is: ``urlsplit`` lowercases the scheme, and measured on pylance
    12.0.0 ``HTTP://`` with ``allow_http=true`` reaches the store over ``http://``. ``https`` and an
    empty endpoint (AWS proper, whose regional endpoint is TLS) answer ``"false"``.
    """
    return "true" if urlsplit(endpoint).scheme == "http" else "false"
