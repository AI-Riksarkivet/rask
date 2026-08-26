"""A namespace identifier's delimiter must survive the proxy hop.

The Lance Namespace spec joins identifier segments with a configured delimiter, and the multipart
form is the **unit separator**, `0x1F` — `["acme", "bronze"]` travels the wire as `acme%1Fbronze`,
and the ROOT namespace is the delimiter alone, `%1F`. Both are ordinary spec traffic, not an edge
case someone invented.

MEASURED against the live estate, 2026-08-26, on a gateway built from HEAD::

    GET /api/catalog/v1/namespace/acme-bronze/list    -> 200
    GET /api/catalog/v1/namespace/%1F/list            -> 500
    GET /api/catalog/v1/namespace/acme%1Fbronze/list  -> 500

with the gateway logging an unhandled ``httpx.InvalidURL: Invalid non-printable ASCII character in
URL, '\\x1f' at position 62``.

THE MECHANISM. Starlette's ``request.url.path`` is percent-DECODED, so `%1F` has already become a
literal `\\x1f` by the time the handler sees it. `_normalize_path` then canonicalises that decoded
string — which is CORRECT and must stay that way, because the dot-segment and duplicate-slash
collapsing exists so that `%2E%2E` cannot dodge the 403 blocklist or slip past a longer route prefix;
a normaliser that read the raw path would be blind to exactly the encodings it is there to catch.
The defect is one step later: the normalised path is interpolated straight into
``httpx.URL(f"{base}{upstream_path}")``, and httpx refuses non-printable ASCII in a URL. So the
encoding is dropped on the way IN and never restored on the way OUT.

TWO THINGS ARE WRONG AND BOTH ARE TESTED HERE. The path must be re-encoded so the delimiter reaches
the upstream intact; and even an unrepresentable path must not escape as a 500 traceback, because the
gateway's contract is explicit that upstream trouble is a clean 502 and a bad request is a 4xx — an
unhandled exception is neither, and it tells an operator the backend is broken when the backend was
never called.

The `$` case is the regression guard rather than the bug: `$` is the DEFAULT delimiter, it is what
every table id in this estate actually uses (`acme-bronze$agnostic`), and it works today only because
`$` is a printable character httpx tolerates. It passes before the fix and must keep passing after —
note that it arrives at the upstream DECODED today (`acme-bronze$agnostic`, not `%24`), so the
assertion is on the identifier rather than its spelling; a fix that re-encodes the path changes the
bytes on the wire and must not change what the upstream resolves.
"""

import importlib
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


