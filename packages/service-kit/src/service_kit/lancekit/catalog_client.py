"""ONE urllib3 client per catalog, shared by every transport that talks to it.

`RestCatalogTransport` and `RestCatalogWriteTransport` each built their own generated `ApiClient`
in `__init__`, and an `ApiClient` builds a `urllib3.PoolManager`. Those ctors run once per
`open_reader` / `open_writer` call — per REQUEST on the annotator's read and write paths — so every
catalog call stood up a fresh pool, opened a fresh connection, and dropped it. Nothing closed them
either: the generated `ApiClient.__exit__` is `pass`, so `with ApiClient(...)` releases nothing and
the sockets went back only when the garbage collector got there.

Keyed on (base URL, retry policy) because those are the only two things that shape the pool. NOT on
the caller's bearer: a per-token cache would be a process-global store of end-user credentials, and
the point of the cache is that the pool outlives any one caller. So the token stops being a DEFAULT
header on the client and rides each request instead — see `request_headers`. That is the load-bearing
half of this change: a shared client carrying one caller's `Authorization` would answer the next
caller's read under the previous caller's identity, and the catalog authorizes on the bearer.

Unbounded on purpose (`maxsize=None`): the key space is the set of catalogs this process talks to,
which is one in every deployment and two in a test. An LRU would evict a live pool for no gain.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import ApiClient


@cache
def catalog_api_client(base_url: str, *, retries: int = 3) -> ApiClient:
    """The shared generated client for one catalog — construct transports through this, never directly."""
    from lance_namespace_urllib3_client import ApiClient, Configuration  # optional dep

    return ApiClient(Configuration(host=base_url, retries=retries))


def request_headers(token: str | None) -> dict[str, str]:
    """The per-request headers carrying THIS caller's bearer (empty when anonymous).

    A FRESH dict every call, and that is not defensive style: the generated serializer takes the
    `_headers` mapping it is handed and writes `Accept` / `Content-Type` into it, so a transport that
    passed its own stored dict would accumulate one operation's content type onto the next request.
    """
    return {"Authorization": f"Bearer {token}"} if token else {}


def reset_catalog_clients() -> None:
    """Drop the cached clients — for tests that assert on pool identity across cases."""
    catalog_api_client.cache_clear()


__all__ = ["catalog_api_client", "request_headers", "reset_catalog_clients"]
