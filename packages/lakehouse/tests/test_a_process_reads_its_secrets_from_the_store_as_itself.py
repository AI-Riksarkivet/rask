"""A process reads its secrets from the secret store itself, logged in as its own role with the token its pod projects.

[[XC-199]] step D2. No sidecar stands between a service and the store: the process presents its projected
ServiceAccount token at the store's Kubernetes auth method, holds the store token that login returns in memory, and reads
each secret under it. Driven at the store's HTTP API, which respx answers in the shapes measured on the development
store (the root conftest's `secret_store`); the one claim about TLS runs against a real listener.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import closing
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from lakehouse.governed import secrets
from lakehouse.governed.secrets import SecretStore, SecretStoreSettings
from lakehouse.supervision import Supervisor
from tests.secret_store_double import SecretStoreDouble


SECRET = "signing-public-rask-sa-catalog"
FIELDS = {"keys": "UA1,UA2"}


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _projected_token() -> Path:
    """The file the kubelet projects this pod's store token into, as the root conftest writes it."""
    return Path(os.environ["RASK_SECRET_STORE_TOKEN_FILE"])


@pytest.fixture
def store(secret_store: SecretStoreDouble) -> Iterator[SecretStore]:
    """A session with the store the process is configured for, which has logged in to nothing yet."""
    opened = SecretStore(SecretStoreSettings())
    yield opened
    opened.close()


def _login_answer(secret_store: SecretStoreDouble, outcome: int | None) -> httpx.Response | type[Exception]:
    """The store's answer to a login: none (it cannot be reached), an admission (200), or a refusal by its status."""
    if outcome is None:
        return httpx.ConnectError
    return secret_store.logged_in() if outcome == 200 else secret_store.refused(outcome)


def _read_answer(secret_store: SecretStoreDouble, outcome: int) -> httpx.Response:
    """The store's answer to a read: the secret (200), or a refusal by its status."""
    return secret_store.held(FIELDS) if outcome == 200 else secret_store.refused(outcome)


def test_a_read_logs_in_as_the_pods_own_role_with_its_projected_token_and_reads_under_the_stores_token(
    store: SecretStore, secret_store: SecretStoreDouble
) -> None:
    read = secret_store.hold(SECRET, FIELDS)
    # The longest lease a login is taken with.
    secret_store.login.mock(return_value=secret_store.logged_in(lease_seconds=3600))

    fields = store.read(SECRET, retries=1, timeout=7.0)

    assert fields == FIELDS
    assert json.loads(secret_store.login.calls.last.request.content) == {"role": secret_store.ROLE, "jwt": _projected_token().read_text()}
    assert read.calls.last.request.headers["X-Vault-Token"] == secret_store.TOKEN
    # Each call is sent with what is left of the attempt's seconds as its bound, never with none.
    bounds = [call.request.extensions["timeout"]["read"] for call in (secret_store.login.calls.last, read.calls.last)]
    assert all(0 < bound <= 7.0 for bound in bounds), bounds


def _presented(secret_store: SecretStoreDouble) -> list[str]:
    """The projected token each login presented, oldest first."""
    return [json.loads(call.request.content)["jwt"] for call in secret_store.login.calls]


def test_a_services_loop_logs_in_again_once_half_the_lease_is_used_with_the_token_the_kubelet_holds_then(secret_store: SecretStoreDouble) -> None:
    """A store token lives 900 s and the kubelet rotates the projected token inside its 600 s life: a pass before 450 s
    asks for no login, and the pass at 450 s logs in with the file as it is then."""
    clock = _Clock()
    first = _projected_token().read_text()
    with closing(SecretStore(SecretStoreSettings(), clock=clock)) as store:
        store.log_in(retries=1)
        _projected_token().write_text("rotated-by-the-kubelet")
        clock.now += 449.0
        early = store.keep_logged_in()
        clock.now += 1.0
        due = store.keep_logged_in()

    assert (early, due, _presented(secret_store)) == (False, True, [first, "rotated-by-the-kubelet"])


