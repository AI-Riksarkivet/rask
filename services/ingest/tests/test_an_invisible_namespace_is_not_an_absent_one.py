"""A namespace this identity cannot SEE must not be reported as one that does not exist.

`_ensure_namespace` probes `/v1/namespace/{id}/exists`, and any status at or above 400 falls through to
`create`. A create of a bare top-level namespace is refused *"must belong to a warehouse"* by design —
tenancy belongs to an admin, not to a data writer — and that refusal is then translated into
*"namespace X is not provisioned ... an admin creates it with POST /v1/projects"*.

That is right for a 404 and wrong for a 403, and the difference is not cosmetic. MEASURED against the
deployed estate 2026-09-10: the lane's namespace `lane-bronze` had just been created by an admin and the
catalog described it 200, while the ingest run failed telling the operator to create it. The remedy the
message names cannot fix the problem it reports — an admin follows it, the namespace already exists, and
the run keeps failing. The real fault is a missing GRANT for the writer's identity.

The catalog answers 403 rather than 404 on purpose (no existence oracle on a door a stranger can poll),
so the ambiguity is real and has to be resolved by the CALLER: 403 means "it may exist and I cannot see
it", which is a grant to fix, and 404 means "it is not there", which is tenancy to provision. Reporting
both as the second is how an operator loses an afternoon.
"""

from __future__ import annotations

import httpx
import pyarrow as pa
import pytest
import respx

from ingest.catalog_service import CatalogError, CatalogServiceClient


BASE = "http://catalog.test"


def _client() -> CatalogServiceClient:
    return CatalogServiceClient(pa.schema([pa.field("id", pa.int64())]), base_url=BASE, token="t")


@respx.mock
def test_a_403_on_the_probe_names_the_GRANT_not_the_tenancy() -> None:
    """The live case. The namespace exists; this identity cannot see it."""
    respx.post(url__regex=r".*/v1/namespace/.*/exists").mock(return_value=httpx.Response(403, text="can_get_metadata required"))

    with pytest.raises(CatalogError) as excinfo:
        _client()._ensure_namespace("lane-bronze")

    message = str(excinfo.value)
    assert "lane-bronze" in message
    assert "POST /v1/projects" not in message, "a 403 sent the operator to provision a namespace that already exists"
    assert "grant" in message.lower() or "can_" in message, "the message must name what is actually missing — a relation for this identity"


@respx.mock
def test_a_404_on_the_probe_still_reports_the_TENANCY_gap() -> None:
    """The other half, and the reason the first cannot simply swap the message: an absent namespace IS
    a tenancy gap, and the admin doors ARE the fix for it."""
    respx.post(url__regex=r".*/v1/namespace/.*/exists").mock(return_value=httpx.Response(404))
    respx.post(url__regex=r".*/v1/namespace/.*/create").mock(
        return_value=httpx.Response(400, text="top-level namespace 'lane-bronze' must belong to a warehouse")
    )

    with pytest.raises(CatalogError) as excinfo:
        _client()._ensure_namespace("lane-bronze")

    assert "POST /v1/projects" in str(excinfo.value), "an absent namespace must still send the operator to the admin doors"


@respx.mock
def test_a_visible_namespace_is_a_no_op() -> None:
    """The negative control: the probe succeeding must reach neither branch."""
    respx.post(url__regex=r".*/v1/namespace/.*/exists").mock(return_value=httpx.Response(200))

    _client()._ensure_namespace("lane-bronze")
