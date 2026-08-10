"""What a REFUSAL looks like after it crosses the `/api/notifications` row.

The two sibling suites drive this row on the happy path: `test_notifications_route.py` proves the row
exists, rewrites and is env-overridable, and `test_notifications_proxy_shapes.py` proves a body and an
opaque cursor survive the hop. Both assert on 200s, which leaves the answers that actually carry
information unasserted — and this row's refusals are the ones a user sees.

The inbox door refuses in four ways and each names itself in an RFC 9457 body: a cursor it did not
mint (400), an unauthenticated read (401), a forbidden object (403), and an authorization or actor
plane that is enabled but unwired (503). The bell renders that reason. A hop that kept the status and
dropped the body would turn "your authorization layer is down" into a blank panel — the empty-200
failure mode the door was built against, re-introduced one layer out.

The other half is diagnosis: the gateway mints its OWN 502 when an upstream is unreachable, so a
service refusal and a missing pod must not arrive looking alike. One sends an operator to the
notifications pod; the other sends them to the route table.

The mock transport returns `stream=httpx.ByteStream(...)`: the proxy re-streams via `aiter_raw()`, and
a preloaded body is already marked consumed (`StreamConsumed`).
"""

import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient


#: The RFC 9457 media type `service_kit.exceptions.register_handlers` serves every domain error with.
PROBLEM_JSON = "application/problem+json"


def _problem(status: int, title: str, detail: str) -> bytes:
    """One refusal body, shaped exactly as `service_kit.exceptions._problem` renders it.

    Written out rather than imported so this file stays a GATEWAY test — what is asserted below is
    that these bytes arrive unchanged, and importing the producer would let a change in either end
    move both at once.
    """
    return json.dumps({"type": f"about:blank#{title.lower().replace(' ', '')}", "title": title, "status": status, "detail": detail}).encode()


#: The four refusals the inbox door issues, with the reason each carries (`services/notifications`:
#: the cursor decoder, `make_auth_deps`, the FGA door, and `require_actor_plane`).
REFUSALS = [
    (400, _problem(400, "Bad Request", "the inbox cursor is not readable")),
    (401, _problem(401, "Unauthorized", "Missing bearer token")),
    (403, _problem(403, "Forbidden", "you may not read that object")),
    (503, _problem(503, "Service Unavailable", "the inbox actor plane is unregistered")),
]


class _Upstream:
    """The notifications service, as far as the gateway can tell.

    A class rather than a closure over a dict because two of the three tests below change the answer
    mid-fixture, and one of them makes the upstream vanish — which is a different KIND of answer, not
    a status code, and it reads better as its own attribute than as a sentinel status.
    """

    def __init__(self) -> None:
        self.status = 200
        self.body = b'{"ok": true}'
        self.content_type = "application/json"
        self.unreachable = False
        self.seen: list[httpx.Request] = []

    def refuses(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.content_type = PROBLEM_JSON

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.unreachable:
            # What a dead pod looks like to httpx: the proxy's `except httpx.RequestError` is the
            # branch under test, and it is reachable no other way through a MockTransport.
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        return httpx.Response(self.status, stream=httpx.ByteStream(self.body), headers={"content-type": self.content_type})


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


@pytest.fixture
def row(gw):
    """(TestClient, the upstream double) with the notifications row pointed at it."""
    upstream = _Upstream()
    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle))
        yield client, upstream


@pytest.mark.parametrize(("status", "body"), REFUSALS, ids=[str(status) for status, _ in REFUSALS])
def test_a_refusal_reaches_the_caller_with_its_status_type_and_reason_intact(row, status: int, body: bytes) -> None:
    """Status, media type and body — the three a proxy can lose one of while the other two look right.

    Byte-identity rather than a parsed comparison: the panel reads `detail`, and a hop that re-encoded
    an equivalent body would satisfy `==` on the parsed dict while still being a hop this row must not
    have. The media type is asserted separately because it is what makes the body a PROBLEM rather
    than an object the client tries to render as a feed.
    """
    client, upstream = row
    upstream.refuses(status, body)

    response = client.get("/api/notifications/inbox")

    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.content == body


def test_a_service_refusal_is_not_flattened_into_the_gateways_own_unreachable_error(row) -> None:
    """A 503 from the inbox and a 502 from the gateway mean different things and must stay different.

    The inbox's 503 says "this service is up and refusing, here is why" — an unregistered actor plane,
    or FGA enabled and unwired. The gateway's 502 says "nothing answered at that address". Conflating
    them costs an operator the first hour: one is a values/scope problem in the notifications pod, the
    other is a Service that does not resolve.
    """
    client, upstream = row
    upstream.refuses(503, _problem(503, "Service Unavailable", "the inbox actor plane is unregistered"))

    response = client.get("/api/notifications/inbox")

    assert response.status_code == 503
    assert response.json()["detail"] == "the inbox actor plane is unregistered"
    assert "unreachable" not in response.text


def test_an_inbox_the_gateway_cannot_reach_is_a_502_naming_the_upstream_rather_than_a_404(row) -> None:
    """The other side of that split, and the one the row's own default makes easy to misread.

    `RASK_NOTIFICATIONS_URL` defaults to `127.0.0.1:8850` — the gateway pod ITSELF when the chart
    forgets to render the address (`chart/templates/configmap.yaml` says so in as many words). The
    answer must name an address, because a 404 here would read as "no such route" and send the reader
    to the route table, which is the one place that is correct.
    """
    client, upstream = row
    upstream.unreachable = True

    response = client.get("/api/notifications/inbox")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "unreachable" in detail
    assert "8850" in detail, f"the 502 does not say WHICH upstream: {detail!r}"
    assert upstream.seen, "the gateway did not attempt the row at all"