def test_a_login_the_store_does_not_admit_leaves_the_held_token_in_use_until_its_lease_is_nearly_out(
    secret_store: SecretStoreDouble, caplog: pytest.LogCaptureFixture
) -> None:
    """The store asks the API server to review every login's token, so a review that is out fails logins while reads
    under a token already issued go on. Such a login costs no read: the held token serves for nine tenths of its lease
    while each pass asks again. A read past that is not served, and asks for no login of its own, and neither does the
    readiness report: while a loop is passing, the loop makes the next one, so a store that is failing is asked once a
    pass and never once a read or a probe."""
    clock = _Clock()
    read = secret_store.hold(SECRET, FIELDS)
    secret_store.login.mock(
        side_effect=[
            secret_store.logged_in(token="s.first"),
            secret_store.refused(),
            secret_store.refused(),
            secret_store.refused(),
            secret_store.logged_in(token="s.second"),
        ]
    )
    with closing(SecretStore(SecretStoreSettings(), clock=clock)) as store, caplog.at_level(logging.WARNING, logger=secrets.__name__):
        store.log_in(retries=1)
        clock.now += 450.0
        renewed = [store.keep_logged_in(), store.keep_logged_in()]
        clock.now += 359.0
        served_inside_the_lease = store.read(SECRET, retries=1)
        logins_inside_the_lease = secret_store.login.call_count
        clock.now += 1.0
        renewed_past_it = store.keep_logged_in()
        # Just under the minute after which a loop counts as silent: between two passes, which is where a service reads.
        clock.now += 59.0
        past_it = store.read(SECRET, retries=1)
        with pytest.raises(ConnectionError, match="holds no usable login"):
            store.answers()
        logins_by_then = secret_store.login.call_count
        logged_in_again = store.keep_logged_in()
        served_again = store.read(SECRET, retries=1)

    assert (renewed, served_inside_the_lease, logins_inside_the_lease) == ([False, False], FIELDS, 3)
    assert (renewed_past_it, past_it, logins_by_then, logged_in_again, served_again) == (False, {}, 4, True, FIELDS)
    assert [call.request.headers["X-Vault-Token"] for call in read.calls] == ["s.first", "s.second"]
    # Each said at the first pass that meets it, not at every one: a review that is out for minutes is one outage.
    assert [record.message for record in caplog.records] == ["secret_store_login_not_renewed", "secret_store_login_lapsed", "secret_store_read_failed"]


def test_a_read_logs_in_itself_once_the_loop_has_made_no_pass_for_a_minute(secret_store: SecretStoreDouble) -> None:
    """A loop that has made no pass for a minute keeps nothing: it was stopped, or a pass has stalled. A read that then
    finds the login used up makes the next one itself, as in a process that keeps no loop, so a loop that stopped does
    not leave every read unserved for good. Driven on the minute itself: a two-minute lease whose last pass came 59 s
    in, before the login was due, is used up (at 108 s) by the read that comes exactly a minute after that pass."""
    clock = _Clock()
    read = secret_store.hold(SECRET, FIELDS)
    secret_store.login.mock(side_effect=[secret_store.logged_in(token="s.first", lease_seconds=120), secret_store.logged_in(token="s.second")])
    with closing(SecretStore(SecretStoreSettings(), clock=clock)) as store:
        store.log_in(retries=1)
        clock.now += 59.0
        store.keep_logged_in()
        clock.now += 60.0
        served = store.read(SECRET, retries=1)

    assert (served, secret_store.login.call_count, read.calls.last.request.headers["X-Vault-Token"]) == (FIELDS, 2, "s.second")


def test_a_process_that_keeps_no_loop_logs_in_again_on_the_read_that_finds_its_token_used_up(secret_store: SecretStoreDouble) -> None:
    """A tool or a test keeps no loop, so nothing logs in for it: the read that finds its login used up makes the next
    one, with the projected token as its file holds it then."""
    clock = _Clock()
    secret_store.hold(SECRET, FIELDS)
    first = _projected_token().read_text()
    with closing(SecretStore(SecretStoreSettings(), clock=clock)) as store:
        store.read(SECRET, retries=1)
        _projected_token().write_text("rotated-by-the-kubelet")
        clock.now += 809.0
        store.read(SECRET, retries=1)
        logins_inside_the_lease = secret_store.login.call_count
        clock.now += 1.0
        store.read(SECRET, retries=1)

    assert (logins_inside_the_lease, _presented(secret_store)) == (1, [first, "rotated-by-the-kubelet"])


def test_a_login_that_ran_out_unrenewed_is_reported_once_however_many_passes_fail(secret_store: SecretStoreDouble, caplog: pytest.LogCaptureFixture) -> None:
    clock = _Clock()
    secret_store.login.mock(side_effect=[secret_store.logged_in(), secret_store.refused(), secret_store.refused(), secret_store.refused()])
    with closing(SecretStore(SecretStoreSettings(), clock=clock)) as store, caplog.at_level(logging.WARNING, logger=secrets.__name__):
        store.log_in(retries=1)
        clock.now += 810.0
        renewed = [store.keep_logged_in() for _ in range(3)]

    said = [(record.levelno, record.message) for record in caplog.records]
    assert (renewed, secret_store.login.call_count, said) == ([False] * 3, 4, [(logging.ERROR, "secret_store_login_lapsed")])


