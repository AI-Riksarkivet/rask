"""Live client-DIRECT write e2e (#2) — against the DEPLOYED kind stack (Dex + OpenFGA + catalog + RustFS).

Proves the whole governed client-direct path end to end: a real Dex OIDC token, a real OpenFGA grant, a
server-side create, then the client writes Lance fragments DIRECTLY to RustFS and the catalog folds only
the tiny ``FragmentMetadata`` into a governed metadata-only commit — **zero data bytes transit the
catalog**. Also pins the authz gates (no token → 401, unauthorized user → 403).

Skipped unless ``LANCE_E2E_CATALOG_URL`` is set and reachable. Run via ``make e2e-client-direct``.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import threading
import time

import lance
import pyarrow as pa
import pytest
import requests
from topology import OUTSIDER, assert_parent_exists, create_top_level


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex").rstrip("/")
FGA = os.environ.get("LANCE_E2E_FGA", "http://localhost:8080").rstrip("/")
S3 = os.environ.get("LANCE_E2E_S3", "http://localhost:9900").rstrip("/")

pytestmark = pytest.mark.e2e

_NS, _TBL = "cddemoe2e", "t"
_TABLE = f"{_NS}${_TBL}"


@pytest.fixture(scope="module")
def catalog() -> str:
    if not CATALOG:
        pytest.skip("set LANCE_E2E_CATALOG_URL (deployed stack) to run the client-direct e2e")
    try:
        requests.get(f"{CATALOG}/livez", timeout=5).raise_for_status()
        requests.get(f"{DEX}/.well-known/openid-configuration", timeout=5).raise_for_status()
        requests.get(f"{FGA}/healthz", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("client-direct stack (catalog/dex/openfga) not reachable")
    return CATALOG


def _token(user: str) -> str:
    r = requests.post(
        f"{DEX}/token",
        data={
            "grant_type": "password",
            "client_id": "lance-catalog",
            "client_secret": "lance-catalog-secret",
            "username": user,
            "password": "password",
            "scope": "openid",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["id_token"]


def _sub(tok: str) -> str:
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))["sub"]


def _store_model() -> tuple[str, str]:
    stores = requests.get(f"{FGA}/stores", timeout=10).json()["stores"]
    st = max(stores, key=lambda s: s["created_at"])["id"]
    m = requests.get(f"{FGA}/stores/{st}/authorization-models", timeout=10).json()
    return st, m["authorization_models"][0]["id"]


def _grant(st: str, m: str, sub: str, rel: str, obj: str) -> None:
    requests.post(
        f"{FGA}/stores/{st}/write",
        json={
            "writes": {"tuple_keys": [{"user": f"user:{sub}", "relation": rel, "object": obj}]},
            "authorization_model_id": m,
        },
        timeout=10,
    )


def _schema() -> pa.Schema:
    return pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.string())])


def test_client_direct_commit_lands_with_zero_byte_ingress(catalog: str) -> None:
    tok = _token("alice@example.com")
    st, m = _store_model()
    _grant(st, m, _sub(tok), "writer", "warehouse:lance_catalog")
    h = {"Authorization": f"Bearer {tok}"}

    # Server-side create seeds the table (create centralizes the 2.2 invariant; bulk append goes direct).
    # The PARENT comes first, through whichever door this estate admits, and its answer is read: firing
    # the root door and discarding the 400 left `_NS` uncreated, and the table create below then 403'd
    # `can_create_table required` on a namespace that was never there.
    assert_parent_exists(create_top_level(catalog, _NS, h), _NS)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, _schema()) as w:
        w.write_table(pa.table({"id": [1, 2], "v": ["a", "b"]}, schema=_schema()))
    r = requests.post(
        f"{catalog}/v1/table/{_TABLE}/create",
        headers={**h, "content-type": "application/vnd.apache.arrow.stream"},
        data=sink.getvalue().to_pybytes(),
        params={"mode": "overwrite"},
        timeout=30,
    )
    assert r.status_code in (200, 409), r.text

    # Vend → the client's write target + optimistic base version.
    r = requests.post(f"{catalog}/v1/table/{_TABLE}/credentials", headers=h, params={"tier": "write"}, timeout=30)
    assert r.status_code == 200, r.text
    cred = r.json()
    location, read_version = cred["location"], cred["read_version"]
    so = (cred.get("credentials") or {}).get("storage_options") or {
        "endpoint": S3,
        "access_key_id": "rustfsadmin",
        "secret_access_key": "rustfsadmin",
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
        "region": "us-east-1",
    }

    # CLIENT-DIRECT: the row data goes straight to RustFS from here — never through the catalog.
    new_rows = pa.table({"id": [3, 4, 5], "v": ["c", "d", "e"]}, schema=_schema())
    frags = lance.fragment.write_fragments(new_rows, location, schema=_schema(), storage_options=so)
    body = json.dumps({"fragments": [f.to_json() for f in frags], "read_version": read_version})

    r = requests.post(
        f"{catalog}/v1/table/{_TABLE}/commit",
        headers={**h, "content-type": "application/json"},
        data=body,
        timeout=30,
    )
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["row_count"] == 5  # 2 seed + 3 appended
    assert resp["version"] >= 2
    # The proof: the commit body is tiny METADATA, not the row data (which went client→RustFS direct).
    assert len(body) < 4096


def test_concurrent_commits_are_acid_no_lost_update(catalog: str) -> None:
    """#36 — #2 ACID at the API layer, LIVE under REAL contention (two properties).

    (A) NO LOST UPDATE under concurrency: N clients read the same base version and commit fragments to the
    SAME table simultaneously (a threading.Barrier fires all N commits at once). Lance appends are
    COMMUTATIVE, so they all land exactly once with no user-visible conflict — a STRONGER guarantee than
    "one wins, losers retry": the final table is seed + N DISTINCT rows, every writer's row present, nothing
    lost or duplicated. (The retry loop is kept for any transient 409; on appends it simply isn't exercised.)

    (B) CLASSIFIED-ERROR CONTRACT, live: a genuinely INCOMPATIBLE commit — a stale append whose base version
    was replaced by an Overwrite — must be a NON-RETRYABLE 400, never a 409/500/silent success. This is the
    P4 audit fix: a 409 "re-read and re-commit" would replay the stale fragments into a semantically-different
    (overwritten) table and CORRUPT it, so incompatible ≠ retryable-conflict. (The unit test
    test_stale_append_after_overwrite_is_a_conflict pins the same taxonomy; this proves it end to end.)
    """
    tok = _token("alice@example.com")
    st, m = _store_model()
    _grant(st, m, _sub(tok), "writer", "warehouse:lance_catalog")
    h = {"Authorization": f"Bearer {tok}"}

    # Fresh table with 2 deterministic seed rows so the final-count assertion is exact + repeatable.
    assert_parent_exists(create_top_level(catalog, _NS, h), _NS)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, _schema()) as w:
        w.write_table(pa.table({"id": [1, 2], "v": ["seed1", "seed2"]}, schema=_schema()))
    r = requests.post(
        f"{catalog}/v1/table/{_TABLE}/create",
        headers={**h, "content-type": "application/vnd.apache.arrow.stream"},
        data=sink.getvalue().to_pybytes(),
        params={"mode": "overwrite"},
        timeout=30,
    )
    assert r.status_code in (200, 409), r.text

    def _vend() -> tuple[str, int, dict]:
        c = requests.post(f"{catalog}/v1/table/{_TABLE}/credentials", headers=h, params={"tier": "write"}, timeout=30).json()
        so = (c.get("credentials") or {}).get("storage_options") or {
            "endpoint": S3, "access_key_id": "rustfsadmin", "secret_access_key": "rustfsadmin",
            "allow_http": "true", "virtual_hosted_style_request": "false", "region": "us-east-1",
        }  # fmt: skip
        return c["location"], c["read_version"], so

    n = 6
    barrier = threading.Barrier(n, timeout=60)

    def writer(i: int) -> dict:
        rows = pa.table({"id": [1000 + i], "v": [f"w{i}"]}, schema=_schema())  # one DISTINCT row per writer
        for attempt in range(1, 16):
            location, read_version, so = _vend()  # fresh read_version each attempt (advances on a conflict)
            frags = lance.fragment.write_fragments(rows, location, schema=_schema(), storage_options=so)
            body = json.dumps({"fragments": [f.to_json() for f in frags], "read_version": read_version})
            if attempt == 1:
                barrier.wait()  # all N fire their FIRST commit simultaneously → guaranteed contention
            resp = requests.post(
                f"{catalog}/v1/table/{_TABLE}/commit",
                headers={**h, "content-type": "application/json"},
                data=body,
                timeout=30,
            )
            if resp.status_code == 200:
                return {"writer": i, "attempts": attempt, "status": "ok"}
            if resp.status_code == 409:  # RETRYABLE conflict — re-vend + re-commit (the contract)
                time.sleep(0.03 * attempt)
                continue
            return {"writer": i, "attempts": attempt, "status": f"ERR {resp.status_code}: {resp.text[:100]}"}
        return {"writer": i, "attempts": 15, "status": "EXHAUSTED"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(writer, range(n)))

    # (A) ACID: every writer landed exactly once (no lost update; no 400/500 — only 200, or a transient 409
    # that the retry resolved). Non-vacuity comes from the Barrier(n): the test cannot pass unless all n
    # threads actually reached /commit together (else the barrier times out and the test fails).
    bad = [r for r in results if r["status"] != "ok"]
    assert not bad, f"contract violation / lost update under contention: {bad}"
    location, _, so = _vend()
    ds = lance.dataset(location, storage_options=so)
    assert ds.count_rows() == 2 + n, f"lost/duplicated rows under contention: {ds.count_rows()} != {2 + n}"
    ids = set(ds.to_table(columns=["id"]).column("id").to_pylist())
    assert all(1000 + i in ids for i in range(n)), f"a writer's row was lost: {sorted(ids)}"

    # (B) Classified-error contract, live: a stale append whose base version was replaced by an Overwrite is
    # INCOMPATIBLE → must be a NON-retryable 400 (never 409/500/silent). This is the P4 audit fix: a 409
    # "re-read and re-commit" would replay these fragments into the overwritten (semantically different) table
    # and corrupt it. Capture a base version, write against it, Overwrite the table out from under it, then
    # commit the stale append.
    stale_loc, stale_rv, stale_so = _vend()
    stale_frags = lance.fragment.write_fragments(
        pa.table({"id": [9999], "v": ["stale"]}, schema=_schema()),
        stale_loc,
        schema=_schema(),
        storage_options=stale_so,
    )
    ow = pa.BufferOutputStream()
    with pa.ipc.new_stream(ow, _schema()) as w:
        w.write_table(pa.table({"id": [1], "v": ["reseeded"]}, schema=_schema()))
    requests.post(
        f"{catalog}/v1/table/{_TABLE}/create",
        headers={**h, "content-type": "application/vnd.apache.arrow.stream"},
        data=ow.getvalue().to_pybytes(),
        params={"mode": "overwrite"},
        timeout=30,
    )
    stale_body = json.dumps({"fragments": [f.to_json() for f in stale_frags], "read_version": stale_rv})
    r = requests.post(
        f"{catalog}/v1/table/{_TABLE}/commit",
        headers={**h, "content-type": "application/json"},
        data=stale_body,
        timeout=30,
    )
    assert r.status_code == 400, f"stale-append-after-overwrite must be a NON-retryable 400 (P4 fix), got {r.status_code}: {r.text}"


def test_commit_is_governed(catalog: str) -> None:
    # No token → 401 (OIDC); a valid token without a grant → 403 (FGA) — /commit is NOT an anonymous write.
    body = json.dumps({"fragments": [{"id": 0}], "read_version": 1})
    h = {"content-type": "application/json"}
    assert requests.post(f"{catalog}/v1/table/{_TABLE}/commit", headers=h, data=body, timeout=10).status_code == 401
    # A REAL outsider, not bob — he is a project admin on this estate. This leg used to pass only
    # because `_NS` had never been created, so the commit 403'd on a missing parent rather than on a
    # missing grant; creating the parent (as this suite now does) exposed the wrong oracle.
    outsider = {**h, "Authorization": f"Bearer {_token(OUTSIDER)}"}
    assert requests.post(f"{catalog}/v1/table/{_TABLE}/commit", headers=outsider, data=body, timeout=10).status_code == 403
