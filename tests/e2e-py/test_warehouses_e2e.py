"""#3-A — live per-warehouse physical isolation against the DEPLOYED catalog.

Proves the completion condition end to end: an admin provisions warehouse A→bucket-a and B→bucket-b
(distinct buckets, created at RUNTIME by the API — not the Helm mc-mb loop); a namespace bound to A + a
table created under it physically land in bucket-a and are ABSENT from bucket-b; and ``POST /v1/warehouses``
as a non-admin is denied 403.

Skipped unless ``LANCE_E2E_CATALOG_URL`` + ``LANCE_E2E_TOKEN`` are set (the stack deployed with
``LANCE_WAREHOUSES_ENABLED=true`` and the token's user granted ``can_create_warehouse`` on the project).
Run via ``make e2e-warehouses``. The 403 leg additionally needs ``LANCE_E2E_NONADMIN_TOKEN``.
"""

from __future__ import annotations

import io
import os

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.ipc as ipc
import pytest
import requests


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
TOKEN = os.environ.get("LANCE_E2E_TOKEN", "")
NONADMIN = os.environ.get("LANCE_E2E_NONADMIN_TOKEN", "")
PROJECT = os.environ.get("LANCE_E2E_PROJECT", "acme")
DELIM = os.environ.get("LANCE_E2E_DELIM", "$")
S3 = os.environ.get("LANCE_E2E_S3", "http://localhost:9900")
ARROW_STREAM = "application/vnd.apache.arrow.stream"
#: The top-level namespace this suite binds into warehouse A — OWNED BY THIS FILE ALONE.
#:
#: A warehouse binding is WRITE-ONCE and permanent; only dropping the namespace releases it. So a name
#: another suite also creates is captured by whichever ran first and can never be bound here again.
#: Measured live 2026-09-06: `e2ens` was bound to `warehouse:lakehouse-wh` by another suite through
#: `topology.create_top_level`, so every table this file created landed in `s3://lakehouse-wh` while its
#: own bind 409'd and bucket-a read empty — the isolation assertion was void before it ran.
NS = os.environ.get("LANCE_E2E_WAREHOUSE_NS", "e2ewhiso")

pytestmark = pytest.mark.e2e


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}"}


def _fs() -> pafs.S3FileSystem:
    return pafs.S3FileSystem(access_key="rustfsadmin", secret_key="rustfsadmin", endpoint_override=S3, scheme="http", region="")


def _list_bucket(bucket: str) -> list[str]:
    fs = _fs()
    infos = fs.get_file_info(pafs.FileSelector(bucket, allow_not_found=True, recursive=True))
    return [i.path for i in infos]


