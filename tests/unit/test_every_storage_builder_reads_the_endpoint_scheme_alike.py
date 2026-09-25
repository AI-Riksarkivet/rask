"""Every storage-options builder derives `allow_http` from the endpoint's scheme by one case-insensitive rule.

Three builders hand options to object_store: `service_kit.lakehouse.objectfs.lance_storage_options`
(every vend and most service opens), the catalog's `namespace_properties` (its own `dir` connection)
and `service_kit.media.config`'s `storage_options`. object_store lowercases the scheme — measured on
pylance 12.0.0 against a closed port, `HTTP://127.0.0.1:1` with `allow_http=true` requests
`http://127.0.0.1:1/…` and with `allow_http=false` dies at `builder error`. So a case-sensitive
`startswith("http://")` hands an upper-case plaintext endpoint `false`, and the client is never built.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from catalog.core.config import Settings as CatalogSettings
from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.media.config import Settings as MediaSettings


def _lance(endpoint: str) -> str:
    return lance_storage_options(endpoint, "AK", "SK", "us-east-1")["allow_http"]


def _catalog(endpoint: str) -> str:
    return CatalogSettings(LANCE_S3_ENDPOINT=endpoint, LANCE_S3_ACCESS_KEY_ID="k").namespace_properties()["storage.allow_http"]


def _media(endpoint: str) -> str:
    # Both static fields set, so the builder takes its no-secret-store branch.
    options = MediaSettings.model_validate(
        {"MEDIA_S3_ENDPOINT": endpoint, "MEDIA_S3_ACCESS_KEY_ID": "AK", "MEDIA_S3_SECRET_ACCESS_KEY": "SK"}
    ).storage_options()
    assert options is not None
    return options["allow_http"]


_BUILDERS: dict[str, Callable[[str], str]] = {"lance_storage_options": _lance, "namespace_properties": _catalog, "media": _media}


@pytest.mark.parametrize("builder", _BUILDERS)
@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://rustfs:9000", "true"),
        ("HTTP://rustfs:9000", "true"),
        ("Http://rustfs:9000", "true"),
        ("https://s3.example.com", "false"),
        ("HTTPS://s3.example.com", "false"),
    ],
)
def test_allow_http_follows_the_scheme_in_any_case(builder: str, endpoint: str, expected: str) -> None:
    assert _BUILDERS[builder](endpoint) == expected
