"""`fetch_image`'s retry is a POLICY, not a hand-rolled loop (PS-03).

Two properties the hand-rolled `for i in range(attempts)` loop could not hold, and both are about
the ~64 concurrent Ray readers the function's own docstring describes:

* **The backoff is jittered.** A fixed `base_delay * 2**i` makes every reader that hit the same
  overloaded IIIF server wake at the same instant and hit it together — the retry storm is
  synchronised by construction, so the load spike it is meant to ride out is reproduced exactly.
* **Exhaustion raises the transport error.** The loop terminated on `assert last is not None`,
  which `python -O` strips; the `attempts <= 0` path then reached `raise None` and answered
  `TypeError: exceptions must derive from BaseException` instead of the error that actually
  happened. A control-flow `assert` is a bug wearing a type-checker's clothes.

The 4xx-except-429 exclusion is the part that must NOT change, so it is pinned here too: a policy
that retries a 404 would be a regression dressed as a fix.
"""

import time

import httpx
import pytest


def _always(status: int | None = None, *, transport_error: bool = False):
    """A MockTransport handler that always fails the same way, and counts the attempts."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if transport_error:
            raise httpx.ConnectError("connection reset by peer", request=request)
        assert status is not None
        return httpx.Response(status, text="nope")

    return handler, calls


def _record_sleeps(monkeypatch) -> list[float]:
    """Capture every blocking sleep the retry policy performs, and never actually sleep."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))
    return slept


def test_the_backoff_is_jittered_so_concurrent_readers_do_not_resynchronise(monkeypatch):
    from htr.iiif import fetch_image

    handler, calls = _always(transport_error=True)
    slept = _record_sleeps(monkeypatch)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(httpx.TransportError):
        fetch_image("https://iiif.example/img", client=client, attempts=3, base_delay=1.0)

    assert len(calls) == 3
    assert len(slept) == 2
    # The un-jittered schedule the hand-rolled loop produced, exactly: 1.0 then 2.0. Any of these
    # landing on the nose means every reader in the fleet wakes together.
    assert slept != [1.0, 2.0]
    for i, delay in enumerate(slept):
        assert delay != pytest.approx(1.0 * (2**i)), f"attempt {i} slept the un-jittered {delay}s"


def test_an_exhausted_budget_raises_the_transport_error_not_a_stripped_assert(monkeypatch):
    from htr.iiif import fetch_image

    handler, calls = _always(transport_error=True)
    _record_sleeps(monkeypatch)

    # `attempts=0` is the shape that reached `assert last is not None` with `last` still None —
    # AssertionError with asserts on, `TypeError: exceptions must derive from BaseException` under
    # `python -O`. Neither is the error the caller needs to see.
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(httpx.TransportError):
        fetch_image("https://iiif.example/img", client=client, attempts=0, base_delay=1.0)

    assert calls, "the budget must still buy one attempt, and its error is what propagates"


def test_a_real_4xx_is_not_retried(monkeypatch):
    from htr.iiif import fetch_image

    handler, calls = _always(404)
    slept = _record_sleeps(monkeypatch)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(httpx.HTTPStatusError):
        fetch_image("https://iiif.example/img", client=client, attempts=3, base_delay=1.0)

    assert len(calls) == 1
    assert slept == []


@pytest.mark.parametrize("status", [429, 503])
def test_a_429_or_5xx_is_retried(monkeypatch, status: int):
    from htr.iiif import fetch_image

    handler, calls = _always(status)
    _record_sleeps(monkeypatch)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(httpx.HTTPStatusError):
        fetch_image("https://iiif.example/img", client=client, attempts=3, base_delay=1.0)

    assert len(calls) == 3
