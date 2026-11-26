"""SK-03 — one urllib3 connection pool per catalog, not one per transport.

A transport may be constructed once per REQUEST, and an `ApiClient` builds a fresh `urllib3.PoolManager`, so a
client per transport would open a pool and a connection for every catalog call and throw it away. The generated
client's `__exit__` is `pass`, so even `with ApiClient(...)` closes nothing.

The remedy is the one pair of clients a lifespan builds for its catalog (`CatalogClients`), which means the caller's
bearer is never a DEFAULT header on a shared object — it rides each request instead. That is the part with
teeth: a token that leaked between transports would authorize one user's read as another's. A read and a
write hold different policies (`CatalogClients.read` sends a read again on a connection the catalog dropped),
so each kind of call shares one pool per catalog.
"""

from collections.abc import Iterator

import pytest


pytest.importorskip("lance_namespace_urllib3_client")

from lineage.services.catalog_client import CatalogClients
from lineage.services.catalog_reader import RestCatalogTransport
from lineage.services.catalog_writer import RestCatalogWriteTransport


@pytest.fixture
def clients() -> Iterator[CatalogClients]:
    """What a lifespan builds for its catalog, closed as the lifespan closes it."""
    built = CatalogClients("http://catalog:2333")
    yield built
    built.close()


def _pool(transport: RestCatalogTransport | RestCatalogWriteTransport) -> object:
    api = transport._api.api_client  # the generated DataApi's ApiClient
    return api.rest_client.pool_manager


def test_a_write_transport_built_again_for_one_catalog_reuses_its_pool(clients: CatalogClients) -> None:
    """A read transport's sharing is pinned with the token below, on the client the two readers share."""
    first, second = (RestCatalogWriteTransport(clients, ["ns", "t"]) for _ in range(2))
    assert _pool(first) is _pool(second)


def test_a_read_and_a_write_do_not_share_a_pool(clients: CatalogClients) -> None:
    assert _pool(RestCatalogTransport(clients)) is not _pool(RestCatalogWriteTransport(clients, ["ns", "t"]))


def test_the_caller_token_never_becomes_a_default_header_on_the_shared_client(clients: CatalogClients) -> None:
    """The whole reason the token is no part of the client: the client is shared."""
    alice = RestCatalogTransport(clients, token="alice")
    bob = RestCatalogTransport(clients, token="bob")
    shared = alice._api.api_client
    assert shared is bob._api.api_client
    assert "Authorization" not in shared.default_headers, "a shared client carrying one caller's bearer authorizes every other caller as them"
    assert alice.request_headers()["Authorization"] == "Bearer alice"
    assert bob.request_headers()["Authorization"] == "Bearer bob"


def test_an_anonymous_transport_sends_no_authorization_header(clients: CatalogClients) -> None:
    assert "Authorization" not in RestCatalogTransport(clients).request_headers()


def test_the_per_call_headers_are_a_fresh_mapping_each_time(clients: CatalogClients) -> None:
    """The generated serializer MUTATES the `_headers` dict it is handed (it writes `Accept` and
    `Content-Type` into it), so handing it the transport's own dict would accumulate one operation's
    content type onto the next."""
    transport = RestCatalogTransport(clients, token="alice")
    first = transport.request_headers()
    first["Content-Type"] = "application/vnd.made-up"
    assert "Content-Type" not in transport.request_headers()


def test_a_close_whose_thread_shutdown_raises_still_clears_both_connection_pools(clients: CatalogClients, monkeypatch: pytest.MonkeyPatch) -> None:
    pools = [client.rest_client.pool_manager for client in (clients.write, clients.read)]
    for pool in pools:
        pool.connection_from_url(clients.url)  # a pool is kept per host once a call names it; no connection is opened

    def refused(*, wait: bool) -> None:
        raise RuntimeError(f"cannot join the writer thread (wait={wait})")

    # Undone before the fixture closes the clients as a lifespan does.
    with monkeypatch.context() as patched, pytest.raises(RuntimeError, match="cannot join the writer thread"):
        patched.setattr(clients.writer, "shutdown", refused)
        clients.close()

    assert [len(pool.pools) for pool in pools] == [0, 0]