@pytest.mark.parametrize(
    ("logins", "reads"),
    [
        pytest.param([None, 200], [200], id="a-store-not-up-yet"),
        pytest.param([400, 200], [200], id="a-role-not-written-yet"),
        pytest.param([200], [403, 200], id="a-policy-not-written-yet"),
    ],
)
def test_an_attempt_the_store_did_not_serve_is_asked_again_within_the_callers_budget(
    store: SecretStore, secret_store: SecretStoreDouble, logins: list[int | None], reads: list[int]
) -> None:
    """A store still being seeded for this process refuses before it serves (measured: 400 for a role not written yet, 403
    for a secret its policy does not name yet; a secret not seeded yet answers 404 and takes the path the 403 does), so a
    boot waits each of them out inside its budget."""
    secret_store.login.mock(side_effect=[_login_answer(secret_store, outcome) for outcome in logins])
    read = secret_store.hold(SECRET, FIELDS)
    read.mock(side_effect=[_read_answer(secret_store, outcome) for outcome in reads])

    fields = store.read(SECRET, retries=3, backoff=0.001)

    assert (fields, secret_store.login.call_count, read.call_count) == (FIELDS, len(logins), len(reads))


def test_a_read_the_store_never_serves_is_reported_empty_after_one_attempt_on_a_request_path_and_said_as_a_warning(
    store: SecretStore, secret_store: SecretStoreDouble, caplog: pytest.LogCaptureFixture
) -> None:
    """What a miss costs is its caller's to say: a start that requires the secret fails on it, while a delivery's door
    answers retry. So the session reports the miss as a warning, one a request, never as an error of its own."""
    read = secret_store.hold(SECRET, secret_store.refused())

    with caplog.at_level(logging.DEBUG, logger=secrets.__name__):
        fields = store.read(SECRET, retries=1)

    failures = [record.levelno for record in caplog.records if record.message == "secret_store_read_failed"]
    assert (fields, read.call_count, failures) == ({}, 1, [logging.WARNING])


@pytest.mark.parametrize(
    "defect",
    [
        pytest.param(RuntimeError, id="the-kind-this-modules-own-not-served-error-is"),
        pytest.param(ValueError, id="the-kind-an-answer-that-does-not-validate-is"),
    ],
)
def test_a_fault_that_is_not_the_store_failing_to_serve_is_raised_never_reported_as_an_empty_secret(
    store: SecretStore, secret_store: SecretStoreDouble, defect: type[Exception]
) -> None:
    """Two errors this module reports as an empty secret are kinds of a broader one: its own not-served error is a
    `RuntimeError`, and an answer that does not validate a `ValueError`. Either broader kind, raised by anything else, is
    a defect and is raised."""
    secret_store.login.mock(side_effect=defect("a defect in the read path, not a store that refused"))

    with pytest.raises(defect, match="a defect in the read path"):
        store.read(SECRET, retries=1)


@pytest.mark.parametrize("held", [pytest.param(None, id="no-file"), pytest.param(b"\xff\xfe", id="not-text")])
def test_a_projected_token_that_cannot_be_read_is_an_attempt_not_served_and_nothing_is_presented(
    store: SecretStore, secret_store: SecretStoreDouble, held: bytes | None
) -> None:
    _projected_token().unlink()
    if held is not None:
        _projected_token().write_bytes(held)

    assert (store.read(SECRET, retries=1), secret_store.login.call_count) == ({}, 0)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param('{"auth": {"client_token": "s.sent-with-its-newline\\n", "lease_duration": 900}}', id="a-token-no-header-carries"),
        pytest.param('{"auth": {"client_token": "s.for-under-a-minute", "lease_duration": 59}}', id="a-lease-the-loop-cannot-keep"),
        pytest.param('{"auth": {"client_token": "s.for-a-month", "lease_duration": 3601}}', id="a-lease-past-the-hour-a-store-token-lives"),
    ],
)
def test_a_login_answer_whose_token_or_lease_this_process_does_not_take_is_no_login(store: SecretStore, secret_store: SecretStoreDouble, answer: str) -> None:
    """A lease is taken from a minute to an hour. The loop that keeps the login looks every 15 s and logs in again at
    half the lease: at a minute it is sure of one pass that asks the store and one that logs in before the token is used
    up, and under it of neither, so reads would go unserved until a pass replaced it; a token the store says never
    expires (zero) is under it too. Past the hour a store token lives at most (ADR
    0169) it is a credential that does not rotate: a role written with no `token_ttl` issues one at the store's default
    of weeks. And a token is what an HTTP header carries as it stands: the client refuses to send any other and quotes it
    in its error, which would put the token in a log line."""
    secret_store.login.mock(return_value=httpx.Response(200, content=answer))

    with pytest.raises(RuntimeError, match="names no token this process takes"):
        store.log_in(retries=1)


