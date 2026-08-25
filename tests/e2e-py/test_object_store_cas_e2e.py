"""Object-store compare-and-swap (conditional-write) validation — the load-bearing assumption behind
Lance commit safety.

Every Lance commit publishes ``_versions/N.manifest`` with put-if-not-exists (``If-None-Match: *``); the
format REQUIRES the store to guarantee exactly ONE writer wins that race (``lance_docs/file_format.md`` —
Conflict resolution). A store that silently IGNORES the header accepts both PUTs and silently loses a
commit — a failure mode seen in the wild (GCS S3-interop), invisible to a single-writer smoke test. This
suite is the contended-writer harness that actually catches it, run against the DEPLOYED store (RustFS).

Three tiers, asserting DATA invariants (winner counts / row counts), not exception names — no S3 error
name is contractual:
  1. conditional-PUT pre-flight — a second ``If-None-Match: *`` PUT of a live key must be REJECTED (412);
  2. 8-thread barrier-gated contended-key stress — exactly ONE winner per round (the silent-ignore detector);
  3. 8-process Lance ``append`` stress — Append⊥Append never logically conflicts, so ALL writers must land
     (800/800 rows) — proving Lance's real manifest-CAS commit path survives contention on this store.

Run: ``make e2e-cas`` (port-forwards RustFS + sets LANCE_E2E_S3_*). Writes under ``__cas_stress/`` — the
compaction sweep skips ``__`` prefixes, so this never collides with the lakehouse or gets GC'd.
"""

from __future__ import annotations

import multiprocessing
import os
import pathlib
import threading
import uuid
from concurrent.futures import ProcessPoolExecutor

import boto3
import lance
import pyarrow as pa
import pytest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cas_append_worker import append_rows


ENDPOINT = os.environ.get("LANCE_E2E_S3_ENDPOINT", "")
ACCESS_KEY = os.environ.get("LANCE_E2E_S3_ACCESS_KEY", "rustfsadmin")
SECRET_KEY = os.environ.get("LANCE_E2E_S3_SECRET_KEY", "rustfsadmin")
BUCKET = os.environ.get("LANCE_E2E_S3_BUCKET", "lance-catalog")
PREFIX = "__cas_stress"

pytestmark = [pytest.mark.e2e, pytest.mark.cas]


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )


def _storage_options() -> dict[str, str]:
    # Same keys the app passes to lance (compaction/medallion config.storage_options), plus a very high AIMD
    # ceiling so Lance's client-side rate limiter can't throttle (and thereby serialize) the concurrent
    # writers — that would mask a store silently dropping the conditional-put header (guide.md § Throttling).
    return {
        "endpoint": ENDPOINT,
        "access_key_id": ACCESS_KEY,
        "secret_access_key": SECRET_KEY,
        "region": "us-east-1",
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
        "lance_aimd_max_rate": "100000",
    }


@pytest.fixture(scope="module")
def s3():
    if not ENDPOINT:
        pytest.skip("set LANCE_E2E_S3_ENDPOINT (see module docstring / make e2e-cas)")
    client = _s3()
    try:
        client.head_bucket(Bucket=BUCKET)
    except ClientError as exc:
        pytest.skip(f"RustFS bucket {BUCKET!r} unreachable at {ENDPOINT}: {exc}")
    return client


def _status(exc: ClientError) -> int:
    return int(exc.response["ResponseMetadata"]["HTTPStatusCode"])


# ── Tier 1 ─ conditional-PUT pre-flight ───────────────────────────────────────────────────────── #


def test_conditional_put_rejects_overwrite_of_live_key(s3) -> None:
    """A second ``If-None-Match: *`` PUT of an existing key must be REJECTED (412) and must NOT overwrite —
    the minimal put-if-not-exists contract every Lance manifest commit relies on."""
    key = f"{PREFIX}/preflight"
    s3.delete_object(Bucket=BUCKET, Key=key)
    first = s3.put_object(Bucket=BUCKET, Key=key, Body=b"first", IfNoneMatch="*")
    assert first["ResponseMetadata"]["HTTPStatusCode"] in (200, 201)

    with pytest.raises(ClientError) as excinfo:
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"second", IfNoneMatch="*")
    assert _status(excinfo.value) == 412  # PreconditionFailed — the header was HONORED

    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"first"  # DATA invariant: no overwrite
    s3.delete_object(Bucket=BUCKET, Key=key)


# ── Tier 2 ─ contended-key stress: exactly one winner (silent-ignore detector) ────────────────── #


