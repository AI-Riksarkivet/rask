"""`allow_http` is DERIVED from the endpoint scheme, so it cannot be configured into a downgrade.

[[LH-238]]. It was a settings bool (`LANCE_S3_ALLOW_HTTP`, default True) that the chart pinned to
`"true"` for every deployment, so the catalog permitted plain-HTTP object-store traffic regardless of
what `LANCE_S3_ENDPOINT` actually named. A TLS endpoint with `allow_http` still on is not a typo the
reader can see: object_store only consults the flag when a request would otherwise be refused, so the
misconfiguration is silent until something downgrades.

`service_kit.media.config` already derived it from the scheme; this is the same rule at the catalog,
which is the plane that hands storage options to pylance AND vends them to clients.

Deriving also removes a whole class of disagreement: the scheme and the flag are now one fact, so a
future endpoint change cannot leave a stale permission behind it.
"""

from __future__ import annotations

import pytest

from catalog.core.config import Settings


_KEY = "storage.allow_http"


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://minio:9000", "true"),
        ("http://rask-rustfs-io:9000", "true"),
        ("https://s3.example.com", "false"),
        ("https://minio:9000", "false"),
    ],
)
def test_allow_http_follows_the_endpoint_scheme(endpoint: str, expected: str) -> None:
    settings = Settings(LANCE_S3_ENDPOINT=endpoint, LANCE_S3_ACCESS_KEY_ID="k")

    assert settings.namespace_properties()[_KEY] == expected


def test_allow_http_ignores_a_stale_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE POINT OF THE ROW: no env may re-enable plain HTTP against a TLS endpoint."""
    monkeypatch.setenv("LANCE_S3_ALLOW_HTTP", "true")

    settings = Settings(LANCE_S3_ENDPOINT="https://s3.example.com", LANCE_S3_ACCESS_KEY_ID="k")

    assert settings.namespace_properties()[_KEY] == "false"
