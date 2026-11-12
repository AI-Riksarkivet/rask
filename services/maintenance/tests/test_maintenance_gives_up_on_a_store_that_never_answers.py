"""Maintenance gives up on a store that never answers within the bounds its settings set.

Every open, listing, manifest read and delete maintenance makes carries `MaintenanceSettings.store_timeouts`, so a store
that has stopped answering fails the pass, which is tried again later, rather than holding a worker replica for as long
as its pass bound allows. Measured on pylance 12.0.0 with object_store's defaults (`lakehouse.objectfs.StoreTimeouts`):
one open against a store that accepts and never answers outlasted a 300 s harness. Lance honours the request bound only
under the key `timeout` and ignores `request_timeout` without a word, so the claim is how long the open takes, not
which keys the options hold.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import lance
import pytest
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings


#: Lance re-issues a failed listing six times per open (`StoreTimeouts`), so under the bounds below an open gives up in
#: seconds; this is the most it may take.
_DEADLINE_S = 20.0


@pytest.fixture
def silent_store() -> Iterator[str]:
    """A store's address that accepts every connection and answers nothing on it."""
    listener = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []

    def hold() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            held.append(connection)

    threading.Thread(target=hold, daemon=True).start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()
    for connection in held:
        connection.close()


def test_an_open_under_maintenances_store_options_gives_up_on_a_store_that_never_answers(silent_store: str) -> None:
    settings = MaintenanceSettings(
        s3_endpoint=silent_store,
        s3_access_key_id="unit",
        s3_secret_access_key=SecretStr("unit"),
        store_connect_timeout_s=0.5,
        store_request_timeout_s=1.0,
    )
    raised: list[BaseException] = []

    def open_a_table() -> None:
        try:
            lance.dataset("s3://bucket/table", storage_options=settings.storage_options().in_clear())
        except (OSError, ValueError) as exc:
            raised.append(exc)

    opener = threading.Thread(target=open_a_table, daemon=True)
    opener.start()
    opener.join(_DEADLINE_S)

    assert (opener.is_alive(), len(raised)) == (False, 1), f"the open had not given up after {_DEADLINE_S} s"