def test_contended_conditional_put_has_exactly_one_winner_per_round(s3) -> None:
    """N threads race to create the SAME key with ``If-None-Match: *`` at a barrier. A store that honors CAS
    lets exactly ONE win each round; a store that silently ignores the header lets several 'win' and the last
    write silently clobbers — the corruption Lance would inherit. Asserted over several rounds."""
    workers, rounds = 8, 5
    for rnd in range(rounds):
        key = f"{PREFIX}/contended-{rnd}"
        s3.delete_object(Bucket=BUCKET, Key=key)
        barrier = threading.Barrier(workers)
        outcomes: list[tuple[str, object]] = [("", None)] * workers

        def worker(
            i: int,
            _key: str = key,
            _bar: threading.Barrier = barrier,
            _out: list[tuple[str, object]] = outcomes,
        ) -> None:
            client = _s3()  # a client per thread (fresh connection pool) so the race is on the STORE, not us
            body = f"writer-{i}".encode()
            _bar.wait()  # release all workers simultaneously
            try:
                client.put_object(Bucket=BUCKET, Key=_key, Body=body, IfNoneMatch="*")
                _out[i] = ("win", body)
            except ClientError as exc:
                _out[i] = ("lose", _status(exc))
            except BotoCoreError as exc:  # transport blip (port-forward) — record so a dead thread can't hide
                _out[i] = ("error", repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every slot must be resolved — an unresolved ('', None) means a worker thread crashed uncaught, which
        # would silently drop it from wins/losers and weaken (or falsify) the race. Fail loudly instead.
        assert all(o[0] in {"win", "lose", "error"} for o in outcomes), f"round {rnd}: a worker thread crashed (unresolved slot): {outcomes}"
        errors = [o for o in outcomes if o[0] == "error"]
        assert not errors, (  # a transport error weakened the race → INCONCLUSIVE (infra flake), not a CAS verdict
            f"round {rnd}: transport error(s) weakened the race — infra flake, not a CAS result: {errors}"
        )
        wins = [o for o in outcomes if o[0] == "win"]
        losers = [o for o in outcomes if o[0] == "lose"]
        assert len(wins) == 1, f"round {rnd}: expected exactly 1 winner, got {len(wins)} — CAS not enforced"
        assert all(code == 412 for _, code in losers), f"round {rnd}: a loser was not a clean 412: {losers}"
        stored = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        assert stored == wins[0][1]  # DATA invariant: the stored bytes are the winner's, un-clobbered
        s3.delete_object(Bucket=BUCKET, Key=key)


# ── Tier 3 ─ concurrent Lance appends all land (real manifest-CAS commit path) ─────────────────── #


def test_concurrent_lance_appends_all_land(s3) -> None:
    """8 processes each append 100 rows to one dataset. Append⊥Append never LOGICALLY conflicts
    (file_format.md — conflict matrix), but every commit still contends on the manifest CAS; each loser
    rebases and retries. On a CAS-honoring store all 8 land → 800 rows. On a store that drops the header,
    two commits take the same version → an append is silently lost → < 800 rows (or a corrupt manifest)."""
    run = f"{PREFIX}/appendtest-{uuid.uuid4().hex}"  # per-run isolation so overlapping runs never collide
    uri = f"s3://{BUCKET}/{run}"
    so = _storage_options()
    workers, per = 8, 100
    try:
        # Seed an EMPTY dataset (v1) so all 8 workers are pure appends — concurrent CREATEs would be Overwrite
        # races (which DO conflict). data_storage_version=2.2 + stable row ids per the §0 cascade-write rule;
        # the appends inherit both (create-time-only).
        lance.write_dataset(
            pa.table({"id": pa.array([], type=pa.int64())}),
            uri,
            mode="create",
            storage_options=so,
            data_storage_version="2.2",
            enable_stable_row_ids=True,
        )
        # spawn (not fork): lance holds its own threads and is NOT fork-safe — a forked child can deadlock.
        # A SPAWNED child re-imports this module to unpickle the work item, and it cannot on its own:
        # the repo runs `--import-mode=importlib`, so this suite is imported as a top-level module from
        # a directory on NO default sys.path (`tests/e2e-py` is not even a legal package name — the
        # hyphen). The child died `ModuleNotFoundError: No module named 'tests'`, which reads as a
        # missing dependency rather than a missing path.
        #
        # PYTHONPATH, not an `initializer=`: the initializer is a function IN THIS MODULE, so the child
        # must already be able to import it to run it — the unpickle fails first, inside spawn's own
        # bootstrap. PYTHONPATH is applied by the interpreter at startup, before any of that, and spawn
        # inherits the parent's environment.
        #
        # Fork would sidestep all of it and must not be used: lance holds its own threads and a forked
        # child can deadlock, which is why spawn was chosen here in the first place.
        here = pathlib.Path(__file__).resolve()
        inherited = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = os.pathsep.join([str(here.parent), str(here.parents[2]), *([inherited] if inherited else [])])
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            # A SPAWNED child re-imports this module to unpickle `_append_rows`, and it cannot: the repo
            # runs `--import-mode=importlib`, so the suite is imported as a top-level module from a
            # directory that is on NO default sys.path (and `tests/e2e-py` is not even a legal package
            # name — the hyphen). The child died with `ModuleNotFoundError: No module named 'tests'`
            # while unpickling the call item, which reads as a missing dependency rather than a path.
            # The initializer runs before the queue is drained, so restoring the two paths there is
            # enough. Fork would sidestep it and must not be used: lance holds its own threads and a
            # forked child can deadlock — the reason spawn was chosen in the first place.
        ) as pool:
            list(pool.map(append_rows, [(uri, so, i * per, per) for i in range(workers)]))

        ds = lance.dataset(uri, storage_options=so)
        assert ds.count_rows() == workers * per, "an append was silently lost — the store did not enforce CAS"
        assert set(ds.to_table().column("id").to_pylist()) == set(range(workers * per)), "row set is wrong — a commit clobbered another's data"
        # Exactly one version per successful commit: v1 seed + 8 appends = 9. More would mean a phantom
        # double-commit; fewer means a lost append — either way a CAS violation Lance's retry couldn't hide.
        assert len(ds.versions()) == workers + 1, f"expected {workers + 1} versions, got {len(ds.versions())}"
    finally:
        _wipe_prefix(s3, run)


def _wipe_prefix(s3, prefix: str) -> None:
    """Delete every object under ``prefix`` (best-effort test cleanup)."""
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        listing = s3.list_objects_v2(**kw)
        objects = [{"Key": o["Key"]} for o in listing.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objects})
        token = listing.get("NextContinuationToken")
        if not token:
            break
