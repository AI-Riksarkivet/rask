"""Each ref's reclaim batches as the catalog runs them for a table no maintenance policy holds (ADR 0246).

An erasure's verification asks the catalog for every batch of each ref's reclaim (``catalog.services.reclaims``); these
suites drive the verification over a local root with no catalog app, so the batch is the real one
(:class:`lakehouse.condemned_versions.Batch`, its record on a control root of the test's own) and the policy read answers
that nothing holds the table. Whether a hold keeps a table whole is
``tests/integration/test_a_reclaim_condemns_before_it_deletes.py``'s claim.
"""

from __future__ import annotations

from collections.abc import Callable

import lance

from lakehouse import condemned_versions
from lakehouse.condemned_versions import Verdict
from lakehouse.storage_options import StorageOptions


def unheld(control_root: str, table_uuid: str, uri: str) -> Callable[[str | None], Callable[[list[int]], Verdict]]:
    """For each ref of the table at ``uri``, its batches: condemned (a fork point among them), pins read, promoted."""

    def of(ref: str | None) -> Callable[[list[int]], Verdict]:
        def batch(versions: list[int]) -> Verdict:
            run = condemned_versions.Batch(control_root, StorageOptions(), table_uuid=table_uuid, location=uri, dataset=lance.dataset(uri), ref=ref)
            run.condemn(versions, forks=True)
            run.read_pins()
            return run.promote(None)

        return batch

    return of
