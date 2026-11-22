"""Reading a committed version's transaction, the one way every reader of one does it.

pylance 14.0.3 cannot read back a ``Rewrite`` that rewrote an index: its binding builds
``LanceOperation.RewrittenIndex`` from two of the class's five fields (``python/src/transaction.rs``), so
``LanceDataset.read_transaction`` raises ``TypeError`` on that version (lance-format/lance#11117, the fix proposed in
#11118). A compaction of a stable-row-id table with a BTREE index commits one, measured 2026-11-15; BITMAP, LABEL_LIST
and INVERTED rewrite none. The catalog judges and announces every version from its transaction, so an unreadable one
answers a stock client's compaction 500 and stops the commit reconciler, the committed-file readers and the erasure on
that version.

:func:`read_transaction` reads through a ``RewrittenIndex`` that takes the three fields the binding leaves out as
absent. Nothing in the lakehouse reads them, and none is committed again: a transaction read here is judged or
announced, never handed back to ``LanceDataset.commit``. A release whose binding passes all five reads the same way,
and ``test_a_rewrite_that_rewrote_an_index_reads_back.py`` says when the stand-in can go.
"""

from __future__ import annotations

from dataclasses import field, make_dataclass  # noqa: TID251 — extends pylance's own RewrittenIndex dataclass, which its binding builds by name
from typing import Any, Protocol

from lance import LanceOperation


__all__ = [
    "read_transaction",
]


class _ReadsTransactions(Protocol):
    """A dataset handle a version's transaction is read from."""

    def read_transaction(self, version: int) -> Any: ...  # noqa: ANN401 - pylance's own return, a Transaction or None


#: A rewritten index as pylance names it, with the three fields its 14.0.3 binding does not pass read as absent.
_REWRITTEN_INDEX = make_dataclass(
    "RewrittenIndex",
    [
        ("new_details_type_url", str | None, field(default=None)),
        ("new_details_value", bytes | None, field(default=None)),
        ("new_index_version", int | None, field(default=None)),
    ],
    bases=(LanceOperation.RewrittenIndex,),
)
# pylance's binding resolves the class by name on each read, so it is named once, when this module is imported, and every
# read in the process takes it.
setattr(LanceOperation, "RewrittenIndex", _REWRITTEN_INDEX)  # noqa: B010 - the attribute's declared type is the class it replaces


def read_transaction(dataset: _ReadsTransactions, version: int) -> Any:  # noqa: ANN401 - pylance's own return
    """The transaction that committed ``version`` of ``dataset``, or None where the version keeps none."""
    return dataset.read_transaction(version)
