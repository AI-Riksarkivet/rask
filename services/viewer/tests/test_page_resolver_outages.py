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

WHAT STAYS 404: the two cases that genuinely mean absent — the catalog answering a non-401/403 4xx,
and a describe that returns no `location`.
"""

from __future__ import annotations

import httpx
import pytest
from viewer.api.v1.endpoints import pages as pages_ep

from service_kit.exceptions import NotFoundError, ServiceUnavailableError
from service_kit.media.config import Settings
from service_kit.media.state import AppState


TABLE = "bronze$pages"
LOCATION = "s3://rask-lake/bronze/pages.lance"
SECRET = "connect to rustfs.internal:9000: connection refused"


def _state() -> AppState:
    """A REAL `AppState`, not a stand-in.

    `_resolve` and `_open` are typed against it, and a duck-typed fake here would pass the test while
    the type checker (correctly) rejected it — the state object is the thing that carries both the
    catalog URI and the storage options these two functions read.
    """
    return AppState(settings=Settings(MEDIA_CATALOG_URI="http://catalog.internal:2333"))


def test_a_catalog_outage_is_not_a_missing_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient dependency failure must be retryable, not terminal."""

    class _Client:
        def __init__(self, **_kwargs: object) -> None: ...
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None: ...
        def post(self, *_args: object, **_kwargs: object) -> None:
            raise httpx.ConnectError(SECRET)

    monkeypatch.setattr(pages_ep.httpx, "Client", _Client)

    with pytest.raises(ServiceUnavailableError) as caught:
        pages_ep._resolve(_state(), TABLE, "tok")  # noqa: SLF001
    assert not isinstance(caught.value, NotFoundError)


def test_the_catalog_outage_detail_carries_no_driver_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_problem` puts `str(exc)` straight in the body, so the message IS the wire."""

    class _Client:
        def __init__(self, **_kwargs: object) -> None: ...
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None: ...
        def post(self, *_args: object, **_kwargs: object) -> None:
            raise httpx.ConnectError(SECRET)

    monkeypatch.setattr(pages_ep.httpx, "Client", _Client)

    with pytest.raises(ServiceUnavailableError) as caught:
        pages_ep._resolve(_state(), TABLE, "tok")  # noqa: SLF001
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


@pytest.mark.parametrize(("status", "payload"), [(404, {}), (200, {})])
def test_a_genuinely_absent_table_stays_a_404(monkeypatch: pytest.MonkeyPatch, status: int, payload: dict) -> None:
    """The two cases that really do mean absent must not become 503 — that would make a missing
    registration look retryable and hide it behind an outage alert."""

    class _Response:
        status_code = status

        def json(self) -> dict:
            return payload

    class _Client:
        def __init__(self, **_kwargs: object) -> None: ...
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None: ...
        def post(self, *_args: object, **_kwargs: object) -> _Response:
            return _Response()

    monkeypatch.setattr(pages_ep.httpx, "Client", _Client)

    with pytest.raises(NotFoundError):
        pages_ep._resolve(_state(), TABLE, "tok")  # noqa: SLF001
