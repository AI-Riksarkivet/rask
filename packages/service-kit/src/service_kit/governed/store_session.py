"""A service's own object-store credential as a session its ServiceAccount token is exchanged for ([[XC-083]], ADR 0170).

THE SERVICE HOLDS NO KEY. Its pod projects a token for the object store's audience, the store trusts the cluster's
ServiceAccount issuer and grants the token the policy its subject names, and STS `AssumeRoleWithWebIdentity` exchanges
the token for a temporary credential under that policy (measured on RustFS 1.0.3,
`docs/audits/2026-10-18/rustfs_web_identity_measure.py`). The exchange carries no signature, because the token is the
credential. The kubelet replaces the projected token before it expires, so the file is read at every exchange.

READS NEVER TOUCH THE STORE, as with a store-held pair (`service_kit.governed.store_credential`): `storage_options()` is
built at every request, so `held_session` only peeks at what `SESSIONS` holds, and raises
`StoreCredentialUnavailableError` (503) once the session is within `EXPIRY_MARGIN_SECONDS` of its expiry. `StoreSession`
does the exchanging: once at boot, failing the boot closed, then in a background loop that exchanges again once half the
session's life is used. A request that took the old session then still has half a session to finish, and an STS outage
shorter than that half serves nobody a 503.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, SecretStr

from service_kit.governed.machine_identity import IdentityTokenUnavailableError, read_identity_token
from service_kit.governed.store_credential import S3KeyPair, StoreCredentialUnavailableError


log = logging.getLogger(__name__)

#: Between the background loop's looks at whether the session is due. A due exchange that failed is retried at this cadence.
WAKE_SECONDS: Final = 15.0
#: A session this close to its expiry is not handed out: a request signed with it could outlive it.
EXPIRY_MARGIN_SECONDS: Final = 60.0
#: The boot exchange waits out a store, an identity provider and a provisioning hook that are still starting (the hook
#: writes the policy the token's subject names), then fails the boot: a pod that never gets a session must not serve.
_BOOT_ATTEMPTS: Final = 20
_BOOT_DELAY_SECONDS: Final = 3.0
#: What an exchange can fail with: the token could not be read, STS refused it, or STS did not answer.
EXCHANGE_FAILURES: Final = (IdentityTokenUnavailableError, ClientError, BotoCoreError)


class WebIdentity(BaseModel):
    """Where a service's token is projected, the STS it is exchanged at, and the role the store maps it to."""

    model_config = ConfigDict(frozen=True)

    token_file: Path
    #: `None` leaves botocore to resolve AWS's regional STS.
    endpoint: str | None
    region: str
    #: The role AWS assumes; a store that binds the policy by the token's subject (RustFS) ignores it, and boto3 requires it.
    role_arn: str


class IssuedSession(BaseModel):
    """What STS answered: the temporary credential and the moment it expires."""

    model_config = ConfigDict(frozen=True)

    key_pair: S3KeyPair
    expiration: datetime


@cache
def _sts(region: str, endpoint: str | None) -> Any:
    """One unsigned STS client per endpoint; botocore clients are thread-safe, and building one costs a model load."""
    from storage import sts_client

    return sts_client(region=region, endpoint=endpoint)


def exchange(identity: WebIdentity, *, session_name: str, duration_seconds: int, policy: str | None = None) -> IssuedSession:
    """Exchange the token the file holds now for a session of ``duration_seconds``, narrowed by ``policy`` when given. Blocking.

    Raises:
        IdentityTokenUnavailableError: The token file is missing or empty.
        ClientError: STS refused the exchange.
        BotoCoreError: STS did not answer.
    """
    request: dict[str, object] = {
        "RoleArn": identity.role_arn,
        "RoleSessionName": session_name,
        "WebIdentityToken": read_identity_token(identity.token_file),
        "DurationSeconds": duration_seconds,
    }
    if policy is not None:
        request["Policy"] = policy
    credentials = _sts(identity.region, identity.endpoint).assume_role_with_web_identity(**request)["Credentials"]
    return IssuedSession(
        key_pair=S3KeyPair(
            access_key_id=credentials["AccessKeyId"],
            secret_access_key=SecretStr(credentials["SecretAccessKey"]),
            session_token=SecretStr(credentials["SessionToken"]),
        ),
        expiration=credentials["Expiration"],
    )


class _Held(BaseModel):
    model_config = ConfigDict(frozen=True)

    key_pair: S3KeyPair
    issued_at: float
    expires_at: float


