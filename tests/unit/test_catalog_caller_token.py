"""The catalog seam carries the CALLER's identity, and 401 stays 401.

Two defects met here and produced one very misleading symptom. Opening the annotate canvas returned
`403` to the browser; the catalog's own log said `401 Unauthorized`. So the estate reported "you lack
a grant" for a request that had never presented a credential at all, and the obvious next move —
hunting for a missing FGA tuple — could not possibly have worked.

1. The read path had no way to carry a credential. `open_reader`/`open_writer` took only
   `settings.catalog_token`, which the chart leaves empty (`values.yaml: catalogToken: ""`), so the
   transport's single header line was skipped and the client went out bare.
2. `translate_catalog_errors` collapsed `UnauthorizedException` and `ForbiddenException` into one
   `ForbiddenError`, laundering authentication failure into authorization failure.

Why the CALLER's bearer and not a service account: the catalog checks one relation on one `table:`
object and injects no row predicate, so a service credential answers 200 for a user who has no grant
— the two principals diverge, not the rows. That is a confused deputy, and it is why the estate's one
service account is scoped to the publish saga, whose stated reason ("outlives any user request")
does not apply to a request-scoped read.

UPDATED 2026-08-26: the seam no longer falls back AT ALL. This file argued the principle from the
start and still encoded the exception, and the exception was the whole hole — `caller_token or
settings.catalog_token` fires exactly when a request arrives with no bearer. `MEDIA_CATALOG_TOKEN` is
removed from the media settings and the chart with it; the movers' `MEDALLION_CATALOG_TOKEN` stays,
because they genuinely have no caller to forward.
"""

from __future__ import annotations

from typing import Any

import pytest

from service_kit.exceptions import ForbiddenError, UnauthorizedError
from service_kit.lancekit.reader import open_reader, translate_catalog_errors


class _Settings:
    """The minimum shape `open_reader` reads — catalog mode, with a configurable service token."""

    read_backend = "catalog"
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
    transport = reader._transport  # noqa: SLF001 - asserting the wire is the point of this test
    assert "Authorization" not in transport._api.api_client.default_headers, (  # noqa: SLF001
        "a caller's bearer is pinned on the SHARED client, so the next caller would send it"
    )
    header = transport.request_headers().get("Authorization")
    return header.removeprefix("Bearer ") if header else None


@pytest.mark.parametrize(
    ("service_token", "caller_token", "expected"),
    [
        # The reported failure: no service token configured, no caller token forwarded -> bare
        # request -> the catalog's 401. This is the state the live cluster was in.
        (None, None, None),
        # The fix: the caller's bearer travels, so the catalog answers about the CALLER.
        (None, "caller-jwt", "caller-jwt"),
        # The caller WINS over a configured service token — otherwise a deployment that sets
        # catalogToken silently reverts every read to the confused-deputy behaviour.
        ("service-jwt", "caller-jwt", "caller-jwt"),
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
        settings=_Settings(service_token),  # ty: ignore[invalid-argument-type]
        caller_token=caller_token,
    )
    assert _token_of(reader) == expected


def test_a_catalog_401_is_not_reported_as_403() -> None:
    """The laundering that made the symptom unreadable.

    A 401 says "I do not know who you are"; a 403 says "I know, and no". Collapsing them told the
    browser the second when the truth was the first.
    """
    api_exc = pytest.importorskip("lance_namespace_urllib3_client.exceptions")

    with pytest.raises(UnauthorizedError, match="rejected the credential"), translate_catalog_errors():
        raise api_exc.UnauthorizedException(status=401, reason="Unauthorized")

    with pytest.raises(ForbiddenError, match="denied the request"), translate_catalog_errors():
        raise api_exc.ForbiddenException(status=403, reason="Forbidden")
