"""Live proof of client-DIRECT writes (#2): vend → write_fragments (client→storage) → POST /commit.

The point being proven: the catalog receives ZERO data bytes. This process writes the Lance data
fragments straight to RustFS with storage creds; only the tiny serialized FragmentMetadata + read_version
cross the wire to the catalog's governed /commit. Run against the kind stack with port-forwards set
(CD_CATALOG / CD_DEX / CD_FGA / CD_S3). Exits non-zero on any failed assertion.
"""

from __future__ import annotations

import base64
import json
import os

import lance
import pyarrow as pa
import requests


CATALOG = os.environ["CD_CATALOG"].rstrip("/")
DEX = os.environ["CD_DEX"].rstrip("/")
FGA = os.environ["CD_FGA"].rstrip("/")
S3 = os.environ["CD_S3"].rstrip("/")
NS, TBL = "cddemo", "t"
TABLE = f"{NS}${TBL}"


def _token() -> str:
    r = requests.post(
        f"{DEX}/token",
        data={
            "grant_type": "password",
            "client_id": "lance-catalog",
            "client_secret": "lance-catalog-secret",
            "username": "alice@example.com",
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


def main() -> None:
    tok = _token()
    sub = _sub(tok)
    st, m = _store_model()
    h = {"Authorization": f"Bearer {tok}"}
    # alice needs writer on the warehouse to create the namespace + table underneath it.
    _grant(st, m, sub, "writer", "warehouse:lance_catalog")

    # Create the namespace + a small seed table (server-side create stays server-side — it is not the
    # byte-proxy we are retiring; the BULK append is what goes client-direct).
    requests.post(f"{CATALOG}/v1/namespace/{NS}/create", headers=h, json={}, timeout=20)
    schema = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.string())])
    seed = pa.table({"id": [1, 2], "v": ["a", "b"]}, schema=schema)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema) as w:
        w.write_table(seed)
    r = requests.post(
        f"{CATALOG}/v1/table/{TABLE}/create",
        headers={**h, "content-type": "application/vnd.apache.arrow.stream"},
        data=sink.getvalue().to_pybytes(),
        params={"mode": "overwrite"},
        timeout=30,
    )
    print(f"create table: {r.status_code}")
    assert r.status_code in (200, 409), r.text

    # Vend: get the write target (location) + optimistic base (read_version) + scoped creds in one call.
    r = requests.post(f"{CATALOG}/v1/table/{TABLE}/credentials", headers=h, params={"tier": "write"}, timeout=30)
    assert r.status_code == 200, r.text
    cred = r.json()
    location, read_version = cred["location"], cred["read_version"]
    print(f"vend: mode={cred['mode']} location={location} read_version={read_version}")
    so = (cred.get("credentials") or {}).get("storage_options")
    if not so:  # mode_b/server_mediated on this deployment → use the store creds directly (still client→store)
        so = {
            "endpoint": S3,
            "access_key_id": "rustfsadmin",
            "secret_access_key": "rustfsadmin",
            "allow_http": "true",
            "virtual_hosted_style_request": "false",
            "region": "us-east-1",
        }

    # CLIENT-DIRECT WRITE: the data fragments go straight to RustFS from HERE — never through the catalog.
    new_rows = pa.table({"id": [3, 4, 5], "v": ["c", "d", "e"]}, schema=schema)
    frags = lance.fragment.write_fragments(new_rows, location, schema=schema, storage_options=so)
    frag_json = [f.to_json() for f in frags]
    body = json.dumps({"fragments": frag_json, "read_version": read_version})
    print(f"/commit body = {len(body)} bytes of METADATA (the {new_rows.nbytes} data bytes were written client→storage direct, NOT through the catalog)")

    # Commit: the catalog folds the fragment metadata into a governed metadata-only Lance commit.
    r = requests.post(
        f"{CATALOG}/v1/table/{TABLE}/commit",
        headers={**h, "content-type": "application/json"},
        data=body,
        timeout=30,
    )
    print(f"commit: {r.status_code} {r.text[:200]}")
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["row_count"] == 5, resp  # 2 seed + 3 appended
    assert len(body) < 4096, "commit body should be tiny metadata, not data"
    print(
        f"✓ PROOF: version {resp['version']}, {resp['row_count']} rows. The catalog's /commit received "
        f"{len(body)} bytes of fragment METADATA; the row data went client→RustFS directly."
    )


if __name__ == "__main__":
    main()
