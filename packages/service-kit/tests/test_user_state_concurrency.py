"""§2.7 — a SHARED state document needs optimistic concurrency; a per-user one does not.

THE DEFECT. `UserStateStore.put` wrote blind and said so: "there is no etag round trip because there
is no second writer to lose a race with". True of the per-USER documents this module was built for —
one person, their own tabs, localStorage semantics. False of `ATTACHED_STORES` under `ESTATE_SUBJECT`,
which is estate-scoped and written by every admin. Two concurrent attaches each read N stores and each
wrote N+1, so whichever landed second erased the other's. The docstring is what made it invisible.

These pin both halves: the default stays last-write-wins (adding concurrency where there is no
contender is machinery for a conflict that cannot happen), and the opt-in actually carries Dapr's
`first-write` — because an etag under the DEFAULT concurrency is accepted and ignored, which would be
the same lost update wearing a safety belt.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from service_kit.exceptions import ConflictError
from service_kit.governed.user_state import (
    ESTATE_SUBJECT,
    UserStateConflict,
    UserStateDocument,
    UserStateStore,
)


DOC = UserStateDocument.ATTACHED_STORES


def _store(handler: Any) -> UserStateStore:
    return UserStateStore(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), store_name="lance-statestore")


@pytest.mark.asyncio
async def test_a_concurrent_write_carries_the_etag_and_first_write() -> None:
    """The token alone is not the fix. Under Dapr's default (`last-write`) an etag is accepted and
    IGNORED — so the request must name `first-write` or nothing is being checked."""
    sent: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        sent.append(_json.loads(request.content))
        return httpx.Response(204)

    await _store(handler).put(subject=ESTATE_SUBJECT, document=DOC, value=[], etag="v7", concurrent=True)

    assert sent[0][0]["etag"] == "v7"
    assert sent[0][0]["options"] == {"concurrency": "first-write"}


@pytest.mark.asyncio
async def test_the_DEFAULT_write_is_unchanged() -> None:
    """Per-user documents have one writer. A round trip there is cost with no benefit, and every
    existing caller passes nothing — so the default must stay exactly what it was."""
    sent: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        sent.append(_json.loads(request.content))
        return httpx.Response(204)

    await _store(handler).put(subject="alice", document=UserStateDocument.SAVED_VIEWS, value={"a": 1})

    assert "etag" not in sent[0][0]
    assert "options" not in sent[0][0], "a per-user write must not pay for concurrency it cannot lose"


@pytest.mark.asyncio
async def test_a_409_is_a_CONFLICT_not_an_outage() -> None:
    """The distinction the caller acts on. A 503 says "your dependency is broken, back off"; this is a
    healthy store correctly refusing a stale write, and the answer is to re-read and re-apply."""
    store = _store(lambda _r: httpx.Response(409, text="etag mismatch"))

    with pytest.raises(UserStateConflict) as caught:
        await store.put(subject=ESTATE_SUBJECT, document=DOC, value=[], etag="stale", concurrent=True)

    assert isinstance(caught.value, ConflictError), "it must render as a 409 to the caller, not a 503"


@pytest.mark.asyncio
async def test_get_versioned_returns_the_sidecar_etag() -> None:
    """The token has to come from the READ, or the write is guessing."""
    body = b'{"subject":"__estate__","document":"attached-stores","updated_at":"2026-08-10T00:00:00Z","value":[]}'
    store = _store(lambda _r: httpx.Response(200, content=body, headers={"ETag": "v9"}))

    stored, etag = await store.get_versioned(subject=ESTATE_SUBJECT, document=DOC)

    assert stored is not None
    assert etag == "v9"


@pytest.mark.asyncio
async def test_an_ABSENT_document_yields_no_etag() -> None:
    """`None` is the correct token for "I believe nobody has written this" — Dapr turns a first-write
    with no etag into create-if-absent, so the very first attach is still race-safe."""
    store = _store(lambda _r: httpx.Response(204))

    stored, etag = await store.get_versioned(subject=ESTATE_SUBJECT, document=DOC)

    assert stored is None
    assert etag is None
