"""OIDC ID-token verification wired directly on **PyJWT** (no python-jose / wrapper lib).

The cryptography is PyJWT's, not ours: ``PyJWKClient`` fetches + caches the provider's JWKS and
``jwt.decode`` does the signature / issuer / audience / expiry math. What we add is the thin OIDC
plumbing PyJWT does not: discover the provider's ``.well-known/openid-configuration`` for the issuer +
JWKS URI, and enforce a *local* signing-algorithm allowlist before handing the token to ``jwt.decode``
(below). Provider-agnostic — works with Keycloak, Dex, Okta, Auth0, Entra, or Google by setting the
issuer + audience. (We do NOT hand-roll JWT crypto — see the don't-reinvent audit.)

Security posture (see CHANGELOG in the task notes):

* **Local algorithm allowlist.** We never trust the provider's advertised
  ``id_token_signing_alg_values_supported`` blindly. We intersect it with a
  configured allowlist of asymmetric algorithms and pass only that safe set to
  ``jwt.decode``. ``none`` and the symmetric ``HS*`` family are rejected to defeat
  alg-confusion (an attacker signing ``HS256`` with the public key as the secret).
* **Clock-skew leeway.** A small, configurable ``leeway`` absorbs clock drift
  between us and the IdP while we still *require* ``exp`` / ``iat`` / ``aud``.
* **HTTPS enforcement.** Both the issuer and its ``jwks_uri`` must be HTTPS unless
  ``allow_insecure`` is set (needed only for the local Dex dev IdP over http).
* **Multiple accepted issuers.** ``issuer`` may be a single string or a list; each
  is discovered independently and a token is verified against the issuer whose
  discovery document matches the token's ``iss`` claim.
* **Split-horizon discovery.** ``discovery_overrides`` maps a configured issuer to
  the base URL its discovery document is fetched from — the reverse-proxy IdP
  topology where tokens carry the *public* issuer (what the browser sees) but that
  URL is not reachable from inside the cluster. Only the fetch location changes:
  the discovery document's ``issuer`` must still equal the configured issuer, and
  tokens are still validated against it. A ``jwks_uri`` under the issuer is rebased
  onto the override so the key fetch stays in-cluster; HTTPS enforcement applies to
  the override URL under the same ``allow_insecure`` knob.
* **Opaque failures, charged to their author.** A failure the presented token causes is a
  generic ``UnauthenticatedError``; one the provider or this deployment's configuration of it
  causes (discovery, the key set, a non-HTTPS URL, no algorithm in common) is a
  ``ProviderUnavailableError`` naming what failed. Neither leaks library names or crypto detail.
"""

from __future__ import annotations

import asyncio
import http.client
import logging
import time
from typing import Annotated, NamedTuple
from urllib.parse import urlsplit

import httpx
import jwt
from lance_namespace import UnauthenticatedError
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError

from service_kit.exceptions import ServiceUnavailableError


_DISCOVERY_SUFFIX = "/.well-known/openid-configuration"

#: Wall-clock ceiling on EVERY outbound fetch this module makes — discovery AND JWKS.
#:
#: One constant because the two were asymmetric, and the unstated half was the one that matters more
#: (SKG-12). Discovery is fetched once per issuer per `cache_ttl`; the JWKS client refetches whenever a
#: token presents an unknown `kid` — on the REQUEST path, at exactly the moment an IdP is mid-rotation
#: and least healthy. `PyJWKClient` is not unbounded (PyJWT defaults it to 30s), but it was left to a
#: library default twice this module's own budget and free to drift on an upgrade, so a hung IdP held a
#: request worker for twice as long as the deployment ever decided it should.
HTTP_FETCH_TIMEOUT_SECONDS = 15.0

#: Asymmetric signing algorithms we are willing to accept. Symmetric (``HS*``) and
#: ``none`` are deliberately excluded — accepting them with a public verification key
#: enables the classic alg-confusion forgery. Callers may further narrow this set,
#: but can never widen it past what the provider also advertises.
DEFAULT_ALLOWED_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
)


class IDToken(BaseModel):
    """OIDC ID-token claims; ``extra='allow'`` keeps provider-specific fields accessible.

    Subclass this for provider-specific claims and validate against the subclass,
    e.g. for Okta::

        class OktaIDToken(IDToken):
            email: str
            groups: list[str] = []

    The required core claims (``iss``/``sub``/``aud``/``exp``/``iat``) are present on
    every conformant OIDC ID token; everything else is preserved via ``extra='allow'``.
    """

    model_config = ConfigDict(extra="allow")

    iss: str
    sub: str
    aud: str | list[str]
    exp: int
    iat: int


