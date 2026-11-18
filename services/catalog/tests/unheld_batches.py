"""Each ref's reclaim batches as the catalog runs them for an erasure (ADR 0246), which reads no maintenance hold.

An erasure's verification asks the catalog for every batch of each ref's reclaim (``catalog.services.reclaims``); these
suites drive the verification over a local root with no catalog app, so the batch is the real one
(:class:`lakehouse.condemned_versions.Batch`, its record on a control root of the test's own), and each batch deletes
through the ref's own handle on the local root, a credential no store expires. Whether a hold keeps a table whole is
``tests/integration/test_a_reclaim_condemns_before_it_deletes.py``'s claim.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import lance

from lakehouse import condemned_versions
from lakehouse.condemned_versions import Credential, RefReclaim, Verdict
from lakehouse.storage_options import StorageOptions


def unheld(control_root: str, table_uuid: str, uri: str) -> Callable[[str | None, Any], RefReclaim]:
    """For each ref of the table at ``uri`` and its handle, its batches: condemned (a fork point among them), pins read,
    promoted; each deleted through that handle."""

    def of(ref: str | None, handle: Any) -> RefReclaim:  # noqa: ANN401 — a lance.LanceDataset of the ref
        def batch(versions: list[int]) -> Verdict:
            run = condemned_versions.Batch(control_root, StorageOptions(), table_uuid=table_uuid, location=uri, dataset=lance.dataset(uri), ref=ref)
            run.condemn(versions, forks=True)
            run.read_pins()
            return run.promote(None)

        return RefReclaim(condemn=batch, mint=lambda: Credential(dataset=handle, expires_at=None))

    return of