def test_no_token_and_no_secret_reaches_a_log_line(secret_store: SecretStoreDouble, caplog: pytest.LogCaptureFixture) -> None:
    """A start the store refuses once and then admits, a read it serves, a read it refuses, a pass it does not answer, a
    pass that finds the token dropped, a pass whose login it refuses and a start it never admits, logged at every level:
    the lines name the role and the secret, and carry neither the projected token, nor a store token, nor a value read."""
    clock = _Clock()
    secret_store.hold(SECRET, {"keys": "the-value-read"})
    secret_store.hold("commit-id-key", secret_store.refused())
    secret_store.login.mock(
        side_effect=[
            secret_store.refused(400, "role not found"),
            secret_store.logged_in(),
            secret_store.logged_in(token="s.the-second"),
            secret_store.refused(),
        ]
    )
    secret_store.lookup.mock(side_effect=[secret_store.honoured(), httpx.ConnectError, secret_store.refused()])

    with caplog.at_level(logging.DEBUG), closing(SecretStore(SecretStoreSettings(), clock=clock)) as store:
        store.log_in(retries=2, backoff=0.001)
        store.read(SECRET, retries=1)
        store.read("commit-id-key", retries=2, backoff=0.001)
        store.keep_logged_in()
        store.keep_logged_in()
        clock.now += 450.0
        store.keep_logged_in()
        secret_store.login.mock(return_value=secret_store.refused(400, "role not found"))
        with closing(SecretStore(SecretStoreSettings())) as never_admitted, pytest.raises(RuntimeError):
            never_admitted.log_in(retries=1)

    said = {record.message.split(" ")[0] for record in caplog.records if record.name == secrets.__name__}
    assert said >= {
        "Retrying",
        "secret_store_logged_in",
        "secret_store_read_failed",
        "secret_store_token_not_checked",
        "secret_store_token_dropped",
        "secret_store_login_not_renewed",
    }, f"a line this test is about was not logged, so it tests less than it says: {sorted(said)}"
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert [carried for carried in (_projected_token().read_text(), secret_store.TOKEN, "s.the-second", "the-value-read") if carried in logged] == []


def test_a_secret_holding_a_field_that_is_no_string_is_not_served(store: SecretStore, secret_store: SecretStoreDouble) -> None:
    """Every field rask reads is a string, so a null is refused with the whole answer, never read as the text "None"."""
    secret_store.hold(SECRET, httpx.Response(200, json={"data": {"data": {"keys": None}, "metadata": {"version": 1}}}))

    assert store.read(SECRET, retries=1) == {}


def test_a_secret_the_store_refuses_is_its_answer_and_costs_no_login(store: SecretStore, secret_store: SecretStoreDouble) -> None:
    """The store answers 403 alike to a token it dropped and to a secret the role's policy does not name, so a refusal
    says nothing of the token: a reader takes it as the answer and asks for no login on its account, however often it
    reads. Whether the token is still honoured is the loop's question to the store, never a reader's."""
    secret_store.hold(SECRET, FIELDS)
    refused = secret_store.hold("commit-id-key", secret_store.refused())

    answers = [store.read(name, retries=1) for name in (SECRET, "commit-id-key", "commit-id-key", "commit-id-key", SECRET)]

    asked = (secret_store.login.call_count, refused.call_count, secret_store.lookup.call_count)
    assert (answers, asked) == ([FIELDS, {}, {}, {}, FIELDS], (1, 3, 0))


