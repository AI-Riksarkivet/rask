"""OIDC ID-token verification wired directly on **PyJWT** (no python-jose / wrapper lib).

The cryptography is PyJWT's, not ours: ``PyJWKClient`` fetches and parses the provider's JWKS (this module holds the
signing keys it returns, never the client: see the rules below) and
``jwt.decode`` does the signature / issuer / audience / expiry math. What we add is the thin OIDC
plumbing PyJWT does not: discover the provider's ``.well-known/openid-configuration`` for the issuer +
JWKS URI, and enforce a *local* signing-algorithm allowlist before handing the token to ``jwt.decode``
(below). Provider-agnostic — works with Keycloak, Dex, Okta, Auth0, Entra, or Google by setting the
issuer + audience. No JWT cryptography is written here.

Security posture:

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
* **A credential for the fetch, when the issuer demands one.** A Kubernetes service-account issuer
  serves discovery and its key set only to a bearer and only from a private CA (measured on k3s
  2026-10-02: `--anonymous-auth=false` answers 401, the default trust store fails TLS). ``fetch_token_file``
  names a projected token re-read on every fetch, because the kubelet rotates it under the process;
  ``ca_file`` names the CA both fetches trust. ([[LH-220]])
* **Opaque failures, charged to their author.** A failure the presented token causes is a
  generic ``UnauthenticatedError``; one the provider or this deployment's configuration of it
  causes (discovery, the key set, a non-HTTPS URL, no algorithm in common) is a
  ``ProviderUnavailableError`` naming what failed. Neither leaks library names or crypto detail.
* **Held keys are never fetched in place; every fetch is a flight under the pace.** Three rules, which the
  code's shape keeps rather than a check:

  1. HELD STATE IS IMMUTABLE. A held `_Provider` carries its discovery document and the signing keys a fetch
     brought, and no client: nothing a request reads can fetch.
  2. EVERY FETCH GOES INTO A FRESH CLIENT (`_fetch_keys`) and replaces what is held only when the set it brought
     holds a signing key. A fetch that fails, or brings a set with no signing key, leaves what is held as it was,
     so the last good keys keep verifying, and is answered as the provider being unavailable to the request that
     fetched and to every request that joined it.
  3. EVERY FETCH ON A REQUEST PATH IS A FLIGHT UNDER THE PACE (`_RefreshPace.turn`): a provider while none is held,
     and the held key set again for a `kid` it lacks, each at most once per `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS`
     per issuer. The only other fetch is `refresh`, at startup and every ``cache_ttl`` in the background
     (`refresh_forever`). What that costs an operator: keys already held are admitted throughout; after the IdP
     repairs its keys, or rotates them, a token signed by a key not yet held is admitted at the first unknown-`kid`
     flight that begins once `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS` (30 s) has passed since the last one began, and at
     the latest at the next background refresh (``RASK_OIDC_CACHE_TTL``, default 300 s) when no request arrives.

  Fetches are installed in the order they BEGAN (`OIDCVerifier._install`): a fetch that began before the one held
  and lands after it is dropped, so a refresh that fetched before a rotation cannot replace the rotated keys a later
  flight installed.

  A request that fetched whenever its keys aged would, through an outage, make one fetch per request, each holding a
  worker thread for up to `HTTP_FETCH_TIMEOUT_SECONDS`, and empty the pool in seconds while the keys had not changed
  (ADR 0203); the `kid` a token names is the caller's unverified text, so no request may buy a fetch the pace did
  not admit. A request that meets a flight joins it, waits for it, bounded, and is judged on what that flight
  installed, as the request that fetched is.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import ssl
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Annotated, Any, Final, override
from urllib.parse import urlsplit

import httpx
import jwt
from lance_namespace import ServiceUnavailableError, UnauthenticatedError
from pydantic import AfterValidator, BaseModel, ConfigDict, InstanceOf, SkipValidation, ValidationError

from lakehouse.governed.projected_token import read_token_file
from lakehouse.process.own_threads import on_own_threads


_DISCOVERY_SUFFIX = "/.well-known/openid-configuration"

#: Wall-clock ceiling on EVERY outbound fetch this module makes — discovery AND JWKS.
#:
#: One constant because the two were asymmetric, and the unstated half was the one that matters more
#: (SKG-12). Discovery is fetched once per issuer per `cache_ttl`; the key set is fetched again when a token
#: presents an unknown `kid`, at most once per `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS` — on the REQUEST
#: path, at exactly the moment an IdP is mid-rotation and least healthy. `PyJWKClient` is not unbounded (PyJWT defaults it to 30s), but it was left to a
#: library default twice this module's own budget and free to drift on an upgrade, so a hung IdP held a
#: request worker for twice as long as the deployment ever decided it should.
HTTP_FETCH_TIMEOUT_SECONDS = 15.0

#: The least time between two key-set refetches forced by a token naming a `kid` the cached set does not hold, per
#: issuer. The `kid` is the caller's own text, unverified, so without a pace every anonymous request naming a novel one
#: buys a live fetch from the IdP (and, for a service-account issuer, the mirror's two from the API server), each free to
#: hold a threadpool worker for `HTTP_FETCH_TIMEOUT_SECONDS` ([[LH-431]]). Paced, a flood costs one fetch per interval,
#: and an honest rotation's first token is found by the first refetch that begins once the interval has passed. The same
#: interval as `lakehouse.events.published_keys.KEY_REFRESH_INTERVAL_SECONDS`, which paces the same refetch for signed lineage events.
UNKNOWN_KID_REFRESH_INTERVAL_SECONDS: Final = 30.0

#: The longest a request waits for a key-set or provider fetch another request began, before it is answered 503 as the
#: IdP's. An IdP that answers at all answers in milliseconds (Dex in-cluster on the dev estate, measured 2026-11-20: the
#: refetch landed 13 ms after a request of the same burst was refused), so one second finds every rotated key of an
#: honest burst. Against a stalled IdP the fetch itself may hold its one worker for `HTTP_FETCH_TIMEOUT_SECONDS`, once per
#: `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS`; every request that joins it, a forged `kid`'s or an honest one's, holds a
#: worker for at most this long and is answered 503.
REFRESH_FLIGHT_WAIT_SECONDS: Final = 1.0

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


class _Provider(BaseModel):
    """A provider as one fetch found it: its discovery document, the key-set URL it was fetched from, the safe
    algorithm set, the signing keys the set held (at least one), and where that fetch began in the verifier's order of
    fetches (`OIDCVerifier._install` keeps the latest).

    Frozen and holding no client, so a request that reads it can fetch nothing (the module docstring's rule 1).
    """

    # `arbitrary_types_allowed` for `keys`: a PyJWK is no pydantic type.
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    #: The document `_fetch` validated, kept as that same object.
    spec: InstanceOf[_Discovery]
    jwks_uri: str
    algorithms: list[str]
    #: The keys PyJWT parsed from the set, kept as it built them.
    keys: SkipValidation[tuple[jwt.PyJWK, ...]]
    began: int


log = logging.getLogger(__name__)


class _FetchCredential:
    """The bearer and CA an issuer's fetches carry; neither is set for a public IdP.

    The token is read on every fetch, never kept: a projected token is rotated by the kubelet at 80% of its
    lifetime, so a copy taken at construction stops working while the process runs.
    """

    def __init__(self, token_file: str | None, ca_file: str | None) -> None:
        self._token_file = token_file
        self.ssl_context: ssl.SSLContext | None = ssl.create_default_context(cafile=ca_file) if ca_file else None

    def headers(self) -> dict[str, str]:
        if self._token_file is None:
            return {}
        return {"Authorization": f"Bearer {read_token_file(self._token_file)}"}

    def verify(self) -> ssl.SSLContext | bool:
        return self.ssl_context if self.ssl_context is not None else True


class _CredentialedJWKClient(jwt.PyJWKClient):
    """PyJWT's key-set client, carrying the fetch credential read afresh on each fetch.

    `PyJWKClient.fetch_data` sends the ``headers`` it was built with; refreshing them first keeps the rest of
    its contract as shipped, including following no redirect, so the bearer reaches no host but the one the key-set URL
    names. A token that cannot be read is an ``OSError``, which `OIDCVerifier._fetch_keys` charges to the provider.

    ONE CLIENT, ONE FETCH, NO CACHE. `_fetch_keys` builds one per fetch and keeps only the signing keys it returns, so no
    held state lives in a client: PyJWT 2.15.1 caches any set that parses before it filters for signing keys, and a
    client kept would hold, and fetch again in place, a set no key of which signs.
    """

    def __init__(self, uri: str, credential: _FetchCredential, *, max_cached_keys: int, timeout: float) -> None:
        super().__init__(uri, cache_jwk_set=False, max_cached_keys=max_cached_keys, timeout=timeout, ssl_context=credential.ssl_context)
        self._credential = credential

    @override
    def fetch_data(self) -> Any:
        self.headers = self._credential.headers()
        return super().fetch_data()


class _Flight:
    """A fetch in flight on the request path: ended once its request leaves the fetch. `landed` is the provider it
    installed, which its own request and every request that joined it are judged on; `failure` says why it failed."""

    def __init__(self) -> None:
        self._ended = threading.Event()
        self.landed: _Provider | None = None
        self.failure: str | None = None

    def end(self, failure: str | None) -> None:
        self.failure = failure
        self._ended.set()

    def wait(self, timeout: float) -> bool:
        """Whether the fetch ended within ``timeout`` seconds; `landed` and `failure` are read only after it has."""
        return self._ended.wait(timeout)


class _Turn(BaseModel):
    """What `_RefreshPace.turn` gives a request. An internal value, like `_Provider`."""

    # `arbitrary_types_allowed` for `flight`: a `_Flight` is a live collaborator, kept as the very object others join.
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    #: This request fetches; the pace's interval starts now.
    fetches: bool
    #: The flight this request runs, or joined and saw end: the request records, or reads, what it installed.
    flight: SkipValidation[_Flight | None] = None
    #: Set when the request joined a fetch that failed, or that outlasted its wait: why the IdP's answer is not to be
    #: had, which the request answers as the fetch's own request does (`ProviderUnavailableError`).
    unavailable: str | None = None


class _RefreshPace:
    """Admits one fetch on the request path per `UNKNOWN_KID_REFRESH_INTERVAL_SECONDS` for one issuer: a provider that
    nothing holds yet, or the key set again for a `kid` the held one lacks.

    The pace is counted from when a fetch BEGAN, and the moment is claimed under the lock before the fetch runs, outside
    it. A request that meets a fetch in flight joins it: it starts none, and waits for that one to end, for at most
    `REFRESH_FLIGHT_WAIT_SECONDS`, so a key the IdP rotated in is found by every request of the burst that carried it
    rather than by the one that happened to claim the fetch; a joiner of a fetch that failed, or that outlasted the wait,
    is told the IdP is unavailable, as the fetch's own request is. A fetch that raises something other than
    `ProviderUnavailableError` is a defect of this module, which classifies every provider failure, and its own request
    answers 500 so the defect is seen; its joiners answer 503 all the same, since they never met the defect and their
    tokens were never judged: all they know is that the keys they waited for did not arrive. A request that meets no
    fetch in flight inside the interval fetches nothing and is judged on what is held. A fetch that fails counts like one that succeeds, since a
    hung IdP is exactly when another attempt costs most.

    Verification runs in the threadpool (every governed door's authentication is a sync dependency, and the body
    judge hands it to `run_in_threadpool`), so the wait is a `threading.Event`, never the event loop's.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._began: float | None = None
        self._flight: _Flight | None = None

    @contextmanager
    def turn(self) -> Generator[_Turn]:
        """This request's turn, held for the ``with`` block. A turn that fetches ends its flight when the block is left,
        failed if the block raised, and so wakes every request that joined it."""
        fetching: _Flight | None = None
        with self._lock:
            joined = self._flight
            if joined is None:
                now = self._clock()
                if self._began is None or now - self._began >= UNKNOWN_KID_REFRESH_INTERVAL_SECONDS:
                    self._began = now
                    self._flight = fetching = _Flight()
        if joined is not None:
            # Outside the lock, so the fetch can end.
            if joined.wait(REFRESH_FLIGHT_WAIT_SECONDS):
                yield _Turn(fetches=False, flight=joined, unavailable=joined.failure)
            else:
                yield _Turn(fetches=False, unavailable=f"The OIDC issuer did not answer within {REFRESH_FLIGHT_WAIT_SECONDS} s")
            return
        if fetching is None:
            yield _Turn(fetches=False)
            return
        failure: str | None = None
        try:
            yield _Turn(fetches=True, flight=fetching)
        except BaseException as exc:
            failure = str(exc) if isinstance(exc, ProviderUnavailableError) else "The OIDC issuer's fetch failed"
            raise
        finally:
            with self._lock:
                self._flight = None
            fetching.end(failure)


class ProviderUnavailableError(ServiceUnavailableError):
    """The provider could not be used — its documents or this deployment's configuration of it, never the token.

    The spec's `ServiceUnavailableError`, so any door answers it 503 with code 17 and a redacted detail; the message,
    which names the URL or setting that failed, reaches the log only. A door that audits the outage catches this type
    and raises the plain spec class, so the wire's title names the outage rather than the verifier.
    """


def _require_https(url: str, *, label: str, allow_insecure: bool) -> None:
    """Reject non-HTTPS ``url`` unless ``allow_insecure`` is set (dev-only escape hatch)."""
    if allow_insecure:
        return
    try:
        scheme = urlsplit(url).scheme
    except ValueError as exc:
        # A value no URL parser reads (an unclosed IPv6 bracket) is the same configuration fault as a wrong scheme.
        log.warning("oidc_url_unparseable", extra={"label": label, "error": str(exc)})
        raise ProviderUnavailableError(f"OIDC {label} is not a URL") from exc
    if scheme != "https":
        # A configuration fault, never the caller's: the URL is this deployment's setting or the provider's
        # discovery document, and no token is read. The log line names which one, because a Lance door's 503
        # redacts the message.
        #
        # The ISSUER case is caught earlier, at settings construction (`GovernedAuthSettings`), where a
        # misconfiguration belongs. This stays for the discovery override, and for `jwks_uri`, which
        # comes from discovery and so cannot be seen until verify time.
        log.warning("oidc_insecure_url", extra={"label": label, "scheme": scheme})
        raise ProviderUnavailableError(f"OIDC {label} must use HTTPS (set RASK_OIDC_ALLOW_INSECURE=true for dev IdPs)")


class OIDCVerifier:
    """Verify ID tokens against one or more providers, holding each issuer's discovery document and key set.

    The verifier is constructed once at app startup and shared across requests. Each configured issuer's provider is
    fetched by :meth:`refresh` (at startup, then every ``cache_ttl`` seconds by :meth:`refresh_forever`) and held until a
    later fetch lands whole; a request fetches one only while none is held, at the issuer's pace.
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
        fetch_token_file: str | None = None,
        ca_file: str | None = None,
        clock: Callable[[], float] = time.monotonic,
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
        self._clock = clock
        # Keyed by configured issuer, so a pace outlives the provider `refresh` replaces every `cache_ttl`, and is
        # bounded by the configuration rather than by anything a token says. Two per issuer: a request fetching the
        # provider nothing holds yet, and one fetching the held key set again for a `kid` it lacks, so neither delays
        # the other.
        self._refresh_paces = {iss: _RefreshPace(clock) for iss in self._issuers}
        self._discovery_paces = {iss: _RefreshPace(clock) for iss in self._issuers}
        self._leeway = leeway
        self._allow_insecure = allow_insecure
        self._credential = _FetchCredential(fetch_token_file, ca_file)
        # Keep the configured allowlist as an ordered, de-duplicated set of upper-cased
        # algorithm names so the intersection with the provider is deterministic.
        self._allowed = list(dict.fromkeys(alg.upper() for alg in allowed_algorithms))
        if not self._allowed:
            raise ValueError("OIDCVerifier requires a non-empty algorithm allowlist")
        # The provider each configured issuer's last whole fetch built, replaced only by a whole fetch that began later
        # (`_install`).
        self._held: dict[str, _Provider] = {}
        # A count, not the clock, orders the fetches: two fetches may begin within one tick of a monotonic clock, and
        # an injected clock may not move at all.
        self._fetches_begun = itertools.count()
        self._install_lock = threading.Lock()

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
        """The provider held for ``configured_issuer``, whatever its age; one fetched now when none is held, as a flight at
        the pace the issuer's discovery `_RefreshPace` admits, so an issuer that does not answer costs one fetch per
        interval, not one per request. A provider is held only once its key set arrived holding a signing key (`_fetch`),
        so a fetch that fails holds nothing and the requests after it meet the pace again. The request that fetched, and
        every request that joined it, read the provider that fetch installed, or are answered 503 when it failed. A
        request declined a fetch reads what is held at that moment, which a fetch that ended since its first read may have
        put there.

        Raises:
            ProviderUnavailableError: A fetch failed, the fetch joined failed or outlasted the wait, or none is held and
                the pace declines a fetch.
        """
        held = self._held.get(configured_issuer)
        if held is not None:
            return held
        with self._discovery_paces[configured_issuer].turn() as turn:
            if turn.fetches and turn.flight is not None:
                turn.flight.landed = self._install(configured_issuer, self._fetch(configured_issuer))
        if turn.unavailable is not None:
            raise ProviderUnavailableError(turn.unavailable)
        landed = turn.flight.landed if turn.flight is not None else None
        held = landed if landed is not None else self._held.get(configured_issuer)
        if held is None:
            raise ProviderUnavailableError(f"The OIDC issuer {configured_issuer} has not answered yet")
        return held

    def _fetch(self, configured_issuer: str) -> _Provider:
        """Fetch ``configured_issuer``'s discovery document and then its key set, into the provider they make. Blocking:
        two fetches, the second into a fresh client (`_fetch_keys`). Called only by a flight (`_resolve`) and by
        `refresh`, the module docstring's rule 3.

        Raises:
            ProviderUnavailableError: The fetch failed, or what arrived cannot be used.
        """
        began = self._begin_fetch()
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
        # reaches its try block, so they are mapped here, to `ProviderUnavailableError`: a 503, as a door
        # answers when no verifier was built, because it is the same fact arriving later.
        try:
            with httpx.Client(timeout=HTTP_FETCH_TIMEOUT_SECONDS, verify=self._credential.verify()) as client:
                response = client.get(f"{discovery_base}{_DISCOVERY_SUFFIX}", headers=self._credential.headers())
                response.raise_for_status()
                # Parsed by the model rather than by `response.json()`: a body that is not JSON at all (a
                # proxy's sign-in page) then lands in the same `ValidationError` as one of the wrong shape,
                # instead of a `json.JSONDecodeError` that no branch below classifies.
                spec = _Discovery.model_validate_json(response.content)
        except (httpx.HTTPError, OSError) as exc:
            # OSError is the fetch credential failing to read: still this deployment's fault, never the token's.
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

        return _Provider(spec=spec, jwks_uri=jwks_uri, algorithms=algorithms, keys=self._fetch_keys(jwks_uri), began=began)

    def _begin_fetch(self) -> int:
        """Where a fetch beginning now stands in this verifier's order of fetches."""
        with self._install_lock:
            return next(self._fetches_begun)

    def _install(self, configured_issuer: str, fetched: _Provider) -> _Provider:
        """Hold ``fetched`` for ``configured_issuer`` when it holds a signing key and began after what is held, and
        return what is held then: the one place a provider is installed, by a flight and by `refresh` alike.

        A fetch that began earlier and landed later is dropped, and the request that made it is judged on the newer one
        held. Under the lock only the comparison and the write run, never a fetch.
        """
        with self._install_lock:
            held = self._held.get(configured_issuer)
            if not fetched.keys or (held is not None and held.began > fetched.began):
                if held is None:
                    raise ProviderUnavailableError(f"The OIDC key set at {fetched.jwks_uri} holds no usable signing key")
                return held
            self._held[configured_issuer] = fetched
            return fetched

    def _fetch_keys(self, jwks_uri: str) -> tuple[jwt.PyJWK, ...]:
        """The signing keys of the set at ``jwks_uri``, fetched now into a fresh client that is then dropped (the module
        docstring's rule 2); every failure to produce them is the PROVIDER's, never the token's. Called only by `_fetch`
        and by the unknown-`kid` flight (`_signing_key_for`).

        The failures are classified by AUTHOR, not by type: the try body is one third-party call whose every
        input is the provider's response, so whatever it raises is the provider's. Two kinds:

        * the set could not be fetched — `PyJWKClientConnectionError`, which `fetch_data` raises for a request
          that fails, is redirected, or is answered short of a whole response (`http.client.HTTPException`), or
          an `OSError` it does not wrap: a connection reset while the body is read, or the fetch credential
          failing to read;
        * anything else: the set arrived and holds no usable signing key (every key marked for another ``use``, or
          carrying no ``kid``, or none PyJWK can build). Measured on PyJWT 2.15.1 (2026-11-06) that is `PyJWTError`
          (`PyJWKClientError`, `PyJWKSetError`), `ValueError` (a body that is not JSON), `TypeError` (a key whose
          ``alg`` is not hashable) and `RecursionError` (a body nested past the JSON parser's depth), but it is
          caught whole: PyJWK hands provider values to `cryptography`, whose exceptions are not all `ValueError`s
          (`UnsupportedAlgorithm` subclasses `Exception` alone), and a type left out of a named tuple would answer
          500 and be audited `invalid_token` against the caller.

        A set that failed is not asked for again in this call: against a slow IdP a second fetch doubles the time a
        request is held, and the next attempt is the pace's or `refresh`'s to make.
        """
        client = _CredentialedJWKClient(jwks_uri, self._credential, max_cached_keys=16, timeout=HTTP_FETCH_TIMEOUT_SECONDS)
        try:
            return tuple(client.get_signing_keys())
        except (jwt.PyJWKClientConnectionError, OSError) as exc:
            log.warning("oidc_jwks_unreachable", extra={"jwks_uri": jwks_uri, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC key set could not be reached at {jwks_uri}") from exc
        # Classified by author, not by type — see this docstring.
        except Exception as exc:
            log.warning("oidc_jwks_malformed", extra={"jwks_uri": jwks_uri, "error": str(exc)})
            raise ProviderUnavailableError(f"The OIDC key set at {jwks_uri} holds no usable signing key") from exc

    def refresh(self) -> list[tuple[str, str]]:
        """Fetch every configured issuer's provider and key set NOW, hold each that arrived whole, and report the ones
        that failed.

        Whole means a key set holding a signing key (`_fetch`). A failed fetch leaves the provider held before it, so an
        identity-provider outage, or a set it publishes broken, serves the last good keys.

        THE CONSTRUCTOR PERFORMS NO I/O, so a verifier that was merely built proves nothing about the
        IdP it points at. Called at startup, this puts a wrong issuer, a wrong split-horizon override or an
        unreachable IdP in the pod's log at boot, rather than in failed user traffic long after the pod
        reported healthy, and moves the fetch off the first user request.

        IT REPORTS, IT DOES NOT DECIDE. Returning the failures instead of raising keeps the decision with the
        caller, and no caller treats an unreachable IdP as fatal, or as a reason to leave rotation: an IdP
        blip would otherwise take every governed pod out of its Service at once, which is strictly worse
        than the 503 the door answers on its own. The pod stays up and says what is wrong.

        Each entry is ``(configured_issuer, reason)``. The issuer is carried because a split-horizon
        deployment fetches from an override, so the URL that failed is not the one an operator set as
        the issuer — and that override is the setting most likely to be the wrong one.
        """
        failures: list[tuple[str, str]] = []
        for configured in self._issuers:
            try:
                self._install(configured, self._fetch(configured))
            # Every failure is reported and none is raised — see this method docstring. An expected one (the provider
            # unreachable, or what it published unusable) was logged where it was classified; anything else is a defect
            # this loop outlives, logged here with its traceback.
            except ProviderUnavailableError as exc:
                failures.append((configured, str(exc)))
            except Exception as exc:
                log.exception("oidc_refresh_unexpected_failure", extra={"issuer": configured})
                failures.append((configured, f"{type(exc).__name__}: {exc}"))
        return failures

    async def refresh_forever(self) -> None:
        """:meth:`refresh` every ``cache_ttl`` seconds, off the event loop, until cancelled; a failed fetch is logged."""
        while True:
            await asyncio.sleep(self._ttl)
            for issuer, reason in await on_own_threads(self.refresh):
                log.warning("oidc_refresh_failed_serving_held_keys", extra={"issuer": issuer, "reason": reason})

    def _provider_for(self, token: str) -> _Provider:
        """Select the configured provider whose issuer matches the token's ``iss`` claim.

        The (still unverified) ``iss`` picks the provider, then verification proves it. A token naming
        no configured issuer is refused here, with one issuer as with several, so another issuer's token
        is a 401 that costs no fetch and never waits on a provider it does not belong to.
        """
        try:
            claimed = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
        for configured in self._issuers:
            if isinstance(claimed, str) and claimed.rstrip("/") == configured:
                return self._resolve(configured)
        raise UnauthenticatedError("Unrecognized token issuer")

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
        key = jwt.PyJWKClient.match_kid(list(provider.keys), kid)
        if key is not None:
            return key
        # An unknown kid may be a key the provider rotated in after the held set, so it earns one refetch, as
        # `PyJWKClient.get_signing_key` grants — but only as a flight at the pace `_RefreshPace` admits, because the kid
        # is the caller's unverified text. The refetch installs a provider of its own, with the keys it brought, only
        # when they include a signing key (`_fetch_keys`); the held one is never fetched into. The request that fetched
        # and every request that joined it are judged on what that flight installed, so a `refresh` replacing the held
        # provider meanwhile cannot answer one burst two ways; one declined a fetch is judged on what is held NOW, which
        # a flight that ended between its first read and its turn may have replaced. A joined flight that failed or
        # outlasted the wait is the IdP's fault, as it is the fetching request's; a set that landed and still lacks the
        # kid is the token's.
        issuer = provider.spec.issuer.rstrip("/")
        with self._refresh_paces[issuer].turn() as turn:
            if turn.fetches and turn.flight is not None:
                base = self._held.get(issuer, provider)
                began = self._begin_fetch()
                turn.flight.landed = self._install(
                    issuer, _Provider(spec=base.spec, jwks_uri=base.jwks_uri, algorithms=base.algorithms, keys=self._fetch_keys(base.jwks_uri), began=began)
                )
        if turn.unavailable is not None:
            raise ProviderUnavailableError(turn.unavailable)
        landed = turn.flight.landed if turn.flight is not None else None
        judged = landed if landed is not None else self._held.get(issuer, provider)
        key = jwt.PyJWKClient.match_kid(list(judged.keys), kid)
        if key is not None:
            return key
        raise UnauthenticatedError("Invalid or expired token")

    def verify(self, token: str) -> IDToken:
        """Verify a bearer token and return its parsed claims.

        Raises ``UnauthenticatedError`` when the token is at fault and ``ProviderUnavailableError`` when the
        provider's documents are — the two refusals the governed doors map. Pinned by
        `packages/lakehouse/tests/test_every_verifier_failure_is_a_401_or_a_503.py`.
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
                    # ``sub`` is required here (not just in the model, `IDToken.sub`): some IdP-issued
                    # tokens legitimately omit it —
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


__all__ = [
    "DEFAULT_ALLOWED_ALGORITHMS",
    "HTTP_FETCH_TIMEOUT_SECONDS",
    "REFRESH_FLIGHT_WAIT_SECONDS",
    "UNKNOWN_KID_REFRESH_INTERVAL_SECONDS",
    "IDToken",
    "OIDCVerifier",
    "ProviderUnavailableError",
]
