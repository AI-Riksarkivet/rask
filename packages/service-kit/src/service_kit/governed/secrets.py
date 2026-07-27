"""Read sensitive secrets from the Dapr secret store at boot — so the store is actually CONSUMED.

The security audit flagged that the OpenBao/Dapr secret store was wired but never read: services still
took their secrets from plaintext pod env, making the integration decorative — AND that the plaintext
secret still shipped in env, so reading it from the store on top was redundant, not protective. The fix:
when ``secrets_from_dapr`` is on, the chart omits the sensitive value from pod env entirely and the
service fetches its secret bundle from the local sidecar's secret store
(``GET /v1.0/secrets/<store>/<key>``) at startup as the SOLE source — retrying while the store seeds, and
failing closed (not silently booting on an empty key) if it never arrives. So the store is genuinely the
source of truth and the plaintext secret no longer lives in the environment.
"""

from __future__ import annotations

import logging

import httpx
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)


log = logging.getLogger(__name__)


def _is_transient(exc: BaseException) -> bool:
    """Whether a fetch failure can heal by waiting: connect/timeout (sidecar still coming up) and 5xx
    (store still seeding) retry; a 4xx is MISCONFIGURATION (bad store name, denied scope) that more
    waiting cannot fix — fail immediately instead of burning the whole boot-retry budget on it."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def fetch_dapr_secret(
    store: str,
    key: str,
    *,
    dapr_http_port: int = 3500,
    timeout: float = 5.0,
    retries: int = 10,
    backoff: float = 3.0,
) -> dict[str, str]:
    """Fetch a secret bundle ``{name: value}`` from the local Dapr secret store, retrying so a service
    that boots before the sidecar/store/seed are ready still gets it. Returns ``{}`` (and logs) only after
    exhausting retries — the caller decides whether that is fatal (fail-closed) or a fallback.

    Retries via tenacity with exponential backoff + jitter (initial=``backoff``, capped at 15s — the
    project resilience default), and ONLY for transient failures: a 4xx from the sidecar is
    misconfiguration and fails immediately rather than looping the boot budget away. Deliberately SYNC
    (blocking httpx — worst case ≈2 min at the defaults): async lifespans must call it via
    ``run_in_threadpool`` so a slow-seeding store never blocks the event loop."""
    url = f"http://localhost:{dapr_http_port}/v1.0/secrets/{store}/{key}"
    try:
        for attempt in Retrying(
            retry=retry_if_exception(_is_transient),
            stop=stop_after_attempt(retries),
            wait=wait_exponential_jitter(initial=backoff, max=15),
            before_sleep=before_sleep_log(log, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                resp = httpx.get(url, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict):
                    return {k: str(v) for k, v in data.items()}
                log.warning("dapr_secret_unexpected_shape", extra={"store": store, "key": key})
                return {}
    except Exception as exc:
        log.warning("dapr_secret_fetch_failed", extra={"store": store, "key": key, "error": str(exc)})
        return {}
    return {}  # unreachable; keeps the type-checker's every-path-returns view honest


def fetch_required_secrets(store: str, key: str, *, require: str) -> dict[str, str]:
    """Fetch the secret bundle, FAILING CLOSED (raise) if ``require`` is absent.

    When a service consumes secrets from the store, the store is the STRICT sole source — the chart ships
    no plaintext env for the sensitive value — so a miss must NOT silently boot on an empty key. Returns
    the full bundle (callers read the fields they need, e.g. the S3 secret + the DB password). This is the
    one place the fail-closed rule lives; catalog / lineage / compaction all call it (their previous
    copy-pasted fetch+raise blocks could drift)."""
    bundle = fetch_dapr_secret(store, key)
    if not bundle.get(require):
        raise RuntimeError(f"secret {require!r} unavailable from Dapr store {store!r}/{key!r} — failing closed (store is the sole source)")
    return bundle
