"""A run's declared source endpoint must reach the WORKER, and must never borrow estate credentials.

`adapters.register_builtin_sources` advertises an `endpoint` option on `s3-prefix` — the compute
zone renders it as a form field — but the fetch half of the plane never saw it: `fetch._fetch_s3`
built its client from `RASK_S3_ENDPOINT_URL` and `UnitTask` had nowhere to carry the override, so
the option crossed the accept door and died at the queue.

That is a SILENT-WRONG-DATA hazard, not merely a broken feature. Best case every unit parks on the
DLQ because the estate's own store has no such key. Worst case the estate holds a bucket of the
same name, the fetch succeeds, and rows land under an external `source_uri` carrying the estate's
own bytes — a wrong dataset with no error anywhere.

The other half of the same defect is credentials. Honouring an arbitrary endpoint with the
deployment's own keys is the failure `viewer...objects._creds` already names: env holds the
WAREHOUSE's credentials, so reaching an external endpoint with them is either a loud
InvalidAccessKeyId or, worse, someone else's bucket. So a declared endpoint must be REGISTERED
(`RASK_STORES`) and its credentials come from the Dapr/OpenBao secret store — never from the
request, never from env.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator

EXTERNAL = "https://objects.partner.example.org"
ESTATE = "http://rask-rustfs-io:9000"

#: One registered store standing for "a raw drop bucket that is NOT on our endpoint", with its
#: credentials behind a named secret — the shape `service_kit.schemas.storage.Store` already has.
EXTERNAL_STORE = {
    "name": "partner-drop",
    "bucket": "pages",
    "role": "raw",
    "endpoint": EXTERNAL,
    "secret": "partner-drop",
    "description": "External raw material.",
}


class _RecordedClient:
    """A stand-in boto3 client that answers one object and remembers nothing else."""

    def __init__(self) -> None:
        self.gets: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        self.gets.append((Bucket, Key))
        return {"Body": _Body(b"EXTERNAL-BYTES")}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


@pytest.fixture
def estate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment's own endpoint and credentials — what the defect silently fell back to."""
    monkeypatch.setenv("RASK_S3_ENDPOINT_URL", ESTATE)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "estate-ak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "estate-sk")
    monkeypatch.delenv("RASK_STORES", raising=False)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch, estate_env: None) -> None:
    monkeypatch.setenv("RASK_STORES", json.dumps([EXTERNAL_STORE]))


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Every `storage.s3_client` construction, with the endpoint and credentials it was given."""
    import storage
    from ingest import objectstore

    recorded: list[dict[str, Any]] = []

    def _spy(endpoint: str | None = None, **kwargs: Any) -> _RecordedClient:
        recorded.append({"endpoint": endpoint, **kwargs})
        return _RecordedClient()

    monkeypatch.setattr(storage, "s3_client", _spy)
    # The connection cache is process-wide by design (a boto3 client owns a pool); a test that
    # asserts on construction must not read another test's memoized client.
    objectstore.reset_connection_cache()
    yield recorded
    objectstore.reset_connection_cache()


def _secret_store(monkeypatch: pytest.MonkeyPatch, bundle: dict[str, str]) -> None:
    from ingest import objectstore

    monkeypatch.setattr(objectstore, "fetch_dapr_secret", lambda *_a, **_kw: bundle)
    objectstore.reset_connection_cache()


# ── the task is what crosses the queue ────────────────────────────────────────────────────


def test_the_unit_task_carries_the_run_s_source_endpoint() -> None:
    """Without this field the override cannot reach a worker at all — the whole defect in one line."""
    from ingest.queue import UnitTask

    task = UnitTask(run_id="r", chunk_id="c", key="s3://pages/a.tif", dataset_uri="s3://w/d.lance", source_endpoint=EXTERNAL)

    assert task.source_endpoint == EXTERNAL, "the run's declared endpoint must survive the queue"
    assert UnitTask(run_id="r", chunk_id="c", key="k", dataset_uri="s3://w/d.lance").source_endpoint is None, (
        "a run that declares no endpoint must stay on the estate default"
    )


def test_the_adapter_declares_the_endpoint_and_the_registry_serves_it() -> None:
    """`publish_chunk_units` must not read `options['endpoint']` itself — only the ADAPTER knows what
    a kind's options mean, the same rule `partition_of` and `external_base_of` already follow."""
    from ingest.adapters import register_builtin_sources
    from ingest.sources import SourceSpec, source_endpoint_for

    register_builtin_sources()

    declared = SourceSpec(kind="s3-prefix", project="p", dataset="d", options={"bucket": "pages", "endpoint": EXTERNAL})
    assert source_endpoint_for(declared) == EXTERNAL
    assert source_endpoint_for(SourceSpec(kind="s3-prefix", project="p", dataset="d", options={"bucket": "pages"})) is None
    assert source_endpoint_for(SourceSpec(kind="local-dir", project="p", dataset="d", options={"root": "/tmp"})) is None