def _arrow_ipc() -> bytes:
    table = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": pa.array(["a", "b", "c"])})
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.fixture(scope="module")
def catalog() -> str:
    if not CATALOG or not TOKEN:
        pytest.skip("set LANCE_E2E_CATALOG_URL + LANCE_E2E_TOKEN (deployed stack with warehouses enabled)")
    try:
        requests.get(f"{CATALOG}/livez", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("catalog not reachable")
    return CATALOG


def test_per_warehouse_physical_isolation(catalog: str) -> None:
    wh_a, wh_b = "e2e-wh-a", "e2e-wh-b"
    for wid in (wh_a, wh_b):
        r = requests.post(f"{catalog}/v1/warehouses", json={"id": wid, "project": PROJECT}, headers=_auth(), timeout=30)
        assert r.status_code == 200, r.text
        assert r.json()["root_uri"] == f"s3://{wid}"  # distinct physical bucket per warehouse

    # A namespace bound to warehouse A, then a table under it — both must land in bucket-a.
    # REPEATABLE (writing-python testing.md F.I.R.S.T.): re-running against a stack that already holds
    # this namespace must not fail.
    #
    # A 409 IS NOT ONE ANSWER. `create_warehouse_namespace` raises `NamespaceAlreadyExistsError` from
    # three distinct places — the write-once hijack guard ("already bound to another warehouse"), the
    # default-root collision guard, and the native create — and only the last is benign here. This
    # tolerated all three on the stated premise that a 409 "means it exists and is ALREADY bound to this
    # same warehouse". Measured live 2026-09-06, that premise was false: `e2ens` was bound to
    # `lakehouse-wh` by another suite, every table below landed there, bucket-a read empty, and the
    # isolation assertion this test exists for was void while the test reported a bind it never made.
    #
    # So the 409 is READ rather than swallowed. A hijack fails loudly and names the holder.
    r = requests.post(
        f"{catalog}/v1/warehouses/{wh_a}/namespaces",
        json={"namespace": NS},
        headers=_auth(),
        timeout=30,
    )
    assert r.status_code in (200, 409), r.text
    if r.status_code == 409:
        assert "another warehouse" not in r.text, (
            f"{NS!r} is bound to a warehouse this suite does not own, so every table it creates lands "
            f"elsewhere and the isolation assertion below proves nothing: {r.text}"
        )

    # mode=overwrite so a re-run replaces the prior version instead of colliding on an existing table.
    r = requests.post(
        f"{catalog}/v1/table/{NS}{DELIM}e2etbl/create?mode=overwrite",
        data=_arrow_ipc(),
        headers={**_auth(), "content-type": ARROW_STREAM},
        timeout=60,
    )
    assert r.status_code == 200, r.text

    a_objs = _list_bucket(wh_a)
    b_objs = _list_bucket(wh_b)
    # The table's Lance data is physically in bucket-a...
    assert any(NS in o or "e2etbl" in o for o in a_objs), a_objs
    # ...and NOT in bucket-b (physical tenant isolation).
    assert not any("e2etbl" in o for o in b_objs), b_objs


def test_deactivate_quarantines_then_activate_restores(catalog: str) -> None:
    """#3-A lifecycle (P2.3), live: deactivating a warehouse quarantines its bound namespaces (create → 403);
    activating restores routing (create → 200). Its own warehouse + namespace so a re-run is repeatable and
    it never strands the isolation test's tenant."""
    wid, ns = "e2e-wh-life", "e2elifens"
    assert requests.post(f"{catalog}/v1/warehouses", json={"id": wid, "project": PROJECT}, headers=_auth(), timeout=30).status_code == 200
    r = requests.post(f"{catalog}/v1/warehouses/{wid}/namespaces", json={"namespace": ns}, headers=_auth(), timeout=30)
    assert r.status_code in (200, 409), r.text  # 409 = already bound on a re-run (same warehouse) — fine

    def _create(name: str) -> int:
        return requests.post(
            f"{catalog}/v1/table/{ns}{DELIM}{name}/create?mode=overwrite",
            data=_arrow_ipc(),
            headers={**_auth(), "content-type": ARROW_STREAM},
            timeout=60,
        ).status_code

    assert _create("t_before") == 200  # active → routes + succeeds

    d = requests.post(f"{catalog}/v1/warehouses/{wid}/deactivate", headers=_auth(), timeout=30)
    assert d.status_code == 200 and d.json()["status"] == "deactivated", d.text
    assert _create("t_during") == 403  # quarantined → the resolver refuses

    a = requests.post(f"{catalog}/v1/warehouses/{wid}/activate", headers=_auth(), timeout=30)
    assert a.status_code == 200 and a.json()["status"] == "active", a.text
    assert _create("t_after") == 200  # restored


def test_create_warehouse_denied_for_non_admin(catalog: str) -> None:
    if not NONADMIN:
        pytest.skip("set LANCE_E2E_NONADMIN_TOKEN to exercise the 403 admin-gate leg")
    r = requests.post(
        f"{catalog}/v1/warehouses",
        json={"id": "e2e-wh-x", "project": PROJECT},
        headers={"authorization": f"Bearer {NONADMIN}"},
        timeout=30,
    )
    assert r.status_code == 403, r.text


# NONADMIN is only provided on an auth-ON governed stack, so it doubles as the "auth is enforced here" gate
# for the two anon legs below (on an auth-off stack an anon POST is a legitimate 200, not a bug).
def test_admin_plane_unauthenticated_is_401(catalog: str) -> None:
    """Admin control plane fails CLOSED to an ANONYMOUS caller — no token is 401, before any FGA check.
    The 403 leg proves a known non-admin is denied; this proves the anonymous door is shut at the auth gate,
    so a token-less request can never provision a warehouse."""
    if not NONADMIN:
        pytest.skip("auth-on stack only (set LANCE_E2E_NONADMIN_TOKEN)")
    r = requests.post(
        f"{catalog}/v1/warehouses",
        json={"id": "e2e-wh-anon", "project": PROJECT},
        timeout=30,  # deliberately no authorization header
    )
    assert r.status_code == 401, r.text


def test_data_plane_unauthenticated_write_is_401(catalog: str) -> None:
    """Data plane fails CLOSED too — a token-less table create is 401 at the router auth gate (before
    namespace resolution), never an anonymous write that would land data with no attributable author
    (the forgeable-provenance / confused-deputy hazard)."""
    if not NONADMIN:
        pytest.skip("auth-on stack only (set LANCE_E2E_NONADMIN_TOKEN)")
    r = requests.post(
        f"{catalog}/v1/table/{NS}{DELIM}e2e_anon/create?mode=overwrite",
        data=_arrow_ipc(),
        headers={"content-type": ARROW_STREAM},  # deliberately no authorization header
        timeout=30,
    )
    assert r.status_code == 401, r.text