@pytest.fixture
def proxied(gw):
    """(TestClient, captured upstream requests) with every upstream mocked 200."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'), headers={"content-type": "application/json"})

    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield client, captured


@pytest.mark.parametrize(
    ("sent", "expected_identifier"),
    [
        ("%1F", "\x1f"),
        ("acme%1Fbronze", "acme\x1fbronze"),
        ("acme%1Fbronze%1Fnested", "acme\x1fbronze\x1fnested"),
    ],
)
def test_the_unit_separator_delimiter_reaches_the_upstream(proxied, sent: str, expected_identifier: str) -> None:
    """`0x1F` is the spec's multipart delimiter; a proxy that cannot carry it cannot serve nested namespaces.

    Asserted on the DECODED identifier rather than a literal `%1F`, because whether the hop spells the
    delimiter percent-encoded or raw is the proxy's business — what the upstream must receive is the
    same identifier the client sent. Pinning the spelling would fail a correct fix that happens to
    normalise the encoding, and would pass a broken one that preserved the bytes of the wrong segment.
    """
    client, captured = proxied

    response = client.get(f"/api/catalog/v1/namespace/{sent}/list")

    assert response.status_code == 200, f"the delimiter path did not survive the hop: {response.status_code} {response.text[:200]}"
    assert captured, "the request never reached an upstream at all"
    arrived = unquote(captured[0].url.raw_path.decode("ascii"))
    assert arrived == f"/v1/namespace/{expected_identifier}/list", f"the delimiter was lost or mangled on the way to the upstream: {arrived!r}"


def test_the_default_dollar_delimiter_is_unchanged(proxied) -> None:
    """`$` is what every table id in this estate uses — re-encoding the path must not move this row."""
    client, captured = proxied

    response = client.get("/api/catalog/v1/table/acme-bronze%24agnostic/describe")

    assert response.status_code == 200
    arrived = unquote(captured[0].url.raw_path.decode("ascii"))
    assert arrived == "/v1/table/acme-bronze$agnostic/describe", f"the table delimiter was rewritten: {arrived!r}"


def test_a_raw_path_the_proxy_cannot_represent_is_a_400_not_a_500(gw, proxied) -> None:
    """The refusal branch, reached through a hand-built ASGI scope because no client can send this.

    An earlier version of this test drove `/api/catalog/v1/namespace/%00%01%02/list` through the
    TestClient and asserted "not 500". That was VACUOUS: once the raw path is forwarded, `%00%01%02`
    is perfectly representable on the wire, the request proxies 200, and the branch it claimed to pin
    was never entered. What genuinely cannot be represented is a NON-ASCII byte sent raw in the path,
    which httpx and the ASCII decode both refuse — and an HTTP client will not produce one, so the
    scope is constructed directly.

    400 rather than 502 is the point. The input is client-controlled, and this gateway's 502 means
    "backend down"; answering 502 for a malformed identifier tells an operator a healthy service is
    broken.
    """
    import anyio

    # `proxied` is taken purely to enter the TestClient context, which runs the lifespan that builds
    # `app.state.api_prefix` and the route table. Driving a bare `app(scope, ...)` without it fails
    # on missing state rather than on the thing under test.
    received: list[dict] = []

    async def drive() -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/catalog/v1/table/x/describe",
            "raw_path": b"/api/catalog/v1/table/\xff\xfe/describe",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            received.append(message)

        await gw.app(scope, receive, send)

    anyio.run(drive)

    start = next(m for m in received if m["type"] == "http.response.start")
    assert start["status"] == 400, f"a non-ASCII raw path did not come back as a refusal: {start['status']}"


@pytest.mark.parametrize(
    ("sent", "arrives_as"),
    [
        # An encoded slash is a literal character INSIDE one segment, never a separator. Promoting it
        # silently addresses a different object: `object/a%2Fb` is one key named `a/b`, not `a` then `b`.
        ("/api/explorer/object/a%2Fb", "/api/object/a%2Fb"),
        # `?` and `#` are the worse pair, because Starlette's `URL.path` re-splits the DECODED string
        # with urlsplit: a decoded `?` starts a query and a decoded `#` starts a fragment, so the tail
        # of the path is moved or dropped BEFORE any routing or blocklist decision is taken. That is
        # "guarded against one resource, executed against another".
        ("/api/catalog/v1/table/x%3Fy/describe", "/v1/table/x%3Fy/describe"),
        ("/api/catalog/v1/table/x%23y/describe", "/v1/table/x%23y/describe"),
    ],
)
def test_a_percent_encoded_reserved_character_stays_inside_its_segment(proxied, sent: str, arrives_as: str) -> None:
    """Re-encoding cannot repair these — the damage happens before the handler runs.

    `quote()` on the decoded path fixes `%1F` (a character that merely offended httpx) and cannot fix
    these three (characters that had already changed the path's STRUCTURE). Only the raw path carries
    the distinction, which is why the fix reads `scope["raw_path"]` rather than re-encoding.
    """
    client, captured = proxied

    response = client.get(sent)

    assert response.status_code == 200, f"{sent} -> {response.status_code}"
    assert captured, "the request never reached an upstream at all"
    assert captured[0].url.raw_path.decode("ascii") == arrives_as


def test_an_encoded_dot_segment_cannot_dodge_the_blocklist(proxied) -> None:
    """The security property that forced normalisation onto the DECODED path in the first place.

    `%2e%2e` is an ordinary segment to a byte-wise walker and a traversal to a decoding one. The
    sidecar-only lineage routes are blocked by prefix on the normalised path, so if the fix moved
    normalisation onto the raw bytes, `lineage-events` would become reachable by spelling the dots in
    hex. Driven through the app rather than by calling `_normalize_path` directly, because the
    decoding happens in the server before the handler and a unit call would test the wrong input.
    """
    client, captured = proxied

    response = client.get("/api/lineage/foo/%2e%2e/lineage-events")

    assert response.status_code == 403, f"an encoded dot-segment reached the lineage proxy: {response.status_code}"
    assert not captured, "a sidecar-only route was forwarded to an upstream"


def test_a_dot_segment_hidden_behind_an_encoded_slash_is_refused(proxied) -> None:
    """The one input where the raw and decoded views genuinely disagree, so neither may be trusted.

    `a%2F..%2Fb` is ONE segment byte-wise and THREE after decoding. Forwarding the raw bytes would
    execute a path the decoded blocklist never evaluated; collapsing it would address an object the
    client never named. Refusing is strictly safer than picking a side, and no legitimate client
    sends it.
    """
    client, captured = proxied

    response = client.get("/api/catalog/v1/table/a%2F..%2Fb/describe")

    assert response.status_code == 400, f"expected a refusal, got {response.status_code}"
    assert not captured, "an ambiguous path was forwarded to an upstream"