def test_the_worker_hands_the_task_s_endpoint_to_the_fetcher() -> None:
    """The task carries it; the worker is what puts it in the fetcher's hand."""
    from ingest.queue import UnitTask, WorkQueue
    from ingest.worker import Worker

    seen: dict[str, Any] = {}

    class _Fetcher:
        async def fetch(self, key: str, *, source_endpoint: str | None = None) -> bytes:
            seen["key"], seen["endpoint"] = key, source_endpoint
            return b"BYTES"

    # `_one` touches neither the queue nor a validator, so a cast keeps the signature honest with
    # no broker in the test — the estate's own precedent (`test_publish_excludes_duplicates`).
    worker = Worker(cast("WorkQueue", object()), _Fetcher(), None, name="w")
    task = UnitTask(run_id="r", chunk_id="c", key="s3://pages/a.tif", dataset_uri="s3://w/d.lance", source_endpoint=EXTERNAL)

    asyncio.run(worker._one(task))

    assert seen["endpoint"] == EXTERNAL, "the worker dropped the unit's endpoint on the floor"


# ── the fetch itself ──────────────────────────────────────────────────────────────────────


def test_the_fetch_uses_the_run_s_endpoint_and_the_store_s_credentials(registry: None, calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEFECT. A declared external endpoint must not be answered by the estate's own store."""
    _secret_store(monkeypatch, {"access_key": "partner-ak", "secret_key": "partner-sk"})

    from ingest.fetch import UriFetcher

    payload = asyncio.run(UriFetcher().fetch("s3://pages/a.tif", source_endpoint=EXTERNAL))

    assert payload == b"EXTERNAL-BYTES"
    assert len(calls) == 1, f"expected exactly one client build, got {calls}"
    assert calls[0]["endpoint"] == EXTERNAL, f"the fetch went to {calls[0]['endpoint']!r}, not the run's endpoint"
    assert calls[0]["access_key"] == "partner-ak", "credentials must come from the store's Dapr secret, not the estate env"
    assert calls[0]["secret_key"] == "partner-sk"


def test_no_declared_endpoint_still_uses_the_estate_default(estate_env: None, calls: list[dict[str, Any]]) -> None:
    """The unchanged path: a run with no override reads the deployment's own store, as before."""
    from ingest.fetch import UriFetcher

    asyncio.run(UriFetcher().fetch("s3://lance-catalog/a.tif"))

    assert calls[0]["endpoint"] is None, "no override must leave endpoint resolution to storage.s3_client's env chain"
    assert calls[0].get("access_key") is None, "the estate path must not invent credentials"


def test_an_unregistered_endpoint_fails_loudly_rather_than_reading_a_same_named_local_bucket(estate_env: None, calls: list[dict[str, Any]]) -> None:
    """The worst case, refused: `s3://pages/a.tif` on an unknown endpoint must NOT fall through to
    the estate's own `pages` bucket. A `ValueError` is `worker._is_permanent`, so the unit parks at
    once instead of spending its redelivery budget on a deployment gap."""
    from ingest.fetch import UriFetcher

    with pytest.raises(ValueError, match="not registered"):
        asyncio.run(UriFetcher().fetch("s3://pages/a.tif", source_endpoint="https://somewhere-else.example.org"))

    assert calls == [], "an unresolvable endpoint must build no client at all — not one aimed at the estate"


def test_a_secret_store_outage_refuses_rather_than_falling_back_to_env(registry: None, calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    """`fetch_dapr_secret` returns `{}` for a down sidecar, a missing secret and an empty one alike.
    All three must refuse: env holds the WAREHOUSE's keys, and retrying an external endpoint with
    them is how a fetch reaches the wrong backend."""
    _secret_store(monkeypatch, {})

    from ingest.fetch import UriFetcher

    with pytest.raises(RuntimeError, match="credentials"):
        asyncio.run(UriFetcher().fetch("s3://pages/a.tif", source_endpoint=EXTERNAL))

    assert calls == [], "no client may be built without the store's own credentials"


# ── the same rule at the enumeration door ─────────────────────────────────────────────────


def test_enumeration_refuses_an_unregistered_endpoint_at_the_accept_door(estate_env: None) -> None:
    """Two doors, one rule — the same shape as `local-dir`'s confinement. Refusing at accept turns a
    deployment gap into a 400 on the request instead of a run that drains entirely onto the DLQ."""
    from ingest.adapters import register_builtin_sources
    from ingest.sources import SourceSpec, build_source

    register_builtin_sources()

    with pytest.raises(ValueError, match="not registered"):
        build_source(SourceSpec(kind="s3-prefix", project="p", dataset="d", options={"bucket": "pages", "endpoint": EXTERNAL}))
