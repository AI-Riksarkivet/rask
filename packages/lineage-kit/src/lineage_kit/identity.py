"""The credential an in-cluster producer presents at the lineage door: its pod's projected token.

The pod's ServiceAccount token, projected by the kubelet with audience ``rask-lineage``, IS the
producer's identity: the door verifies it against the cluster's issuer and maps the ServiceAccount to
a subject. No header names the caller, so a producer cannot claim to be anyone its token does not say.

READ ON EVERY EMIT, never cached. The kubelet rewrites the file at about 515 s of a 600 s token
(measured on the estate, LH-220 probe d), so a token held for the life of a process is refused ten
minutes after the pod starts. OpenLineage's ``HttpTransport`` asks its ``TokenProvider`` for the
bearer once per request (``_prepare_request`` -> ``_auth_headers`` -> ``get_bearer``, openlineage-python
1.52), which is the hook this provider fills.

An unreadable or empty file RAISES rather than answering ``None``: ``None`` makes the transport send
the event with no ``Authorization`` header at all, which turns a missing credential into an anonymous
request. Raising leaves the event unsent, and ``ClientEmitter`` records it as a transport drop, so the
caller is told it needs recovery.

This package cannot import ``service_kit.governed.machine_identity``, the fleet's reader of the same
files: the sealed runners take lineage-kit as a dependency and carry no FastAPI. The read rule is the
same one, kept here for that reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openlineage.client.transport.http import TokenProvider


if TYPE_CHECKING:
    from pathlib import Path


class IdentityTokenUnavailableError(RuntimeError):
    """The projected identity token could not be read, so the event is not sent rather than sent without it."""


class ProjectedTokenProvider(TokenProvider):
    """An OpenLineage token provider that answers with the token file's current contents."""

    def __init__(self, path: Path) -> None:
        super().__init__({})
        self._path = path

    def get_bearer(self) -> str:
        """``Bearer <token>``, read from the file now."""
        try:
            token = self._path.read_text().strip()
        except OSError as exc:
            raise IdentityTokenUnavailableError(f"the lineage identity token at {self._path} could not be read") from exc
        if not token:
            raise IdentityTokenUnavailableError(f"the lineage identity token at {self._path} is empty")
        return f"Bearer {token}"
