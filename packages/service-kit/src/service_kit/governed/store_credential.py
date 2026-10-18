"""A service's own object-store key pair: read from the secret store, and read again while the service runs ([[XC-082]], ADR 0166).

THE PAIR LIVES IN THE STORE, both halves. Every S3-family store rotates by issuing a new key id (AWS cannot change a
key's secret, and a secret changed in place leaves no window in which both sign), so a rotation without an outage adds a second
key, writes its id and secret to the store together, waits out the TTL, then revokes the first. An id held in
configuration would leave the new secret signing under the old id. Lakekeeper keeps the same pair as one secret
(`S3AccessKeyCredential`, `crates/lakekeeper/src/service/storage/s3.rs`).

READS NEVER TOUCH THE STORE. `storage_options()` is built on the event loop at every request, so `store_key_pair` only
peeks at what `service_kit.governed.secret_cache.STORE_READS` holds and raises `StoreCredentialUnavailableError` (503)
when the store has not confirmed the pair within its TTL. `StoreCredential` does the reading: once at boot, failing the
boot closed, then in a background loop that re-reads the pair once half its TTL is used, so a rotation is served within
half a TTL and a store outage shorter than half a TTL serves nobody a 503.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import suppress
from functools import partial
from typing import Final, Protocol

from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, SecretStr

from service_kit.governed import secret_cache
from service_kit.governed.secret_cache import StoreRead
from service_kit.governed.secrets import fetch_required_secrets


log = logging.getLogger(__name__)

#: Between the background loop's looks at whether the pair is due. A due read that failed is retried at this cadence.
WAKE_SECONDS: Final = 15.0

#: The fetch policy of a re-read in the background: two attempts of two seconds. The boot read takes
#: `fetch_required_secrets`' own defaults instead, which wait out a store that is still being seeded.
_REREAD_TIMEOUT: Final = 2.0
_REREAD_RETRIES: Final = 2
_REREAD_BACKOFF: Final = 0.25


class S3KeyPair(BaseModel):
    """An object-store access key: its id and its secret, which sign only together, and the session token a temporary one carries.

    A store-held pair has no token. A session STS issued (`service_kit.governed.store_session`) signs only with its token
    beside the pair, and the store refuses the pair without it.
    """

    model_config = ConfigDict(frozen=True)

    access_key_id: str
    secret_access_key: SecretStr
    session_token: SecretStr | None = None


class StoreCredentialSource(Protocol):
    """Where a service's settings say its key pair is held in the store."""

    secrets_from_dapr: bool
    dapr_secret_store: str
    dapr_secret_key: str
    dapr_secret_s3_id_field: str
    dapr_secret_s3_field: str


class StoreCredentialUnavailableError(ServiceUnavailableError):
    """The store has not confirmed this service's key pair within its TTL, so nothing is signed with it (503).

    `ServiceUnavailableError` is `lance_namespace`'s, which the problem handlers of both planes map to 503.
    """


def store_read(source: StoreCredentialSource) -> StoreRead:
    """The read that holds ``source``'s pair: its key id field and its secret field, from one secret."""
    return StoreRead(store=source.dapr_secret_store, secret=source.dapr_secret_key, fields=(source.dapr_secret_s3_id_field, source.dapr_secret_s3_field))


def store_key_pair(source: StoreCredentialSource) -> S3KeyPair:
    """The pair the store last returned for ``source``, while that is under its TTL. Never reads the store.

    Raises:
        StoreCredentialUnavailableError: Nothing read it yet, or the store has not confirmed it within its TTL.
    """
    read = store_read(source)
    values = secret_cache.STORE_READS.peek(read)
    if values is None:
        raise StoreCredentialUnavailableError(
            f"the secret store has not confirmed this service's object-store credential ({read.store}/{read.secret}, fields {', '.join(read.fields)}) "
            "within its TTL, so nothing is signed with it"
        )
    return S3KeyPair(access_key_id=values[source.dapr_secret_s3_id_field], secret_access_key=SecretStr(values[source.dapr_secret_s3_field]))