def test_a_services_loop_logs_in_again_on_the_pass_that_finds_the_store_dropped_its_token(secret_store: SecretStoreDouble) -> None:
    """A store that was replaced, or revoked what it issued, answers the held token 403 whatever it is asked. Every pass
    has the token ask the store about itself (measured: 200 for a token the store honours, 403 for one it never issued),
    and the pass that finds it dropped logs in again, with most of the lease unused; the next pass asks about the new
    token, and a read goes out under it. The store here takes half a second to refuse: the login that follows is
    sent with the pass's whole seconds as its bound, since a slow store's answer to the question must not leave the
    login too few to be admitted in."""
    read = secret_store.hold(SECRET, FIELDS)
    secret_store.login.mock(side_effect=[secret_store.logged_in(token="s.first"), secret_store.logged_in(token="s.second")])
    answers = iter([secret_store.honoured(), secret_store.honoured(), None, secret_store.honoured()])

    def _answered(_request: httpx.Request) -> httpx.Response:
        answer = next(answers)
        if answer is not None:
            return answer
        time.sleep(0.5)
        return secret_store.refused()

    secret_store.lookup.mock(side_effect=_answered)
    with closing(SecretStore(SecretStoreSettings(), clock=_Clock())) as store:
        store.log_in(retries=1, timeout=3.0)
        logged_in_again = [store.keep_logged_in(timeout=3.0) for _ in range(3)]
        store.read(SECRET, retries=1)

    assert (logged_in_again, secret_store.login.call_count, read.calls.last.request.headers["X-Vault-Token"]) == ([False, True, False], 2, "s.second")
    assert [call.request.headers["X-Vault-Token"] for call in secret_store.lookup.calls] == ["s.first", "s.first", "s.first", "s.second"]
    # Each question is sent with what is left of its attempt's seconds as its bound, never with none.
    bounds = [call.request.extensions["timeout"]["read"] for call in secret_store.lookup.calls]
    assert all(bound is not None and 0 < bound <= 3.0 for bound in bounds), bounds
    login_bound = secret_store.login.calls.last.request.extensions["timeout"]["read"]
    assert 2.75 < login_bound <= 3.0, f"the login after a question that took 0.5 s was given {login_bound:.2f} s"


def test_a_pass_the_store_does_not_answer_asks_for_no_login_and_is_said_once_an_outage(
    secret_store: SecretStoreDouble, caplog: pytest.LogCaptureFixture
) -> None:
    """Silence and the store's own 5xx say nothing of the token, so the held one stays and no login is asked: only the
    store's refusal of the question is a dropped token. A loop that meets an outage at every pass says so at the first
    one, and again only once the store has answered in between."""
    secret_store.lookup.mock(
        side_effect=[secret_store.honoured(), httpx.ConnectError, secret_store.refused(503, "Vault is sealed"), secret_store.honoured(), httpx.ConnectError]
    )
    with closing(SecretStore(SecretStoreSettings(), clock=_Clock())) as store, caplog.at_level(logging.WARNING, logger=secrets.__name__):
        store.log_in(retries=1)
        logged_in_again = [store.keep_logged_in() for _ in range(4)]

    said = [record.message for record in caplog.records]
    assert (logged_in_again, secret_store.login.call_count, said) == ([False] * 4, 1, ["secret_store_token_not_checked"] * 2)


@pytest.mark.parametrize(
    ("lease_seconds", "questions"),
    [
        pytest.param(3600, 66, id="a-lease-not-due-meanwhile"),
        pytest.param(480, 63, id="a-lease-due-on-the-pass-a-wait-ends-at"),
    ],
)
def test_logins_the_store_goes_on_refusing_the_question_wait_twice_as_long_each_time_and_are_said_once(
    secret_store: SecretStoreDouble, caplog: pytest.LogCaptureFixture, lease_seconds: int, questions: int
) -> None:
    """The store refuses the question to the held token and to every one a new login returns: the role's policy lost the
    grant (the rendered one carries `auth/token/lookup-self`), or more than one store goes on answering at the address.
    A login at every pass would be a storm that cures nothing, and none at all would leave a store that dropped two
    tokens in a row unserved for half a lease. So each login the store refuses in turn waits twice the passes the last
    did, up to fifteen (four minutes at the loop's cadence), the loop says so once, and the readiness report says the
    store is not answering. Under an hour's lease, never due here, every one of those logins answers a refusal and the
    fifteen-pass wait is what spaces the last four. A login the lease asks for meanwhile is one of that row: under an
    eight-minute lease, whose half falls on the pass a wait ends at, the same passes log in, three of them without a
    question, and the pass after each makes none."""
    clock = _Clock()
    secret_store.login.mock(return_value=secret_store.logged_in(lease_seconds=lease_seconds))
    # The sixth pass falls in a wait and goes unanswered: it is a pass sat out all the same, so the wait ends when it would.
    secret_store.lookup.mock(
        side_effect=[secret_store.honoured()] + [secret_store.refused()] * 5 + [httpx.ConnectError] + [secret_store.refused()] * (questions - 7)
    )
    passes_that_logged_in = []
    with closing(SecretStore(SecretStoreSettings(), clock=clock)) as store, caplog.at_level(logging.ERROR, logger=secrets.__name__):
        store.log_in(retries=1)
        for number in range(1, 65):
            clock.now += 15.0
            if store.keep_logged_in():
                passes_that_logged_in.append(number)
        with pytest.raises(ConnectionError, match="auth/token/lookup-self"):
            store.answers()

    asked = (secret_store.login.call_count, secret_store.lookup.call_count)
    said = [record.message for record in caplog.records]
    assert (passes_that_logged_in, asked, said) == ([1, 2, 4, 8, 16, 32, 48, 64], (9, questions), ["secret_store_token_cannot_ask_about_itself"])