def _fetchable_url(url: str) -> str:
    """``url`` unchanged, if it parses and names an HTTP scheme; `urlsplit` raising on one that does not parse is part of the check."""
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("must be an http or https URL")
    return url


class _Discovery(BaseModel):
    """The subset of the provider's discovery document we rely on (boundary-validated)."""

    issuer: str
    # Checked here, where a bad value is the document's fault: `_require_https` and `PyJWKClient` parse it
    # outside any mapping, and raise `ValueError` / `PyJWKClientError` for one that does not parse or is not HTTP.
    jwks_uri: Annotated[str, AfterValidator(_fetchable_url)]
    id_token_signing_alg_values_supported: list[str] = []


class _Provider(NamedTuple):
    """A resolved provider: its discovery doc, JWKS client, and the safe algorithm set.

    An internal value bundle (not boundary input), so a ``NamedTuple`` is the right
    container — no per-construction validation, just an immutable typed triple.
    """

    spec: _Discovery
    jwk_client: jwt.PyJWKClient
    algorithms: list[str]


log = logging.getLogger(__name__)


class ProviderUnavailableError(ServiceUnavailableError):
    """The provider could not be used — its documents or this deployment's configuration of it, never the token.

    A fleet `ServiceUnavailableError`, so the fleet doors render it with the URL or setting that failed. A
    door that answers in the Lance taxonomy catches this type and raises its own `lance_namespace` 503
    instead: that body carries the spec's `code` and redacts the message. Named here so such a door can
    catch it without importing the fleet taxonomy.
    """


def _require_https(url: str, *, label: str, allow_insecure: bool) -> None:
    """Reject non-HTTPS ``url`` unless ``allow_insecure`` is set (dev-only escape hatch)."""
    if allow_insecure:
        return
    if urlsplit(url).scheme != "https":
        # A configuration fault, never the caller's: the URL is this deployment's setting or the provider's
        # discovery document, and no token is read. The log line names which one, because a Lance door's 503
        # redacts the message.
        #
        # The ISSUER case is caught earlier, at settings construction (`GovernedAuthSettings`), where a
        # misconfiguration belongs. This stays for the discovery override, and for `jwks_uri`, which
        # comes from discovery and so cannot be seen until verify time.
        log.warning("oidc_insecure_url", extra={"label": label, "scheme": urlsplit(url).scheme})
        raise ProviderUnavailableError(f"OIDC {label} must use HTTPS (set RASK_OIDC_ALLOW_INSECURE=true for dev IdPs)")