def _pair_from(read: StoreRead, bundle: Mapping[str, str]) -> dict[str, str]:
    """Both fields of the pair, from one fetch's bundle. Half a credential signs nothing, so a missing half fails the read.

    Raises:
        RuntimeError: The bundle carries the secret and no key id.
    """
    id_field, secret_field = read.fields
    if not bundle.get(id_field):
        raise RuntimeError(f"secret {read.store!r}/{read.secret!r} carries {secret_field!r} but no {id_field!r}: half a credential signs nothing")
    return {id_field: bundle[id_field], secret_field: bundle[secret_field]}


def _read_at_boot(read: StoreRead) -> dict[str, str]:
    """The pair, under `fetch_required_secrets`' boot policy, which waits out a store still being seeded."""
    return _pair_from(read, fetch_required_secrets(read.store, read.secret, require=read.fields[1]))


def _read_again(read: StoreRead) -> dict[str, str]:
    """The pair, under the background re-read's short policy."""
    bundle = fetch_required_secrets(read.store, read.secret, require=read.fields[1], timeout=_REREAD_TIMEOUT, retries=_REREAD_RETRIES, backoff=_REREAD_BACKOFF)
    return _pair_from(read, bundle)


class StoreCredential:
    """Keeps a service's key pair confirmed by the store: read at boot, read again whenever half its TTL is used."""

    def __init__(self, source: StoreCredentialSource, *, wake_seconds: float = WAKE_SECONDS) -> None:
        self._read = store_read(source)
        self._wake_seconds = wake_seconds
        self._expired = False
        self._task: asyncio.Task[None] | None = None

    def read_at_boot(self) -> None:
        """Read the pair, waiting out a store that is still being seeded. Blocking.

        Raises:
            RuntimeError: The store never answered with both fields. The boot fails closed: no service signs with a
                credential the store does not hold.
        """
        secret_cache.STORE_READS.refresh(self._read, partial(_read_at_boot, self._read))
        log.info("store_credential_read", extra={"store": self._read.store, "secret": self._read.secret, "fields": list(self._read.fields)})

    def refresh_if_due(self) -> bool:
        """Read the pair again if half its TTL is used. Blocking: one sidecar read, with retries.

        A read that fails leaves the held pair to its own TTL, after which `store_key_pair` refuses.

        Returns:
            Whether the store was read and answered.
        """
        cache = secret_cache.STORE_READS
        if not cache.due(self._read):
            return False
        id_field = self._read.fields[0]
        held = cache.peek(self._read)
        try:
            values = cache.refresh(self._read, partial(_read_again, self._read))
        except RuntimeError as exc:
            self._report_failed(cache, str(exc))
            return False
        self._expired = False
        if held is not None and held[id_field] != values[id_field]:
            log.info("store_credential_rotated", extra={"secret": self._read.secret, "from_key_id": held[id_field], "to_key_id": values[id_field]})
        return True

    def _report_failed(self, cache: secret_cache.SecretCache, error: str) -> None:
        # Logged at ERROR once, when the held pair runs out: a failed re-read every wake would repeat itself.
        if cache.peek(self._read) is not None:
            log.warning("store_credential_reread_failed", extra={"secret": self._read.secret, "error": error})
        elif not self._expired:
            self._expired = True
            log.error("store_credential_expired", extra={"secret": self._read.secret, "error": error})

    async def start(self) -> None:
        """Read the pair at boot, then keep it confirmed in the background until `stop`.

        Raises:
            RuntimeError: The boot read failed; see `read_at_boot`.
        """
        await asyncio.to_thread(self.read_at_boot)
        self._task = asyncio.create_task(self._wake_forever(), name=f"store-credential-{self._read.secret}")

    async def stop(self) -> None:
        """End the background loop. Safe to call when it never started."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _wake_forever(self) -> None:
        while True:
            await asyncio.sleep(self._wake_seconds)
            try:
                await asyncio.to_thread(self.refresh_if_due)
            except Exception:
                # A fault in one pass must not end the loop: the next pass is the only way the pair is read again.
                log.exception("store_credential_pass_failed", extra={"secret": self._read.secret})


def make_store_credential(source: StoreCredentialSource) -> StoreCredential | None:
    """The holder of ``source``'s pair when the store is its source, ``None`` when the environment is."""
    return StoreCredential(source) if source.secrets_from_dapr else None
