"""The scheme of an object-store endpoint: one case-insensitive rule, for Lance and pyarrow alike.

`objectfs.lance_storage_options` and `objectfs.s3_filesystem`, the catalog's `namespace_properties` and
`service_kit.media.config` apply it, and so does every caller that builds through them; an options
dict or `S3FileSystem` assembled by hand bypasses it.

Its own module, importing only the stdlib, because the catalog's and the media plane's config modules
apply it and neither loads pyarrow; `objectfs` does, and importing it from a settings module would.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def endpoint_scheme(endpoint: str) -> str:
    """The scheme a client reaches ``endpoint`` over, lowercased: ``http``, ``https``, or what it names.

    Case-insensitive, as object_store is: ``urlsplit`` lowercases the scheme, and measured on pylance
    12.0.0 ``HTTP://`` with ``allow_http=true`` reaches the store over ``http://``. An empty endpoint
    is AWS proper, whose regional endpoint is TLS, so it answers ``https``.
    """
    return urlsplit(endpoint).scheme if endpoint else "https"


def allow_http_for(endpoint: str) -> str:
    """The ``allow_http`` storage option ``endpoint`` warrants: ``"true"`` exactly when its scheme is ``http``."""
    return "true" if endpoint_scheme(endpoint) == "http" else "false"
