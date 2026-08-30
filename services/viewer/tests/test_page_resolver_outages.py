"""A dependency being down is not a missing page, and its driver text is not for the browser.

open_fastapi-audit — "viewer's page resolver reports a catalog or object-store OUTAGE as 404 Not
Found, with the raw httpx/lance exception text in the client-visible detail".

TWO DEFECTS, and the first is what makes the second reachable.

**Wrong class.** `_resolve` caught `httpx.RequestError` — the base of ConnectError, ConnectTimeout,
ReadTimeout and DNS failure — and raised `NotFoundError`. `_open` caught bare `Exception` around
`lance.dataset(...)` and did the same, so a RustFS outage, expired vended credentials and a corrupt
manifest all reported 404. A 404 is TERMINAL: the zone renders "page not found", the reader stops,
and nothing retries — for a condition that is transient and would clear on its own. The message even
said so out loud ("catalog unreachable") while the class said the opposite.

The file already argues this exact point twelve lines below, refusing to launder 401/403 into
"unknown table" because "the annotator lost real debugging time to exactly this laundering". The
reasoning simply was not extended two lines up.

**The leak.** Both messages interpolated the caught exception into `detail`, and
`service_kit.exceptions._problem` puts `str(exc)` verbatim into the client body with no redaction at
any status. So the browser received the httpx connect error (internal host and port) or the lance/S3
driver message (the `s3://` location, credential state). `ns_errors` redacts every 5xx detail for
exactly this reason — and never applied here, because the class chosen was a 4xx. Mislabelling the
outage is what put the internals on the wire.

The estate states the rule in its own words in `service_kit/exceptions.py`: "Keep the message STABLE;
never interpolate a raw upstream exception into it (log that instead)."

WHAT STAYS 404: the two cases that genuinely mean absent — the catalog answering a non-401/403 4xx
(4xx ONLY: a 5xx response is the outage again, caught by the re-audit after this header first
claimed the branch was already that narrow), and a describe that returns no `location`.
"""

from __future__ import annotations

import httpx
import pytest

from service_kit.exceptions import NotFoundError, ServiceUnavailableError
from service_kit.media.config import Settings
from service_kit.media.state import AppState
from viewer.api.v1.endpoints import pages as pages_ep


TABLE = "bronze$pages"
LOCATION = "s3://rask-lake/bronze/pages.lance"
SECRET = "connect to rustfs.internal:9000: connection refused"


def _state(http: object | None = None) -> AppState:
    """A REAL `AppState`, not a stand-in.

    `_resolve` and `_open` are typed against it, and a duck-typed fake here would pass the test while
    the type checker (correctly) rejected it — the state object is the thing that carries both the
    catalog URI and the storage options these two functions read.

    `http` is the pooled client `_resolve` posts through (VS-12): the resolve reuses `state.http`, so
    the fake catalog lives there rather than behind a patched `httpx.Client` constructor.
    """
    return AppState(settings=Settings(MEDIA_CATALOG_URI="http://catalog.internal:2333"), http=http)


class _ConnectErrorClient:
    """A pooled client whose catalog is unreachable — the transient-outage case."""

    def post(self, *_args: object, **_kwargs: object) -> None:
        raise httpx.ConnectError(SECRET)


def test_a_catalog_outage_is_not_a_missing_page() -> None:
    """A transient dependency failure must be retryable, not terminal."""
    with pytest.raises(ServiceUnavailableError) as caught:
        pages_ep._resolve(_state(_ConnectErrorClient()), TABLE, "tok")  # noqa: SLF001
    assert not isinstance(caught.value, NotFoundError)


def test_the_catalog_outage_detail_carries_no_driver_text() -> None:
    """`_problem` puts `str(exc)` straight in the body, so the message IS the wire."""
    with pytest.raises(ServiceUnavailableError) as caught:
        pages_ep._resolve(_state(_ConnectErrorClient()), TABLE, "tok")  # noqa: SLF001
    message = str(caught.value)
    assert SECRET not in message, f"the client-visible detail carried the httpx error: {message}"
    assert TABLE in message, "the detail must still name the table, or it helps nobody"


def test_an_unreadable_dataset_is_an_outage_not_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """A RustFS outage, expired vended credentials and a corrupt manifest all land here."""
    monkeypatch.setattr(pages_ep, "_resolve", lambda *_a, **_k: LOCATION)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(SECRET)

    monkeypatch.setattr(pages_ep.lance, "dataset", _boom)

    with pytest.raises(ServiceUnavailableError) as caught:
        pages_ep._open(_state(), TABLE, "tok")  # noqa: SLF001
    message = str(caught.value)
    assert not isinstance(caught.value, NotFoundError)
    assert SECRET not in message, f"the detail carried the driver text: {message}"
    assert LOCATION not in message, f"the detail carried the object-store location: {message}"


class _RespondingClient:
    """A pooled client that answers `status` with `payload` — the response-path cases."""

    def __init__(self, status: int, payload: dict) -> None:
        self._status = status
        self._payload = payload

    def post(self, *_args: object, **_kwargs: object) -> object:
        status = self._status
        payload = self._payload

        class _Response:
            status_code = status

            def json(self) -> dict:
                return payload

        return _Response()


@pytest.mark.parametrize(("status", "payload"), [(404, {}), (200, {})])
def test_a_genuinely_absent_table_stays_a_404(status: int, payload: dict) -> None:
    """The two cases that really do mean absent must not become 503 — that would make a missing
    registration look retryable and hide it behind an outage alert."""
    with pytest.raises(NotFoundError):
        pages_ep._resolve(_state(_RespondingClient(status, payload)), TABLE, "tok")  # noqa: SLF001


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_catalog_5xx_RESPONSE_is_an_outage_not_a_404(status: int) -> None:
    """Found by the adversarial re-audit of this file's own finding: the branch was `>= 400`.

    A catalog answering HTTP 500 — or a proxy in front of it answering 502/503 — is exactly the
    outage this file exists to classify, and it was still laundered into a terminal 404 ("catalog
    does not know table"). The connection-level cases above never see it because they raise before a
    response exists; only a 5xx RESPONSE walks this path. The "stays 404" parametrization below
    covered 404 and 200-with-no-location, never a 5xx, so the residual was unpinned and invisible.
    """
    with pytest.raises(ServiceUnavailableError) as caught:
        pages_ep._resolve(_state(_RespondingClient(status, {})), TABLE, "tok")  # noqa: SLF001
    assert not isinstance(caught.value, NotFoundError)
    assert TABLE in str(caught.value)
