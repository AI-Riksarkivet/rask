"""The number a reclaimer exists to produce must be a metric, not only a log line.

[[LH-099]]. `bytes_removed` reaches three places — the per-dataset outcome, the audit line and the sweep
summary (`sweep.py:699,1010,1159`) — and no instrument. The only bytes series the maintenance service
exports is `maintenance.trash.bytes_reclaimed`, fed solely by the PURGE, so "how much did the sweep get
back" has no answer that outlives a log rotation. Measured on the deployed estate 2026-09-19:
`compaction_bytes_reclaimed_total` returns ZERO series from GreptimeDB while `compaction_runs_total`
returns one — the sweep is observable except in the dimension it is for.

It is also [[LH-102]]'s closing condition, which asks for "a recorded reclaimed-bytes figure". A figure
recorded only in a log line is recorded for as long as the log lives.

THIS HALF NEEDS NO DECISION, and the row says so. The control-EVENT question beside it — whether a
person should be TOLD a table was compacted — is a separate owner call and is not touched here.

ZERO IS EMITTED, which is the rule `record_reclaimed` already states for its three siblings and which
matters more for bytes than for any of them: "nothing was reclaimed this tick" and "the sweep never
ran" must not look identical on a dashboard, and the second is what a broken reclaimer looks like.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from maintenance.core import metrics


class _Spy:
    """Records what a counter was asked to add, in call order."""

    def __init__(self) -> None:
        self.added: list[int] = []

    def add(self, amount: int, attributes: dict[str, object] | None = None) -> None:
        self.added.append(amount)


@pytest.fixture
def bytes_counter(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy()
    monkeypatch.setattr(metrics, "_bytes_reclaimed", spy)
    return spy


def test_a_reclaiming_sweep_records_its_bytes(bytes_counter: _Spy) -> None:
    """The defect: 71,295 bytes reclaimed on the deployed estate reached no series at all."""
    metrics.record_reclaimed(fragments_removed=0, versions_removed=97, bytes_removed=71_295)

    assert bytes_counter.added == [71_295]


def test_a_sweep_that_reclaimed_NOTHING_still_emits_zero(bytes_counter: _Spy) -> None:
    """ "Nothing to reclaim" and "the reclaimer never ran" must not read alike on a dashboard."""
    metrics.record_reclaimed(fragments_removed=0, versions_removed=0, bytes_removed=0)

    assert bytes_counter.added == [0], "the zero was skipped, so the series only exists after the first non-zero tick"


def test_the_sweep_passes_the_bytes_it_measured() -> None:
    """Asserted on the CALL SITE, because an instrument nothing feeds is the same defect one rung out.

    PARSED, not grepped. `bytes_removed=result.bytes_removed` already appears in this module at the
    AUDIT line, so a substring search passes before the fix exists and proves nothing — which is the
    failure this file is about, one rung further out again. The AST names the callee.
    """
    source = Path(metrics.__file__).parents[1] / "services" / "sweep.py"
    calls = [
        node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "record_reclaimed"
    ]

    assert calls, "the sweep no longer calls `record_reclaimed` at all"
    assert any(kw.arg == "bytes_removed" for call in calls for kw in call.keywords), "`record_reclaimed` is called without the bytes the sweep measured"
