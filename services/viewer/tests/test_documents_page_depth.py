"""`?page=1000000` was a legal request against the document gallery.

open_fastapi-audit — "The estate's only offset-paginated route has no MAX_OFFSET guard and runs an
unconditional `count_rows()` on a Lance table".

HALF THE FINDING HOLDS AND HALF IS WITHDRAWN BY ITS OWN VERIFIER, and only the half that holds is
implemented. Real: `page: Annotated[int, Query(ge=1)] = 1` has no upper bound, so the derived offset
is unbounded and `grep -rn 'MAX_OFFSET'` returned zero across the estate — nothing implemented the
reference's guard.

Not implemented, deliberately: `total = ds.count_rows() if page == 1 else None`. The verifier
withdraws the cost model behind it — an unfiltered Lance `count_rows()` is answered from fragment
metadata rather than a table scan, so it does not carry the `SELECT COUNT(*)` cost the reference
warns about, and "pins a threadpool worker for the duration of a full scan" is asserted, not shown.
Making `total` null on every page but the first would degrade the envelope for an unproven saving.

DRIVEN OVER HTTP, because the guard lives in the DEPENDENCY and a direct call to the handler would
never reach it — the property is that the refusal happens before the route body, which is the only
place it can be cheap.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real documents router, with the dataset resolved to its empty-descriptor branch.

    The stub only has to get PAST dependency resolution: with no document binding the handler returns
    its empty gallery, so a 200 proves the request reached the body and a refusal proves it did not.
    `get_state` is overridden rather than monkeypatched because it is a real FastAPI dependency;
    `dataset_handle` is a plain call inside the body, so it is patched at the module that calls it.
    """
    from viewer.api.v1.endpoints import system

    from service_kit.exceptions import register_handlers
    from service_kit.media.deps import get_state

    class _Declared:
        document = None

    class _Descriptor:
        declared = _Declared()
        tables: dict[str, Any] = {}

    class _Handle:
        descriptor = _Descriptor()

    monkeypatch.setattr(system, "dataset_handle", lambda *_args, **_kwargs: _Handle())

    app = FastAPI()
    app.include_router(system.router)
    app.dependency_overrides[get_state] = lambda: cast("Any", object())
    register_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


def _get(client: TestClient, **params: object) -> Any:
    return client.get("/api/documents", params=cast("dict[str, Any]", params))


def test_a_page_beyond_the_offset_limit_is_REFUSED(client: TestClient) -> None:
    """The finding: nothing bounded how far in a caller could ask to start."""
    response = _get(client, page=1_000_000, per_page=100)
    assert response.status_code in {400, 422}, f"page=1000000 was accepted ({response.status_code}) — the derived offset is 99,999,900 and nothing refused it"
    assert "cursor" in response.text.lower() or "keyset" in response.text.lower(), (
        f"the refusal does not name the alternative, so the caller has no way forward: {response.text[:300]}"
    )


def test_an_ordinary_page_is_untouched(client: TestClient) -> None:
    """The failure mode that would hide the fix: refusing everything also passes the test above."""
    assert _get(client, page=2, per_page=24).status_code == 200


def test_the_page_size_ceiling_is_still_enforced(client: TestClient) -> None:
    """The pre-existing `le=100` must survive the move into the shared params."""
    assert _get(client, page=1, per_page=101).status_code == 422


# ── the envelope ────────────────────────────────────────────────────────────────────────────────
#
# The other half of the same finding: `Page[Item]` in `service_kit.pagination` would be an envelope
# nothing sends. The gallery is its natural first consumer, and it is the reference's own argument —
# `pages`/`has_next`/`has_prev` belong on the wire so a client is not computing them from a partial
# view and drifting off-by-one from the server.


def test_the_gallery_sends_the_shared_envelope(client: TestClient) -> None:
    """An envelope no route sends is a type, not a contract."""
    body = _get(client, page=1, per_page=24).json()
    for field in ("items", "total", "page", "page_size", "pages", "has_next", "has_prev"):
        assert field in body, f"the gallery does not send `{field}` — the shared envelope is unused: {sorted(body)}"


def test_the_old_key_is_still_emitted_for_one_release(client: TestClient) -> None:
    """A RENAME IS A WIRE CHANGE, and the web pods roll separately from the viewer.

    `annotator/src/lib/select/DataSelection.svelte` reads `docsPage.docs`, and its Deployment is not
    this service's — so during a rolling upgrade an old web pod talks to a new viewer. Emitting both
    keeps that window working; the alias is marked for removal rather than left to become permanent.
    """
    body = _get(client, page=1, per_page=24).json()
    assert "docs" in body, "the deprecated `docs` alias is gone, so an un-rolled annotator pod renders nothing"
    assert body["docs"] == body["items"], "the alias must MIRROR the new field, not default"
