"""Each ref's reclaim batches as the catalog runs them for an erasure (ADR 0246), which reads no maintenance hold.

An erasure's verification asks the catalog for every batch of each ref's reclaim (``catalog.services.reclaims``); these
suites drive the verification over a local root with no catalog app, so the batch is the real one
(:class:`lakehouse.condemned_versions.Batch`, its record on a control root of the test's own), and each batch deletes
through the ref's own handle on the local root, a credential no store expires. Whether a hold keeps a table whole is
``tests/integration/test_a_reclaim_condemns_before_it_deletes.py``'s claim.
"""

from __future__ import annotations

from collections.abc import Callable

import lance

from lakehouse import condemned_versions
from lakehouse.condemned_versions import Credential, RefReclaim, Verdict
from lakehouse.storage_options import StorageOptions


def unheld(control_root: str, table_uuid: str, uri: str) -> Callable[[str | None], RefReclaim]:
    """For each ref of the table at ``uri``, its batches, naming the ref's identity as read when asked for: condemned (a
    fork point among them), pins read, promoted; each deleted through the ref's own handle."""

    def of(ref: str | None) -> RefReclaim:
        listed_on = condemned_versions.identity_of(lance.dataset(uri), ref, table_uuid=table_uuid)

        def batch(versions: list[int]) -> Verdict:
            main = lance.dataset(uri)
            run = condemned_versions.Batch(
                control_root, StorageOptions(), table_uuid=table_uuid, location=uri, dataset=main, ref=ref, listed_on=listed_on, registered=lambda: table_uuid
            )
            run.condemn(versions, forks=True)
            run.read_pins()
            return run.promote(None)

        def mint() -> Credential:
            main = lance.dataset(uri)
            return Credential(dataset=main if ref is None else main.checkout_version((ref, None)), expires_at=None)

        return RefReclaim(condemn=batch, mint=mint)

    return of
