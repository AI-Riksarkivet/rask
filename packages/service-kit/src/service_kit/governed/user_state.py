"""Per-subject user state on the Dapr state store — a user's own work, kept off their laptop.

The media zone's workflow graph and saved views lived in ``localStorage``, so the same signed-in person
opening the estate on another machine found an empty canvas. This module gives that work a home on the
already-deployed ``lance-statestore`` component (``state.postgresql`` on ``lance-ns-age``, DSN resolved
from OpenBao through the ``lance-secrets`` Dapr secret store — chart/templates/dapr-statestore.yaml).

**Identity is derived, never accepted.** Every function here takes ``subject`` as the VERIFIED token
subject its caller was handed by the OIDC layer. There is deliberately no ``user`` path parameter, body
field or header anywhere in this module or its router: a store keyed on caller-supplied identity is one
missing check away from serving one person's work to another, and a naive test cannot tell the two designs
apart. The stored record additionally CARRIES its owner (:class:`StoredState.subject`), so even a
mis-keyed or hand-edited row cannot be served to the wrong caller — :meth:`UserStateStore.get` refuses a
record whose owner is not the requester.

**Why the key looks like it does.** Dapr prefixes every key it writes with ``<appId>||``, and ``||`` is
RESERVED as that separator: a key containing it is rejected by the sidecar with a 400. Subjects come from
an identity provider, so they are not ours to trust with that constraint, and the obvious fix does not
work — the key travels in the URL PATH of the sidecar's state API, which decodes it, so a
percent-encoded ``%7C%7C`` arrives at the store as ``||`` again. The encoding therefore has to be
decode-stable: base64url (``A-Za-z0-9-_``, padding stripped) cannot express ``|`` at all, whatever an
identity provider puts in ``sub``, and stays reversible so an operator can read a Postgres row back to a
person.

**Transport.** The local Dapr sidecar's HTTP state API, exactly as :mod:`service_kit.governed.secrets` reaches the
secret store — ``http://localhost:<DAPR_HTTP_PORT>/v1.0/state/<store>``. No service opens a Postgres
connection of its own: the component owns the DSN, the schema and the migrations, and the DSN never
touches pod env.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

import httpx
from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, JsonValue, ValidationError

from service_kit.exceptions import ConflictError


log = logging.getLogger(__name__)

#: Dapr's app-id separator. A state key containing it is a 400 from the sidecar, so it is the one
#: character sequence :func:`state_key` must be unable to emit.
DAPR_APP_ID_SEPARATOR = "||"

#: Namespaces our keys inside the store's key space, so a future component sharing the store cannot
#: collide with a user document.
KEY_PREFIX = "user-state"


class UserStateDocument(StrEnum):
    """The documents a user owns. A closed set: the key space is ours, not the caller's."""

    WORKFLOW_GRAPH = "workflow-graph"
    SAVED_VIEWS = "saved-views"
    #: Every dock workbench layout this subject has arranged, keyed by workbench id inside ONE
    #: document (see :class:`service_kit.schemas.dock_layout.DockLayouts`). One member rather than one
    #: per workbench on purpose: the enum is a closed key space precisely so a caller cannot mint keys,
    #: and a zone adding a workbench must not require a release of this package to store its layout.
    DOCK_LAYOUT = "dock-layout"
    #: The NAMED layouts ("views") a subject has saved, keyed by workbench id
    #: (:class:`service_kit.schemas.dock_layout.DockLayoutLibrary`). A SEPARATE document from
    #: :attr:`DOCK_LAYOUT` rather than a key inside it, and the separation is load-bearing three times:
    #:
    #: 1. :class:`DockLayouts` is ``extra="forbid"``, so a client that does not know a sibling key
    #:    cannot echo it back — it must DROP it, and the first autosave from any older bundle would
    #:    then erase every named view the user had.
    #: 2. The byte ceiling is per DOCUMENT, so a large library would otherwise start failing the
    #:    IMPLICIT autosave — silently, because ``save()`` only returns False.
    #: 3. An unreadable library must not 409 the implicit layout, which the client turns into a
    #:    permanent save lock for the session. With two documents it cannot.
    DOCK_LAYOUT_LIBRARY = "dock-layout-library"
    #: Object stores attached from the UI, as a list of `Store`. ESTATE-scoped, not per-user: it is
    #: written under the reserved `ESTATE_SUBJECT` rather than a person, because a bucket someone
    #: attaches must be visible to everyone the FGA layer permits — a per-user copy would make the
    #: same bucket appear and disappear depending on who is looking, which is indistinguishable from
    #: the store being broken. The estate-admin door on the write is what keeps that safe.
    ATTACHED_STORES = "attached-stores"


