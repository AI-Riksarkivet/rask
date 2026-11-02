"""A client's catalog read path carries the CALLER's identity, and never a service token in its place.

Why the CALLER's bearer and not a service account: the catalog checks one relation on one `table:`
object and injects no row predicate, so a service credential answers 200 for a user who has no grant
— the two principals diverge, not the rows. That is a confused deputy, and it is why the estate's one
service account is scoped to the publish saga, whose stated reason ("outlives any user request")
does not apply to a request-scoped read.

A caller with no bearer sends none: `caller_token or settings.catalog_token` would fire exactly when a request
arrives anonymous, re-issuing it under the estate's own identity.
"""

from __future__ import annotations

from typing import Any

import pytest

from client_kit.lancekit.reader import open_reader


class _Settings:
    """The `TablePathSettings` shape in full catalog mode, with a configurable service token."""

    read_backend = "catalog"
    write_backend = "catalog"
    rest_catalog_mode = True
    catalog_uri = "http://catalog.invalid:2333"
    catalog_delimiter = "$"

    def __init__(self, service_token: str | None) -> None:
        self.catalog_token = service_token


def _token_of(reader: Any) -> str | None:
    """The bearer the constructed transport would actually send.

    READ OFF THE PER-REQUEST HEADERS, not the client's defaults, and the change is load-bearing
    rather than cosmetic (SK-03). The transport used to build its own `ApiClient` per call and pin
    the caller's bearer as a DEFAULT header on it. That client is now shared across every transport
    aimed at the same catalog — one connection pool instead of one per request — so a bearer left in
    `default_headers` would be sent for the NEXT caller too. The assertion below therefore also pins
    that the shared client carries no `Authorization` of its own.
    """
    transport = reader._transport
    assert "Authorization" not in transport._api.api_client.default_headers, (
        "a caller's bearer is pinned on the SHARED client, so the next caller would send it"
    )
    header = transport.request_headers().get("Authorization")
    return header.removeprefix("Bearer ") if header else None


@pytest.mark.parametrize(
    ("service_token", "caller_token", "expected"),
    [
        # The fix: the caller's bearer travels, so the catalog answers about the CALLER.
        (None, "caller-jwt", "caller-jwt"),
        # NO CALLER TOKEN -> NO TOKEN, even with a service identity configured. This case asserted
        # "service-jwt" until 2026-08-26, on the stated grounds that a caller with no request context
        # (the publish saga) needed the fallback. This file's OWN docstring already refuted that —
        # "whose stated reason does not apply to a request-scoped read" — and the tree agrees: every
        # call site of open_reader/open_writer is a request-scoped annotator route, and the publish
        # saga mints its own bearer from Dex (annotator/projects/lakehouse.py:388) rather than
        # touching this seam. So the branch fired only for an ANONYMOUS request, re-issued under the
        # estate's own identity against a catalog that injects no row predicate.
        ("service-jwt", None, None),
    ],
)
def test_the_reader_sends_the_callers_bearer_and_NEVER_substitutes_a_service_token(
    service_token: str | None, caller_token: str | None, expected: str | None
) -> None:
    reader = open_reader(
        dataset=None,
        table_id=["transcripts_v2", "annotations"],
        settings=_Settings(service_token),
        caller_token=caller_token,
    )
    assert _token_of(reader) == expected
