"""The actor callback surface is not a public read API for other people's inboxes.

THE HOLE. `DaprActor(app)` root-mounts `PUT /actors/{actor_type}/{actor_id}/method/{method}`, and for
this service the actor IS the private store — one inbox per subject, keyed by an id that is merely
`base64url(sub)`. The inbox's own doors (OIDC, `require_actor_plane`, the FGA re-check) hang on the
ROUTER under `settings.api_prefix`; these routes are mounted at the ROOT and inherit none of them. So
before the guard, anything able to reach the pod read any actor's state by name. The notifications inbox is the
sharpest case — one inbox per subject, actor id `base64url(sub)`:

    curl -XPUT :8850/actors/InboxActor/YWxpY2U/method/Page -d '{"state":"all","limit":100}'

`YWxpY2U` is `base64url("alice")` and subjects are readable off lineage's author facets — the actor id
was never a secret, so it cannot be the access control.

It lives in `service_kit` because TWO services mount `DaprActor` this way — notifications (the
per-subject inbox) and the annotator (claims, drafts, reviews, the lease holder) — and two copies of
an auth guard is how they drift apart.

These drive the middleware through a real app rather than calling it directly, because what is being
asserted is that the guard is actually MOUNTED and matches the right paths — a correct function that
nothing installs would pass a direct call and leave the hole open.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.governed.dapr_auth import guard_actor_routes


ALICE = "YWxpY2U"  # base64url("alice"), exactly as the exploit would compose it


@pytest.fixture
def client() -> TestClient:
    """An app carrying the guard plus one route at each side of the boundary."""
    app = FastAPI()
    guard_actor_routes(app)

    @app.put("/actors/{actor_type}/{actor_id}/method/{method}")
    async def _actor(actor_type: str, actor_id: str, method: str) -> dict[str, str]:
        return {"actor_id": actor_id}

    @app.get("/api/inbox")
    async def _inbox() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app, raise_server_exceptions=False)


def test_an_unauthenticated_caller_cannot_read_an_inbox_by_actor_id(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression, in the exact shape of the reported exploit."""
    monkeypatch.setenv("APP_API_TOKEN", "the-sidecar-token")

    response = client.put(f"/actors/InboxActor/{ALICE}/method/Page", json={"state": "all", "limit": 100})

    assert response.status_code == 403
    assert ALICE not in response.text, "the refusal echoed the actor id back"


def test_a_public_front_door_is_refused_even_with_no_token_configured(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that still holds in dev. `APP_API_TOKEN` unset makes the token check a no-op, so the
    public-caller refusal is the only guard there is — and a front door's Dapr token authenticates the
    PROXY, not the person behind it."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    response = client.put(f"/actors/InboxActor/{ALICE}/method/Page", json={}, headers={"dapr-caller-app-id": "gateway"})

    assert response.status_code == 403


def test_the_sidecar_itself_still_gets_through(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not break the thing it protects: daprd calls these routes back, presenting
    `dapr-api-token`. A gate that also refuses the sidecar would take the whole actor plane down."""
    monkeypatch.setenv("APP_API_TOKEN", "the-sidecar-token")

    response = client.put(
        f"/actors/InboxActor/{ALICE}/method/Page",
        json={},
        headers={"dapr-api-token": "the-sidecar-token"},
    )

    assert response.status_code == 200


def test_the_guard_does_not_touch_the_inbox_ROUTER(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scope check. The inbox routes carry their own doors (OIDC + FGA); applying the sidecar token to
    them as well would refuse every human caller, which is the failure mode of a path-prefix guard
    written one character too broad."""
    monkeypatch.setenv("APP_API_TOKEN", "the-sidecar-token")

    assert client.get("/api/inbox").status_code == 200


def test_the_dapr_CONFIG_probe_is_guarded_too(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/dapr/config` advertises the registered entity list and the actor runtime's timings. It is the
    same sidecar-only class and it leaks the actor TYPE names an attacker would otherwise have to
    guess, so it is inside the boundary rather than beside it."""
    monkeypatch.setenv("APP_API_TOKEN", "the-sidecar-token")

    assert client.get("/dapr/config").status_code in {403, 404}, "guarded (403) or absent (404) — never a 200 to an unauthenticated caller"
