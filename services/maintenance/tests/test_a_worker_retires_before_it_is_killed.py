"""A worker that has spent its memory budget leaves on its own terms instead of being OOMKilled.

[[LH-183]]. The retention is measured and it is not a leak this estate can fix: a compaction pass
leaves **~10-14 MiB permanently resident** (9.6 / 14.4 / 54.2 MiB over runs of 1, 1 and 4 commits) in
Lance/pyarrow's native allocator, while the transient peak — 644/724/677Mi — comes back every time.
The floor rises, the ceiling does not. A 4Gi pod over a ~300Mi baseline therefore affords roughly 300
passes, which is a long time on an estate where nearly every unit is a no-op and a short time on one
whose tables are genuinely fragmented — exactly when maintenance matters most.

THE ROW REJECTED THIS REMEDY ON A PREMISE THAT IS NOW FALSIFIED. It read: "every restart strands the
sidecar's buffer for a full `ackWait`, so the recycle interval and that remedy must be chosen
together". Measured on the live lane 2026-09-22 ([[LH-190]]), a restart costs pod-restart-time plus
about five seconds and the units redeliver at once — NATS sees the subscriber's connection drop and
does not wait on the ack timer. Recycling is cheap, so it is now the answer rather than the crude
fallback.

COUNTED IN PASSES, NOT UNITS, because that is what the cost tracks: two runs over identically-shaped
tables differed 4x in retention and 4x in commits, and a no-op unit never reaches a rewrite at all.
`record_committed_rewrite` already produces exactly this number.

THE THRESHOLD IS A CEILING, NOT AN EQUALITY. A worker whose count passes the mark while a recycle is
already in flight must not be asked twice, and a count that jumps (two threads committing between
checks) must still trip — `>=`, and the caller is idempotent.
"""

from __future__ import annotations

import pytest

from maintenance.services.rewrite_slot import should_retire


@pytest.mark.parametrize("passes", [1, 99, 199])
def test_a_worker_below_the_mark_stays(passes: int) -> None:
    """The control. Without it every assertion here passes on a worker that retires immediately."""
    assert should_retire(passes, after=200) is False


@pytest.mark.parametrize("passes", [200, 201, 4096])
def test_a_worker_at_or_past_the_mark_retires(passes: int) -> None:
    """`>=`, not `==`: two threads can commit between checks, and a worker that skipped its exact
    number would run to the OOM the mark exists to prevent."""
    assert should_retire(passes, after=200) is True


def test_zero_disables_it() -> None:
    """The shipped default must be able to mean "never", or an estate that has not measured its own
    retention gets restarts it did not ask for."""
    assert should_retire(10_000, after=0) is False


def test_a_negative_mark_is_off_rather_than_always_on() -> None:
    """A misconfiguration must fail toward NOT recycling. The opposite reading turns a typo into a
    worker that exits after every pass, which looks exactly like a crash loop."""
    assert should_retire(1, after=-5) is False


# --------------------------------------------------------------------------- #
# The wiring: the decision has to reach the process
# --------------------------------------------------------------------------- #


def test_the_count_can_be_READ_without_advancing_it() -> None:
    """`record_committed_rewrite` consumes an `itertools.count`, so the handler that decides whether
    to retire cannot ask it — calling it to look would itself advance the number it is reading."""
    from maintenance.services import rewrite_slot

    before = rewrite_slot.passes_committed()
    assert rewrite_slot.passes_committed() == before, "reading the count must not change it"
    recorded = rewrite_slot.record_committed_rewrite()
    assert rewrite_slot.passes_committed() == recorded, "the reader must see what the recorder wrote"


