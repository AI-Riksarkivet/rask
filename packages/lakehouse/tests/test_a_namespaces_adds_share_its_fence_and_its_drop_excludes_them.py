"""ADR 0233: adds beneath a namespace share its fence whatever their number, and an add and the namespace's drop exclude
each other (``specs/catalog/Fence.tla``).

Every create beneath a namespace takes its fence for an add, so adds must never refuse adds: measured on RustFS 1.0.3
with one shared record per namespace, 8 of 16, and 39 of 64, concurrent creates into one namespace answered 409 with no
drop anywhere. The doors' side, a create refused while the namespace's drop runs, is pinned in
``tests/integration/test_a_cascade_keeps_what_it_answered.py``. Here, on a local control root, the fence's own rules.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from lakehouse.namespace_fences import NamespaceFence, NamespaceFencedError, NamespaceWork


_ADDS = 64


def test_concurrent_adds_all_hold_the_fence_and_a_drop_beside_them_is_refused(tmp_path: Path) -> None:
    fence = NamespaceFence(control_root=str(tmp_path), storage_options={}, name="wh$ns")
    outcomes: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(_ADDS)

    def _add() -> None:
        start.wait()
        try:
            fence.take(NamespaceWork.ADD)
            outcome = "held"
        except NamespaceFencedError as exc:
            outcome = str(exc)
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=_add) for _ in range(_ADDS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    refused = [outcome for outcome in outcomes if outcome != "held"]
    assert not refused, f"{len(refused)} of {_ADDS} adds racing only adds were refused: {refused[0]}"
    with pytest.raises(NamespaceFencedError):
        fence.take(NamespaceWork.DROP)