def test_a_login_the_store_has_answered_since_is_made_again_on_the_pass_that_finds_it_dropped(secret_store: SecretStoreDouble) -> None:
    """A replacement store beside the one it replaces: two stores answer at one address and each refuses the other's
    tokens. The replacement admits no login while it is still being seeded, which starts no wait: the next pass asks
    again. A login a pass then makes can land on the outgoing store and be refused by the one that stays, and the next
    pass logs in again at once. The store's answer to that login ends the waiting: the pass that later finds the token
    dropped logs in again as for any token, and the readiness report says the store dropped it meanwhile."""
    secret_store.login.mock(
        side_effect=[
            secret_store.logged_in(token="s.first"),
            secret_store.refused(400, "role not found"),
            secret_store.logged_in(token="s.second"),
            secret_store.logged_in(token="s.third"),
            secret_store.logged_in(token="s.fourth"),
        ]
    )
    secret_store.lookup.mock(
        side_effect=[
            secret_store.honoured(),  # the start's own question
            secret_store.refused(),  # the replacement never issued s.first, and holds no role yet to log in to
            secret_store.refused(),  # seeded now: the pass logs in again, as s.second
            secret_store.refused(),  # s.second came from the outgoing store, now gone: logged in again, as s.third
            secret_store.honoured(),  # the replacement answers s.third
            secret_store.refused(),  # that store is replaced in turn: the readiness report's question
            secret_store.refused(),  # and the pass's, which logs in again at once, as s.fourth
        ]
    )
    with closing(SecretStore(SecretStoreSettings(), clock=_Clock())) as store:
        store.log_in(retries=1)
        through_the_surge = [store.keep_logged_in() for _ in range(4)]
        with pytest.raises(ConnectionError):
            store.answers()
        after_the_second_replacement = store.keep_logged_in()

    assert (through_the_surge, after_the_second_replacement, secret_store.login.call_count) == ([False, True, True, False], True, 5)


def test_a_reader_waits_for_another_readers_login_no_longer_than_its_own_attempt(store: SecretStore, secret_store: SecretStoreDouble) -> None:
    """A store that keeps a login waiting keeps that reader's attempt, not every reader's in turn: the verification on a
    delivery's path names one short attempt and gets one, while another reader's login is still in flight."""
    in_flight, answered = threading.Event(), threading.Event()

    def _kept_waiting(_request: httpx.Request) -> httpx.Response:
        in_flight.set()
        answered.wait(timeout=5.0)
        return secret_store.logged_in()

    secret_store.login.mock(side_effect=_kept_waiting)
    secret_store.hold(SECRET, FIELDS)
    slow = threading.Thread(target=store.read, args=(SECRET,), kwargs={"retries": 1, "timeout": 30.0})
    slow.start()
    try:
        assert in_flight.wait(timeout=10.0), "the first reader's login never reached the store"
        began = time.monotonic()
        fields = store.read(SECRET, retries=1, timeout=0.2)
        waited = time.monotonic() - began
    finally:
        answered.set()
        slow.join(timeout=10.0)

    assert (fields, secret_store.login.call_count) == ({}, 1)
    assert waited < 2.0, f"a 0.2 s attempt waited {waited:.1f} s on another reader's login"


@pytest.mark.parametrize(
    ("login", "lookup", "answering"),
    [
        pytest.param(200, 200, True, id="a-token-the-store-honours"),
        pytest.param(403, None, False, id="a-login-it-does-not-admit"),
        pytest.param(200, 503, False, id="a-sealed-store"),
        pytest.param(None, None, False, id="a-store-out-of-reach"),
    ],
)
def test_the_store_is_answering_when_it_admits_this_process_and_honours_its_token(
    store: SecretStore, secret_store: SecretStoreDouble, login: int | None, lookup: int | None, answering: bool
) -> None:
    """What `/readyz` reports of the store is the store's own answer about this process's token: a store that admits
    no login, silence and the store's own 5xx are each not answering, at every probe. A store that refuses the token the
    question is driven where the loop meets it, in the tests of a refused pass."""
    secret_store.login.mock(side_effect=[_login_answer(secret_store, login)])
    if lookup is not None:
        secret_store.lookup.mock(return_value=secret_store.honoured() if lookup == 200 else secret_store.refused(lookup))

    if answering:
        store.answers()
    else:
        with pytest.raises(ConnectionError):
            store.answers()


