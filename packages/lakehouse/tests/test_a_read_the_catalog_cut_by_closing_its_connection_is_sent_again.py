"""A read the catalog cut by closing its connection is sent again on a new one; a write is never sent twice.

Every Lance-namespace read is a POST, and urllib3 sends a POST again only when the request never left, so a read that
meets a pooled connection the catalog has just closed for idleness fails outright unless its client allows the read to
be sent again. Measured live 2026-11-12: all 8 of lineage's readiness flips in 6.7 h were that race (`RemoteDisconnected`
6, `ConnectionResetError` 2), its 5 s feed read meeting the catalog's 5 s keep-alive close. Driven against a catalog that
answers the first request on a connection, then reads the next one and closes the connection unanswered, which is that
race made certain.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from typing import BinaryIO

import pytest
import urllib3

from lakehouse.lancekit.catalog_client import reset_catalog_clients
from lakehouse.lancekit.reader import RestCatalogTransport
from lakehouse.lancekit.writer import RestCatalogWriteTransport


_ANSWER = b'{"version": 7}'


class _ClosingCatalog:
    """Answers the first request on each connection, then reads the next one and closes the connection unanswered."""

    def __init__(self) -> None:
        self._server = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._server.getsockname()[1]}"
        self.cut = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._server.close()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        with connection, connection.makefile("rb") as requests:
            if not _read_request(requests):
                return
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(_ANSWER), _ANSWER))
            if _read_request(requests):
                self.cut += 1


def _read_request(requests: BinaryIO) -> bool:
    """Read one request off the connection; False when the client closed it first."""
    if not requests.readline():
        return False
    length = 0
    while (header := requests.readline()) not in (b"\r\n", b""):
        name, _, value = header.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    requests.read(length)
    return True


@pytest.fixture
def catalog() -> Iterator[_ClosingCatalog]:
    reset_catalog_clients()
    closing = _ClosingCatalog()
    yield closing
    closing.close()
    reset_catalog_clients()


def _read(url: str) -> Callable[[], object]:
    """The feed's version, as lineage's readiness reads it through the catalog."""
    return lambda: RestCatalogTransport(url, timeout=5.0).table_version(["ns", "t"])


def _write(url: str) -> Callable[[], object]:
    """A delete through the catalog, a write it may have applied before the connection went."""
    return lambda: RestCatalogWriteTransport(url, ["ns", "t"], timeout=5.0).delete("id > 1")


@pytest.mark.parametrize(
    ("call_for", "sent_again"),
    [pytest.param(_read, True, id="read"), pytest.param(_write, False, id="write")],
)
def test_a_request_the_catalog_cut_by_closing_its_connection_is_sent_again_only_for_a_read(
    catalog: _ClosingCatalog, call_for: Callable[[str], Callable[[], object]], sent_again: bool
) -> None:
    call = call_for(catalog.url)
    call()

    try:
        call()
    except urllib3.exceptions.ProtocolError:
        answered = False
    else:
        answered = True

    assert (answered, catalog.cut) == (sent_again, 1)