class SessionCache:
    """The session each identity holds, timed on this process's own clock.

    A session is held for the duration it was asked for, counted on ``clock`` from the moment it was asked for. No wall
    clock enters, so a pod whose clock disagrees with the store's still renews in time. The store grants the duration
    whole: RustFS ends a session at its own now plus the duration, within 900-43,200 s
    (`rustfs@1.0.3:rustfs/src/admin/handlers/sts.rs:409-416`), and AWS refuses a duration the role does not allow rather
    than shortening it.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: dict[WebIdentity, _Held] = {}
        self._lock = threading.Lock()

    def now(self) -> float:
        """The moment to count a session from, read before it is asked for."""
        return self._clock()

    def hold(self, identity: WebIdentity, key_pair: S3KeyPair, *, asked_at: float, lifetime_seconds: float) -> None:
        """Hold ``key_pair`` for ``lifetime_seconds`` from ``asked_at``, a reading of `now`."""
        held = _Held(key_pair=key_pair, issued_at=asked_at, expires_at=asked_at + lifetime_seconds)
        with self._lock:
            self._held = {**self._held, identity: held}

    def peek(self, identity: WebIdentity) -> S3KeyPair | None:
        """The session ``identity`` holds, unless it is within `EXPIRY_MARGIN_SECONDS` of expiring."""
        held = self._held.get(identity)
        if held is None or self._clock() >= held.expires_at - EXPIRY_MARGIN_SECONDS:
            return None
        return held.key_pair

    def due(self, identity: WebIdentity) -> bool:
        """Whether ``identity`` holds no session, or half of the one it holds is used."""
        held = self._held.get(identity)
        return held is None or self._clock() >= (held.issued_at + held.expires_at) / 2

    def forget(self, identity: WebIdentity) -> None:
        with self._lock:
            self._held = {held: session for held, session in self._held.items() if held != identity}


#: The process's sessions. A test that drives the expiry replaces it with one on its own clock.
SESSIONS = SessionCache()


def held_session(identity: WebIdentity) -> S3KeyPair:
    """The session ``identity`` holds while it is unexpired. Never calls STS.

    Raises:
        StoreCredentialUnavailableError: No session was exchanged, or it is within `EXPIRY_MARGIN_SECONDS` of expiring.
    """
    key_pair = SESSIONS.peek(identity)
    if key_pair is None:
        raise StoreCredentialUnavailableError(f"no unexpired object-store session is held for the token at {identity.token_file}, so nothing is signed with it")
    return key_pair


class StoreSession:
    """Keeps a service's object-store session unexpired: exchanged at boot, exchanged again once half its life is used."""

    def __init__(self, identity: WebIdentity, *, session_name: str, duration_seconds: int, wake_seconds: float = WAKE_SECONDS) -> None:
        self._identity = identity
        self._session_name = session_name
        self._duration_seconds = duration_seconds
        self._wake_seconds = wake_seconds
        self._expired = False
        self._task: asyncio.Task[None] | None = None

    def _exchange_and_hold(self) -> IssuedSession:
        asked_at = SESSIONS.now()
        issued = exchange(self._identity, session_name=self._session_name, duration_seconds=self._duration_seconds)
        SESSIONS.hold(self._identity, issued.key_pair, asked_at=asked_at, lifetime_seconds=self._duration_seconds)
        return issued

    def exchange_at_boot(self) -> None:
        """Exchange the token, waiting out a store or a policy that is still being provisioned. Blocking.

        Raises:
            RuntimeError: Every attempt failed. The boot fails closed: no service signs with a session it does not hold.
        """
        for attempt in range(1, _BOOT_ATTEMPTS + 1):
            try:
                issued = self._exchange_and_hold()
            except EXCHANGE_FAILURES as exc:
                if attempt == _BOOT_ATTEMPTS:
                    raise RuntimeError(f"the object store exchanged no session for the token at {self._identity.token_file}: {exc}") from exc
                log.warning("store_session_boot_retry", extra={"attempt": attempt, "error": str(exc)})
                time.sleep(_BOOT_DELAY_SECONDS)
                continue
            log.info("store_session_issued", extra={"session_name": self._session_name, "expiration": issued.expiration.isoformat()})
            return

    def refresh_if_due(self) -> bool:
        """Exchange again if half the session's life is used. Blocking: one STS call.

        A failed exchange leaves the held session to its expiry, after which `held_session` refuses.

        Returns:
            Whether STS was asked and issued a session.
        """
        if not SESSIONS.due(self._identity):
            return False
        try:
            self._exchange_and_hold()
        except EXCHANGE_FAILURES as exc:
            self._report_failed(str(exc))
            return False
        self._expired = False
        return True

    def _report_failed(self, error: str) -> None:
        # Logged at ERROR once, when the held session runs out: a failed exchange every wake would repeat itself.
        if SESSIONS.peek(self._identity) is not None:
            log.warning("store_session_renewal_failed", extra={"error": error})
        elif not self._expired:
            self._expired = True
            log.error("store_session_expired", extra={"error": error})

    async def start(self) -> None:
        """Exchange at boot, then keep the session renewed in the background until `stop`.

        Raises:
            RuntimeError: The boot exchange failed; see `exchange_at_boot`.
        """
        await asyncio.to_thread(self.exchange_at_boot)
        self._task = asyncio.create_task(self._wake_forever(), name="store-session")

    async def stop(self) -> None:
        """End the background loop and forget the session. Safe to call when it never started."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        SESSIONS.forget(self._identity)

    async def _wake_forever(self) -> None:
        while True:
            await asyncio.sleep(self._wake_seconds)
            try:
                await asyncio.to_thread(self.refresh_if_due)
            except Exception:
                # A fault in one pass must not end the loop: the next pass is the only way the session is renewed.
                log.exception("store_session_pass_failed")
