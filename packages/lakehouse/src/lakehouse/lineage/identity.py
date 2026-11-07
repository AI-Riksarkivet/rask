"""The credential an in-cluster producer presents at the lineage door: its pod's projected token.

The pod's ServiceAccount token, projected by the kubelet with audience ``rask-lineage``, IS the
producer's identity: the door verifies it against the cluster's issuer and maps the ServiceAccount to
a subject. No header names the caller, so a producer cannot claim to be anyone its token does not say.

READ ON EVERY EMIT, never cached, through the fleet's one reader of a projected token
(``lakehouse.governed.machine_identity.read_identity_token``). The kubelet rewrites the file at about 515 s of a 600 s
token (measured on the estate, LH-220 probe d), so a token held for the life of a process is refused ten minutes after
the pod starts. OpenLineage's ``HttpTransport`` asks its ``TokenProvider`` for the bearer once per request
(``_prepare_request`` -> ``_auth_headers`` -> ``get_bearer``, openlineage-python 1.52), which is the hook this provider
fills.

An unreadable or empty file RAISES rather than answering ``None``: ``None`` makes the transport send
the event with no ``Authorization`` header at all, which turns a missing credential into an anonymous
request. Raising leaves the event unsent, and ``ClientEmitter`` records it as a transport drop, so the
caller is told it needs recovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from openlineage.client.transport.http import TokenProvider

from lakehouse.governed.machine_identity import read_identity_token


if TYPE_CHECKING:
    from pathlib import Path


class ProjectedTokenProvider(TokenProvider):
    """An OpenLineage token provider that answers with the token file's current contents."""

    def __init__(self, path: Path) -> None:
        super().__init__({})
        self._path = path

    @override
    def get_bearer(self) -> str:
        """``Bearer <token>``, read from the file now.

        Raises:
            IdentityTokenUnavailableError: The file is missing, unreadable or empty
                (``lakehouse.governed.machine_identity``).
        """
        return f"Bearer {read_identity_token(self._path)}"