class OIDCVerifier:
    """Verify ID tokens against one or more providers, caching discovery + JWKS per issuer.

    The verifier is constructed once at app startup and shared across requests. Each
    configured issuer is discovered lazily and cached for ``cache_ttl`` seconds.
    """

    def __init__(
        self,
        issuer: str | list[str],
        audience: str,
        cache_ttl: int,
        *,
        allowed_algorithms: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_ALGORITHMS,
        leeway: int = 60,
        allow_insecure: bool = False,
        discovery_overrides: dict[str, str] | None = None,
    ) -> None:
        issuers = [issuer] if isinstance(issuer, str) else list(issuer)
        if not issuers:
            raise ValueError("OIDCVerifier requires at least one issuer")
        # Normalise (strip trailing slash) and de-duplicate while preserving order.
        self._issuers = list(dict.fromkeys(iss.rstrip("/") for iss in issuers))
        # Split-horizon fetch locations, keyed by the normalized configured issuer (see the
        # module docstring): only where discovery/JWKS are FETCHED — never what tokens carry.
        self._discovery_overrides = {iss.rstrip("/"): url.rstrip("/") for iss, url in (discovery_overrides or {}).items()}
        self._audience = audience
        self._ttl = cache_ttl
        self._leeway = leeway
        self._allow_insecure = allow_insecure
        # Keep the configured allowlist as an ordered, de-duplicated set of upper-cased
        # algorithm names so the intersection with the provider is deterministic.
        self._allowed = list(dict.fromkeys(alg.upper() for alg in allowed_algorithms))
        if not self._allowed:
            raise ValueError("OIDCVerifier requires a non-empty algorithm allowlist")
        # Per-issuer cache: configured_issuer -> (fetched_at, _Provider).
        self._cache: dict[str, tuple[float, _Provider]] = {}

    def _safe_algorithms(self, advertised: list[str]) -> list[str]:
        """Intersect the provider's advertised algorithms with our local allowlist.

        Order follows our allowlist (our preference), and the result excludes anything
        not asymmetric. An empty intersection means we cannot safely verify against this
        provider, and the caller refuses it as the provider's fault.
        """
        advertised_upper = {alg.upper() for alg in advertised}
        # If the provider advertises nothing, fall back to our allowlist (RFC 8414 makes
        # the field OPTIONAL; many IdPs still only sign with RS256). This is still safe:
        # PyJWT enforces that the token's actual ``alg`` is in this asymmetric set.
        if not advertised_upper:
            return list(self._allowed)
        return [alg for alg in self._allowed if alg in advertised_upper]

    def _resolve(self, configured_issuer: str) -> _Provider:
        """Return the cached provider for ``configured_issuer``, refreshing past the TTL."""
        now = time.monotonic()
        cached = self._cache.get(configured_issuer)
        if cached is not None and (now - cached[0]) < self._ttl:
            return cached[1]

        # Split-horizon: fetch discovery from the override when one is configured for this
        # issuer; the issuer STRING (and every token check against it) is unchanged.
        override = self._discovery_overrides.get(configured_issuer)
        discovery_base = configured_issuer if override is None else override
        _require_https(
            discovery_base + _DISCOVERY_SUFFIX,
            label="issuer" if override is None else "discovery override",
            allow_insecure=self._allow_insecure,
        )
        # OURS, NOT THE CALLER'S. Everything from here to the mismatch check below is a statement
        # about this deployment's configuration and the issuer it points at — the presented token is
        # not read and plays no part. These errors are raised from `_provider_for`, before `verify`
        # reaches its try block, so they are mapped here, to `ProviderUnavailableError`: a 503, as
        # `deps.py` answers for a verifier it does not have, because it is the same fact arriving later.
        try:
            with httpx.Client(timeout=HTTP_FETCH_TIMEOUT_SECONDS) as client:
                response = client.get(f"{discovery_base}{_DISCOVERY_SUFFIX}")
                response.raise_for_status()
                # Parsed by the model rather than by `response.json()`: a body that is not JSON at all (a
                # proxy's sign-in page) then lands in the same `ValidationError` as one of the wrong shape,
                # instead of a `json.JSONDecodeError` that no branch below classifies.
                spec = _Discovery.model_validate_json(response.content)
        except httpx.HTTPError as exc:
            # The message names the LOCATION rather than the exception: an operator reading a 503
            # needs to know which URL this deployment could not use, and a split-horizon override is
            # precisely the setting most likely to be the one that is wrong.
            log.warning("oidc_discovery_unreachable", extra={"discovery_base": discovery_base, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC issuer could not be reached at {discovery_base}{_DISCOVERY_SUFFIX}") from exc
        except ValidationError as exc:
            log.warning("oidc_discovery_malformed", extra={"discovery_base": discovery_base, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC discovery document at {discovery_base}{_DISCOVERY_SUFFIX} is not a discovery document") from exc

        # The discovery document's own ``issuer`` is authoritative for token validation;
        # it must match what we configured (defends against a tampered discovery doc).
        #
        # A 503, not a 401, and that is the deliberate half of this change: the check compares the
        # DOCUMENT against this deployment's configuration, so a caller whose bearer is perfectly
        # good is told their credential is bad. The honest statement is that this service cannot
        # currently trust its own issuer. `_provider_for`'s "Unrecognized token issuer" stays a 401 —
        # that one IS about the token.
        if spec.issuer.rstrip("/") != configured_issuer:
            log.warning("oidc_discovery_issuer_mismatch", extra={"configured": configured_issuer, "advertised": spec.issuer})
            raise ProviderUnavailableError("The OIDC discovery document advertises a different issuer than this deployment is configured for")

        # A jwks_uri the provider advertises under its (public) issuer must be fetched from
        # the same split-horizon location as discovery; anything not under the issuer is a
        # provider hosting keys elsewhere and is used as advertised.
        jwks_uri = spec.jwks_uri
        if override is not None and (jwks_uri == configured_issuer or jwks_uri.startswith(configured_issuer + "/")):
            jwks_uri = override + jwks_uri[len(configured_issuer) :]
        _require_https(jwks_uri, label="jwks_uri", allow_insecure=self._allow_insecure)
        algorithms = self._safe_algorithms(spec.id_token_signing_alg_values_supported)
        if not algorithms:
            # The provider's list against this deployment's allowlist; the token's own `alg` is not read.
            log.warning("oidc_no_common_algorithm", extra={"advertised": spec.id_token_signing_alg_values_supported, "allowed": self._allowed})
            raise ProviderUnavailableError("No mutually-supported OIDC signing algorithm")

        jwk_client = jwt.PyJWKClient(jwks_uri, cache_jwk_set=True, max_cached_keys=16, timeout=HTTP_FETCH_TIMEOUT_SECONDS)
        provider = _Provider(spec=spec, jwk_client=jwk_client, algorithms=algorithms)
        self._cache[configured_issuer] = (now, provider)
        return provider

    def warm(self) -> list[tuple[str, str]]:
        """Resolve every configured issuer NOW, and report the ones that failed.

        THE CONSTRUCTOR PERFORMS NO I/O, so a verifier that was merely built proves nothing about the
        IdP it points at. Discovery happens lazily in :meth:`_resolve` on the first request carrying a
        bearer, so without this call a wrong issuer, a wrong split-horizon override or an unreachable
        IdP surfaces as failed user traffic, long after the pod has reported healthy.

        IT REPORTS, IT DOES NOT DECIDE, and that is the load-bearing half. Returning the failures
        instead of raising keeps the decision with the caller, and today no caller treats an
        unreachable IdP as fatal — deliberately. Readiness that gates on a downstream dependency turns
        one IdP blip into every governed pod leaving its Service endpoints at once, which is strictly
        worse than the 503 the door answers on its own. The pod stays up and says what is wrong.

        Each entry is ``(configured_issuer, reason)``. The issuer is carried because a split-horizon
        deployment fetches from an override, so the URL that failed is not the one an operator set as
        the issuer — and that override is the setting most likely to be the wrong one.

        Warming also moves the discovery round-trip off the first user request: `_resolve` caches per
        issuer for ``cache_ttl``, so whoever signs in first after a rollout no longer pays for it.
        """
        failures: list[tuple[str, str]] = []
        for configured in self._issuers:
            try:
                self._resolve(configured)
            # Every failure is reported and none is raised — see this method docstring.
            except Exception as exc:
                failures.append((configured, str(exc)))
        return failures

    def _provider_for(self, token: str) -> _Provider:
        """Select the configured provider whose issuer matches the token's ``iss`` claim.

        With a single issuer this is trivial. With several, we read the (still unverified)
        ``iss`` from the token to pick the right provider, then verification proves it.
        """
        if len(self._issuers) == 1:
            return self._resolve(self._issuers[0])
        try:
            claimed = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
        for configured in self._issuers:
            if isinstance(claimed, str) and claimed.rstrip("/") == configured:
                return self._resolve(configured)
        raise UnauthenticatedError("Unrecognized token issuer")

    @staticmethod
    def _key_set(provider: _Provider, *, refresh: bool) -> list[jwt.PyJWK]:
        """The provider's signing keys; every failure to produce them is the PROVIDER's, never the token's.

        The failures are classified by AUTHOR, not by type: the try body is one third-party call whose every
        input is the provider's response, so whatever it raises is the provider's. Two kinds:

        * the set could not be fetched — `PyJWKClientConnectionError`, or the `OSError` /
          `http.client.HTTPException` raised once the request is sent (a connection closed before the status
          line or reset mid-body, a body cut short), which `fetch_data` does not wrap;
        * anything else: the set arrived and holds no usable signing key. Measured on pyjwt 2.13.0 that is
          `PyJWTError`, `ValueError`, `AttributeError`, `TypeError` and `RecursionError`, but it is caught
          whole: PyJWK hands provider values to `cryptography`, whose exceptions are not all `ValueError`s
          (`UnsupportedAlgorithm` subclasses `Exception` alone), and a type left out of a named tuple would
          answer 500 and be audited `invalid_token` against the caller.

        A set the cache served that fails is read once more with ``refresh=True``: `fetch_data` caches a body
        before it is parsed, so without that read an unusable set outlives the provider's fix by the cache's
        lifespan (300 s). A set fetched in this call is not asked for again — it is already the provider's
        current answer, and against a slow IdP a second fetch doubles the time a request is held. Nor is one
        that never arrived.
        """
        uri = provider.jwk_client.uri
        cache = provider.jwk_client.jwk_set_cache
        # The same test `get_jwk_set` applies before deciding to fetch.
        served_from_cache = not refresh and cache is not None and cache.get() is not None
        try:
            return provider.jwk_client.get_signing_keys(refresh=refresh)
        except (jwt.PyJWKClientConnectionError, OSError, http.client.HTTPException) as exc:
            log.warning("oidc_jwks_unreachable", extra={"jwks_uri": uri, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC key set could not be reached at {uri}") from exc
        # Classified by author, not by type — see this docstring.
        except Exception as exc:
            if served_from_cache:
                return OIDCVerifier._key_set(provider, refresh=True)
            log.warning("oidc_jwks_malformed", extra={"jwks_uri": uri, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC key set at {uri} holds no usable signing key") from exc

    def _signing_key_for(self, provider: _Provider, token: str) -> jwt.PyJWK:
        """The key the token's ``kid`` selects, with each failure charged to its one possible author.

        `PyJWKClient.get_signing_key_from_jwt` does the same lookup but raises one `PyJWKClientError` for
        "the set could not be fetched" and "the set holds no such kid" alike. Composed from its public parts
        instead, so the header (the caller's) and the key set (the provider's) are read in separate steps.
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
        # A token naming no key selects none: `get_signing_keys` keeps only keys that carry a `kid`, so no
        # fetch could answer it. (A non-string `kid` never gets here — the header read above refuses it.)
        if not isinstance(kid, str):
            raise UnauthenticatedError("Invalid or expired token")
        # An unknown kid may be a key the provider rotated in after the cached set: refetch once, as
        # `PyJWKClient.get_signing_key` does, before refusing the token.
        for refresh in (False, True):
            key = provider.jwk_client.match_kid(self._key_set(provider, refresh=refresh), kid)
            if key is not None:
                return key
        raise UnauthenticatedError("Invalid or expired token")

    def verify(self, token: str) -> IDToken:
        """Verify a bearer token and return its parsed claims.

        Raises ``UnauthenticatedError`` when the token is at fault and ``ProviderUnavailableError`` when the
        provider's documents are — the two refusals the governed doors map. Pinned by
        `packages/service-kit/tests/test_every_verifier_failure_is_a_401_or_a_503.py`.
        """
        provider = self._provider_for(token)
        signing_key = self._signing_key_for(provider, token)
        try:
            payload = jwt.decode(
                token,
                # The `PyJWK`, not its `.key`. PyJWT then verifies with the algorithm the key is bound to and
                # refuses a header naming any other (RFC 8725 §3.1: one key, one algorithm). Given the raw key
                # it prepares whatever family the header names, and a caller naming ES256 beside an RSA `kid`
                # gets a `TypeError` out of `prepare_key` rather than a `PyJWTError`.
                signing_key,
                algorithms=provider.algorithms,
                audience=self._audience,
                issuer=provider.spec.issuer,
                leeway=self._leeway,
                # PyJWT does not verify the OIDC ``at_hash`` binding (it has no access
                # token to bind against), so there is nothing to disable — unlike
                # python-jose, no ``verify_at_hash`` option exists or is needed here.
                options={
                    # ``sub`` is required here (not just in the model): the whole authz layer keys
                    # grants and checks on it, and some IdP-issued tokens legitimately omit it —
                    # without the require, such a token failed pydantic OUTSIDE the 401 mapping (500).
                    "require": ["exp", "iat", "aud", "sub"],
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
            # Inside the 401 mapping on purpose: a signed token whose claim SHAPES pydantic rejects
            # (e.g. a non-numeric exp) is a bad token — a 401, never an unhandled 500.
            return IDToken.model_validate(payload)
        except (jwt.PyJWTError, ValidationError) as exc:
            # Never leak the underlying JWT/crypto/validation error to the client.
            raise UnauthenticatedError("Invalid or expired token") from exc


async def verify_off_loop(verifier: OIDCVerifier, token: str) -> IDToken:
    """``verify`` for ``async def`` callers — the ONE place that knows verification blocks.

    :meth:`OIDCVerifier.verify` is synchronous, and on a cold cache or a key rotation it performs OIDC
    discovery and a JWKS fetch over the network (both bounded by :data:`HTTP_FETCH_TIMEOUT_SECONDS`
    at :meth:`_resolve`). Awaited inline from a coroutine it stalls the entire worker: every
    in-flight request in the pod, and any liveness probe mounted on the same app.

    A plain ``def`` route calling ``verify`` directly is CORRECT — FastAPI runs it in a threadpool —
    so this exists for the coroutine call sites only, and the estate's ``def`` doors are unchanged.

    It is a function here, not a method, on purpose: the test doubles across the estate implement
    ``verify`` and nothing else, so a method would force every fake to grow an async twin that decides
    its own threading — which is precisely the thing under test. Routing the hop through one function
    keeps the fakes honest and gives the estate a single place to change if verification ever gains a
    genuinely async path.

    Why it is centralised at all: the fix kept not travelling. It was written once on the ingest door
    (``docs/DECISIONS.md "The Python estate audit"`` ING-02) and the medallion door — a copy of the same ~120-line function —
    went on blocking, gating the cascade head. A fourth door should not be able to get this wrong.
    """
    return await asyncio.to_thread(verifier.verify, token)
