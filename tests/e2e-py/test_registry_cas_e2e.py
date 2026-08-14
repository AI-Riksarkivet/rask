"""F1 live half — contended registry-create against the DEPLOYED store (RustFS).

``tests/unit/test_registry_cas.py`` proves the seam's logic (local-FS exclusivity + the boto3
``IfNoneMatch`` call shape); THIS suite proves the deployed store actually arbitrates it under
contention — the same silent-ignore hazard ``test_object_store_cas_e2e.py`` exists for (a store
that ignores the header accepts both PUTs and the tenant-isolation guards are back to
last-writer-wins, invisibly). Mirrors that suite's tier-2 shape through the REGISTRY seam:
barrier-gated threads race ``records.create_json`` on one key; exactly one must win per round.

Run: port-forward RustFS + set ``LANCE_E2E_S3_*`` (same env as its sibling). Writes under
``__cas_stress/`` — the compaction sweep skips ``__`` prefixes, so this never collides with the
lakehouse or gets GC'd.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from service_kit.lakehouse import records


ENDPOINT = os.environ.get("LANCE_E2E_S3_ENDPOINT", "")
ACCESS_KEY = os.environ.get("LANCE_E2E_S3_ACCESS_KEY", "rustfsadmin")
SECRET_KEY = os.environ.get("LANCE_E2E_S3_SECRET_KEY", "rustfsadmin")
BUCKET = os.environ.get("LANCE_E2E_S3_BUCKET", "lance-catalog")
PREFIX = "__cas_stress/registry"

pytestmark = [pytest.mark.e2e, pytest.mark.cas]

_SO = {
    "endpoint": ENDPOINT,
    "access_key_id": ACCESS_KEY,
    "secret_access_key": SECRET_KEY,
    "region": "us-east-1",
}


@pytest.fixture(scope="module")
def live_root() -> str:
    if not ENDPOINT:
        pytest.skip("set LANCE_E2E_S3_ENDPOINT (see module docstring)")
    try:
        records._s3_client(_SO).head_bucket(Bucket=BUCKET)
    except (ClientError, BotoCoreError) as exc:
        pytest.skip(f"RustFS bucket {BUCKET!r} unreachable at {ENDPOINT}: {exc}")
    return f"s3://{BUCKET}"


def test_registry_create_is_write_once_live(live_root: str) -> None:
    key = f"{PREFIX}/{uuid.uuid4().hex}.json"
    records.create_json(live_root, _SO, key, {"id": "first"})
    with pytest.raises(records.RecordExistsError):
        records.create_json(live_root, _SO, key, {"id": "second"})


def test_registry_create_contended_exactly_one_winner(live_root: str) -> None:
    # 8 barrier-gated threads per round, 5 rounds: a store that silently drops the conditional
    # header passes the sequential test above and fails HERE — the same detector shape as the
    # sibling suite's tier 2. Data invariant asserted (winner count), not exception names.
    for _ in range(5):
        key = f"{PREFIX}/{uuid.uuid4().hex}.json"
        barrier = threading.Barrier(8)
        outcomes: list[str] = []
        lock = threading.Lock()

        def attempt(
            worker: int,
            key: str = key,
            barrier: threading.Barrier = barrier,
            outcomes: list[str] = outcomes,
            lock: threading.Lock = lock,
        ) -> None:
            barrier.wait()
            try:
                records.create_json(live_root, _SO, key, {"winner": worker})
                result = "won"
            except records.RecordExistsError:
                result = "lost"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count("won") == 1, f"expected exactly one winner, got {outcomes}"
