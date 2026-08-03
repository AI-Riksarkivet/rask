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
    """The bearer the constructed transport would actually send."""
    transport = reader._transport  # noqa: SLF001 - asserting the wire is the point of this test
    client = transport._api.api_client  # noqa: SLF001
    header = client.default_headers.get("Authorization")
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
        # No request context (the publish saga) still falls back to the service identity.
        ("service-jwt", None, "service-jwt"),
    ],
)
def test_the_reader_sends_the_callers_bearer_and_falls_back_to_the_service_token(
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
