"""How every OpenFGA call retries a transient failure and fails closed, and the process's one circuit over OpenFGA.

Every read of ``lakehouse.governed.fga_reads`` (the checks, the listings, ``expand``) and the
post-create grant writes go through a bounded retry with exponential backoff + jitter (tenacity). Only
*transient* failures are retried — OpenFGA 429/5xx AND the dominant outage modes raised
by the aiohttp transport (connection refused / DNS / read timeout, i.e.
``aiohttp.ClientError`` / ``TimeoutError`` / ``OSError``). A definitive ``allowed=false``
is never retried. The per-request timeout is carried by the client itself
(``ClientConfiguration.timeout_millisec``, set in ``fga_provision.make_client``) and the SDK's own
retry layer is disabled there so tenacity is the single authoritative one. When retries
are exhausted (OpenFGA outage) we **fail closed** and raise ``ServiceUnavailableError``
so the request surfaces as a 503 rather than a 500 — and never as a silent allow/deny.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final, cast

import aiohttp
from lance_namespace import ServiceUnavailableError
from openfga_sdk.exceptions import ApiException
from openfga_sdk.models.check_error import CheckError
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential_jitter,
)

from lakehouse.governed.machine_identity import IdentityTokenUnavailableError


log = logging.getLogger(__name__)


class RetryPolicy(BaseModel):
    """How one OpenFGA call retries a transient failure before it fails closed: every call's one statement of it."""

    model_config = ConfigDict(frozen=True)

    #: Tries in all, the first included.
    attempts: int = Field(default=3, ge=1)
    #: The first wait between two tries, which doubles (with jitter) up to ``max_backoff_seconds``.
    backoff_seconds: float = Field(default=0.2, ge=0)
    max_backoff_seconds: float = Field(default=2.0, ge=0)
    #: The wall clock one logical call's tries may take in all, so a flapping OpenFGA (whose own backoff can be long)
    #: cannot pin a request worker for minutes.
    overall_budget_seconds: float = Field(default=15.0, gt=0)


#: What a call retries by when its caller names no policy.
DEFAULT_RETRY: Final = RetryPolicy()
#: One try and no wait, for a read on a path with a deadline of its own, which degrades rather than waits.
ONE_ATTEMPT: Final = RetryPolicy(attempts=1)

#: Calls in a row that end in an outage (each after its retries) before the circuit opens. While it is open every call
#: is refused at once with the same 503, so a request does not pay the retry budget against an OpenFGA that is down.
BREAKER_THRESHOLD: Final = 5
#: Seconds an open circuit refuses every call before it lets calls through to ask again; the first answer closes it, the
#: first outage opens it again.
BREAKER_RESET_SECONDS = 15.0

# The async (aiohttp) OpenFGA client raises a down/unreachable server as a raw transport
# error — NOT openfga_sdk.ApiException — so these must be retried AND caught by the
# fail-closed handlers, otherwise the single most likely outage escapes as a 500.
# (asyncio.TimeoutError is builtin TimeoutError on 3.11+; ClientConnectorError ⊂ OSError.)
#
# The pod's own token file failing to read is the same kind: the request was never sent, the next read may find the
# file the kubelet wrote, and its error names the file's path, which the fail-closed answer keeps out of the body.
_TRANSIENT_NETWORK: tuple[type[BaseException], ...] = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
    IdentityTokenUnavailableError,
)


class UnansweredBatchItemsError(Exception):
    """BatchCheck answered, but not for every item: each one named here carries its own ``CheckError``.

    The SDK reports such an item as ``allowed=False``, so reading the verdict alone turns an OpenFGA
    fault into a denial. It is transient only when no item names an ``input_error``: an input fault is
    the per-item form of the 400 :func:`lakehouse.governed.fga_reads.check` does not retry, and it recurs on every attempt. The split
    is not exact on the deployed engine: with ``weighted_graph_check``, OpenFGA v1.18.3 reports an object
    carrying a control character as ``internal_error`` (measured), which :func:`lakehouse.governed.fga_ids.is_object_id` keeps from
    ever being sent.
    """

    def __init__(self, unanswered: dict[str, CheckError]) -> None:
        self.transient = not any(error.input_error for error in unanswered.values())
        named = ", ".join(f"{obj} ({error.input_error or error.internal_error}: {error.message})" for obj, error in sorted(unanswered.items()))
        super().__init__(f"OpenFGA could not answer {len(unanswered)} batch item(s): {named}")


# Every exception class the read/write paths fail CLOSED on (→ 503), never propagate.
FAIL_CLOSED: Final[tuple[type[BaseException], ...]] = (ApiException, UnansweredBatchItemsError, *_TRANSIENT_NETWORK)


# --------------------------------------------------------------------------- #
# Resilience helpers
# --------------------------------------------------------------------------- #


def _is_transient(exc: BaseException) -> bool:
    """True for failures worth retrying: transport errors or OpenFGA 429/5xx.

    The aiohttp transport raises a down/unreachable server as a raw network error
    (``aiohttp.ClientError`` / ``TimeoutError`` / ``OSError``) — the dominant outage
    mode — so those are retried. OpenFGA HTTP errors arrive as ``ApiException``; only
    429/5xx (and the SDK's status==0 prep failures) are transient. Definitive 4xx
    client errors (other than 429) are permanent and never retried. A BatchCheck whose
    unanswered items are all server-side faults is transient on the same reasoning.
    """
    if isinstance(exc, _TRANSIENT_NETWORK):
        return True
    if isinstance(exc, UnansweredBatchItemsError):
        return exc.transient
    if not isinstance(exc, ApiException):
        return False
    if exc.status in (0, None):
        return True  # request-prep failure surfaced by the SDK
    return bool(exc.is_retryable())


