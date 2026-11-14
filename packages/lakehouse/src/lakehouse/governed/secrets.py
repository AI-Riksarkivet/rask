"""Read this process's secrets from the secret store itself, as the pod's own identity: the SOLE source, never the environment.

A SERVICE LOGS IN TO THE STORE AS ITSELF ([[XC-199]]). The chart names the store, the role and the mounts
(`lance.secretStoreEnv`) and projects a ServiceAccount token for the store's audience into the pod. The process presents
that token at the store's Kubernetes auth method, as the role bound to its own ServiceAccount, and holds the store token
the login returns in memory: never in the environment, never on a volume. Each secret is read under it
(`GET <kv mount>/data/<name>`, KV v2): at start with a budget that waits out a store still being seeded and fails closed
when it never serves, and again while the process runs, through `lakehouse.governed.secret_cache` (ADR 0166).

THE LOGIN IS MADE AT START AND KEPT BY A LOOP: MADE AGAIN, NEVER RENEWED. A service logs in before it serves and refuses
to start when the store gives it no usable login (`secret_store_session`). A store token lives its role's `token_ttl` (15
minutes as the chart renders it; a login is taken with a lease of a minute to an hour), and the service logs in again
once half that lease is used, on a loop its supervisor keeps, with the projected token as its file holds it then, which
the kubelet rotates inside its 600 s life. A login the store does not admit leaves the held token in use for nine tenths
of its lease while the next pass asks again: the store asks the API server to review every login's token, and a review
that is out for a while must cost no read. NO READ LOGS IN WHILE A LOOP IS PASSING: the start and the loop do. So a
store that is failing, or the API server behind its token review, is asked for a login once a pass and never once a
read, and a read that finds the login used up is not served until a pass has made the next one. A read logs in itself
only where no loop has made a pass for a minute: a process that keeps none (a tool, a test), or one whose loop was
stopped or has stalled. Such a process learns of a token the store dropped only then.

A REFUSED SECRET IS THE STORE'S ANSWER, AND THE STORE SAYS WHETHER A TOKEN IS STILL HONOURED. The store answers 403
both to a token it dropped (it was revoked, or the store was replaced) and to a secret the role's policy does not name,
so a refusal says nothing of the token and a reader never logs in on one: it takes the refusal as the answer. What tells
the two apart is the store itself, asked by the token about itself (`GET auth/token/lookup-self`, which the role's
rendered policy grants):

- A service's START asks it of the login it has just made. A login the store refuses the question is no usable one:
  it is dropped, and the next attempt of the start makes another, since the store may have been replaced under the
  start. So no service runs under a policy without the grant, and the loop's question is one the store has answered.
- The LOOP asks it of the held token at every pass until the login is due, and logs in again on the pass that finds it
  refused, however much of the lease is unused: a store that was replaced serves this process again within a pass of
  admitting its login, or, where a row of refused logins is being sat out, when that wait ends. A store can also
  refuse the login a pass has just made: the grant was withdrawn, or more than one store answers at the
  address, as a replacement does beside the store it replaces, where a login can land on the store that is leaving.
  The next pass then logs in again at once, and each further login the store refuses in turn waits twice the passes the
  last did (1, 3, 7, then 15), where a login at every pass would be a storm and none at all would leave a second drop
  unserved for half a lease. A login the lease asks for meanwhile counts as one of that row, so a store that admits
  every login and answers none of their tokens costs, beyond the logins its lease asks for anyway, one every four
  minutes at most once the waits have grown to that. Only the store's answer ends the row, so two stores that both go on answering at one address end it at each
  answer and cost a login whenever a token meets the store that did not issue it: that state is the platform's to end.
- The READINESS REPORT asks it at every probe, so a token the store refuses is never reported as served.

The loop says each outage it meets once, at its first pass, and again only after the store has answered a question in
between.

EVERY WAIT OF AN ATTEMPT IS BOUNDED BY ITS SECONDS. An attempt's wait for another reader's login, its own login and its
read each take no longer than what is left of the attempt's seconds. So a reader that names one short attempt is not kept
by a store that is down, unreachable or silent, however many readers that store keeps waiting (measured 2026-11-14: six
readers of 1.0 s each, against an unroutable address and against a listener that never answers, returned in 1.0 s each).
The bound is per wait, as httpx applies a timeout, and not on the attempt as a whole: a store that keeps sending its
answer a byte at a time holds an attempt for as long as it does. It does not cover resolving the store's name either,
which the client does before it connects and the pod's resolver bounds. Closing the session starts no further attempt
and ends the wait between two: a call waiting out its backoff is woken and asks no more, so a start that was cancelled
leaves no thread waiting to ask a store again. A wait already under way (another reader's login, a request in flight)
ends within its own bound.

ONE SESSION PER PROCESS, OPENED AND CLOSED BY THE SERVICE THAT RUNS IT. Every reader in a process shares one login
(`read_secret`), so a pod holds one store token. The session is this module's state, as the object-store sessions are
`lakehouse.store_session`'s: its readers include settings validation, the signing key's and the commit ids' holders and
the maintenance worker, none of which has an app to carry it. The service's lifespan opens it and closes it
(`secret_store_session`).

MEASURED on the development OpenBao, from the catalog's own container as its ServiceAccount, 2026-11-13: a login answers
`auth.client_token` and `auth.lease_duration` (900); a read answers the fields at `data.data`; a secret the role's policy
does not name answers 403 whether or not it exists; a role the store does not hold answers 400; a wrong account, 403.
And 2026-11-14: `auth/token/lookup-self` answers a held token 200 and a token the store never issued 403, as a read of
a secret answers that token too; the login's `token_policies` name the role's own policy alone, so the grant of that
question is the rendered policy's.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    sleep_using_event,
    stop_after_attempt,
    stop_when_event_set,
    wait_exponential_jitter,
)


if TYPE_CHECKING:
    from lakehouse.supervision import Supervisor

log = logging.getLogger(__name__)

#: A login is made again once this share of its lease is used, by the loop a service keeps, so a login the store does
#: not admit at once has the rest of the lease to be asked again in.
_RENEWED_AT: Final = 1 / 2

#: The share of its lease a login is used for. The rest is left for a read in flight and for clocks that differ.
_LEASE_USED: Final = 9 / 10

#: The longest lease a login is taken with: the hour a store token lives at most (ADR 0169, the chart's
#: `openbao.identity.tokenMaxTtl`). A role written with no `token_ttl` issues one at the store's default of weeks, which
#: is a credential that does not rotate, so the login is refused and the start fails closed on it.
_LONGEST_LEASE_SECONDS: Final = 3600

#: How often a service's loop asks the store about its token and looks whether its login is due.
_RENEWAL_WAKE_SECONDS: Final = 15.0

#: The shortest lease a login is taken with (the chart's `lance.storeTokenSeconds` holds a role to the same minute). The
#: loop logs in again at half the lease and looks every `_RENEWAL_WAKE_SECONDS`, a pass taking up to five seconds for
#: its question and five for its login: at a minute it is sure of one pass that asks the store (by 20 s) and one, the
#: lease being due, that logs in (by 40 s) before the token is used up (54 s), and under it of neither, so reads would
#: go unserved between a token used up and the pass that replaces it.
_SHORTEST_LEASE_SECONDS: Final = 60

#: How long after a loop's last pass began a read still leaves the login to it: four passes at the default cadence. A
#: loop silent for longer keeps nothing (it was stopped, or a pass has stalled), and a read logs in itself.
_LOOP_SILENT_SECONDS: Final = 4 * _RENEWAL_WAKE_SECONDS

#: The longest row of logins the loop counts. Each login the store admits and then refuses the question doubles the
#: passes sat out before the next (none, 1, 3, 7, 15); from the fifth in a row the wait stays at fifteen passes, four
#: minutes at the default cadence.
_LONGEST_UNANSWERED_ROW: Final = 5

#: Where a token asks the store about itself, under the store's `/v1/`: the grant a role's policy carries for it.
_ASKS_ABOUT_ITSELF: Final = "auth/token/lookup-self"

#: The bound on the readiness report's one read of the store, inside the health watch's own budget for it.
_ANSWER_SECONDS: Final = 2.0

#: THE BOOT BUDGET every read and the login at start default to: ten attempts of five seconds, about two minutes with
#: their jittered backoff from three seconds, which waits out a store still being seeded for this process. A caller on a
#: request path passes one attempt.
_BOOT_ATTEMPT_SECONDS: Final = 5.0
_BOOT_ATTEMPTS: Final = 10
_BOOT_BACKOFF_SECONDS: Final = 3.0

#: The longest wait between two attempts.
_LONGEST_BACKOFF_SECONDS: Final = 15

#: A secret's name becomes a segment of the store's path, so only what the chart's own names are made of is read.
_SECRET_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,252}")

#: What an HTTP header carries as it stands: visible ASCII (`fits_a_header`).
_HEADER_SAFE: Final = re.compile(r"[\x21-\x7e]+")


class SecretStoreSettings(BaseSettings):
    """Where this process's secret store is, and who the process logs in to it as (the chart's `lance.secretStoreEnv`).

    Built from the environment at each look and never kept at module level, since the environment is what the chart
    renders and what a test sets. A session keeps the settings it was opened with.
    """

    # NO `populate_by_name`: it would teach the env source the bare field name as a second variable for each field
    # (`tests/unit/test_settings_env_namespace.py`), and nothing builds this by field name.
    model_config = SettingsConfigDict(extra="ignore")

    #: The store's address. Empty for a process with no secret store (a local run, the suite): it reads no secret, signs
    #: nothing and keys nothing.
    address: str = Field(default="", alias="RASK_SECRET_STORE_ADDR")
    #: The Kubernetes auth role this process logs in as, which the store binds to the pod's own ServiceAccount.
    role: str = Field(default="", alias="RASK_SECRET_STORE_ROLE")
    #: The mount of the store's Kubernetes auth method.
    auth_mount: str = Field(default="kubernetes", alias="RASK_SECRET_STORE_AUTH_MOUNT")
    #: The KV v2 mount the secrets are under.
    kv_mount: str = Field(default="secret", alias="RASK_SECRET_STORE_KV_MOUNT")
    #: The projected ServiceAccount token a login presents, read at each login because the kubelet rotates it. The chart
    #: names the path it mounts the token at, so this process knows no path of its own.
    token_file: Path | None = Field(default=None, alias="RASK_SECRET_STORE_TOKEN_FILE")
    #: The CA the store's certificate is checked against, and then the only one: a platform store under a private CA.
    #: Unset, the certificate is checked against the certifi bundle the image carries (httpx's default), never the node's.
    ca_file: Path | None = Field(default=None, alias="RASK_SECRET_STORE_CA_FILE")

    @model_validator(mode="after")
    def _a_store_names_who_logs_in(self) -> Self:
        if self.address and not (self.role and self.token_file):
            raise ValueError(
                "RASK_SECRET_STORE_ADDR names a store this process logs in to as itself: set RASK_SECRET_STORE_ROLE to its role "
                "there and RASK_SECRET_STORE_TOKEN_FILE to its projected token"
            )
        return self


def fits_a_header(credential: str) -> bool:
    """Whether ``credential`` is visible ASCII, which an HTTP header carries as it stands.

    The client refuses to send a header value outside it and quotes the value in its error (measured on httpx 0.28.1:
    `LocalProtocolError: Illegal header value b'...'`), which whoever logs that error writes into a log line. So a
    credential read from the store is held to this before it is put in a header, never found out by sending it.
    """
    return _HEADER_SAFE.fullmatch(credential) is not None


class _NotServedError(RuntimeError):
    """An attempt the store did not serve, for a reason this module found itself: no identity to present, an answer that
    is not the store's shape, or no time left."""


