"""A bearer the verifier cannot use is answered 401 or 503 at the governed door, and never 500.

`OIDCVerifier.verify` has exactly two refusal types, and the door every governed service authenticates at
(`lakehouse.governed.door_caller.caller_of`) is built around them: `UnauthenticatedError` when the PRESENTED TOKEN is
at fault (401, audited `invalid_token`), and `ProviderUnavailableError` when the IdP, or this deployment's view of it,
is at fault (503, audited `verifier_unavailable`, the fault itself in the service's log and not in the answer). Any
third type reaches the door's `except Exception` branch and is recorded as `invalid_token` against the caller, so each
503 case below also pins that its raise site uses the subclass the door catches.

Measured 2026-11-06 on PyJWT 2.15.1 / httpx 0.28.1, these paths reach the door as a type outside PyJWT's own
taxonomy unless `oidc.py` classifies them:

* a discovery document that is not JSON (a proxy's sign-in page) — pydantic's `ValidationError`, or
  `json.JSONDecodeError` were it read through `response.json()`;
* a key set that is not JSON — `json.JSONDecodeError` out of `PyJWKClient.fetch_data`, which wraps only transport
  failures;
* a key-set connection reset while its body is read — a bare `ConnectionResetError`, an `OSError` `fetch_data` does
  not wrap;
* a key whose `alg` is not a string — `TypeError` (unhashable) out of `PyJWK`, which `PyJWKSet` does not skip, since it
  skips only keys failing with `PyJWTError`;
* a key set nested past the JSON parser's depth — `RecursionError`;
* a token whose `alg` names another key family than the key its `kid` selects — `TypeError` out of PyJWT's
  `prepare_key` when `jwt.decode` is handed the raw key rather than the `PyJWK` that binds its algorithm. This is the
  one a CALLER chooses, so it must be a 401.

The key-set cases also cover the failures PyJWT does type: `PyJWKClientConnectionError` (a connection closed before
the status line, a body cut short, both `http.client.HTTPException`s `fetch_data` wraps), `PyJWKClientError` and
`PyJWKSetError` (a set with no key, or none PyJWK can build). None of them reads the token either, so they are the
provider's too, and a 401 for them would tell a caller holding a good bearer that it is bad — the same misattribution
`test_an_unreachable_idp_is_not_the_callers_fault.py` refuses for the discovery document.

THE IdP IS A REAL HTTP SERVER ON LOOPBACK. Neither httpx nor urllib is patched, so both fetches run the libraries'
own parsing — which is where every one of these failures is raised. A stubbed response object would replace exactly
the code under test.
"""

import base64
import json
import logging
import socket
import struct
import threading
import time
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from lakehouse.governed import oidc
from lakehouse.governed.audit import AUDIT_LOGGER
from lakehouse.governed.door_caller import caller_of
from lakehouse.governed.idp_identity import IdpVerifier
from lakehouse.governed.oidc import UNKNOWN_KID_REFRESH_INTERVAL_SECONDS, OIDCVerifier
from lakehouse.governed.settings import OidcProvider
from lakehouse.problems.ns_errors import install_problem_handlers


AUDIENCE = "rask"
KID = "signing-1"
DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_PATH = "/jwks"

#: How long a held answer waits for its release before the IdP answers anyway, so a test that fails never hangs.
HOLD_CEILING_SECONDS = 10.0

#: What a reverse proxy in front of an IdP answers when its session is gone: a 200 whose body is a page.
SIGN_IN_PAGE = b"<!doctype html><title>Sign in</title>"


class _DoorSettings(BaseModel):
    oidc_enabled: bool = True
    sa_issuer: str | None = None


_bearer = HTTPBearer(auto_error=False)


