"""A service's object-store credential as one value, and the 503 a service raises while it holds none ([[XC-084]], ADR 0170).

No lakehouse service holds a store key: each signs with the sessions its ServiceAccount token is exchanged for
(`lakehouse.store_session`), and a vend is a session too. The offline and Dagger lanes, which run no secret store and
project no token, sign with the environment's pair. Every one of these is an :class:`S3KeyPair`, whose secret and session
token are ``SecretStr`` so a repr, a log line or an error that echoes one carries neither ([[LH-543]]).
"""

from __future__ import annotations

from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, SecretStr


class S3KeyPair(BaseModel):
    """An object-store access key: its id and its secret, which sign only together, and the session token a temporary one carries.

    The environment's pair has no token. A session STS issued (`lakehouse.store_session`) signs only with its token
    beside the pair, and the store refuses the pair without it.
    """

    model_config = ConfigDict(frozen=True)

    access_key_id: str
    secret_access_key: SecretStr
    session_token: SecretStr | None = None


class StoreCredentialUnavailableError(ServiceUnavailableError):
    """This service holds no unexpired store credential right now, so nothing is signed with one (503).

    `ServiceUnavailableError` is `lance_namespace`'s, which the problem handlers of every plane map to 503.
    """
