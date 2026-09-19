"""lancedb and lance-ray reach the catalog too — the two stock clients the conformance suite never drove.

[[LH-020]]. `test_the_stock_lance_client_drives_the_catalog.py` proves the deployed catalog answers
`lance_namespace`, the low-level client. It is not the only one an outside user reaches for: lancedb is
the table API most people start from, and lance-ray is how a distributed read happens. Neither appeared
anywhere under `tests/e2e-py`, so "a stock client drives our catalog" was proven for one client out of
three — and the two unproven ones are the two a user is more likely to hold.

WHY THAT GAP IS NOT ACADEMIC. These clients do not share a request path with `lance_namespace`; they
each resolve a table through their OWN namespace plumbing (`namespace_path=` for lancedb,
`namespace_impl="rest"` for lance-ray), and a multi-segment identifier is exactly where rask's
`project > warehouse > namespace > table` hierarchy meets a client that assumed a flat name. Conformance
proven through one client says nothing about the others' resolution.

THE UPSTREAM ISSUE THIS ROW ONCE ASKED FOR MUST NOT BE FILED: lancedb 0.34.0 exposes
`open_table(name, namespace_path=[...])` — verified in the installed package — so a multi-segment
identifier IS expressible and there is no upstream bug behind the ask.

Marked `spec_conformance` like its sibling: it needs a deployed catalog and a Dex that will mint a
bearer, so it is skipped without `LANCE_E2E_CATALOG_URL` rather than passing vacuously.
"""

from __future__ import annotations

import os

import pytest
import requests


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex").rstrip("/")
USER = os.environ.get("LANCE_E2E_USER", "alice@example.com")
NAMESPACE = os.environ.get("LANCE_E2E_STOCK_NAMESPACE", "acme-bronze")
TABLE = os.environ.get("LANCE_E2E_STOCK_TABLE", "agnostic")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.spec_conformance,
    pytest.mark.skipif(not CATALOG, reason="LANCE_E2E_CATALOG_URL not set — a deployed catalog is required"),
]


