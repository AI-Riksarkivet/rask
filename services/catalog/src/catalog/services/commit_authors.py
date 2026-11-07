"""The author of a version Lance committed itself, recorded just after it lands (ADR 0187).

Every commit of an operation the doors announce records the caller its door authenticated
(:mod:`lakehouse.commit_records`), and the commit reconciler signs a re-announcement only for that record. A
detached commit (``dataplane.commit_stamped``) and a stock commit through ``version/create`` record before their
finalize. The create door's ``write_dataset`` and an unkeyed ``/insert`` let Lance commit in one call, and Lance mints the
transaction's uuid inside it, so the author is recorded just after the version lands, under the transaction its files
were recorded under (:func:`catalog.services.committed_files.record_landed`): a crash or a store failure in between leaves
the version unrecorded, so a lost announcement of it is counted unauthored and pages rather than being signed for anyone
(owner, 2026-11-01).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from lance.dataset import Transaction
from opentelemetry import metrics


log = logging.getLogger(__name__)

#: Versions Lance committed itself whose author the catalog could not record. Added to at every such commit, zero
#: included, so the series exists before the first miss.
_unrecorded = metrics.get_meter("lance.catalog").create_counter(
    "catalog.commit_authors.unrecorded",
    unit="{commit}",
    description="Versions Lance committed itself whose author the catalog could not record once they landed (ADR 0187): a "
    "lost announcement of one is counted unauthored rather than signed for anyone.",
)


def record_landed(record: Callable[[str, int], object], transaction: Transaction | None, *, version: int, table_location: str) -> None:
    """Record, through ``record`` (a transaction's uuid and the version it landed at), the author of ``version``, which
    Lance committed itself as ``transaction``; ``None`` for a version whose transaction could not be read.

    After the version lands, since the write and its commit are one call: a failure here is logged at ERROR and counted,
    and the door still answers its version, because the write landed and an error would have its caller retry it.
    """
    if transaction is None:
        _count_unrecorded(table_location, version, "the version names no readable transaction")
        return
    try:
        record(transaction.uuid, version)
    except Exception as exc:  # noqa: BLE001 — the version landed; a failed record must not fail the door that wrote it
        _count_unrecorded(table_location, version, f"{type(exc).__name__}: {exc}")
        return
    _unrecorded.add(0)


def _count_unrecorded(table_location: str, version: int, reason: str) -> None:
    _unrecorded.add(1)
    log.error("commit_author_unrecorded", extra={"location": table_location, "version": version, "error": reason[:300]})
