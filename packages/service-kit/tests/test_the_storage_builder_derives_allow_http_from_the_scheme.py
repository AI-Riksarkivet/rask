"""`lance_storage_options` derives `allow_http` from the endpoint's scheme, and nothing else can set it.

[[LH-238]]. A parameter here would be a second source for one fact, and a defaulted one is worse: a
default of `True` that no caller overrides hands `allow_http=true` beside an `https://` endpoint to
every credential the catalog VENDS and to every service opening Lance through this builder (lineage,
medallion, maintenance, ingest, the Ray jobs).

A permit held against a TLS store is invisible until it matters: `lance_docs/guide.md:2338` defines
`allow_http` as "Allow non-TLS, i.e. non-HTTPS connections", and object_store consults it only when a
request would otherwise be refused. Measured on pylance 12.0.0 against a closed port: `http://` with
`allow_http=false` dies at client construction (`builder error`), while `https://` with
`allow_http=false` proceeds to its TLS request. The scheme decides both halves, which is why the
builder offers no knob: one fact cannot disagree with itself.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.objectfs import lance_storage_options


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://rustfs:9000", "true"),
        ("http://127.0.0.1:9000", "true"),
        ("https://s3.example.com", "false"),
        ("https://rustfs:9000", "false"),
        # AWS proper: callers pass "" and drop the key, leaving botocore's regional https endpoint.
        ("", "false"),
    ],
)
def test_allow_http_follows_the_endpoint_scheme(endpoint: str, expected: str) -> None:
    assert lance_storage_options(endpoint, "AK", "SK", "us-east-1")["allow_http"] == expected


def test_a_vended_credential_for_a_tls_store_is_not_permitted_plaintext() -> None:
    """The vending shape — a session token on top — takes the same rule; the token changes nothing."""
    options = lance_storage_options("https://s3.example.com", "AK", "SK", "us-east-1", session_token="TOK")
    assert options["allow_http"] == "false"