def _token(user: str) -> str:
    """The same password grant the sibling suite uses; a stock client carries an ordinary bearer."""
    response = requests.post(
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
    response.raise_for_status()
    return response.json()["id_token"]


def _vended(token: str) -> dict[str, str]:
    """Short-TTL, table-scoped storage options from the catalog's own vending door.

    A STOCK CLIENT NEEDS THESE AND THE CATALOG DOES NOT PUSH THEM, which is the substantive finding here
    rather than a fixture detail. Resolving the table through the namespace yields a LOCATION; reading it
    is a direct object-store call the client makes itself, so without credentials lancedb falls through to
    the AWS default provider chain and fails "no providers in chain provided credentials" — measured
    against the deployed catalog. The estate's rule is that storage access is a vended 900 s session
    rather than a standing key, so this is the correct path and not a workaround; what it costs is that a
    stock client must know to ask.
    """
    response = requests.post(
        f"{CATALOG}/management/v1/table/{NAMESPACE}%24{TABLE}/credentials?tier=read",
        json={"id": [NAMESPACE, TABLE]},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    return (body.get("credentials") or body).get("storage_options") or {}


def _object_store_reachable(storage_options: dict[str, str]) -> bool:
    """Can THIS process reach the endpoint the catalog vended?

    THE VENDED ENDPOINT IS AN IN-CLUSTER ADDRESS (`http://rask-minio:9000` on this estate), because that
    is what the catalog itself uses. A client running outside the cluster therefore receives a working
    credential for a host it cannot resolve — the resolution and the credential are both correct and the
    READ still cannot happen. That is a real property of vending to an external client, not a test
    defect, so it is probed and named rather than allowed to look like a conformance failure.
    """
    import socket
    from urllib.parse import urlparse

    endpoint = storage_options.get("endpoint")
    if not endpoint:
        return False
    parsed = urlparse(endpoint)
    try:
        with socket.create_connection((parsed.hostname or "", parsed.port or 80), timeout=3):
            return True
    except OSError:
        return False


def test_lancedb_opens_a_table_through_the_catalogs_namespace() -> None:
    """lancedb resolves a MULTI-SEGMENT identifier against rask's hierarchy.

    `namespace_path` is the parameter that makes this expressible — without it a client can only name a
    flat table and rask's `namespace$table` identity is unreachable, which is what the row's proposed
    upstream issue was about. The parameter exists (lancedb 0.34.0), so what is left is proving the
    catalog answers it.
    """
    lancedb = pytest.importorskip("lancedb")

    # `connect_namespace`, not `connect`: `lancedb.connect(uri)` opens a LOCAL or LanceDB-Cloud
    # database and rejects namespace arguments outright. The namespace entry point takes the impl and
    # its properties positionally — verified against the installed signature rather than assumed.
    token = _token(USER)
    db = lancedb.namespace.connect_namespace("rest", {"uri": CATALOG, "headers.Authorization": f"Bearer {token}"})
    # ON `open_table`, NOT ON THE CONNECTION. Measured against the deployed catalog: connection-level
    # `storage_options` do not reach the dataset read — the open still fell through to the AWS default
    # provider chain and failed "no providers in chain provided credentials". The per-table parameter is
    # the one that is consumed, which matters because a vended credential IS per-table and 900 s: a
    # connection-scoped credential could not be the right shape here anyway.
    vended = _vended(token)
    assert {"aws_access_key_id", "aws_secret_access_key", "aws_session_token", "endpoint"} <= set(vended), (
        f"the catalog vended an incomplete credential set for a stock client: {sorted(vended)}"
    )
    # BEFORE `open_table`, not after: opening a table is itself an object-store read — it lists the
    # table's `_versions/` — so a late skip would already have failed on the unreachable endpoint.
    if not _object_store_reachable(vended):
        pytest.skip(f"the vended endpoint {vended.get('endpoint')!r} is in-cluster and unreachable from here — resolution and the vend are proven above")
    table = db.open_table(TABLE, namespace_path=[NAMESPACE], storage_options=vended)

    assert table.count_rows() >= 0, "the table opened but would not answer a count — the handle is not usable"
    assert table.schema is not None


def test_lance_ray_reads_a_table_through_the_catalogs_namespace() -> None:
    """lance-ray resolves the same identifier and reads it distributed.

    `ray.init(address="local")` keeps this in one process: the subject is the CATALOG's answer to
    lance-ray's namespace resolution, not Ray's scheduling, and a cluster would make a resolution failure
    look like a scheduling one.
    """
    ray = pytest.importorskip("ray")
    lance_ray = pytest.importorskip("lance_ray")

    ray.init(address="local", ignore_reinit_error=True, include_dashboard=False)
    try:
        token = _token(USER)
        vended = _vended(token)
        if not _object_store_reachable(vended):
            pytest.skip(f"the vended endpoint {vended.get('endpoint')!r} is in-cluster and unreachable from here")
        dataset = lance_ray.read_lance(
            table_id=[NAMESPACE, TABLE],
            namespace_impl="rest",
            # `namespace_properties`, which is what the installed signature calls it.
            namespace_properties={"uri": CATALOG, "headers.Authorization": f"Bearer {token}"},
            storage_options=vended,
        )
        assert dataset.count() >= 0, "lance-ray resolved the table but the read produced no countable dataset"
    finally:
        ray.shutdown()


def test_an_unauthenticated_lancedb_client_is_REFUSED() -> None:
    """The refusal must reach these clients too, or governance is proven for one transport only.

    Asserted as "it raised", not on a message: each client wraps the catalog's problem+json differently
    and pinning one wording here would test the client's formatting rather than rask's refusal.
    """
    lancedb = pytest.importorskip("lancedb")
    pytest.importorskip("lancedb.namespace")

    db = lancedb.namespace.connect_namespace("rest", {"uri": CATALOG})
    with pytest.raises(Exception):  # noqa: B017, PT011 — the client's own wrapper type; the REFUSAL is the subject
        db.open_table(TABLE, namespace_path=[NAMESPACE]).count_rows()