#: What an attempt raises when the store did not serve it: a transport fault or a refusal (`httpx.HTTPError`), or this
#: module's own finding. Anything else is a defect and propagates.
_NOT_SERVED: Final = (httpx.HTTPError, _NotServedError)


class _Login(BaseModel):
    """A login the store admitted: its token, when a service's loop makes the next one, and when this process stops using
    it, both on the session's clock."""

    model_config = ConfigDict(frozen=True)

    token: SecretStr
    renew_after: float
    expires: float


class _Issued(BaseModel):
    client_token: SecretStr
    #: A lease that ends, in the whole seconds the store states it in, of a minute to an hour. A token the store says
    #: never expires (zero) is refused with every lease the loop cannot keep, and so is one with a longer lease: no
    #: credential this process holds outlives its lease, and none is long-lived.
    lease_duration: int = Field(ge=_SHORTEST_LEASE_SECONDS, le=_LONGEST_LEASE_SECONDS)

    @field_validator("client_token")
    @classmethod
    def _a_header_carries_it(cls, token: SecretStr) -> SecretStr:
        if not fits_a_header(token.get_secret_value()):
            raise ValueError("the token is not one an HTTP header carries as it stands")
        return token


class _LoginAnswer(BaseModel):
    auth: _Issued


class _Versioned(BaseModel):
    data: dict[str, str]