class UserStateConflict(ConflictError):
    """A first-write etag check lost the race — the document changed since it was read.

    Distinct from `ServiceUnavailableError` on purpose: the store is healthy and the write was
    correctly refused, so the caller's move is to re-read and re-apply, not to back off.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"the state document {key!r} changed since it was read — re-read and retry")


class UserStateUnreadable(Exception):
    """A record exists but cannot be served — and must NOT be reported as absent.

    The distinction is the whole point. "Absent" tells a client to seed a fresh document, and the client's
    next autosave then overwrites whatever is really there. So an unparseable record (schema drift) and a
    record whose stored owner disagrees with the derived key both raise this instead: the caller learns the
    document is unavailable rather than empty, and a blind overwrite needs an explicit act.
    """

    def __init__(self, document: UserStateDocument, reason: str) -> None:
        super().__init__(f"{document.value}: {reason}")
        self.document = document
        self.reason = reason


class StoredState(BaseModel):
    """What is actually persisted: the value, its owner, and when it was written.

    ``subject`` is redundant with the key by construction, and that is the point — it is the second lock.
    A record whose owner disagrees with the caller is refused rather than served (see
    :meth:`UserStateStore.get`).
    """

    subject: str
    document: UserStateDocument
    updated_at: datetime
    value: JsonValue


#: The subject that owns ESTATE-scoped documents (currently only `ATTACHED_STORES`).
#:
#: A reserved name rather than a person, and it cannot collide with one: every real subject here is an
#: OIDC `sub`, and no issuer mints one shaped like this. It rides the same key space, encoder and
#: fail-closed read semantics as user documents instead of introducing a second persistence path for
#: three fields — the store, the 503-on-unreachable rule and the absent/unreadable distinction are all
#: things estate config needs exactly as much as a saved layout does.
ESTATE_SUBJECT = "__estate__"


def encode_subject(subject: str) -> str:
    """A subject as it appears in a key: base64url, padding stripped.

    Not percent-encoding, deliberately. The key travels in the sidecar's URL path, which is decoded on
    arrival, so ``%7C%7C`` would become ``||`` at the store — the encoding has to survive a decode. It
    also keeps the key free of ``/``, ``%``, ``+`` and ``=``, so nothing downstream has to re-escape it.
    """
    return base64.urlsafe_b64encode(subject.encode()).decode().rstrip("=")


def decode_subject(encoded: str) -> str:
    """Inverse of :func:`encode_subject` — for reading a stored key back to a person."""
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()


def state_key(subject: str, document: UserStateDocument) -> str:
    """The state-store key for one subject's document.

    Raises:
        ValueError: If ``subject`` is empty (there is no such thing as anonymous user state), or — the
            guard that outlives this encoding choice — if the composed key ever contains ``||``.
    """
    if not subject.strip():
        raise ValueError("user state requires a non-empty verified subject")
    key = f"{KEY_PREFIX}:{encode_subject(subject)}:{document.value}"
    if DAPR_APP_ID_SEPARATOR in key:
        raise ValueError(f"state key {key!r} contains Dapr's reserved app-id separator {DAPR_APP_ID_SEPARATOR!r} — the sidecar would reject it with 400")
    return key


class UserStateStore:
    """Reads and writes one subject's documents through the local Dapr sidecar's state API.

    Constructed once per process (in the app lifespan) over a shared ``httpx.AsyncClient`` — one
    connection pool for the process, never one per request. Every failure to reach the store FAILS CLOSED
    as a 503: a state store that is down must look unavailable, never empty, or a stalled sidecar would
    read as "you have no saved work" and the next autosave would overwrite the real document with a
    freshly seeded canvas.
    """

    def __init__(
        self,
        *,
        store_name: str,
        client: httpx.AsyncClient,
        dapr_http_port: int = 3500,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._store = store_name
        self._client = client
        self._base = f"http://localhost:{dapr_http_port}/v1.0/state/{store_name}"
        self._timeout = timeout_seconds

    @classmethod
    def build(
        cls,
        *,
        store_name: str,
        dapr_http_port: int = 3500,
        timeout_seconds: float = 5.0,
    ) -> Self:
        """Build a store owning its own client — the lifespan path. Close it with :meth:`aclose`."""
        return cls(
            store_name=store_name,
            client=httpx.AsyncClient(timeout=timeout_seconds),
            dapr_http_port=dapr_http_port,
            timeout_seconds=timeout_seconds,
        )

    async def aclose(self) -> None:
        """Close the underlying connection pool (no-op if it is shared and already closed)."""
        await self._client.aclose()

    async def get(self, *, subject: str, document: UserStateDocument) -> StoredState | None:
        """One subject's document, or ``None`` when they have never written it.

        Dapr answers a missing key with ``204 No Content``, which is the ordinary first-visit case — not
        an error. A record whose stored ``subject`` is not the caller is treated as absent and logged at
        ERROR: it can only mean the key derivation and the record disagree, and serving it would be the
        cross-user leak this module exists to make impossible.
        """
        key = state_key(subject, document)
        response = await self._call("GET", f"{self._base}/{key}", op="get", key=key)
        if response.status_code == httpx.codes.NO_CONTENT or not response.content:
            return None
        return self._parse(response.content, subject=subject, document=document, key=key)

    def _parse(self, content: bytes, *, subject: str, document: UserStateDocument, key: str) -> StoredState:
        """The two failure modes a stored record can have, both of which must look UNAVAILABLE.

        Shared by `get` and `get_versioned` so the leak-and-overwrite reasoning below has exactly one
        implementation — a second copy is how one of the two paths quietly grows a `return None`.
        """
        try:
            stored = StoredState.model_validate_json(content)
        except ValidationError as exc:
            # NOT absent. This module's own docstring says a store that is down must look unavailable and
            # never empty, "or the next autosave would overwrite the real document" — and an unparseable
            # record is that same hazard arriving through schema drift instead of an outage. Reported as
            # absent, the client seeds a fresh canvas and its next save destroys work the user still has.
            # Proven live before this raised: one field of a real row changed from str to int (a plausible
            # evolution) and `GET /v1/user-state/saved-views` answered `exists: false` over an intact row.
            log.exception("user_state_unreadable", extra={"document": document.value, "key": key})
            raise UserStateUnreadable(document, "the stored record no longer fits its schema") from exc
        if stored.subject != subject:
            # Same reasoning, different cause: the key derivation and the record disagree. Serving it would
            # be a cross-user leak, but calling it absent would let this caller overwrite the other's work.
            log.error(
                "user_state_owner_mismatch",
                extra={"document": document.value, "key": key, "stored_subject": stored.subject},
            )
            raise UserStateUnreadable(document, "the stored record belongs to another subject")
        return stored

    async def get_versioned(self, *, subject: str, document: UserStateDocument) -> tuple[StoredState | None, str | None]:
        """:meth:`get`, plus the sidecar's ETag — the token a later :meth:`put` uses to detect a race.

        A separate method rather than a wider return on `get`, because the versioned read is the
        exception: per-subject documents have one writer and need no token, and every existing caller
        would otherwise pay for a concern it does not have.

        The etag is `None` for a document that does not exist yet. That is not a missing token — it is
        the correct one for "I believe nobody has written this", and `put` turns it into the sidecar's
        create-if-absent semantics rather than a blind overwrite.
        """
        key = state_key(subject, document)
        response = await self._call("GET", f"{self._base}/{key}", op="get", key=key)
        etag = response.headers.get("ETag")
        if response.status_code == httpx.codes.NO_CONTENT or not response.content:
            return None, None
        return self._parse(response.content, subject=subject, document=document, key=key), etag

    async def put(self, *, subject: str, document: UserStateDocument, value: JsonValue, etag: str | None = None, concurrent: bool = False) -> StoredState:
        """Write (or overwrite) one subject's document and return the record as persisted.

        LAST WRITE WINS BY DEFAULT, and that default is right for what this module was built for: one
        person's own work, replacing a localStorage whose semantics across their own tabs were exactly
        that. Optimistic concurrency with no contender is machinery for a conflict that cannot happen.

        **It is wrong for a SHARED document, and one caller has one** (open_dapr.md §2.7). The catalog's
        `POST /v1/stores` read-modify-writes `ATTACHED_STORES` under `ESTATE_SUBJECT` — an estate-scoped
        key every admin writes — so two concurrent attaches each read N stores and each wrote N+1,
        silently dropping one. The docstring here asserted the opposite ("there is no second writer to
        lose a race with") and that assertion is what made the defect invisible.

        So the round trip is OPT-IN: pass `concurrent=True` with the `etag` from
        :meth:`get_versioned`, and the write carries Dapr's `first-write` concurrency. A mismatch is a
        `UserStateConflict` (a 409 mapped in `_call`), never the fail-closed 503 — the store is healthy
        and the caller's move is to re-read and re-apply.
        """
        key = state_key(subject, document)
        stored = StoredState(subject=subject, document=document, updated_at=datetime.now(UTC), value=value)
        entry: dict[str, object] = {"key": key, "value": stored.model_dump(mode="json")}
        if concurrent:
            # `first-write` is what makes the etag load-bearing: under Dapr's default (`last-write`) the
            # token is accepted and ignored, which would be the same lost update wearing a safety belt.
            entry["options"] = {"concurrency": "first-write"}
            if etag is not None:
                entry["etag"] = etag
        await self._call("POST", self._base, op="put", key=key, json=[entry])
        return stored

    async def delete(self, *, subject: str, document: UserStateDocument) -> None:
        """Drop one subject's document. Deleting what was never written is not an error."""
        key = state_key(subject, document)
        await self._call("DELETE", f"{self._base}/{key}", op="delete", key=key)

    async def _call(self, method: str, url: str, *, op: str, key: str, json: object | None = None) -> httpx.Response:
        """One request to the sidecar, with every failure mapped to a fail-closed 503.

        Both failure shapes land here: the sidecar being unreachable (it is still starting, or wedged) and
        the sidecar refusing the request (the component is not loaded, or this app-id is not in its
        ``scopes``). The distinction matters to an operator, so it is logged; it does not matter to the
        caller, who cannot proceed either way.
        """
        try:
            response = await self._client.request(method, url, json=json, timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.CONFLICT:
                # NOT an outage — the store is working exactly as asked. Dapr answers a first-write
                # etag mismatch with 409, which means another writer changed the document between this
                # caller's read and its write. Mapping it to the 503 below would tell the caller to
                # retry a broken dependency when what they must actually do is re-read and re-apply.
                log.info("user_state_etag_conflict", extra={"op": op, "key": key, "store": self._store})
                raise UserStateConflict(key) from exc
            log.warning(
                "user_state_store_rejected",
                extra={
                    "op": op,
                    "key": key,
                    "store": self._store,
                    "status": exc.response.status_code,
                    "body": exc.response.text[:500],
                },
            )
            raise ServiceUnavailableError(f"user state store {self._store!r} rejected the {op}") from exc
        except httpx.HTTPError as exc:
            log.warning(
                "user_state_store_unreachable",
                extra={"op": op, "key": key, "store": self._store, "error": str(exc)},
            )
            raise ServiceUnavailableError(f"user state store {self._store!r} is unreachable") from exc
        return response
