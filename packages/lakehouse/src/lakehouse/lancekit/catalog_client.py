"""ONE urllib3 client per catalog, shared by every transport that talks to it.

An `ApiClient` builds a `urllib3.PoolManager`, and a transport may be constructed once per REQUEST, so a client per
transport would stand up a fresh pool and connection for every catalog call and drop it. Nothing would close them
either: the generated `ApiClient.__exit__` is `pass`, so `with ApiClient(...)` releases nothing and the sockets go back
only when the garbage collector gets there.

Keyed on (base URL, retry policy) because those are the only two things that shape the pool. NOT on
the caller's bearer: a per-token cache would be a process-global store of end-user credentials, and
the point of the cache is that the pool outlives any one caller. So the token stops being a DEFAULT
header on the client and rides each request instead — see `request_headers`. That is the load-bearing
half of this change: a shared client carrying one caller's `Authorization` would answer the next
caller's read under the previous caller's identity, and the catalog authorizes on the bearer.

Unbounded on purpose (`maxsize=None`): the key space is the set of catalogs this process talks to,
which is one in every deployment and two in a test. An LRU would evict a live pool for no gain.

A READ HAS A CLIENT OF ITS OWN (:func:`catalog_read_client`), which sends a request again on a connection the catalog
dropped. Every Lance-namespace read is a POST, and urllib3 sends a POST again only when the request never left (its
`Retry` takes a dropped connection's error as a read error, which it does not retry for a method it holds unsafe); so a
read meeting a pooled connection the catalog has just closed for idleness fails outright on the stricter client.
Measured live 2026-11-12: all 8 of lineage's readiness flips in 6.7 h were that race (`RemoteDisconnected` 6,
`ConnectionResetError` 2), its 5 s feed read meeting the catalog's 5 s keep-alive close. A write keeps the stricter
client, since a write the catalog may have applied is never sent twice.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, cast

from urllib3.util.retry import Retry


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import ApiClient


@cache
def catalog_api_client(base_url: str, *, retries: int = 3) -> ApiClient:
    """The shared generated client for one catalog, for a write — construct transports through this, never directly."""
    from lance_namespace_urllib3_client import ApiClient, Configuration  # optional dep

    return ApiClient(Configuration(host=base_url, retries=retries))


@cache
def catalog_read_client(base_url: str, *, retries: int = 3) -> ApiClient:
    """The shared generated client for one catalog, for a read: a POST included, a request the catalog's dropped
    connection cut is sent again on a new one, up to ``retries`` times."""
    from lance_namespace_urllib3_client import ApiClient, Configuration  # optional dep

    configuration = Configuration(host=base_url)
    # The generated `Configuration` annotates `retries` as an int and forwards it unchanged to urllib3's `PoolManager`,
    # which takes a `Retry` as well: `allowed_methods=None` is what lets a POST be sent again.
    configuration.retries = cast("int", Retry(total=retries, allowed_methods=None))
    return ApiClient(configuration)


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
    catalog_read_client.cache_clear()


__all__ = ["catalog_api_client", "catalog_read_client", "request_headers", "reset_catalog_clients"]