class _ReadAnswer(BaseModel):
    data: _Versioned


class SecretStore:
    """One process's session with its secret store: a login held while its lease lasts, and reads under it."""

    def __init__(self, settings: SecretStoreSettings, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Open a session that has logged in to nothing yet.

        Args:
            settings: The store, the role and the mounts.
            clock: Monotonic seconds; a login's lease is measured on it.

        Raises:
            ValueError: ``settings`` names no store.
            OSError: The CA file ``settings`` names cannot be read.
            ssl.SSLError: That file holds no certificate.
        """
        if not settings.address or settings.token_file is None:
            raise ValueError("no secret store is configured: RASK_SECRET_STORE_ADDR is unset")
        self._settings = settings
        self._token_file = settings.token_file
        self._clock = clock
        # `trust_env=False`: a login carries the pod's identity token, so only the declared settings say where it goes
        # and what it trusts, never an ambient proxy or CA variable. For the same reason no redirect is followed
        # (httpx's default): the address names the node that serves, and a store that answers with another is not served.
        self._client = httpx.Client(base_url=settings.address.rstrip("/"), verify=_trusted(settings.ca_file), trust_env=False)
        self._held: _Login | None = None
        self._guard = threading.Lock()
        #: Set when the session is closed: no attempt starts after it, and a wait between two attempts is woken.
        self._closed = threading.Event()
        #: When a loop last made a pass, on the session's clock: while passes come, no reader logs in.
        self._last_pass: float | None = None
        #: How many logins in a row the loop has made on a refusal that the store has not answered since, and how many
        #: passes it sits out before the next. Only the store's answer ends the row; only the loop's pass reads or
        #: writes either, and the same holds for what it has said.
        self._unanswered = 0
        self._sitting_out = 0
        #: What the loop has said of the outage it is in: each said once, until the store next answers a question.
        self._said: set[str] = set()

    def log_in(self, *, timeout: float = _BOOT_ATTEMPT_SECONDS, retries: int = _BOOT_ATTEMPTS, backoff: float = _BOOT_BACKOFF_SECONDS) -> None:
        """Log in, asking again within the caller's budget until the store has admitted this process and answered its
        token about itself: a role not written yet answers 400, a store not up yet answers nothing, and a policy that
        does not carry the question yet refuses it. The login is held for the reads that follow.

        Blocking: an async caller runs it in a thread.

        Args:
            timeout: The seconds one attempt may take.
            retries: How many attempts are made.
            backoff: The first wait between two attempts, in seconds.

        Raises:
            RuntimeError: The store gave this process no usable login within the budget: it did not admit it, answered
                with a token this process does not take, or refused that token the question about itself.
        """
        try:
            self._asking_again(retries, backoff)(self._log_in_once, timeout)
        except _NOT_SERVED as exc:
            raise RuntimeError(f"the secret store gave this process no usable login as its role {self._settings.role!r}: {exc}") from exc

    def keep_logged_in(self, *, timeout: float = _BOOT_ATTEMPT_SECONDS) -> bool:
        """One pass of the loop a service keeps: log in again once half the held login's lease is used, or on the pass
        that finds the store refusing it. Blocking: one question and one login, each bounded by ``timeout``, so a slow
        store's answer to the first leaves the second its own seconds.

        A login the store does not admit leaves the held one in use until its lease is nearly out, and the next pass asks
        again. Readers go on under the held login meanwhile, and none of them waits on this one; a reader that holds no
        usable login waits for it, no longer than its own seconds. A refusal of the question is
        answered with a login, and each login the store refuses in turn waits twice the passes the last did
        (`_LONGEST_UNANSWERED_ROW`), until the store answers one.

        Returns:
            Whether the store was asked for a login and admitted it.
        """
        now = self._last_pass = self._clock()
        held = self._held
        deadline = time.monotonic() + timeout
        refused = False
        if held is not None and now < held.renew_after:
            try:
                refused = not self._honoured(held, deadline)
            except _NOT_SERVED as exc:
                # A pass sat out is a pass, answered or not: an outage does not lengthen the wait it falls in.
                self._sitting_out = max(self._sitting_out - 1, 0)
                self._say_once(logging.WARNING, "secret_store_token_not_checked", error=str(exc))
                return False
            if not refused:
                self._unanswered = self._sitting_out = 0
                self._said.clear()
                return False
            if self._unanswered:
                # The login made on the last refusal is refused too: no dropped token explains that.
                self._say_once(logging.ERROR, "secret_store_token_cannot_ask_about_itself", role=self._settings.role, grant=_ASKS_ABOUT_ITSELF)
                if self._sitting_out:
                    self._sitting_out -= 1
                    return False
            else:
                self._say_once(logging.WARNING, "secret_store_token_dropped", role=self._settings.role)
            deadline = time.monotonic() + timeout
        try:
            with self._one_login(deadline):
                if self._held is not held:
                    return False  # a reader replaced it while this pass waited
                self._held = self._log_in(deadline)
        except _NOT_SERVED as exc:
            self._report_not_renewed(held, exc)
            return False
        if refused or self._unanswered:
            # A login in a row the store has not answered: one made on a refusal, or one the lease asked for meanwhile,
            # which the store would refuse the question as it does the rest.
            self._unanswered = min(self._unanswered + 1, _LONGEST_UNANSWERED_ROW)
            self._sitting_out = 2 ** (self._unanswered - 1) - 1
        return True

    def read(
        self, name: str, *, timeout: float = _BOOT_ATTEMPT_SECONDS, retries: int = _BOOT_ATTEMPTS, backoff: float = _BOOT_BACKOFF_SECONDS
    ) -> dict[str, str]:
        """The fields of secret ``name``, or ``{}`` (logged as a warning) when the store did not serve it within the
        caller's budget. What a miss costs is the caller's to say: a start that requires the secret fails on it
        (`read_required_secret`), and a door on a delivery's path answers retry.

        EVERY ATTEMPT THE STORE DID NOT SERVE IS ASKED AGAIN, a refusal included. A store still being seeded for this
        process refuses before it serves: a role not written yet is a 400, a secret its policy does not name yet a 403,
        one not seeded yet a 404. So the defaults are the BOOT budget, and a caller on a request path passes one attempt.

        Blocking: an async caller runs it in a thread.

        Args:
            name: The secret.
            timeout: The seconds each wait of one attempt is bounded by: its wait for another reader's login, its own
                login and its read, each within what is left of them.
            retries: How many attempts are made.
            backoff: The first wait between two attempts, in seconds.

        Raises:
            ValueError: ``name`` is no secret name.
        """
        if _SECRET_NAME.fullmatch(name) is None:
            raise ValueError(f"{name!r} is no secret name: it becomes a segment of the store's path, so only [a-z0-9-] is read")
        try:
            return self._asking_again(retries, backoff)(self._read_once, name, timeout)
        except _NOT_SERVED as exc:
            log.warning("secret_store_read_failed", extra={"secret": name, "error": str(exc)})
            return {}

    def answers(self) -> None:
        """Raise unless the store admits this process and answers its token about itself: what a readiness report shows
        of the store (`lakehouse.health`). The store is asked about this process's own token at every call, so a store
        that dropped it is told from one that serves it, which no read of a secret can tell. Blocking: one attempt, each
        of its waits bounded.

        Raises:
            ConnectionError: The store did not admit this process, did not answer, or refused its token the question, or
                the session holds no usable login while its loop is passing: the report asks for no login of its own there.
        """
        deadline = time.monotonic() + _ANSWER_SECONDS
        try:
            honoured = self._honoured(self._login(deadline), deadline)
        except httpx.HTTPStatusError as refused:
            raise ConnectionError(f"the secret store did not admit or answer this process: it answered {refused.response.status_code}") from refused
        except httpx.HTTPError as exc:
            raise ConnectionError(f"the secret store did not answer: {type(exc).__name__}") from exc
        except _NotServedError as exc:
            # This module's own finding, in its own words, which name no secret: a session with no usable login whose
            # loop is passing is waiting for that loop, and the report should say so.
            raise ConnectionError(str(exc)) from exc
        if not honoured:
            raise ConnectionError(
                f"the secret store refuses this process's token the question about itself ({_ASKS_ABOUT_ITSELF}): it dropped the "
                "login, or the role's policy does not grant the question"
            )

    def close(self) -> None:
        """Close the session: end every wait of an attempt in flight, and close its connections. The store token it held
        expires by itself."""
        self._closed.set()
        self._client.close()

    def _log_in_once(self, timeout: float) -> None:
        """One attempt at a usable login: one the store admits and then answers about itself. A login the store refuses
        the question is dropped, so the next attempt makes another: a store replaced under the start honours none the
        store before it issued.

        Raises:
            httpx.HTTPError: The store could not be reached, refused the login, or answered the question neither yes nor no.
            _NotServedError: As `_log_in`, or the store refused the login it admitted the question about itself.
        """
        deadline = time.monotonic() + timeout
        login = self._login(deadline)
        if self._honoured(login, deadline):
            return
        # Dropped whatever time the attempt has left, and without waiting: a login in flight holds the guard, and its
        # outcome is the next attempt's to judge.
        if self._guard.acquire(blocking=False):
            try:
                if self._held is login:
                    self._held = None
            finally:
                self._guard.release()
        raise _NotServedError(
            "the store admitted the login and refuses its token the question about itself: it was replaced since, or the "
            f"role's policy does not grant read on {_ASKS_ABOUT_ITSELF}"
        )

    def _read_once(self, name: str, timeout: float) -> dict[str, str]:
        """One attempt: the fields of ``name`` under the held login.

        Raises:
            httpx.HTTPError: The store could not be reached, or refused the login or the read.
            _NotServedError: The projected token could not be read, the login's answer names no token this process takes,
                the read's answer is not a KV v2 read of string fields, or the attempt ran out of time.
        """
        deadline = time.monotonic() + timeout
        login = self._login(deadline)
        answer = self._client.get(f"/v1/{self._settings.kv_mount}/data/{name}", headers=_under(login), timeout=self._left(deadline))
        answer.raise_for_status()
        return _fields(answer)

    def _honoured(self, login: _Login, deadline: float) -> bool:
        """Whether the store answers ``login`` about itself: 403 for a token it revoked, one a replaced store never
        issued, and one whose policy does not grant the question.

        Raises:
            httpx.HTTPError: The store could not be reached, or answered neither yes nor no.
            _NotServedError: The attempt ran out of time, or the session is closed.
        """
        answer = self._client.get(f"/v1/{_ASKS_ABOUT_ITSELF}", headers=_under(login), timeout=self._left(deadline))
        if answer.status_code == httpx.codes.FORBIDDEN:
            return False
        answer.raise_for_status()
        return True

    def _login(self, deadline: float) -> _Login:
        """The login to read under. A reader that finds a usable one takes it without waiting, whatever login is in
        flight. One that finds none waits out a login in flight and takes its outcome; with none in flight it logs in,
        one reader at a time, unless a loop is passing, whose next pass then makes the login.

        Raises:
            httpx.HTTPError: As `_log_in`.
            _NotServedError: As `_log_in`, or the session holds no usable login while its loop is passing.
        """
        held = self._held
        if held is not None and self._clock() < held.expires:
            return held
        with self._one_login(deadline):
            held = self._held  # another reader, or the loop, may have logged in while this one waited
            if held is not None and self._clock() < held.expires:
                return held
            if self._last_pass is not None and self._clock() - self._last_pass < _LOOP_SILENT_SECONDS:
                raise _NotServedError("this process holds no usable login to the secret store: its loop makes the next one, at its next pass")
            self._held = admitted = self._log_in(deadline)
            return admitted

    @contextmanager
    def _one_login(self, deadline: float) -> Generator[None]:
        """Hold the session's one login in flight, waiting for another reader's no longer than this attempt has left.

        Raises:
            _NotServedError: Another reader's login was still in flight when this attempt ran out of time.
        """
        if not self._guard.acquire(timeout=self._left(deadline)):
            raise _NotServedError("another reader's login to the secret store was still in flight when this attempt ran out of time")
        try:
            yield
        finally:
            self._guard.release()

    def _log_in(self, deadline: float) -> _Login:
        settings = self._settings
        try:
            projected = self._token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            # The exception's type alone, and no chain: a decoding error carries the bytes it was given, which are a token's.
            raise _NotServedError(f"the projected token {self._token_file} is unreadable: {type(exc).__name__}") from None
        asked = self._clock()
        answer = self._client.post(f"/v1/auth/{settings.auth_mount}/login", json={"role": settings.role, "jwt": projected}, timeout=self._left(deadline))
        answer.raise_for_status()
        try:
            issued = _LoginAnswer.model_validate_json(answer.content).auth
        except ValidationError:
            # `from None`: a validation error quotes its input, and this input may hold the token.
            raise _NotServedError(
                "the secret store's login answer names no token this process takes: one an HTTP header carries, with a lease "
                f"of {_SHORTEST_LEASE_SECONDS} to {_LONGEST_LEASE_SECONDS} s (the role's token_ttl)"
            ) from None
        lease = issued.lease_duration
        login = _Login(token=issued.client_token, renew_after=asked + lease * _RENEWED_AT, expires=asked + lease * _LEASE_USED)
        log.info("secret_store_logged_in", extra={"role": settings.role, "lease_seconds": lease})
        return login

    def _report_not_renewed(self, held: _Login | None, exc: Exception) -> None:
        # A warning while reads go on under the held login; an error when it has run out and reads have none.
        if held is not None and self._clock() < held.expires:
            self._say_once(logging.WARNING, "secret_store_login_not_renewed", error=str(exc))
        else:
            self._say_once(logging.ERROR, "secret_store_login_lapsed", error=str(exc))

    def _say_once(self, level: int, event: str, **extra: object) -> None:
        """Log ``event`` at the first pass of an outage and not again until the store has answered a question, which is
        what says the outage is over."""
        if event not in self._said:
            self._said.add(event)
            log.log(level, event, extra=extra)

    def _asking_again(self, retries: int, backoff: float) -> Retrying:
        """The retry every budgeted call runs under: whatever the store did not serve is asked again, a refusal
        included, ``retries`` attempts in all, with jittered exponential backoff from ``backoff`` seconds. Closing the
        session wakes a call from its wait between two attempts and ends its asking."""
        return Retrying(
            retry=retry_if_exception_type(_NOT_SERVED),
            stop=stop_after_attempt(retries) | stop_when_event_set(self._closed),
            wait=wait_exponential_jitter(initial=backoff, max=_LONGEST_BACKOFF_SECONDS),
            sleep=sleep_using_event(self._closed),
            before_sleep=before_sleep_log(log, logging.WARNING),
            reraise=True,
        )

    def _left(self, deadline: float) -> float:
        """The seconds an attempt has left before ``deadline``: none once the session is closed.

        Raises:
            _NotServedError: It has none.
        """
        if self._closed.is_set():
            raise _NotServedError("the session with the secret store is closed")
        left = deadline - time.monotonic()
        if left <= 0:
            raise _NotServedError("the attempt ran out of time before the secret store answered")
        return left


def _under(login: _Login) -> dict[str, str]:
    """The header a call is made under ``login`` with."""
    return {"X-Vault-Token": login.token.get_secret_value()}


def _trusted(ca_file: Path | None) -> ssl.SSLContext | bool:
    """What a store's certificate is checked against: the named CA alone, else httpx's default, the certifi bundle."""
    return ssl.create_default_context(cafile=str(ca_file)) if ca_file is not None else True


def _fields(answer: httpx.Response) -> dict[str, str]:
    """The fields a KV v2 read answered. Every field rask reads is a string, so an answer holding any other value is
    refused whole rather than read as text it never was.

    Raises:
        _NotServedError: The answer is not a KV v2 read of string fields.
    """
    try:
        return _ReadAnswer.model_validate_json(answer.content).data.data
    except ValidationError:
        # `from None`: a validation error quotes its input, and this input is the secret.
        raise _NotServedError("the secret store's answer is not a KV v2 read of string fields") from None


class _ProcessStore:
    """This process's one session, opened at the first use from the settings in force then."""

    def __init__(self) -> None:
        self._store: SecretStore | None = None
        self._guard = threading.Lock()

    def configured(self) -> bool:
        """Whether this process has a secret store: the session it holds, else what its environment names now."""
        with self._guard:
            return self._store is not None or bool(SecretStoreSettings().address)

    def get(self) -> SecretStore:
        with self._guard:
            if self._store is None:
                settings = SecretStoreSettings()
                if not settings.address:
                    raise RuntimeError("this process is configured with no secret store (RASK_SECRET_STORE_ADDR is unset), so it has no secret to read")
                self._store = SecretStore(settings)
            return self._store

    def forget(self) -> None:
        with self._guard:
            store, self._store = self._store, None
        if store is not None:
            store.close()


_PROCESS: Final = _ProcessStore()


def secret_store_configured() -> bool:
    """Whether the chart named this process a secret store. A process with none reads no secret, signs nothing and keys
    nothing."""
    return _PROCESS.configured()


@asynccontextmanager
async def secret_store_session(loops: Supervisor, *, wake_seconds: float = _RENEWAL_WAKE_SECONDS) -> AsyncGenerator[None]:
    """Keep this process logged in to its secret store for the length of the block, or do nothing for a process the
    chart named no secret store.

    Logs in with the BOOT budget before the block runs, then keeps the login on a loop of ``loops``, which asks the store
    every ``wake_seconds`` (`SecretStore.keep_logged_in`). On the way out the loop is ended and the process's session
    closed, whether the block ended, failed, or the login never came.

    Raises:
        RuntimeError: The store gave this process no usable login within the budget. The start fails closed: a wrong
            role, a token the role's audience refuses, a CA that does not sign the store's certificate, a lease outside
            the minute to the hour a login is taken with, a policy that does not let the token ask about itself and a
            store that never comes up all end here, whichever secrets the service goes on to read.
    """
    if not secret_store_configured():
        yield
        return
    store = _PROCESS.get()
    try:
        await asyncio.to_thread(store.log_in)
        loop = loops.keep("secret-store-login", partial(_keep_logged_in, store, wake_seconds))
        try:
            yield
        finally:
            loop.cancel()
            # `gather`, not an await under `suppress(CancelledError)`: a cancellation of THIS task that lands while the
            # loop ends is its own to end on, and `gather` hands it on where the bare await would swallow it.
            await asyncio.gather(loop, return_exceptions=True)
    finally:
        forget_secret_store()


async def _keep_logged_in(store: SecretStore, wake_seconds: float) -> None:
    while True:
        await asyncio.sleep(wake_seconds)
        try:
            await asyncio.to_thread(store.keep_logged_in)
        except Exception:
            # A fault in one pass must not end the loop: while it passes, the next pass is the only way the login is
            # made again.
            log.exception("secret_store_pass_failed")


def read_secret(name: str, *, timeout: float = _BOOT_ATTEMPT_SECONDS, retries: int = _BOOT_ATTEMPTS, backoff: float = _BOOT_BACKOFF_SECONDS) -> dict[str, str]:
    """`SecretStore.read` on this process's session.

    Raises:
        RuntimeError: This process is configured with no secret store.
        ValueError: ``name`` is no secret name.
    """
    return _PROCESS.get().read(name, timeout=timeout, retries=retries, backoff=backoff)


def read_required_secret(
    name: str, *, require: str, timeout: float = _BOOT_ATTEMPT_SECONDS, retries: int = _BOOT_ATTEMPTS, backoff: float = _BOOT_BACKOFF_SECONDS
) -> dict[str, str]:
    """The fields of secret ``name`` from this process's session, FAILING CLOSED when ``require`` is not among them.

    The store is the strict sole source, since the chart ships no plaintext for the value, so a miss must not boot a
    process on an empty key. ``timeout``, ``retries`` and ``backoff`` are `SecretStore.read`'s, defaulting to its BOOT
    budget; a caller on a request path passes its own bound.

    Raises:
        RuntimeError: This process is configured with no secret store, the store did not serve ``name``, or the secret
            holds no ``require``.
        ValueError: ``name`` is no secret name.
    """
    fields = read_secret(name, timeout=timeout, retries=retries, backoff=backoff)
    if not fields.get(require):
        raise RuntimeError(f"secret {name!r} gave this process no {require!r}: failing closed (the secret store is the sole source)")
    return fields


def secret_store_answers() -> None:
    """`SecretStore.answers` on this process's session.

    Raises:
        ConnectionError: The store did not admit this process, did not answer, or no longer honours its login.
        RuntimeError: This process is configured with no secret store.
    """
    _PROCESS.get().answers()


def forget_secret_store() -> None:
    """Close this process's session and forget its login, so the next read opens one from the settings in force then."""
    _PROCESS.forget()
