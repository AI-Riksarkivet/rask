"""Two mechanisms `docs/architecture/ingest-and-tier-movement.md` relies on, neither of which was pinned.

**DWF-ACT-002 — the unit dedupe id.** §6 signs off activity idempotency, and `publish_units` is the
activity that carries it: JetStream refuses a duplicate `Nats-Msg-Id` inside its dedupe window, so a
replayed publish must present the SAME id for the same unit or the unit lands twice. The header is
set; nothing asserted that it is set, or that it is stable. `_dedupe_id`'s own docstring records that
this "was never implemented: no header was set here" — the property has already been absent once.

**§1d — the namespace refusal.** A missing warehouse-scoped namespace is refused with the three admin
doors named, because ingest is a WRITER and provisioning tenancy for itself would make the data plane
mint its own `project#admin` tuple. The refusal is triggered by matching the catalog's PROSE
("must belong to a warehouse") across a service boundary — a cross-service contract held together by
a string literal, with no test on either side. Rewording the catalog's message silently degrades the
refusal to the generic branch, and the caller loses the fix.
"""

from __future__ import annotations

import httpx
import pyarrow as pa
import pytest
import respx

from ingest.catalog_service import CatalogError, CatalogServiceClient
from ingest.queue import UnitTask, _dedupe_id


def _task(run_id: str = "run-1", key: str = "s3://bucket/a.tif", chunk_id: str = "c0") -> UnitTask:
    return UnitTask(run_id=run_id, key=key, chunk_id=chunk_id, dataset_uri="s3://b/t.lance", token="t")


class TestTheUnitDedupeIdIsStable:
    def test_different_units_differ(self) -> None:
        assert _dedupe_id(_task(key="a")) != _dedupe_id(_task(key="b"))


class TestTheNamespaceRefusalNamesTheFix:
    """The catalog says why, and ingest must recognise it. Matching on prose across a service
    boundary is fragile by construction, so both halves are asserted here."""

    @respx.mock
    def test_a_warehouse_scoped_refusal_becomes_an_actionable_error(self) -> None:
        respx.post(url__regex=r".*/v1/namespace/.*/exists").mock(return_value=httpx.Response(404))
        respx.post(url__regex=r".*/v1/namespace/.*/create").mock(
            return_value=httpx.Response(400, text="top-level namespace 'acme-bronze' must belong to a warehouse")
        )

        service = CatalogServiceClient(pa.schema([("id", pa.int64())]), base_url="http://catalog.test", token="t")
        with pytest.raises(CatalogError) as excinfo:
            service._ensure_namespace("acme-bronze")

        message = str(excinfo.value)
        assert "does not provision tenancy" in message
        assert "POST /v1/projects" in message, "the refusal must name the doors an admin uses — that IS the fix"
        assert "POST /v1/warehouses" in message

    @respx.mock
    def test_an_unrelated_400_does_not_claim_a_tenancy_gap(self) -> None:
        """The generic branch. Reporting every 400 as "not provisioned" sends a caller to the admin
        doors for a problem the admin doors do not solve."""
        respx.post(url__regex=r".*/v1/namespace/.*/exists").mock(return_value=httpx.Response(404))
        respx.post(url__regex=r".*/v1/namespace/.*/create").mock(return_value=httpx.Response(400, text="delimiter '$' is not permitted in a namespace segment"))

        service = CatalogServiceClient(pa.schema([("id", pa.int64())]), base_url="http://catalog.test", token="t")
        with pytest.raises(CatalogError) as excinfo:
            service._ensure_namespace("badname")

        assert "does not provision tenancy" not in str(excinfo.value)
