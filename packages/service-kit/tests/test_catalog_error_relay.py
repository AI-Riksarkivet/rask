"""A 5xx from the catalog leg must not carry whatever answered into the browser.

open_fastapi-audit — "lancekit's catalog-error translator relays the upstream response body into the
client-visible detail on the 5xx branch, defeating ns_errors' 5xx redaction".

`ns_errors.problem_detail` deliberately replaces `str(exc)` with a fixed "Internal Server Error" on
every 5xx so paths, DSNs and driver text cannot reach a client — `_UNREDACTED_5XX` is a one-element
frozenset and its comment explains why 501 is the only member. The catch-all branch here routed around
that: `ServiceUnavailableError(f"catalog unavailable: {exc.body or exc.reason}")` is a 503 whose detail
is the raw body of whatever answered, and `service_kit`'s own handler keeps a 4xx-style message
verbatim.

WHY THAT BRANCH SPECIFICALLY, and why the others stay. The module comment — "the catalog's own problem
detail rides in the message" — is a real and defensible decision for the four 4xx branches: there the
upstream body IS the catalog's own redacted problem+json, and relaying it is what turns an opaque 500
into the answer the direct path would have given. It does not hold for the catch-all, which is the
branch that fires when the upstream is NOT the catalog: on the failure this actually catches, the
responder is usually an ingress or a sidecar returning an HTML error page or a proxy diagnostic, and
that text was being served to the browser.

`exception-handlers.md`: "Never include exception internals in the response body — those leak via logs
only." The body goes to `log.exception`; the caller gets a sentence.
"""

from __future__ import annotations

import logging

import pytest

from service_kit.exceptions import ConflictError, NotFoundError, ServiceUnavailableError, UnauthorizedError
from service_kit.lancekit import reader


def _exc(kind: str, body: str):
    from lance_namespace_urllib3_client import exceptions as api_exc

    cls = getattr(api_exc, kind)
    instance = cls.__new__(cls)
    instance.body = body
    instance.reason = "Bad Gateway"
    instance.status = 502
    return instance


def test_the_catch_all_does_not_relay_the_upstream_body(caplog: pytest.LogCaptureFixture) -> None:
    """The branch that fires when the responder is NOT the catalog."""
    secret = "<html>nginx/1.25.3 upstream 10.42.0.7:2333 connect() failed</html>"
    with caplog.at_level(logging.ERROR), pytest.raises(ServiceUnavailableError) as caught, reader.translate_catalog_errors():
        raise _exc("ApiException", secret)

    assert secret not in str(caught.value), f"the 503's client-visible detail carried the upstream body: {caught.value}"
    # On the RECORD, not in `caplog.text`: `extra=` fields do not appear in the default format, so a
    # text match would fail against a log line that does carry them. What reaches the log pipeline is
    # the attribute.
    assert any(getattr(r, "body", None) == secret for r in caplog.records), "the body must still reach the operator — via the log, not the wire"


@pytest.mark.parametrize(
    ("kind", "error"),
    [
        ("ConflictException", ConflictError),
        ("UnauthorizedException", UnauthorizedError),
        ("NotFoundException", NotFoundError),
    ],
)
def test_the_4xx_branches_still_relay_the_catalogs_own_detail(kind: str, error: type[Exception]) -> None:
    """Their rationale is recorded and correct: the upstream body IS the catalog's redacted
    problem+json, and relaying it is what turns an opaque 500 into the answer the direct path gives.
    A blanket redaction here would have been the easy over-correction."""
    detail = "table bronze$pages is not registered"
    with pytest.raises(error) as caught, reader.translate_catalog_errors():
        raise _exc(kind, detail)
    assert detail in str(caught.value)
