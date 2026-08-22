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

import hashlib

import httpx
import pyarrow as pa
import pytest
import respx
from ingest.catalog_service import CatalogError, CatalogServiceClient
from ingest.queue import UnitTask, _dedupe_id


def _task(run_id: str = "run-1", key: str = "s3://bucket/a.tif", chunk_id: str = "c0") -> UnitTask:
    return UnitTask(run_id=run_id, key=key, chunk_id=chunk_id, dataset_uri="s3://b/t.lance", token="t")


class TestTheUnitDedupeIdIsStable:
    def test_the_same_unit_of_the_same_run_hashes_the_same(self) -> None:
        """The whole requirement. A replay that produced a different id would defeat JetStream's
        dedupe window and land the unit twice."""
        assert _dedupe_id(_task()) == _dedupe_id(_task())

    def test_it_is_the_hash_the_code_actually_computes(self) -> None:
        """The separator is the LITERAL four characters `\\x00`, not a NUL byte — the f-string escapes
        the backslash. The docstring claimed the NUL form until 2026-08-22; this pins the real one so
        the two cannot drift again, and `_dedupe_id`'s docstring now records why it is not changed."""
        expected = hashlib.sha256(b"run-1\\x00s3://bucket/a.tif").hexdigest()
        assert _dedupe_id(_task()) == expected

    def test_different_units_differ(self) -> None:
        assert _dedupe_id(_task(key="a")) != _dedupe_id(_task(key="b"))

    def test_different_runs_differ(self) -> None:
        """Two runs over one source are two legitimate publishes, not a duplicate."""
        assert _dedupe_id(_task(run_id="r1")) != _dedupe_id(_task(run_id="r2"))

    def test_the_chunk_id_is_NOT_part_of_the_identity(self) -> None:
        """Deliberate: chunk_id is how enumeration happened to batch, which is not part of a unit's
        identity. Including it would make the id depend on batching and break dedupe across a
        re-enumeration that chunked differently."""
        assert _dedupe_id(_task(chunk_id="c0")) == _dedupe_id(_task(chunk_id="c7"))

    def test_it_is_header_safe(self) -> None:
        """A NATS header value must not contain CR or LF, and a source key is a URI that can carry
        either — which would corrupt the header frame rather than fail loudly. Hashing is what makes
        that unreachable, so a key with a newline must still produce a clean id."""
        got = _dedupe_id(_task(key="s3://bucket/a\r\nb.tif"))
        assert "\r" not in got and "\n" not in got
        assert len(got) == 64

    def test_the_publisher_actually_sets_the_header(self) -> None:
        """The property that was absent once already: the id can be perfect and unattached."""
        import inspect

        from ingest import queue

        source = inspect.getsource(queue.WorkQueue.publish_units)
        assert '"Nats-Msg-Id": _dedupe_id(task)' in source, "publish_units no longer stamps the dedupe id — JetStream cannot refuse a replayed unit"


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

    def test_the_catalog_still_emits_the_phrase_ingest_matches_on(self) -> None:
        """The other half of the cross-service contract. Rewording the catalog's message silently
        degrades the refusal above to the generic branch, on a deployment nobody is testing."""
        from pathlib import Path

        catalog = Path(__file__).resolve().parents[3] / "services/catalog/src/catalog"
        hits = [p for p in catalog.rglob("*.py") if "must belong to a warehouse" in p.read_text()]
        assert hits, (
            "no catalog source emits 'must belong to a warehouse' any more — ingest matches on that "
            "prose, so its actionable refusal has silently become a generic 400 passthrough"
        )
