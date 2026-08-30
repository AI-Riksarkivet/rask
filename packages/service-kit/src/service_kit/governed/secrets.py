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
import os
from typing import Final, Protocol

import httpx
from pydantic import SecretStr
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)


log = logging.getLogger(__name__)

#: Dapr's own default sidecar HTTP port, used only when daprd has not injected `DAPR_HTTP_PORT`.
_DEFAULT_DAPR_HTTP_PORT: Final = 3500


def _sidecar_http_port() -> int:
    """The local sidecar's HTTP port — `DAPR_HTTP_PORT` when daprd injected it (SKG-10).

    3500 was hard-coded as this module's only answer, which is wrong for any pod whose sidecar is not
    on the default port: the fetch then retries a closed port for the whole boot budget and reports an
    unreadable store. daprd injects `DAPR_HTTP_PORT` into every annotated pod, and
    `governed/actor_state_store.py` already reads it — this puts the two on the same source.
    """
    raw = os.environ.get("DAPR_HTTP_PORT")
    if not raw:
        return _DEFAULT_DAPR_HTTP_PORT
    try:
        return int(raw)
    except ValueError:
        # A malformed value is a misconfiguration, not a reason to fail the boot in a module whose
        # whole contract is "report, let the caller decide": say so and use the documented default.
        log.error("dapr_http_port_malformed", extra={"value": raw, "using": _DEFAULT_DAPR_HTTP_PORT})
        return _DEFAULT_DAPR_HTTP_PORT


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
    dapr_http_port: int | None = None,  # None → `DAPR_HTTP_PORT`, else Dapr's default; see `_sidecar_http_port`
    timeout: float = 5.0,
    retries: int = 10,
    backoff: float = 3.0,
) -> dict[str, str]:
    """Fetch a secret bundle ``{name: value}`` from the local Dapr secret store, retrying so a service
    that boots before the sidecar/store/seed are ready still gets it. Returns ``{}`` (and logs at ERROR,
    because this blocks a boot) only after exhausting retries against a store that would not answer —
    the caller decides whether that is fatal (fail-closed) or a fallback. A fault that is NOT the store
    failing to answer is raised, never laundered into an empty bundle that reads as "it holds nothing".

    Retries via tenacity with exponential backoff + jitter (initial=``backoff``, capped at 15s — the
    project resilience default), and ONLY for transient failures: a 4xx from the sidecar is
    misconfiguration and fails immediately rather than looping the boot budget away. Deliberately SYNC
    (blocking httpx — worst case ≈2 min at the defaults): async lifespans must call it via
    ``run_in_threadpool`` so a slow-seeding store never blocks the event loop."""
    port = dapr_http_port if dapr_http_port is not None else _sidecar_http_port()
    url = f"http://localhost:{port}/v1.0/secrets/{store}/{key}"
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
    except (httpx.HTTPError, ValueError) as exc:
        # NARROW, AND AN ERROR (SKG-15). `except Exception` here caught programming faults too — a bad
        # URL build, a broken monkeypatch, an AttributeError inside this function — and reported every
        # one of them as an empty bundle, which is indistinguishable from a store that legitimately
        # holds nothing. On a boot path that means a service comes up unconfigured and says so at
        # WARNING, below the level anything pages on. Only the failures the retry loop is ABOUT may be
        # reported as "the store did not answer": transport errors and non-2xx statuses (both
        # `httpx.HTTPError`) plus a malformed body (`ValueError`, which `Response.json` raises).
        # Anything else is a defect and propagates.
        log.error("dapr_secret_fetch_failed", extra={"store": store, "key": key, "error": str(exc)})
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


class SupportsDaprSecrets(Protocol):
    """The settings shape every store-consuming service already declares.

    A PROTOCOL rather than a shared base class: the four settings objects are independent
    ``BaseSettings`` subclasses with their own env prefixes and their own required fields, and giving
    them a common ancestor would drag one service's validators into the others. What they genuinely
    share is these five attributes, which is exactly what this names.
    """

    secrets_from_dapr: bool
    dapr_secret_store: str
    dapr_secret_key: str
    dapr_secret_s3_field: str
    s3_secret_access_key: SecretStr


def apply_dapr_secrets(settings: SupportsDaprSecrets) -> dict[str, str]:
    """Consume the S3 secret from the Dapr secret store and splice it into ``settings`` IN PLACE.

    THE ONE IMPLEMENTATION. This was written four times — ``lineage.core.config``,
    ``medallion.core.config``, ``maintenance.core.config`` and, inline in a lifespan,
    ``catalog.main`` — which is open_python-audit DUP-09, and the copies had already drifted: only the
    catalog's logged that the store had been consumed, and only the catalog's failed closed when the
    flag was OFF and no plaintext key was configured either.

    When ``secrets_from_dapr`` is on the store is the STRICT sole source: a store miss FAILS CLOSED
    (``fetch_required_secrets`` raises), never falling back to a plaintext env value — the chart ships
    none, and silently using one would contradict "OpenBao is the sole source". No-op (and no fetch)
    when off.

    Returns the whole bundle, so a service that needs a SECOND field from the same secret reads it off
    THIS fetch instead of issuing another one — that is how lineage's AGE password reaches
    ``apply_lineage_secrets`` (see ``lineage.core.config``). Empty dict when the flag is off.

    SYNC by design (the fetch retries while the store seeds, ~80s worst case), so async lifespans call
    it through ``run_in_threadpool``.

    IN PLACE IS THE MECHANISM — do not "fix" it into a copy. Every caller hands this the object its
    ``@lru_cache``d ``get_settings()`` returns, and that same object is what ``SettingsDep`` (and every
    later non-request read) resolves; assigning onto it IS how a boot-time secret reaches the request
    path. Returning a new object or ``model_copy(update=...)`` would leave the cache holding the
    un-spliced one, so every S3 call signs with an empty key — and it fails INVISIBLY: the boot
    succeeds, the pods go ready, and the estate reports itself healthy while the object store 403s.
    Freezing the model lands in the same place, because the accessor would then have to be re-seated
    (its cache cleared AND a spliced instance forced back in) at five call sites — more moving parts
    guarding a singleton that no request can reach. Holding the secret OUTSIDE ``Settings``, in a
    credential object the storage seam reads, is the one genuinely better shape; it is a change across
    four services and every ``storage_options()`` caller, not a local edit, and nobody has decided to
    spend it.

    Mutating shared state is sound here because of WHERE it runs: once, inside the lifespan, before the
    app serves its first request, single-threaded — no reader exists yet, so there is no race and no
    request can observe a half-spliced object. That is also the constraint: never call this from a
    request handler or a background task. Pinned by
    ``tests/unit/test_boot_secret_splice_is_in_place.py``.
    """
    if not settings.secrets_from_dapr:
        return {}
    bundle = fetch_required_secrets(settings.dapr_secret_store, settings.dapr_secret_key, require=settings.dapr_secret_s3_field)
    settings.s3_secret_access_key = SecretStr(bundle[settings.dapr_secret_s3_field])
    log.info("secret_from_dapr_store", extra={"store": settings.dapr_secret_store, "field": settings.dapr_secret_s3_field})
    return bundle
