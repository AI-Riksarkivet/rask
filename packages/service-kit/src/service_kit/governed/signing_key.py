"""A signer's own private signing key: read from the secret store, held only while it is published ([[LH-064]]).

Each signing identity owns an Ed25519 pair in the store. The seed sits in `signing-key-<identity>` (field `seed`) and
only that identity's own sidecar may read it; the public half sits in `signing-public-<identity>` (field `keys`, the
current key first) and every verifier reads it. A signer resolves its seed through its own sidecar, derives the public
key from it, and signs only while that key is listed.

READY MEANS LISTED, NOT MERELY READABLE. A verifier refuses an event signed with a key its identity does not publish,
and a refusal is acknowledged and gone, so a signer that kept signing with an unlisted key would destroy every event it
emits while every probe stayed green. The publish and the seed are two independent writes, so the readable-but-unlisted
state is the likelier fault. Until the key is listed the signer takes no delivery (`retry_until_signed`), reports
itself not ready (`signing_ready_check`) and hands out no key (`SigningKeyHolder.key`).

NOTHING EMPTY IS EVER HELD. Every pass replaces the whole resolution, so an unreadable seed, a malformed one or an
unlisted key leaves the holder with no key rather than the previous one: a store that was re-minted swaps to its new key
on the next pass and an outage is not mistaken for a key. The background loop keeps re-resolving, on a short interval
while unresolved and at most every five minutes once ready, so a signer heals in place without a restart. `/livez` is
untouched: a signer that is waiting for its key is alive.

THE KEY AND ITS PARSING ARE CALLABLES because this package cannot import lineage-kit, which owns the wire format: the
caller passes `SigningKey.from_seed` and `parse_published_keys`, and a key is listed exactly when its public NKEY is in
the list the second returns.

THE VERIFIERS' READ LIVES HERE TOO ([[XC-078]]). `published_key_fetch` is the secret read every verifying door hands
`lineage_kit.keys.PublishedKeys`, so the timeout and retry policy of a read on the delivery path is written once.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import Final, Protocol

from fastapi import FastAPI, Request

from service_kit.draining import RETRY
from service_kit.governed.secrets import fetch_dapr_secret
from service_kit.probes import ReadyCheck
from service_kit.schemas.health import Readiness, ReadinessStatus


log = logging.getLogger(__name__)

#: The `app.state` attribute the lifespan publishes the holder under, read by the gate and the readiness check.
SIGNING_STATE: Final = "signing"

#: The field of `signing-key-<identity>` that holds the NKEY user seed.
SEED_FIELD: Final = "seed"

#: The field of `signing-public-<identity>` that holds the comma-separated public NKEYs, current first.
KEYS_FIELD: Final = "keys"

#: The longest a ready signer goes between resolutions. A store that was re-minted is noticed within this bound.
MAX_REFRESH_SECONDS: Final = 300.0

#: A signing identity becomes part of a secret name and so of a sidecar URL path: only a DNS-label-like name is read.
_IDENTITY: Final = re.compile(r"[a-z0-9][a-z0-9-]*")

#: The bound on one verifier's read of a published key list. ONE attempt, and short: verification sits on the delivery
#: path of every signed event, and the boot-time retry `fetch_dapr_secret` defaults to (about two minutes) would hold a
#: worker that long per event through a store blip. An outage is answered RETRY instead, and the sidecar's redelivery is
#: the retry.
KEY_READ_TIMEOUT_SECONDS: Final = 2.0


class SigningKeyUnavailableError(RuntimeError):
    """The service's own signing key is not resolved, so nothing may be signed.

    Raised where an event would otherwise leave unsigned: a verifier refuses it and acknowledges the refusal, so the
    event would be lost, and the caller must not tell anyone a write succeeded on the strength of an emit that
    cannot happen.
    """


class SigningKeyLike(Protocol):
    """What the holder needs of a loaded key: the public text a verifier lists, and the id that names it."""

    @property
    def public_nkey(self) -> str: ...

    @property
    def kid(self) -> str: ...


class SigningStatus(Protocol):
    """What the gate and the readiness check read of a holder, whatever key type it carries."""

    @property
    def ready(self) -> bool: ...

    @property
    def reason(self) -> str: ...


def signing_key_secret(identity: str) -> str:
    """The secret holding `identity`'s private seed. Dapr grants by secret name, so this name is what is scoped."""
    return f"signing-key-{identity}"