class _Circuit:
    """The process's one circuit over OpenFGA, which every call through :func:`guarded` passes.

    Closed, it counts outages in a row. At ``BREAKER_THRESHOLD`` it opens and refuses every call for
    ``BREAKER_RESET_SECONDS``; after that calls pass again, and the first to end decides: an answer closes the circuit,
    an outage opens it again at once. This pod's own token failing to read counts as an outage: no call can be sent
    without it. A call that ends any other way (cancelled, another fault of this process) says nothing about OpenFGA
    and changes nothing. Timed on the monotonic clock, so a stepped wall clock neither holds it open nor
    closes it early.
    """

    def __init__(self) -> None:
        self._outages = 0
        self._opened_at: float | None = None

    def refuses(self) -> bool:
        """Whether a call made now is refused without dialling."""
        return self._opened_at is not None and time.monotonic() - self._opened_at < BREAKER_RESET_SECONDS

    def answered(self) -> None:
        """OpenFGA answered, with a verdict or a definitive refusal of the request."""
        self._outages = 0
        self._opened_at = None

    def outage(self) -> None:
        """A call ended in an outage after its retries."""
        self._outages += 1
        if self._opened_at is not None or self._outages >= BREAKER_THRESHOLD:
            self._opened_at = time.monotonic()


_CIRCUIT = _Circuit()


def reset_circuit() -> None:
    """Close the circuit over OpenFGA and forget the outages it counted, as a process that has just started."""
    _CIRCUIT.answered()


def _log_retry(retry_state: RetryCallState) -> None:
    """Log each retry so a flapping OpenFGA is visible (silent retries hide outages)."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    log.warning(
        "openfga_call_retry",
        extra={
            "attempt": retry_state.attempt_number,
            "exception_type": type(exc).__name__ if exc else None,
            "status": getattr(exc, "status", None),
        },
    )


def retrying[T](policy: RetryPolicy) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Build a tenacity ``@retry`` decorator over transient OpenFGA failures.

    Retries both OpenFGA ``ApiException`` 429/5xx and raw transport errors (a down
    server), and is bounded by BOTH an attempt count and an overall wall-clock budget
    so a flapping OpenFGA cannot pin a worker. tenacity is the single retry layer — the
    SDK's own retries are disabled in :func:`lakehouse.governed.fga_provision.make_client`.
    """
    decorator = retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(policy.attempts) | stop_after_delay(policy.overall_budget_seconds),
        wait=wait_exponential_jitter(initial=policy.backoff_seconds, max=policy.max_backoff_seconds),
        before_sleep=_log_retry,
        reraise=True,
    )
    # `cast` rather than a looser annotation: tenacity's `retry` is typed to return its own
    # `WrappedFn`-preserving decorator, which does not unify with the plain callable shape every call
    # site here uses (a zero- or one-argument async closure). The cast asserts exactly what tenacity
    # documents — the wrapper has the wrapped function's signature and awaits to the same value.
    return cast("Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]", decorator)


async def guarded[T](
    operation: Callable[[], Awaitable[T]],
    *,
    event: str,
    extra: dict[str, object] | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> T:
    """Run one OpenFGA operation under the module's SINGLE resilience posture: retry, then fail closed.

    Nine call sites carried a byte-identical copy of this — the ``@retrying(...)`` decoration, the
    ``except FAIL_CLOSED``, the ``log.error(..., exc_info=True)`` and the same
    ``ServiceUnavailableError("authorization service unavailable")`` (SKG-06). Duplication is the wrong
    shape for THIS in particular: the posture is a security property, so the failure mode of the copies
    is that one of them quietly stops matching — a path that logs at the wrong level, retries a
    definitive deny, or worst, lets a transport error escape as a 500 and a caller reads an outage as a
    server bug rather than a refusal. One body makes that impossible by construction.

    ``event`` and ``extra`` are all that ever differed: the log line's name and the fields identifying
    what was being asked. The message is deliberately CONSTANT — a caller must not be able to tell
    which authorization call failed, and every one of them means the same thing to a client.

    Every call passes the process's circuit over OpenFGA (:class:`_Circuit`): after ``BREAKER_THRESHOLD`` outages in a
    row a call is refused with that same 503 without dialling, until ``BREAKER_RESET_SECONDS`` have passed.
    """
    if _CIRCUIT.refuses():
        log.warning("openfga_circuit_open", extra=extra)
        raise ServiceUnavailableError("authorization service unavailable")
    try:
        answer = await retrying(retry)(operation)()
    except FAIL_CLOSED as exc:
        if _is_transient(exc):
            _CIRCUIT.outage()
        else:
            _CIRCUIT.answered()
        log.error(event, extra=extra, exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc
    _CIRCUIT.answered()
    return answer


__all__ = [
    "BREAKER_RESET_SECONDS",
    "BREAKER_THRESHOLD",
    "DEFAULT_RETRY",
    "FAIL_CLOSED",
    "ONE_ATTEMPT",
    "RetryPolicy",
    "UnansweredBatchItemsError",
    "guarded",
    "reset_circuit",
    "retrying",
]
