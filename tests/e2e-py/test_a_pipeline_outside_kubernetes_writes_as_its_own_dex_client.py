"""A pipeline outside Kubernetes writes as its own Dex client, never as a person (ADR 0181, [[XC-174]]).

The pipeline signs in with Dex's client-credentials grant as the confidential client the Dex fixture declares
(`machineClients`, `rask-pipeline`), asking for the lakehouse's audience, which the audience client issues only to a
client it trusts. The catalog names that bearer `dex-client~<client id>`, a machine: granted `writer` on a scratch tenant's
bronze namespace, it creates a table, the commit Lance holds names it as the author, and since a machine never owns what
it creates (LH-433) its own drop of that table is refused while the tenant's admin, who owns it through the namespace,
drops it at teardown.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import lance
import lance_namespace
import pyarrow as pa
import pytest
import requests
from byo_scenario import CLOSE_EACH_REQUEST
from principals import DEX_AUDIENCE, principal_of
from scratch_tenant import CATALOG, DEX, Tenant, ok, provisioned

from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The machine client the Dex fixture declares, and its secret (`deploy/fixtures/dex/chart/values.yaml` `machineClients`).
CLIENT_ID = os.environ.get("LANCE_E2E_MACHINE_CLIENT_ID", "rask-pipeline")
CLIENT_SECRET = os.environ.get("LANCE_E2E_MACHINE_CLIENT_SECRET", "rask-pipeline-dev-secret")


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("dexclient", grants=())


@pytest.fixture(scope="module")
def client_bearer() -> str:
    """The bearer Dex issues the machine client for itself, for the lakehouse's audience."""
    answer = requests.post(
        f"{DEX}/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials", "scope": f"openid audience:server:client_id:{DEX_AUDIENCE}"},
        timeout=20,
    )
    assert answer.status_code == 200, f"Dex refused the client-credentials grant: {answer.status_code} {answer.text[:300]}"
    return str(answer.json()["id_token"])


def test_a_pipeline_signed_in_as_its_own_dex_client_writes_as_that_client_and_does_not_own_what_it_writes(tenant: Tenant, client_bearer: str) -> None:
    principal = principal_of(CATALOG, client_bearer)
    assert principal == f"dex-client~{CLIENT_ID}", f"the client's bearer proves {principal}"
    ok(tenant.admin.http.post(f"{CATALOG}/management/v1/namespace/{tenant.bronze}/access/grant", json={"user": principal, "relation": "writer"}))
    table = f"{tenant.bronze}$written_by_a_client"
    headers = {"authorization": f"Bearer {client_bearer}", "connection": CLOSE_EACH_REQUEST}
    rows = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))

    created = requests.post(f"{CATALOG}/v1/table/{table}/create", data=rows, headers={**headers, "content-type": ARROW_STREAM_MEDIA_TYPE}, timeout=60)
    tenant.tables.append(table)

    assert created.status_code == 200, created.text[:500]
    stock = lance_namespace.connect("rest", {"uri": CATALOG, "headers.Authorization": f"Bearer {client_bearer}", "headers.Connection": CLOSE_EACH_REQUEST})
    written = lance.dataset(namespace_client=stock, table_id=table.split("$"))
    transaction = written.read_transaction(written.version)
    assert transaction is not None, f"{table} version {written.version} has no transaction to read"
    assert (transaction.transaction_properties or {}).get("rask.author") == principal, transaction.transaction_properties
    dropped = requests.post(f"{CATALOG}/v1/table/{table}/drop", json={}, headers=headers, timeout=60)
    assert dropped.status_code == 403, f"a machine dropped the table it created: {dropped.status_code} {dropped.text[:300]}"
