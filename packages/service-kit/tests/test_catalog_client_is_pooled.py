"""SK-03 — one urllib3 connection pool per catalog, not one per `open_reader`/`open_writer` call.

`RestCatalogTransport.__init__` / `RestCatalogWriteTransport.__init__` each built a fresh generated
`ApiClient`, and an `ApiClient` builds a fresh `urllib3.PoolManager`. Both ctors run once per
`open_reader` / `open_writer` call — i.e. per REQUEST on the annotator's read and write paths — so
every catalog call opened a brand-new pool, connected, and threw the connection away. The generated
client's `__exit__` is `pass`, so even `with ApiClient(...)` closes nothing: the pools were released
only by the garbage collector.

The remedy is one client per (catalog host, retry policy), which means the caller's bearer can no
longer be a DEFAULT header on a shared object — it rides each request instead. That is the part with
teeth: a token that leaked between transports would authorize one user's read as another's.
"""

from __future__ import annotations

import pytest

from service_kit.lancekit.catalog_client import catalog_api_client, reset_catalog_clients


pytest.importorskip("lance_namespace_urllib3_client")

from service_kit.lancekit.reader import RestCatalogTransport
from service_kit.lancekit.writer import RestCatalogWriteTransport


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    reset_catalog_clients()


def _pool(transport: RestCatalogTransport | RestCatalogWriteTransport) -> object:
    api = transport._api.api_client  # the generated DataApi's ApiClient
    return api.rest_client.pool_manager


def test_repeated_open_reader_calls_share_one_connection_pool() -> None:
    pools = {id(_pool(RestCatalogTransport("http://catalog:2333", token=f"user-{n}"))) for n in range(5)}
    assert len(pools) == 1, f"{len(pools)} connection pools for 5 reads against one catalog — each one connects from scratch"


def test_the_reader_and_the_writer_share_the_pool_for_one_catalog() -> None:
    reader = RestCatalogTransport("http://catalog:2333")
    writer = RestCatalogWriteTransport("http://catalog:2333", ["ns", "t"])
    assert _pool(reader) is _pool(writer)


def test_two_catalogs_do_not_share_a_pool() -> None:
    assert _pool(RestCatalogTransport("http://a:2333")) is not _pool(RestCatalogTransport("http://b:2333"))


def test_the_caller_token_never_becomes_a_default_header_on_the_shared_client() -> None:
    """The whole reason the token moved off the client: the client is shared."""
    alice = RestCatalogTransport("http://catalog:2333", token="alice")
    bob = RestCatalogTransport("http://catalog:2333", token="bob")
    shared = alice._api.api_client
    assert shared is bob._api.api_client
    assert "Authorization" not in shared.default_headers, "a shared client carrying one caller's bearer authorizes every other caller as them"
    assert alice.request_headers()["Authorization"] == "Bearer alice"
    assert bob.request_headers()["Authorization"] == "Bearer bob"


def test_an_anonymous_transport_sends_no_authorization_header() -> None:
    assert "Authorization" not in RestCatalogTransport("http://catalog:2333").request_headers()


def test_the_per_call_headers_are_a_fresh_mapping_each_time() -> None:
    """The generated serializer MUTATES the `_headers` dict it is handed (it writes `Accept` and
    `Content-Type` into it), so handing it the transport's own dict would accumulate one operation's
    content type onto the next."""
    transport = RestCatalogTransport("http://catalog:2333", token="alice")
    first = transport.request_headers()
    first["Content-Type"] = "application/vnd.made-up"
    assert "Content-Type" not in transport.request_headers()


def test_the_shared_client_is_reachable_by_name() -> None:
    assert catalog_api_client("http://catalog:2333") is catalog_api_client("http://catalog:2333")
