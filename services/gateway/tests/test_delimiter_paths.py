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


def test_a_path_the_proxy_cannot_represent_is_never_an_unhandled_500(proxied) -> None:
    """The gateway's contract has no 500 in it: upstream trouble is 502, a bad request is 4xx.

    Asserted as "not 5xx-by-accident" rather than one exact code, because the point is the CLASS of
    answer. A 500 here reports a healthy backend as broken — the backend was never called.
    """
    client, _ = proxied

    response = client.get("/api/catalog/v1/namespace/%00%01%02/list")

    assert response.status_code != 500, f"an unrepresentable path escaped as an unhandled 500: {response.text[:200]}"
