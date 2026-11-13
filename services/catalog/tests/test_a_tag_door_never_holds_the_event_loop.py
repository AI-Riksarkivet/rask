"""A tag door's store call runs off the event loop, so a slow store never stalls another request (XC-201, audit C4).

The three tag doors are `async def` and their store calls are blocking pylance calls. Run on the event loop, one slow
call stalls every request the process holds, readiness and liveness included, so the kubelet takes a serving replica out
of rotation for a slow table. The store call is held here while `/livez` is asked: a door that runs it on the loop holds
the probe until the call returns, past the kubelet's one second.

The store call is the one thing replaced: the door, its claim, its dependencies and the app are the catalog's own, on a
`dir` root of the test's own.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from catalog.core.config import get_settings
from catalog.services import dataplane
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The kubelet's default `timeoutSeconds`: the chart's probes set none (`lance.appProbes`).
PROBE_SECONDS = 1.0
#: How long a held store call holds before the test lets it go, well past the probe's second.
HELD_SECONDS = 10.0
TABLE = "/v1/table/lance_catalog$db$t"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The catalog on a `dir` root of its own, holding ``lance_catalog$db$t`` at version 1 with the tag ``kept`` on it."""
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as made:
        assert made.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        rows = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))
        assert made.post(f"{TABLE}/create", content=rows, headers={"content-type": ARROW_STREAM_MEDIA_TYPE}).status_code == 200
        assert made.post(f"{TABLE}/tags/create", json={"tag": "kept", "version": 1}).status_code == 200
        yield made
    get_settings.cache_clear()


def _held(real: Callable[..., Any], entered: threading.Event, release: threading.Event) -> Callable[..., Any]:
    """``real``, made to wait for ``release`` once it has said it was entered, as a store that does not answer holds it."""

    def holding(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the store call's own arguments and answer
        entered.set()
        release.wait(HELD_SECONDS)
        return real(*args, **kwargs)

    return holding


@pytest.mark.parametrize(
    ("store_call", "door", "body"),
    [
        pytest.param("create_tag", "tags/create", {"tag": "made", "version": 1}, id="create"),
        pytest.param("update_tag", "tags/update", {"tag": "kept", "version": 1}, id="update"),
        pytest.param("delete_tag", "tags/delete", {"tag": "kept"}, id="delete"),
    ],
)
def test_liveness_answers_while_a_tag_doors_store_call_is_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, store_call: str, door: str, body: dict[str, Any]
) -> None:
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(dataplane, store_call, _held(getattr(dataplane, store_call), entered, release))
    asking = ThreadPoolExecutor(max_workers=2)
    try:
        tagging = asking.submit(client.post, f"{TABLE}/{door}", json=body)
        assert entered.wait(HELD_SECONDS), f"the {door} door never reached its store call"
        probe = asking.submit(client.get, "/livez")
        try:
            answered = probe.result(timeout=PROBE_SECONDS).status_code
        except FutureTimeout:
            answered = None
    finally:
        release.set()
        asking.shutdown(wait=True)

    assert answered == 200, f"/livez did not answer within {PROBE_SECONDS} s while the {door} door's store call was held"
    assert tagging.result().status_code == 200, tagging.result().text