def _subject(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> str:
    """The subject the door every governed service authenticates at (`door_caller.caller_of`) reads off the bearer."""
    caller = caller_of(request.app.state, _DoorSettings(), credentials.credentials if credentials is not None else None)
    assert caller is not None
    return caller.subject


_Subject = Annotated[str, Depends(_subject)]


class _Fault(StrEnum):
    """An IdP that goes away mid-answer — a pod restarting while keys rotate."""

    CLOSED_BEFORE_ANSWERING = "closed-before-answering"
    BODY_CUT_SHORT = "body-cut-short"
    #: A pod killed mid-response: the reader gets a bare `ConnectionResetError`, which `fetch_data` does not wrap in
    #: `PyJWKClientConnectionError` — the one case only `_key_set`'s `OSError` member catches.
    RESET_MID_BODY = "reset-mid-body"


class _IdP(BaseModel):
    """What the loopback IdP serves, by path; a case overwrites one document, or faults one path, to break it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    issuer: str
    documents: dict[str, bytes]
    faults: dict[str, _Fault] = {}
    #: Paths whose answer waits until the mapped event is set: the fetch stays in flight for as long as the test says.
    holds: dict[str, threading.Event] = {}
    #: Set once a held path has been asked for: a fetch is in flight.
    holding: threading.Event = Field(default_factory=threading.Event)
    #: Paths answered 302 with the mapped URL as their `Location`.
    redirects: dict[str, str] = {}
    #: Requests served, by path.
    fetches: dict[str, int] = {}


def _b64url_uint(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def _discovery(issuer: str, *, jwks_path: str = JWKS_PATH, algorithms: tuple[str, ...] = ("RS256", "ES256")) -> bytes:
    """Both key families advertised by default, as Keycloak does and as an IdP advertising none implies.

    Both matter: `verify` intersects the advertised algorithms with its allowlist, so an IdP advertising only RS256
    would refuse an ES256 header before any key is prepared, and the family-mismatch case could not arise.
    """
    return json.dumps({"issuer": issuer, "jwks_uri": f"{issuer}{jwks_path}", "id_token_signing_alg_values_supported": list(algorithms)}).encode()


def _key_set(key: rsa.RSAPrivateKey, kid: str, *, use: str = "sig") -> bytes:
    public = key.public_key().public_numbers()
    jwk = {"kty": "RSA", "kid": kid, "use": use, "alg": "RS256", "n": _b64url_uint(public.n), "e": _b64url_uint(public.e)}
    return json.dumps({"keys": [jwk]}).encode()


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def stranger_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def idp(rsa_key: rsa.RSAPrivateKey) -> Iterator[_IdP]:
    #: Connections whose close must be a reset rather than an orderly FIN.
    resets: set[socket.socket] = set()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            served.fetches[self.path] = served.fetches.get(self.path, 0) + 1
            fault = served.faults.get(self.path)
            body = served.documents.get(self.path)
            if (release := served.holds.get(self.path)) is not None:
                served.holding.set()
                release.wait(HOLD_CEILING_SECONDS)
            if (location := served.redirects.get(self.path)) is not None:
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if fault is _Fault.CLOSED_BEFORE_ANSWERING:
                self.close_connection = True
                return
            if fault in (_Fault.BODY_CUT_SHORT, _Fault.RESET_MID_BODY) and body is not None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[: len(body) // 2])
                self.close_connection = True
                if fault is _Fault.RESET_MID_BODY:
                    resets.add(self.connection)
                return
            self.send_response(404 if body is None else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib's own parameter name
            return

    class _Server(ThreadingHTTPServer):
        def shutdown_request(self, request: socket.socket | tuple[bytes, socket.socket]) -> None:
            if not isinstance(request, socket.socket) or request not in resets:
                super().shutdown_request(request)
                return
            resets.discard(request)
            # A zero linger makes close() send RST. The base sends SHUT_WR first, whose FIN would reach the reader
            # ahead of the reset and read as a short body.
            request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.close_request(request)

    server = _Server(("127.0.0.1", 0), _Handler)
    issuer = f"http://127.0.0.1:{server.server_address[1]}"
    served = _IdP(issuer=issuer, documents={DISCOVERY_PATH: _discovery(issuer), JWKS_PATH: _key_set(rsa_key, KID)})
    # `shutdown()` waits out one poll of `serve_forever`; at the 0.5 s default that is most of this file's runtime.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        yield served
    finally:
        for release in served.holds.values():
            release.set()
        server.shutdown()
        server.server_close()


def _token(idp: _IdP, key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey, algorithm: str, *, kid: str | None = KID) -> str:
    now = int(time.time())
    claims = {"iss": idp.issuer, "sub": "gina", "aud": AUDIENCE, "iat": now, "exp": now + 300}
    return jwt.encode(claims, key, algorithm=algorithm, headers={} if kid is None else {"kid": kid})


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _client(idp: _IdP, *, clock: Callable[[], float] = time.monotonic) -> TestClient:
    """A governed app wired as every governed service wires one: the problem handlers, one verifier."""
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.oidc = IdpVerifier(
        OIDCVerifier(idp.issuer, AUDIENCE, cache_ttl=300, allow_insecure=True, clock=clock), [OidcProvider(issuer=idp.issuer, idp_id="corp")]
    )

    @app.get("/gated")
    def _gated(subject: _Subject) -> dict[str, str]:
        return {"subject": subject}

    return TestClient(app, raise_server_exceptions=False)


def _call(idp: _IdP, token: str, *, client: TestClient | None = None) -> tuple[int, str]:
    response = (client or _client(idp)).get("/gated", headers={"Authorization": f"Bearer {token}"})
    return response.status_code, response.text


@pytest.fixture
def audit_trail(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    return caplog


def _audited_reasons(caplog: pytest.LogCaptureFixture) -> list[object]:
    return [getattr(record, "audit.reason", None) for record in caplog.records if record.name == AUDIT_LOGGER]


def _logged_fault(caplog: pytest.LogCaptureFixture) -> str:
    """What the verifier said was wrong, as the service's own log carries it: the door answers a 503 that names no
    cause, and logs the refusal with the verifier's fault as its cause."""
    [fault] = [record.exc_info[1].__cause__ for record in caplog.records if record.name == __name__ and record.exc_info and record.exc_info[1]]
    return str(fault)


# ── the controls: the fix must not blur the line between the two refusals ─────────────────────────────


def test_a_token_the_key_set_signed_is_accepted(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    status, body = _call(idp, _token(idp, rsa_key, "RS256"))

    assert status == 200, body
    assert json.loads(body) == {"subject": "corp~gina"}


class _Joiners:
    """Counts the requests waiting on another request's key-set fetch, so a test releases that fetch only once they
    are all waiting on it rather than after a sleep tuned to the machine."""

    def __init__(self) -> None:
        self._waiting = 0
        self._changed = threading.Condition()

    def joined(self) -> None:
        with self._changed:
            self._waiting += 1
            self._changed.notify_all()

    def wait_for(self, count: int) -> None:
        with self._changed:
            assert self._changed.wait_for(lambda: self._waiting >= count, timeout=HOLD_CEILING_SECONDS), f"{self._waiting} of {count} joined"


@pytest.fixture
def joiners(monkeypatch: pytest.MonkeyPatch) -> _Joiners:
    counted = _Joiners()

    class _CountedFlight(oidc._Flight):
        def wait(self, timeout: float) -> bool:
            counted.joined()
            return super().wait(timeout)

    monkeypatch.setattr(oidc, "_Flight", _CountedFlight)
    return counted


def _rotated(idp: _IdP, rsa_key: rsa.RSAPrivateKey, new_key: rsa.RSAPrivateKey, client: TestClient) -> threading.Event:
    """The verifier holds the set `rsa_key` is in; the IdP now serves `new_key` alone, its answer held until the
    returned event is set."""
    assert _call(idp, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    idp.documents[JWKS_PATH] = _key_set(new_key, "signing-2")
    release = idp.holds[JWKS_PATH] = threading.Event()
    return release


def test_a_burst_meeting_a_key_rotation_waits_for_one_key_set_fetch(
    idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey, joiners: _Joiners
) -> None:
    """The IdP rotates its key after the verifier fetched the set (a Dex restart mints new keys), and a page then sends
    several calls at once with a token the new key signed: every one is judged on the set ONE refetch brings, so none is
    refused for arriving while that refetch was in flight (measured on the dev estate 2026-11-20: one of two concurrent
    lineage calls answered 401 while the other's refetch landed 13 ms later). A `kid` is the caller's to write,
    unverified, so the forged ones in the same burst cost no fetch of their own and are refused once the set has landed
    without them, and a forged one after it, inside the interval, is refused at once ([[LH-431]]). The IdP holds its
    answer until every other request of the burst is waiting on that fetch."""
    client = _client(idp)
    release = _rotated(idp, rsa_key, stranger_key, client)
    rotated = [_token(idp, stranger_key, "RS256", kid="signing-2") for _ in range(8)]
    forged = [_token(idp, rsa_key, "RS256", kid=f"forged-{n}") for n in range(8)]
    burst = [token for pair in zip(rotated, forged, strict=True) for token in pair]

    with ThreadPoolExecutor(max_workers=len(burst)) as pool:
        answers = [pool.submit(_call, idp, token, client=client) for token in burst]
        joiners.wait_for(len(burst) - 1)
        release.set()
        statuses = [answer.result()[0] for answer in answers]

    assert statuses == [200, 401] * len(rotated)
    assert _call(idp, _token(idp, rsa_key, "RS256", kid="forged-after"), client=client)[0] == 401
    assert idp.fetches[JWKS_PATH] == 2


def test_a_request_that_read_the_old_set_while_the_refetch_landed_is_judged_on_the_new_one(
    idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that read the held set before the refetch landed, and reaches the pace only after it ended, finds no
    fetch to join and the interval not passed: it is judged on the set held THEN, which the refetch has just replaced,
    not on the one it read."""
    client = _client(idp)
    release = _rotated(idp, rsa_key, stranger_key, client)
    token = _token(idp, stranger_key, "RS256", kid="signing-2")
    late_arrived, late_go = threading.Event(), threading.Event()
    turns = 0
    turn = oidc._RefreshPace.turn

    @contextmanager
    def late_second_turn(pace: oidc._RefreshPace) -> Generator[object]:
        nonlocal turns
        turns += 1
        if turns == 2:
            late_arrived.set()
            late_go.wait(HOLD_CEILING_SECONDS)
        with turn(pace) as granted:
            yield granted

    monkeypatch.setattr(oidc._RefreshPace, "turn", late_second_turn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_call, idp, token, client=client)
        assert idp.holding.wait(HOLD_CEILING_SECONDS)
        late = pool.submit(_call, idp, token, client=client)
        assert late_arrived.wait(HOLD_CEILING_SECONDS)
        release.set()
        assert first.result()[0] == 200
        late_go.set()
        status, body = late.result()

    assert status == 200, body
    assert idp.fetches[JWKS_PATH] == 2


def test_a_refresh_landing_beside_a_refetch_leaves_its_requests_one_answer(
    idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey, joiners: _Joiners, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background refresh may replace the held provider at any moment, here with the set it fetched before the IdP
    rotated, just as a refetch lands. The request that fetched and the requests that joined it are judged on the same
    set, the one the refetch brought, so one burst carrying one token is not answered 200 and 401 at once."""
    refreshed_before_rotation = OIDCVerifier(idp.issuer, AUDIENCE, cache_ttl=300, allow_insecure=True)
    assert refreshed_before_rotation.refresh() == []
    before_rotation = refreshed_before_rotation._held[idp.issuer]
    client = _client(idp)
    release = _rotated(idp, rsa_key, stranger_key, client)
    verifier: OIDCVerifier = client.app.state.oidc._verifier

    class _RefreshLandsWithTheRefetch(oidc._Flight):
        def end(self, failure: str | None) -> None:
            verifier._held[idp.issuer] = before_rotation
            super().end(failure)

    monkeypatch.setattr(oidc, "_Flight", _RefreshLandsWithTheRefetch)
    token = _token(idp, stranger_key, "RS256", kid="signing-2")

    with ThreadPoolExecutor(max_workers=3) as pool:
        submitted = [pool.submit(_call, idp, token, client=client) for _ in range(3)]
        joiners.wait_for(2)
        release.set()
        statuses = [answer.result()[0] for answer in submitted]

    assert statuses == [200, 200, 200]
    # The stale refresh's fetch, the first request's, and the one refetch the burst shares.
    assert idp.fetches[JWKS_PATH] == 3


def test_a_refresh_that_fetched_before_a_rotation_does_not_replace_the_keys_fetched_after_it(
    idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fetches are installed in the order they BEGAN, whatever order they land in: a background refresh that fetched
    the set before the IdP rotated, and lands only after a refetch brought the rotated set, is dropped, so a token the
    new key signed is admitted at once rather than refused until the next refetch the pace admits. The clock never
    moves, so the order holds without it."""
    verifier = OIDCVerifier(idp.issuer, AUDIENCE, cache_ttl=300, allow_insecure=True, clock=_Clock())
    assert verifier.refresh() == []
    fetch_keys = verifier._fetch_keys
    refresh_fetched, refresh_lands = threading.Event(), threading.Event()

    def held_back_in_the_refresh(jwks_uri: str) -> tuple[jwt.PyJWK, ...]:
        keys = fetch_keys(jwks_uri)
        if threading.current_thread().name == "refresh":
            refresh_fetched.set()
            refresh_lands.wait(HOLD_CEILING_SECONDS)
        return keys

    monkeypatch.setattr(verifier, "_fetch_keys", held_back_in_the_refresh)
    refresh = threading.Thread(target=verifier.refresh, name="refresh")
    refresh.start()
    assert refresh_fetched.wait(HOLD_CEILING_SECONDS)
    idp.documents[JWKS_PATH] = json.dumps(
        {"keys": [*json.loads(_key_set(rsa_key, KID))["keys"], *json.loads(_key_set(stranger_key, "signing-2"))["keys"]]}
    ).encode()
    rotated = _token(idp, stranger_key, "RS256", kid="signing-2")
    assert verifier.verify(rotated).sub == "gina"

    refresh_lands.set()
    refresh.join(HOLD_CEILING_SECONDS)

    assert verifier.verify(rotated).sub == "gina"
    assert verifier.verify(_token(idp, rsa_key, "RS256")).sub == "gina"


class _FlightEnd(StrEnum):
    """How the refetch the joiner waits on ends."""

    #: The IdP closes the connection: the fetching request's 503 is the IdP's.
    IDP_FAILED = "idp-failed"
    #: The verifier raises, before it fetches, something no branch classifies: a defect of ours.
    VERIFIER_DEFECT = "verifier-defect"


@pytest.mark.parametrize(("end", "answers", "key_set_fetches"), [(_FlightEnd.IDP_FAILED, [503, 503], 2), (_FlightEnd.VERIFIER_DEFECT, [500, 503], 1)])
def test_the_requests_that_joined_a_refetch_that_failed_are_told_the_idp_failed(
    end: _FlightEnd,
    answers: list[int],
    key_set_fetches: int,
    idp: _IdP,
    rsa_key: rsa.RSAPrivateKey,
    stranger_key: rsa.RSAPrivateKey,
    joiners: _Joiners,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refetch that fails answers its own request 503, the IdP's fault. A request that waited on it, holding a token
    whose `kid` only that refetch could have found, is answered the same, and as soon as the refetch fails, not when its
    wait runs out: a 401 would tell an honest caller its token is bad because the IdP is away. A refetch that raises
    what nothing classifies is a defect, and its own request answers 500 so the defect is seen; the joiner, which never
    met the defect and whose token was never judged, is told only that the keys it waited for did not arrive."""
    client = _client(idp)
    release = _rotated(idp, rsa_key, stranger_key, client)
    if end is _FlightEnd.IDP_FAILED:
        idp.faults[JWKS_PATH] = _Fault.CLOSED_BEFORE_ANSWERING
    else:

        def defective(self: OIDCVerifier, jwks_uri: str) -> tuple[jwt.PyJWK, ...]:
            release.wait(HOLD_CEILING_SECONDS)
            raise RuntimeError("a defect in the verifier")

        monkeypatch.setattr(OIDCVerifier, "_fetch_keys", defective)
    monkeypatch.setattr(oidc, "REFRESH_FLIGHT_WAIT_SECONDS", HOLD_CEILING_SECONDS)
    token = _token(idp, stranger_key, "RS256", kid="signing-2")

    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted = [pool.submit(_call, idp, token, client=client) for _ in range(2)]
        joiners.wait_for(1)
        released = time.monotonic()
        release.set()
        statuses = sorted(answer.result()[0] for answer in submitted)

    assert statuses == answers
    assert time.monotonic() - released < HOLD_CEILING_SECONDS / 2
    assert idp.fetches[JWKS_PATH] == key_set_fetches


def test_a_request_whose_wait_for_a_refetch_runs_out_is_told_the_idp_is_away(
    idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait for another request's refetch is bounded, so a stalled IdP holds no worker for its whole fetch budget;
    the request whose wait runs out is answered 503, as the IdP's, never 401 against its token."""
    monkeypatch.setattr(oidc, "REFRESH_FLIGHT_WAIT_SECONDS", 0.05)
    client = _client(idp)
    release = _rotated(idp, rsa_key, stranger_key, client)
    token = _token(idp, stranger_key, "RS256", kid="signing-2")

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_call, idp, token, client=client)
        assert idp.holding.wait(HOLD_CEILING_SECONDS)
        cut = _call(idp, token, client=client)
        release.set()
        assert first.result()[0] == 200

    assert cut[0] == 503, cut[1]
    assert _call(idp, token, client=client)[0] == 200
    assert idp.fetches[JWKS_PATH] == 2


def test_a_key_rotated_in_during_a_flood_is_found_once_the_interval_passes(idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey) -> None:
    """A key the provider rotates in after the cached set is found by a refetch, which a forged kid met first may delay by
    at most one interval and never lock out: refusing a rotated token from the cache would lock every caller out for
    the cache lifetime after each rotation. Three fetches in all: the first request's, the forged kid's, and the
    rotated token's once the interval has passed."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    assert _call(idp, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    assert _call(idp, _token(idp, rsa_key, "RS256", kid="forged"), client=client)[0] == 401
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "signing-2")
    clock.now += UNKNOWN_KID_REFRESH_INTERVAL_SECONDS

    status, body = _call(idp, _token(idp, stranger_key, "RS256", kid="signing-2"), client=client)

    assert status == 200, body
    assert idp.fetches[JWKS_PATH] == 3


# ── the caller's fault ──────────────────────────────────────────────────────────────────────────────


def test_a_token_signed_by_another_key_is_the_callers(idp: _IdP, stranger_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    status, body = _call(idp, _token(idp, stranger_key, "RS256"))

    assert status == 401, body
    assert _audited_reasons(audit_trail) == ["invalid_token"]


def test_a_token_that_names_no_kid_is_the_callers(idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    """The key set is fine; the token names no key at all."""
    status, body = _call(idp, _token(idp, rsa_key, "RS256", kid=None))

    assert status == 401, body
    assert _audited_reasons(audit_trail) == ["invalid_token"]


def test_a_token_whose_alg_names_another_key_family_is_the_callers(idp: _IdP, audit_trail: pytest.LogCaptureFixture) -> None:
    """ES256 in the header, the RSA key's `kid` beside it. Both values are the caller's to write, and RFC 8725 §3.1
    requires each key to be used with exactly one algorithm — so this is a refused token, not a fault."""
    status, body = _call(idp, _token(idp, ec.generate_private_key(ec.SECP256R1()), "ES256"))

    assert status == 401, body
    assert _audited_reasons(audit_trail) == ["invalid_token"]


# ── the IdP's fault, which is ours ──────────────────────────────────────────────────────────────────


def test_a_discovery_url_that_answers_404_is_ours(idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    del idp.documents[DISCOVERY_PATH]

    status, body = _call(idp, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "could not be reached" in _logged_fault(audit_trail)
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


def test_a_discovery_document_naming_another_issuer_is_ours(idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    """The document disagrees with this deployment's configuration; the token is never read."""
    idp.documents[DISCOVERY_PATH] = _discovery("https://another-issuer.example.test")

    status, body = _call(idp, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "different issuer" in _logged_fault(audit_trail)
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


def test_a_provider_advertising_no_algorithm_we_accept_is_ours(idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    """The provider's advertised list against this deployment's allowlist; the token's own `alg` plays no part."""
    idp.documents[DISCOVERY_PATH] = _discovery(idp.issuer, algorithms=("PS256",))

    status, body = _call(idp, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "signing algorithm" in _logged_fault(audit_trail)
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


def test_a_discovery_document_that_is_not_json_is_ours(idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    idp.documents[DISCOVERY_PATH] = SIGN_IN_PAGE

    status, body = _call(idp, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


def test_a_key_set_url_that_redirects_is_ours_and_the_redirect_is_not_followed(
    idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture
) -> None:
    """A key-set fetch follows no redirect, so the bearer it carries reaches no host but the one the key-set URL names.

    The redirect here points at the issuer's own key set, which would verify the token: a fetch that followed it would
    accept the caller.
    """
    idp.documents[DISCOVERY_PATH] = _discovery(idp.issuer, jwks_path="/moved")
    idp.redirects["/moved"] = f"{idp.issuer}{JWKS_PATH}"

    status, body = _call(idp, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert _logged_fault(audit_trail) == f"The OIDC key set could not be reached at {idp.issuer}/moved"
    assert JWKS_PATH not in idp.fetches
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


@pytest.mark.parametrize("fault", [_Fault.BODY_CUT_SHORT, _Fault.RESET_MID_BODY])
def test_a_key_set_the_idp_drops_mid_answer_is_ours(fault: _Fault, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    """Named unreachable, not unusable: the set never arrived, so nothing is known about its keys."""
    idp.faults[JWKS_PATH] = fault

    status, body = _call(idp, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "could not be reached" in _logged_fault(audit_trail)
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


#: Key-set bodies the provider could serve, none of which the token has any part in: one per exception family that
#: reaches `_key_set`'s catch-all on PyJWT 2.15.1, so narrowing it to a tuple that leaves a family out answers 500.
UNUSABLE_KEY_SETS = {
    # `json.JSONDecodeError`, a `ValueError`.
    "not-json": SIGN_IN_PAGE,
    # `PyJWKSetError`, a `PyJWTError`.
    "no-keys": json.dumps({"keys": []}).encode(),
    # `TypeError`: PyJWK looks the `alg` up in a dict.
    "alg-not-a-string": json.dumps({"keys": [{"kty": "RSA", "kid": KID, "use": "sig", "alg": ["RS256"], "n": "AQAB", "e": "AQAB"}]}).encode(),
    # `RecursionError`.
    "nested-past-the-parsers-depth": b"[" * 100_000 + b"]" * 100_000,
}


@pytest.mark.parametrize("key_set", UNUSABLE_KEY_SETS.values(), ids=UNUSABLE_KEY_SETS.keys())
def test_a_key_set_with_no_usable_key_is_ours(key_set: bytes, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    idp.documents[JWKS_PATH] = key_set

    status, body = _call(idp, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "holds no usable signing key" in _logged_fault(audit_trail)
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


def test_a_provider_whose_key_set_failed_is_fetched_again_only_at_the_pace(
    idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture
) -> None:
    """A provider is held only once its key set has arrived. Held without it, every later request, anonymous and naming
    a configured issuer, would fetch the set itself, unpaced, each holding a worker for the fetch budget. Unheld, the
    requests behind the failure answer 503 at the discovery pace, and once the IdP answers again the first request past
    the interval fetches both."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    idp.faults[JWKS_PATH] = _Fault.CLOSED_BEFORE_ANSWERING
    assert _call(idp, token, client=client)[0] == 503

    assert [_call(idp, token, client=client)[0] for _ in range(8)] == [503] * 8
    assert (idp.fetches[DISCOVERY_PATH], idp.fetches[JWKS_PATH]) == (1, 1)
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"] * 9

    del idp.faults[JWKS_PATH]
    clock.now += UNKNOWN_KID_REFRESH_INTERVAL_SECONDS
    status, body = _call(idp, token, client=client)

    assert status == 200, body
    assert (idp.fetches[DISCOVERY_PATH], idp.fetches[JWKS_PATH]) == (2, 2)


def test_a_set_with_no_signing_key_leaves_the_held_keys_verifying(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """A refetch that brings a set with no signing key (every key for encryption) replaces nothing: the request that
    fetched it is told the IdP is unusable, the keys held before it keep verifying the tokens they signed, and no request
    behind it fetches the set again. A refetch that replaced the held set with the one it brought would leave every
    request a set it cannot verify with and a fetch of its own to make, outside every pace."""
    client = _client(idp)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, token, client=client)[0] == 200
    idp.documents[JWKS_PATH] = _key_set(rsa_key, KID, use="enc")
    assert _call(idp, _token(idp, rsa_key, "RS256", kid="novel"), client=client)[0] == 503

    assert [_call(idp, token, client=client)[0] for _ in range(20)] == [200] * 20
    assert idp.fetches[JWKS_PATH] == 2


def test_a_key_set_the_idp_has_since_fixed_is_believed_at_the_next_paced_refetch(
    idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """A key the IdP published unusable (for encryption only) and then fixed is believed by the first refetch the pace
    admits once the IdP serves it fixed, not at once: inside the interval the token it signed is judged on the keys held,
    as any `kid` the held set lacks is."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    assert _call(idp, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "signing-2", use="enc")
    token = _token(idp, stranger_key, "RS256", kid="signing-2")
    assert _call(idp, token, client=client)[0] == 503

    idp.documents[JWKS_PATH] = _key_set(stranger_key, "signing-2")
    assert _call(idp, token, client=client)[0] == 401
    clock.now += UNKNOWN_KID_REFRESH_INTERVAL_SECONDS
    status, body = _call(idp, token, client=client)

    assert status == 200, body
    assert idp.fetches[JWKS_PATH] == 3


class _RefreshFault(StrEnum):
    DISCOVERY_DOWN = "discovery-down"
    KEY_SET_DOWN = "key-set-down"
    NO_SIGNING_KEY = "no-signing-key"


@pytest.mark.parametrize("fault", list(_RefreshFault))
def test_a_refresh_that_fails_leaves_the_held_keys_verifying(fault: _RefreshFault, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """The background refresh replaces the held provider only with one that arrived whole, so an IdP that is down, or
    that serves a set no key of which signs, leaves the keys held before it verifying, and costs no request a fetch."""
    verifier = OIDCVerifier(idp.issuer, AUDIENCE, cache_ttl=300, allow_insecure=True)
    assert verifier.refresh() == []
    if fault is _RefreshFault.DISCOVERY_DOWN:
        idp.faults[DISCOVERY_PATH] = _Fault.CLOSED_BEFORE_ANSWERING
    elif fault is _RefreshFault.KEY_SET_DOWN:
        idp.faults[JWKS_PATH] = _Fault.CLOSED_BEFORE_ANSWERING
    else:
        idp.documents[JWKS_PATH] = _key_set(rsa_key, KID, use="enc")
    [(issuer, _)] = verifier.refresh()
    fetched = dict(idp.fetches)

    claims = verifier.verify(_token(idp, rsa_key, "RS256"))

    assert (issuer, claims.sub) == (idp.issuer, "gina")
    assert idp.fetches == fetched


def test_a_key_set_that_cannot_be_reached_is_asked_for_once(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """A set that never arrives is asked for once by the request that met it: against a hung IdP each attempt waits out
    the fetch timeout, so a second would double the time a request is held."""
    idp.faults[JWKS_PATH] = _Fault.CLOSED_BEFORE_ANSWERING

    assert _call(idp, _token(idp, rsa_key, "RS256"))[0] == 503
    assert idp.fetches[JWKS_PATH] == 1
