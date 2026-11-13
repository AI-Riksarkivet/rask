"""A lakehouse door judges a request from its headers before it reads the body, and a body it cannot parse is the
caller's error ([[XC-201]], audit C5, C9, A2).

- C9: lineage refuses a 64 MiB post that carries no token from its declared length, 413, without reading the body. The
  leg sends the headers alone and waits for the answer, as a caller holding a slow upload would.
- A2: uploads that carry no token hold none of the catalog's write seats: with as many unauthenticated bulk writes open
  as the catalog has seats, each sending its headers and none of its body, each is refused 401 from its headers and an
  authenticated insert lands. The seats are the release's own count over its replicas; with more than one replica the
  Service spreads the connections at random, so a replica may be left a seat short of full.
- C5: a grant whose body is not JSON answers 400 code 13 (InvalidInput), the caller's error, never a 500.

The pre-fix answers were measured on helm revision 47 (#794): lineage gave no answer within 5 s to the headers alone, and
the grant answered 500 code 18. A2's was not measured there, and is read off the limiter at 9f0f56c73^
(`catalog.api.load_shed`): it took a seat as a bulk write arrived, before its body and before any token was checked, and
held it while FastAPI waited for a body that never came, so the held uploads were answered nothing and the insert 429
(code 21), which this leg answers too with the held uploads admitted.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from contextlib import ExitStack
from urllib.parse import urlsplit

import pyarrow as pa
import pytest
from byo_scenario import LINEAGE
from chaos import CATALOG as CATALOG_DEPLOYMENT
from chaos import kubectl
from lance_namespace import ErrorCode
from refusals import assert_refused, namespace_uuid
from scratch_tenant import CATALOG, DEX, Tenant, ok, provisioned, tuples_on

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: Lineage's body cap is 8 MiB (`services.lineage.maxBodyBytes`); the Gateway admits 64 MiB.
OVERSIZED = 64 << 20
#: How long a door is given to answer a request from its headers.
HEADERS_ANSWER_SECONDS = 10.0

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (CATALOG and LINEAGE and DEX), reason="set LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL and LANCE_E2E_DEX (a deployed estate)"),
]


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("judge", grants=())


def _rows() -> bytes:
    return encode_arrow_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64())}))


def _headers_only(base: str, path: str, headers: dict[str, str]) -> socket.socket:
    """A connection to ``base`` that has sent a POST's headers and none of its body."""
    url = urlsplit(base)
    held = socket.create_connection((url.hostname or "", url.port or 80), timeout=HEADERS_ANSWER_SECONDS)
    lines = [f"POST {path} HTTP/1.1", f"Host: {url.netloc}", *(f"{name}: {value}" for name, value in headers.items())]
    held.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    return held


def _status(held: socket.socket) -> int | None:
    """The status a held request was answered with, or None when none came within the time a door is given."""
    try:
        line = held.recv(64).split(b"\r\n", 1)[0].decode()
    except TimeoutError:
        return None
    return int(line.split()[1]) if line.startswith("HTTP/") else None


def _write_seats() -> int:
    """The catalog's bulk-write seats, over all its replicas, as the release declares them."""
    deployment = json.loads(kubectl("get", f"deployment/{CATALOG_DEPLOYMENT}", "-o", "json"))
    container = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "catalog")
    seats = next(int(e["value"]) for e in container["env"] if e["name"] == "LANCE_MAX_CONCURRENT_WRITES")
    return seats * int(deployment["spec"]["replicas"])


def test_an_oversized_post_with_no_token_is_refused_from_its_headers() -> None:
    held = _headers_only(LINEAGE, "/api/v1/lineage", {"Content-Type": "application/json", "Content-Length": str(OVERSIZED), "Connection": "close"})
    with held:
        answered = _status(held)

    assert answered == 413, f"lineage answered a {OVERSIZED >> 20} MiB post with no token, its body unsent, with {answered}"


def test_uploads_with_no_token_hold_no_write_seat(tenant: Tenant) -> None:
    table = f"{tenant.bronze}$seats"
    ok(tenant.admin.http.post(f"{CATALOG}/v1/table/{table}/create", data=_rows(), headers={"content-type": ARROW_STREAM_MEDIA_TYPE}, timeout=60))
    tenant.tables.append(table)
    upload = {"Content-Type": ARROW_STREAM_MEDIA_TYPE, "Content-Length": str(1 << 20)}
    with ExitStack() as stack:
        held = [stack.enter_context(_headers_only(CATALOG, f"/v1/table/{table}/insert?mode=append", upload)) for _ in range(_write_seats())]
        # Each held upload's answer is read before the insert is sent, so the insert meets every one already judged.
        answers = [_status(each) for each in held]

        landed = tenant.admin.http.post(
            f"{CATALOG}/v1/table/{table}/insert", params={"mode": "append"}, data=_rows(), headers={"content-type": ARROW_STREAM_MEDIA_TYPE}, timeout=60
        )

    assert (answers, landed.status_code) == ([401] * len(held), 200), (
        f"{len(held)} uploads with no token were answered {answers}, and an authenticated insert beside them {landed.status_code}: {landed.text[:300]}"
    )


def test_a_grant_whose_body_is_not_json_is_the_callers_error(tenant: Tenant) -> None:
    """Sent as `text/plain`: a body declared JSON is parsed by FastAPI before any gate runs and answered 422, so only a
    body the client did not declare JSON reaches the grant gate's own read of it, which is where C5 answered 500."""
    admin = tenant.admin
    namespace = namespace_uuid(CATALOG, tenant.bronze, dict(admin.http.headers))

    assert_refused(
        lambda: admin.http.post(
            f"{CATALOG}/management/v1/namespace/{tenant.bronze}/access/grant", data=b"{", headers={"content-type": "text/plain"}, timeout=60
        ),
        status=400,
        code=ErrorCode.INVALID_INPUT,
        unchanged=lambda: tuples_on(admin, f"namespace:{namespace}"),
        foreign=(),
    )