def test_a_name_that_is_no_secret_name_is_refused_before_the_store_is_asked(store: SecretStore, secret_store: SecretStoreDouble) -> None:
    """A secret's name becomes a segment of the store's path, so one that would address another path is never sent."""
    with pytest.raises(ValueError, match="is no secret name"):
        store.read("../../sys/seal", retries=1)

    assert secret_store.login.call_count == 0


@pytest.mark.parametrize(
    ("logins", "questions", "usable"),
    [
        pytest.param([400, 200], [200], True, id="a-role-written-inside-the-budget"),
        pytest.param([400, 400], [], False, id="a-role-never-written"),
        pytest.param([200, 200], [403, 200], True, id="a-login-refused-the-question-is-made-again"),
        pytest.param([200, 200], [403, 403], False, id="a-policy-that-never-grants-the-question"),
    ],
)
def test_the_login_at_start_waits_out_a_store_still_being_seeded_and_fails_when_it_never_gives_a_usable_login(
    store: SecretStore, secret_store: SecretStoreDouble, logins: list[int], questions: list[int], usable: bool
) -> None:
    """The login a service makes before it serves is asked again within its budget and is the login the reads that
    follow are made under. It is usable once the store has answered its token about itself, the question the loop and
    the readiness report ask from then on. A login the store refuses that question is dropped and made again at the
    next attempt, since the store may have been replaced under the start; a role whose policy never grants the question
    fails the start, naming the role."""
    secret_store.login.mock(side_effect=[_login_answer(secret_store, outcome) for outcome in logins])
    secret_store.lookup.mock(side_effect=[secret_store.honoured() if outcome == 200 else secret_store.refused(outcome) for outcome in questions])

    if usable:
        secret_store.hold(SECRET, FIELDS)
        store.log_in(retries=2, backoff=0.001)
        assert store.read(SECRET, retries=1) == FIELDS
    else:
        with pytest.raises(RuntimeError, match=f"gave this process no usable login as its role '{secret_store.ROLE}'"):
            store.log_in(retries=2, backoff=0.001)

    assert (secret_store.login.call_count, secret_store.lookup.call_count) == (len(logins), len(questions))


def test_closing_a_session_ends_the_login_that_is_waiting_to_ask_again(
    store: SecretStore, secret_store: SecretStoreDouble, caplog: pytest.LogCaptureFixture
) -> None:
    """A start that is cancelled leaves its login running in a thread nothing can stop, waiting out its backoff to ask a
    store that is not admitting it. Closing the session ends that wait, so the thread holds no interpreter's exit for
    the backoff's seconds, and the login ends as one the store never gave: it asks the store nothing more and announces
    no further attempt."""
    secret_store.login.mock(return_value=secret_store.refused(400, "role not found"))

    def announced() -> list[str]:
        return [record.message for record in caplog.records if record.message.startswith("Retrying")]

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with caplog.at_level(logging.WARNING, logger=secrets.__name__):
            asking = pool.submit(store.log_in, retries=3, backoff=15.0)  # the longest wait between two attempts
            # The line that announces the wait is the sign the login has been refused and is about to wait.
            waiting_by = time.monotonic() + 10
            while not announced() and time.monotonic() < waiting_by:
                time.sleep(0.01)
            store.close()
            wait([asking], timeout=5)
            ended = str(asking.exception()) if asking.done() else "still waiting out its backoff"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    assert (ended.startswith("the secret store gave this process no usable login"), secret_store.login.call_count, len(announced())) == (True, 1, 1), ended


@pytest.mark.asyncio
async def test_a_service_is_logged_in_before_its_block_runs_kept_logged_in_by_its_loop_and_its_session_closed_after(
    monkeypatch: pytest.MonkeyPatch, secret_store: SecretStoreDouble
) -> None:
    """Driven with the store dropping the token at the loop's first pass, so the login the loop makes comes at once, and
    with the shortest lease a login is taken with."""
    secret_store.login.mock(return_value=secret_store.logged_in(lease_seconds=60))
    questions = iter([secret_store.honoured(), secret_store.refused()])
    secret_store.lookup.mock(side_effect=lambda _request: next(questions, secret_store.honoured()))

    async with Supervisor() as loops:
        async with secrets.secret_store_session(loops, wake_seconds=0.01):
            logged_in_at_start = secret_store.login.call_count
            async with asyncio.timeout(10):
                while secret_store.login.call_count < 2:
                    await asyncio.sleep(0.01)
            # With its environment gone, what still says this process has a store is the session it holds.
            monkeypatch.delenv("RASK_SECRET_STORE_ADDR")
            held_while_running = secrets.secret_store_configured()
        closed_after = secrets.secret_store_configured()

    assert (logged_in_at_start, held_while_running, closed_after) == (1, True, False)