def signing_public_secret(identity: str) -> str:
    """The secret holding `identity`'s published public keys. Never denied to any sidecar."""
    return f"signing-public-{identity}"


def published_key_fetch(store: str) -> Callable[[str], Mapping[str, str]]:
    """The secret read a verifier's `lineage_kit.keys.PublishedKeys` takes: a secret's fields through THIS pod's sidecar.

    One attempt bounded by `KEY_READ_TIMEOUT_SECONDS`. A secret the sidecar cannot serve answers ``{}``, which the
    reader turns into `KeySourceUnavailableError`, so the door answers RETRY rather than refusing an honest event.

    Args:
        store: The Dapr secret store holding every identity's `signing-public-<identity>`.
    """

    def fetch(secret: str) -> dict[str, str]:
        return fetch_dapr_secret(store, secret, timeout=KEY_READ_TIMEOUT_SECONDS, retries=1)

    return fetch


class SigningKeyHolder[KeyT: SigningKeyLike]:
    """One service's own signing key, resolved through its own Dapr sidecar and held only while it is listed."""

    def __init__(
        self,
        *,
        identity: str,
        store: str,
        load_key: Callable[[str], KeyT],
        parse_published: Callable[[str], Sequence[str]],
        refresh_seconds: float = MAX_REFRESH_SECONDS,
        retry_seconds: float = 15.0,
    ) -> None:
        """Build a holder that has resolved nothing yet.

        Args:
            identity: The identity this service signs as, as the chart names it.
            store: The Dapr secret store holding the pair.
            load_key: Turns the stored seed text into a key, raising `ValueError` or `TypeError` for text that is no
                seed (`lineage_kit.SigningKey.from_seed`).
            parse_published: Turns the stored `keys` text into the public NKEYs it lists
                (`lineage_kit.parse_published_keys`).
            refresh_seconds: Between resolutions while ready, at most `MAX_REFRESH_SECONDS`.
            retry_seconds: Between resolutions while not ready, at most `refresh_seconds`.

        Raises:
            ValueError: `identity` is not a lower-case DNS-label-like name, or the intervals are out of order.
        """
        if _IDENTITY.fullmatch(identity) is None:
            raise ValueError(f"{identity!r} is not a signing identity: it becomes part of a secret name, so only [a-z0-9-] is read")
        if not 0 < retry_seconds <= refresh_seconds <= MAX_REFRESH_SECONDS:
            raise ValueError(f"the intervals must satisfy 0 < retry <= refresh <= {MAX_REFRESH_SECONDS}s, got {retry_seconds}s and {refresh_seconds}s")
        self._identity = identity
        self._store = store
        self._load_key = load_key
        self._parse_published = parse_published
        self._refresh_seconds = refresh_seconds
        self._retry_seconds = retry_seconds
        self._key: KeyT | None = None
        self._reason = "the signing key has not been resolved yet"
        self._task: asyncio.Task[None] | None = None

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def ready(self) -> bool:
        """Whether a key is held: read from the store, well formed, and listed by its identity."""
        return self._key is not None

    @property
    def reason(self) -> str:
        """Why the signer is not ready, for `/readyz`. Empty while ready. Names no secret value."""
        return "" if self._key is not None else self._reason

    def key(self) -> KeyT:
        """The key to sign with.

        Raises:
            SigningKeyUnavailableError: no key is held.
        """
        key = self._key
        if key is None:
            raise SigningKeyUnavailableError(f"{self._identity} cannot sign: {self._reason}")
        return key

    def resolve(self) -> bool:
        """Read the seed and the published list once and settle on the outcome. Blocking: one sidecar read each.

        A single attempt per read, not the boot retry budget: the loop calls this again soon, and a pass that blocked
        for minutes would hold readiness at whatever it last said.

        Returns:
            Whether the signer is ready after this pass.
        """
        seed = fetch_dapr_secret(self._store, signing_key_secret(self._identity), retries=1).get(SEED_FIELD, "")
        if not seed.strip():
            return self._settle(None, "the signing key cannot be read from the secret store")
        try:
            key = self._load_key(seed)
        except (TypeError, ValueError) as exc:
            return self._settle(None, f"the stored seed is not a signing key ({exc})")
        published = fetch_dapr_secret(self._store, signing_public_secret(self._identity), retries=1).get(KEYS_FIELD, "")
        if key.public_nkey not in self._parse_published(published):
            return self._settle(None, f"key {key.kid} is not in {self._identity}'s published list")
        return self._settle(key, "")

    def _settle(self, key: KeyT | None, reason: str) -> bool:
        before = self._key
        before_kid = before.kid if before is not None else None
        before_reason = self._reason
        self._key = key
        self._reason = reason
        # Logged on a change only: an unresolved signer is re-read every few seconds and would repeat itself.
        if key is None:
            if reason != before_reason:
                level = logging.ERROR if before is not None else logging.WARNING
                log.log(level, "signing_key_unresolved", extra={"identity": self._identity, "reason": reason})
        elif key.kid != before_kid:
            log.info("signing_key_resolved", extra={"identity": self._identity, "kid": key.kid})
        return key is not None

    async def start(self) -> None:
        """Resolve once now, then keep re-resolving in the background until `stop`.

        An unresolved key does not fail the start: a signer that is waiting for its key must still boot, so that its
        probes and its retry answers are served while it heals.
        """
        await asyncio.to_thread(self.resolve)
        self._task = asyncio.create_task(self._refresh_forever(), name=f"signing-key-{self._identity}")

    async def stop(self) -> None:
        """End the background loop. Safe to call when it never started."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds if self.ready else self._retry_seconds)
            try:
                await asyncio.to_thread(self.resolve)
            except Exception:
                # A fault in a pass must not end the loop: the next pass is the only way a signer heals.
                log.exception("signing_key_pass_failed", extra={"identity": self._identity})


def make_signing_holder[KeyT: SigningKeyLike](
    *,
    secrets_from_dapr: bool,
    identity: str,
    store: str,
    load_key: Callable[[str], KeyT],
    parse_published: Callable[[str], Sequence[str]],
) -> SigningKeyHolder[KeyT] | None:
    """The holder for a service that signs, or None for one that does not.

    A service signs when its secret store is wired and the chart named the identity it signs as
    (`RASK_SIGNING_IDENTITY`). A stack with no store has no key to read, and a service without an identity has no
    name to publish under: both emit exactly what they emit without signing.
    """
    if not (secrets_from_dapr and identity):
        return None
    return SigningKeyHolder(identity=identity, store=store, load_key=load_key, parse_published=parse_published)


def attach_signing(app: FastAPI, holder: SigningStatus | None) -> None:
    """Publish the holder on `app.state`, where the gate and the readiness check find it. None means no signing."""
    setattr(app.state, SIGNING_STATE, holder)


def _signing_of(app: FastAPI) -> SigningStatus | None:
    holder: SigningStatus | None = getattr(app.state, SIGNING_STATE, None)
    return holder


async def retry_until_signed(request: Request) -> dict[str, str] | None:
    """FastAPI dependency for a sidecar-delivered route that emits: the RETRY verdict while the key is unresolved.

    The same shape as `retry_when_draining`, and for the same reason: Dapr delivers a message to a pod whatever its
    readiness says, so a signer that is not ready would otherwise receive its share of work it cannot sign. It
    returns rather than raises, and it runs before the handler body, so a trigger that would be dropped as malformed
    is retried instead and nothing is parsed, started or emitted while the key is unresolved. RETRY and never DROP: a
    drop is acknowledged and the work is gone. A service that does not sign has no holder, so the gate is open.

    `async def` although it awaits nothing: a sync dependency is queued behind the threadpool, and a pod whose
    threads are all busy with long work would then answer the gate late, which is the moment it matters.
    """
    holder = _signing_of(request.app)
    return RETRY if holder is not None and not holder.ready else None


def signing_ready_check(inner: ReadyCheck | None = None) -> ReadyCheck:
    """A `/readyz` check that adds the signing key to whatever `inner` already reports.

    A signer that cannot sign is `degraded`, which the probe router serves as 503: the pod leaves its Service while
    `/livez` stays 200, so it is waited on rather than restarted.
    """

    async def check(request: Request) -> Readiness:
        body = await inner(request) if inner is not None else Readiness(status=ReadinessStatus.ready)
        holder = _signing_of(request.app)
        if holder is None or holder.ready:
            return body
        return body.model_copy(update={"status": ReadinessStatus.degraded, "components": {**body.components, "signing": holder.reason}})

    return check
