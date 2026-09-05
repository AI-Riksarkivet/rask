"""The blocking S3-secret fetch must never run on the event loop.

docs/DECISIONS.md "The Python estate audit" (E2, P0) — "`Settings.storage_options` is a property that performs a blocking Dapr
secret fetch and raises", read inline on the event loop by `list_datasets` (it built its registry at
the top of the `async def`, before the threadpooled `_collect`). The property form is fixed at the
source (`test_media_s3_secret` pins it is now a method); this pins the one on-loop request-path reader
the audit named — the registry construction, which is where `storage_options()` is actually called.

PROVED BY THREAD IDENTITY, not by timing. An `@pytest.mark.asyncio` test body runs ON the event
loop's thread; `run_in_threadpool` dispatches to a worker thread. So if the secret fetch — reached
only through the registry build — records a thread different from the coroutine's, the build happened
off the loop. If it records the SAME thread, it blocked the loop, which is the defect.
"""

from __future__ import annotations

import threading
from typing import Any, cast

import pytest

from service_kit.media import config as media_config
from service_kit.media.state import AppState
from viewer.core.config import ViewerSettings


@pytest.fixture(autouse=True)
def _fresh_cache():
    getattr(media_config._store_secret, "cache_clear", lambda: None)()
    yield
    getattr(media_config._store_secret, "cache_clear", lambda: None)()


@pytest.mark.asyncio
async def test_list_datasets_resolves_the_secret_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from viewer.api.v1.endpoints import datasets as datasets_ep

    loop_thread = threading.get_ident()
    fetch_threads: list[int] = []

    def _record_fetch(*_a: object) -> str:
        fetch_threads.append(threading.get_ident())
        return "the-secret"

    # The Dapr-secret path: endpoint + access-key-id set, NO static secret, so `storage_options()`
    # must reach `_store_secret` — which we make record the thread it runs on.
    monkeypatch.setattr(media_config, "_store_secret", _record_fetch)
    # The registry lists a LOCAL/stubbed root, so the test never touches S3 — the property fetch is
    # the only thing under test, not the dataset open.
    from service_kit.lancekit.registry import DatasetRegistry

    monkeypatch.setattr(DatasetRegistry, "list_ids", lambda self: [])

    settings = ViewerSettings.model_validate(
        {"MEDIA_S3_ENDPOINT": "http://rustfs:9000", "MEDIA_S3_ACCESS_KEY_ID": "id", "MEDIA_S3_DB_ROOT": "s3://bucket/root"}
    )
    state = AppState(settings=settings)

    result = await datasets_ep.list_datasets(state=cast("Any", state), client=None, subject="anon", settings=cast("Any", settings))

    assert fetch_threads, "the secret was never fetched — the registry build did not read storage_options()"
    assert all(t != loop_thread for t in fetch_threads), f"the blocking Dapr secret fetch ran on the event-loop thread ({loop_thread}): {fetch_threads}"
    assert result.datasets == []
