"""Ingest's own NATS user, read from its own sidecar's secret store at startup and held for the process ([[XC-078]]).

Every other app reaches NATS through its Dapr sidecar, whose pubsub component carries that app's user. Ingest's work
queue is a direct nats-py client (`ingest.queue`), so it presents its own user: the JWT the account issued for it, and a
signature over the nonce each server connection sends, made with the user's NKEY seed. Both are fields of
`nats-user-ingest` (`jwt`, `seed`) in the Dapr secret store, read through this pod's sidecar while the app starts. Never
the environment, never a file.

UNSET IS INERT. A service whose secrets do not come from the store, or a store that holds no `nats-user-ingest`,
connects with no credential, exactly as before; so does every connect to a server that does not authenticate, since it
sends no nonce to sign. A secret that the store does hold but that is no usable NATS user refuses the boot: an
authenticating server would refuse every connect made with it.

THE SLOT IS A MODULE SLOT, as `ingest.signing`'s is: the connects run inside workflow activities, which have no request
and no app to read it from. The lifespan installs the user before the workflow worker starts and withdraws it after the
worker has stopped.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from ingest.config import settings
from lineage_kit import SigningKey
from service_kit.governed.secrets import fetch_dapr_secret


if TYPE_CHECKING:
    from collections.abc import Mapping


log = logging.getLogger(__name__)

#: The secret holding ingest's NATS user, named `nats-user-<app-id>` like every app's.
NATS_USER_SECRET: Final = "nats-user-ingest"  # noqa: S105 — the secret's name in the store, not a credential


class NatsUser(BaseModel):
    """A NATS user: the JWT its account issued, and the key its NKEY user seed (`SU...`) encodes.

    The JWT is public; the seed lives only inside `key`, whose `repr` carries the public key alone.
    """

    model_config = ConfigDict(frozen=True)

    jwt: str = Field(min_length=1)
    key: SigningKey

    @classmethod
    def from_secret(cls, fields: Mapping[str, str]) -> NatsUser:
        """The user a `nats-user-<app-id>` secret's `jwt` and `seed` fields hold.

        Raises:
            ValueError: `jwt` is missing or blank, or `seed` is not an NKEY user seed. The message never echoes the seed.
        """
        return cls(jwt=fields.get("jwt", "").strip(), key=SigningKey.from_seed(fields.get("seed", "")))


_held: NatsUser | None = None


def held_nats_user() -> NatsUser | None:
    """The user ingest presents to the broker, or None when it connects without one."""
    return _held


async def install_nats_user() -> None:
    """Read ingest's NATS user through its sidecar and hold it for the process.

    The read takes the store's boot budget (`fetch_dapr_secret`'s retries), so a sidecar or store that is still coming up
    is waited for. A store that never yields the secret leaves ingest connecting without a user, and says so. daprd
    answers a secret its store lacks with 500 (`ERR_SECRET_GET`, dapr v1.18.1 `pkg/messages/predefined.go`), which reads
    as a store still seeding, so a store without the user spends the whole budget, about two minutes, before the boot
    goes on.

    Raises:
        ValueError: the store holds `nats-user-ingest` and it is not a usable NATS user.
    """
    global _held
    _held = await asyncio.to_thread(_read_nats_user)


def withdraw_nats_user() -> None:
    """Stop presenting a user: for the lifespan's shutdown, once the workflow worker has stopped."""
    global _held
    _held = None


def _read_nats_user() -> NatsUser | None:
    config = settings()
    if not config.secrets_from_dapr:
        return None
    fields = fetch_dapr_secret(config.secret_store, NATS_USER_SECRET)
    if not fields:
        log.warning("nats_user_unavailable", extra={"secret": NATS_USER_SECRET, "store": config.secret_store, "connects_as": "no user"})
        return None
    try:
        user = NatsUser.from_secret(fields)
    except ValueError as exc:
        raise ValueError(f"{NATS_USER_SECRET} in the {config.secret_store!r} secret store is not a usable NATS user: {exc}") from exc
    log.info("nats_user_resolved", extra={"secret": NATS_USER_SECRET, "user": user.key.public_nkey})
    return user
