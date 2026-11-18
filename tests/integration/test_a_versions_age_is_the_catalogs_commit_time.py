"""A version a stock writer commits through ``version/create`` ages by the catalog's clock, never the writer's (ADR 0243 §9).

Lance's cleanup ages a version by its manifest's ``timestamp`` (``lance_docs/lance/guide/read_and_write.md`` § Cleanup old
versions), and a stock writer stamps it on its own clock. Before the catalog stamped the bytes it finalizes, a writer
whose clock ran years ahead committed a version no retention and no erasure release's cleanup would reclaim (an erased
subject's rows in it survived the release), and one whose clock ran behind committed a version the first cleanup took as
soon as it was superseded. Driven with stock pylance against the live catalog, each staged manifest's stamp set by the
writer between staging its commit and asking for it, as a writer on a wrong clock stages it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import lance
import lance_namespace
import pyarrow as pa
import pyarrow.fs as pafs
import pytest

from catalog.core import manifest_bytes
from catalog.services.erasure import ErasureReport, reclaim_refs
from lakehouse import condemned_versions
from lakehouse.storage_options import StorageOptions
from tests.integration.conftest import RaceCatalog
from tests.integration.test_the_store_refuses_a_put_over_a_committed_file import _HookedCommits


TABLE_ID = ["lance_catalog", "clocks", "t"]


def unheld(control_root: str, table_uuid: str, uri: str) -> Callable[[str | None, Any], condemned_versions.RefReclaim]:
    """Each ref's reclaim batches as the catalog runs them for an erasure (``catalog.services.reclaims``), each deleted
    through the ref's own handle."""

    def of(ref: str | None, handle: Any) -> condemned_versions.RefReclaim:  # noqa: ANN401 — a lance.LanceDataset of the ref
        def batch(versions: list[int]) -> condemned_versions.Verdict:
            run = condemned_versions.Batch(control_root, StorageOptions(), table_uuid=table_uuid, location=uri, dataset=lance.dataset(uri), ref=ref)
            run.condemn(versions, forks=True)
            run.read_pins()
            return run.promote(None)

        return condemned_versions.RefReclaim(condemn=batch, mint=lambda: condemned_versions.Credential(dataset=handle, expires_at=None))

    return of


def _rows(ids: range) -> pa.Table:
    return pa.table({"id": pa.array(list(ids), pa.int64())})


def _restamped(at: datetime) -> Callable[[lance_namespace.CreateTableVersionRequest], None]:
    """A writer's hook putting its staged manifest again stamped ``at``, as a writer on that clock writes it."""

    def _before(request: lance_namespace.CreateTableVersionRequest) -> None:
        # pylance hands a hook the request as a dict of its fields.
        fs, path = pafs.LocalFileSystem(), f"/{dict(request)['manifest_path']}"
        with fs.open_input_stream(path) as staged:
            body = staged.read()
        with fs.open_output_stream(path) as restaged:
            restaged.write(manifest_bytes.with_commit_time(body, at))

    return _before


@pytest.mark.parametrize("race_catalog", ["dir"], indirect=True)
@pytest.mark.parametrize("skew", ["ahead", "behind"])
def test_a_version_a_writer_stamps_on_a_wrong_clock_ages_by_the_catalogs(race_catalog: RaceCatalog, skew: str, tmp_path: Path) -> None:
    """Ahead: once superseded and two seconds old, the erasure release's own cleanup (``erasure.reclaim_refs``, what
    ``erasure_release._reclaim`` runs) at a retention of one second reclaims it; a release at retention 0 removes every
    superseded version whatever its stamp (measured on pylance 13.0.0), so the stamp bites at any retention above 0.
    Behind: a cleanup keeping a day keeps it. Either way the stock client reads its commit back as its own."""
    assert httpx.post(f"{race_catalog.url}/v1/namespace/lance_catalog$clocks/create", json={}, timeout=60).status_code == 200
    stock = lance_namespace.connect("rest", {"uri": race_catalog.url})
    lance.write_dataset(_rows(range(3)), namespace_client=stock, table_id=TABLE_ID, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    writer_clock = datetime.now(UTC) + (timedelta(days=3650) if skew == "ahead" else -timedelta(days=3650))
    hooked = _HookedCommits(stock, _restamped(writer_clock))
    written = lance.write_dataset(_rows(range(3, 6)), namespace_client=hooked, table_id=TABLE_ID, mode="append")
    table = lance.dataset(namespace_client=stock, table_id=TABLE_ID)
    stamped = {int(entry["version"]): cast("datetime", entry["timestamp"]).replace(tzinfo=UTC) for entry in table.versions()}
    lance.write_dataset(_rows(range(6, 9)), namespace_client=stock, table_id=TABLE_ID, mode="append")
    location = str(table.uri)

    if skew == "ahead":
        time.sleep(2)
        reclaim_refs(
            ErasureReport(table="lance_catalog$clocks$t", predicate=""),
            lance.dataset(location),
            [None],
            clean={None},
            retention=timedelta(seconds=1),
            storage_options=StorageOptions(),
            condemn=unheld(str(tmp_path), "clocks", location),
        )
    else:
        lance.dataset(location).cleanup_old_versions(older_than=timedelta(days=1), error_if_tagged_old_versions=False)
    kept = [int(entry["version"]) for entry in lance.dataset(location).versions()]

    assert (hooked.refused, written.version, abs(stamped[2] - datetime.now(UTC)) < timedelta(minutes=10), 2 in kept) == ([], 2, True, skew == "behind"), (
        f"the commit was refused {hooked.refused}, read back at {written.version}, stamped {stamped[2]} by a writer {skew}, and kept {kept}"
    )
