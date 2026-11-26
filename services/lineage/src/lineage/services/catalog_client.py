"""What lineage reaches its catalog with, built once by its lifespan and closed by it (:class:`CatalogClients`): ONE
urllib3 client per kind of call, shared by every transport that talks to the catalog, and the threads those calls run on.

An `ApiClient` builds a `urllib3.PoolManager`, and a transport may be constructed once per REQUEST, so a client per
transport would stand up a fresh pool and connection for every catalog call and drop it. Nothing would close them
either: the generated `ApiClient.__exit__` is `pass`, so `with ApiClient(...)` releases nothing and the sockets go back
only when the garbage collector gets there.

One pair per catalog, shaped by its base URL and retry policy alone. NOT by the caller's bearer: a client per token
would be a process-wide store of end-user credentials, and the point of sharing is that the pool outlives any one
caller. So the token is never a DEFAULT header on the client and rides each request instead — see `request_headers`:
a shared client carrying one caller's `Authorization` would answer the next caller's read under the previous caller's
identity, and the catalog authorizes on the bearer.

A READ HAS A CLIENT OF ITS OWN (:attr:`CatalogClients.read`), which sends a request again on a connection the catalog
dropped. Every Lance-namespace read is a POST, and urllib3 sends a POST again only when the request never left (its
`Retry` takes a dropped connection's error as a read error, which it does not retry for a method it holds unsafe); so a
read meeting a pooled connection the catalog has just closed for idleness fails outright on the stricter client.
Measured live 2026-11-12: all 8 of lineage's readiness flips in 6.7 h were that race (`RemoteDisconnected` 6,
`ConnectionResetError` 2), its 5 s feed read meeting the catalog's 5 s keep-alive close. A write keeps the stricter
client, since a write the catalog may have applied is never sent twice.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

from urllib3.util.retry import Retry

from lakehouse.process.own_threads import ThreadSet


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import ApiClient


class CatalogClients:
    """The clients and threads lineage's calls through one catalog share, for the lifespan that builds them to close.

    Attributes:
        url: The catalog's base URL, without a trailing slash.
        write: The generated client for a write: a request the catalog may have applied is never sent twice.
        read: The generated client for a read: a POST included, a request the catalog's dropped connection cut is sent
            again on a new one, up to ``retries`` times.
        calls: The threads lineage's calls through the catalog run on, a door's, the ingest lane's and the relay's alike,
            a set of their own: a burst of them holds no request thread, and queues none of the process's own work (its
            renewals, and the feed read its readiness rests on, `ProvenanceStore.ready`).
        writer: The one thread a burst of recorded events is landed on (`ProvenanceStore.record`), apart from ``calls``,
            so the doors' reads take no seat from the writer and it takes none from them.
    """

    def __init__(self, base_url: str, *, retries: int = 3) -> None:
        from lance_namespace_urllib3_client import ApiClient, Configuration  # optional dep

        self.url = base_url.rstrip("/")
        self.write: ApiClient = ApiClient(Configuration(host=self.url, retries=retries))
        reading = Configuration(host=self.url)
        # The generated `Configuration` annotates `retries` as an int and forwards it unchanged to urllib3's `PoolManager`,
        # which takes a `Retry` as well: `allowed_methods=None` is what lets a POST be sent again.
        reading.retries = cast("int", Retry(total=retries, allowed_methods=None))
        self.read: ApiClient = ApiClient(reading)
        self.calls = ThreadSet("catalog-calls")
        self.writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lakehouse-provenance-record")

    def close(self) -> None:
        """End the threads, each call that had started first, then close both clients' connections. The generated client
        has no close of its own (its ``__exit__`` is ``pass``), so the pools are cleared here. Blocking: a burst still
        landing is waited for, so an async caller runs this in a worker thread. A thread set whose shutdown raises
        keeps neither the other set nor the connections open: each is still ended, and the failure is raised after."""
        try:
            self.writer.shutdown(wait=True)
        finally:
            try:
                self.calls.close()
            finally:
                for client in (self.write, self.read):
                    client.rest_client.pool_manager.clear()


def request_headers(token: str | None) -> dict[str, str]:
    """The per-request headers carrying THIS caller's bearer (empty when anonymous).

    A FRESH dict every call, and that is not defensive style: the generated serializer takes the
    `_headers` mapping it is handed and writes `Accept` / `Content-Type` into it, so a transport that
    passed its own stored dict would accumulate one operation's content type onto the next request.
    """
    return {"Authorization": f"Bearer {token}"} if token else {}


__all__ = ["CatalogClients", "request_headers"]