def test_retiring_sends_this_process_the_SAME_signal_kubernetes_would(monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM to self, not `sys.exit` and not `os._exit`.

    It reuses the shutdown path a rolling restart already takes: `arm_drain_on_sigterm` flips
    `app.state.shutting_down`, so the next delivery gets RETRY instead of being started; uvicorn
    finishes the in-flight response, which is the ack for the unit that tripped the mark; and the
    container then exits for Kubernetes to restart. Any other exit skips the drain and abandons the
    response mid-flight — losing the very ack that made this pass count.
    """
    import signal as signal_module

    from maintenance.services import rewrite_slot

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(rewrite_slot.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(rewrite_slot.os, "getpid", lambda: 4242)

    rewrite_slot.retire_this_worker(passes=200)
    assert sent == [(4242, signal_module.SIGTERM)]


def test_retiring_TWICE_signals_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unit finishing after the mark would otherwise fire again. The flag is per-process and
    one-way: the answer to "have we already asked to leave" cannot become no."""
    from maintenance.services import rewrite_slot

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(rewrite_slot.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(rewrite_slot, "_retiring", False)

    rewrite_slot.retire_this_worker(passes=200)
    rewrite_slot.retire_this_worker(passes=201)
    assert len(sent) == 1, f"a retiring worker must not re-signal on every later unit: {sent}"


def test_the_HANDLER_asks_after_acking_and_not_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hop a green predicate cannot prove. `should_retire` being correct says nothing about
    `handle_unit` calling it, and the ORDER is the part that matters: the signal must be raised after
    the unit's status is settled, or the response carrying its ack is abandoned mid-flight and the
    pass that tripped the mark is redelivered to the replacement.
    """
    import asyncio
    from types import SimpleNamespace
    from typing import Any, cast

    from maintenance.api import work as work_module

    order: list[str] = []

    def _execute(item: Any, **_kw: Any) -> Any:
        order.append("executed")
        return SimpleNamespace(error_type=None, uri=item.uri, table_id=item.table_id)

    async def _no_lineage(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(work_module, "execute_unit", _execute)
    monkeypatch.setattr(work_module, "emit_sweep_lineage", _no_lineage)
    monkeypatch.setattr(work_module, "ack_for", lambda _r: (order.append("acked"), "SUCCESS")[1])
    monkeypatch.setattr(work_module.base_refs, "sibling_base_refs", lambda *_a, **_k: SimpleNamespace(is_protected=lambda _u: False))
    monkeypatch.setattr(work_module, "passes_committed", lambda: 200)
    monkeypatch.setattr(work_module, "retire_this_worker", lambda *, passes: order.append(f"retired@{passes}"))

    settings = cast(
        Any,
        SimpleNamespace(
            storage_options=lambda: {},
            delimiter="$",
            recycle_after_passes=200,
        ),
    )
    event = {"data": {"uri": "s3://b/t", "table_id": "ns$t", "plan": {}}}
    got = asyncio.run(work_module.handle_unit(event, settings, cast(Any, object())))

    assert got == {"status": "SUCCESS"}, got
    assert order == ["executed", "acked", "retired@200"], f"the retirement must come last: {order}"


def test_the_handler_does_NOT_retire_below_the_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for the hop. Without it the assertion above passes on a handler that always
    retires, which would make every unit the last one this worker ever runs."""
    import asyncio
    from types import SimpleNamespace
    from typing import Any, cast

    from maintenance.api import work as work_module

    retired: list[int] = []

    async def _no_lineage(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(work_module, "execute_unit", lambda item, **_k: SimpleNamespace(error_type=None, uri=item.uri, table_id=item.table_id))
    monkeypatch.setattr(work_module, "emit_sweep_lineage", _no_lineage)
    monkeypatch.setattr(work_module, "ack_for", lambda _r: "SUCCESS")
    monkeypatch.setattr(work_module.base_refs, "sibling_base_refs", lambda *_a, **_k: SimpleNamespace(is_protected=lambda _u: False))
    monkeypatch.setattr(work_module, "passes_committed", lambda: 199)
    monkeypatch.setattr(work_module, "retire_this_worker", lambda *, passes: retired.append(passes))

    settings = cast(Any, SimpleNamespace(storage_options=lambda: {}, delimiter="$", recycle_after_passes=200))
    asyncio.run(work_module.handle_unit({"data": {"uri": "s3://b/t", "table_id": "ns$t", "plan": {}}}, settings, cast(Any, object())))
    assert retired == []