@pytest.mark.asyncio
async def test_a_service_cancelled_while_its_session_closes_ends_cancelled(secret_store: SecretStoreDouble) -> None:
    """A session on its way out waits for its loop to end. A cancellation of the service that lands in that wait is the
    service's own, so it ends cancelled: whatever cancelled it (a timeout around its stop, the process's teardown) is
    otherwise left waiting on a service that went on as if nothing had asked it to end."""
    serving, leave = asyncio.Event(), asyncio.Event()

    async def service() -> None:
        async with Supervisor() as loops, secrets.secret_store_session(loops):
            serving.set()
            await leave.wait()

    running = asyncio.create_task(service())
    async with asyncio.timeout(10):
        await serving.wait()
    leave.set()
    await asyncio.sleep(0)  # one turn of the event loop: the session has cancelled its loop and waits for it to end
    running.cancel()
    await asyncio.wait([running], timeout=10)

    assert running.cancelled()


@pytest.mark.asyncio
async def test_a_process_with_no_store_configured_says_so_reads_nothing_and_keeps_no_session() -> None:
    assert secrets.secret_store_configured() is False
    async with Supervisor() as loops, secrets.secret_store_session(loops):
        pass
    with pytest.raises(RuntimeError, match="RASK_SECRET_STORE_ADDR"):
        secrets.read_secret(SECRET, retries=1)


@pytest.mark.parametrize("unnamed", ["RASK_SECRET_STORE_ROLE", "RASK_SECRET_STORE_TOKEN_FILE"])
def test_a_store_address_without_the_role_and_the_token_to_log_in_with_is_refused(monkeypatch: pytest.MonkeyPatch, unnamed: str) -> None:
    """The chart names all three together, and the process knows no role or token path of its own to fall back on."""
    monkeypatch.setenv("RASK_SECRET_STORE_ADDR", SecretStoreDouble.ADDRESS)
    monkeypatch.setenv("RASK_SECRET_STORE_ROLE", SecretStoreDouble.ROLE)
    monkeypatch.delenv(unnamed)

    with pytest.raises(ValidationError, match="RASK_SECRET_STORE_ROLE to its role there and RASK_SECRET_STORE_TOKEN_FILE"):
        SecretStoreSettings()


class _TlsStore(BaseHTTPRequestHandler):
    """The two calls a read makes, answered over TLS."""

    def do_POST(self) -> None:
        self._answer({"auth": {"client_token": "s.over-tls", "lease_duration": 900}})

    def do_GET(self) -> None:
        self._answer({"data": {"data": FIELDS, "metadata": {"version": 1}}})

    def _answer(self, body: object) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the name the base class gives it
        """Quiet: the handshake a test refuses would otherwise be written to the suite's stderr."""


def _named(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


@pytest.fixture
def tls_store(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A store listening on TLS at a loopback port, under a certificate a private CA signed: its address and that CA's file."""
    now = datetime.now(UTC)
    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    authority = (
        x509.CertificateBuilder()
        .subject_name(_named("the platform's private CA"))
        .issuer_name(_named("the platform's private CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_named("secret-store"))
        .issuer_name(authority.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file, chain, key = tmp_path / "ca.crt", tmp_path / "tls.crt", tmp_path / "tls.key"
    ca_file.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    chain.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain, key)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TlsStore)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", ca_file
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.mark.parametrize(
    ("names_the_ca", "fields"),
    [
        pytest.param("RASK_SECRET_STORE_CA_FILE", FIELDS, id="the-ca-the-deployment-names-is-trusted"),
        pytest.param("SSL_CERT_FILE", {}, id="a-ca-the-environment-names-is-not"),
    ],
)
def test_a_platform_stores_certificate_is_checked_against_the_ca_the_deployment_names(
    monkeypatch: pytest.MonkeyPatch, tls_store: tuple[str, Path], names_the_ca: str, fields: dict[str, str]
) -> None:
    """A store under a private CA is read only when the deployment names that CA. A login carries the pod's identity
    token, so an ambient `SSL_CERT_FILE` naming the same CA does not say whom it is sent to."""
    address, ca_file = tls_store
    monkeypatch.setenv("RASK_SECRET_STORE_ADDR", address)
    monkeypatch.setenv("RASK_SECRET_STORE_ROLE", "rask-test")
    monkeypatch.setenv(names_the_ca, str(ca_file))
    with closing(SecretStore(SecretStoreSettings())) as store:
        assert store.read(SECRET, retries=1, timeout=5.0) == fields
