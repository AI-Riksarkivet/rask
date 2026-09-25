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

    rewrite_slot.retire_this_worker(passes=200, reason="committed-rewrite budget spent")
    assert sent == [(4242, signal_module.SIGTERM)]


def test_retiring_TWICE_signals_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unit finishing after the mark would otherwise fire again. The flag is per-process and
    one-way: the answer to "have we already asked to leave" cannot become no."""
    from maintenance.services import rewrite_slot

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(rewrite_slot.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(rewrite_slot, "_retiring", False)

    rewrite_slot.retire_this_worker(passes=200, reason="committed-rewrite budget spent")
    rewrite_slot.retire_this_worker(passes=201, reason="committed-rewrite budget spent")
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
    monkeypatch.setattr(work_module, "retire_this_worker", lambda *, passes, reason: order.append(f"retired@{passes}"))

    settings = cast(
        Any,
        SimpleNamespace(
            storage_options=lambda: {},
            delimiter="$",
            recycle_after_passes=200,
            recycle_at_memory_fraction=0.0,
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
    monkeypatch.setattr(work_module, "retire_this_worker", lambda *, passes, reason: retired.append(passes))

    settings = cast(Any, SimpleNamespace(storage_options=lambda: {}, delimiter="$", recycle_after_passes=200, recycle_at_memory_fraction=0.0))
    asyncio.run(work_module.handle_unit({"data": {"uri": "s3://b/t", "table_id": "ns$t", "plan": {}}}, settings, cast(Any, object())))
    assert retired == []


# --------------------------------------------------------------------------- #
# EVERY path that rewrites must count, or the budget bounds only one of them
# --------------------------------------------------------------------------- #


def test_every_module_that_holds_a_rewrite_slot_also_counts_the_pass() -> None:
    """The structural half, and it caught a real hole the hour this was written.

    `rewrite_slot` is documented as "the only step that holds bytes", and TWO modules acquire it:
    `compaction_executor` on the distributed path and `optimize` in-pod. Only the first called
    `record_committed_rewrite`, so a worker doing in-pod compactions retained ~12 MiB per pass and
    could never reach its budget — the retirement bounded one path and looked like it bounded both.

    Asserted over the SOURCE rather than by calling them, because the failure is an ABSENCE: a third
    rewrite path added later is covered the day it acquires a slot, which no behavioural test of the
    two existing ones can promise.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "maintenance" / "services"
    holders: dict[str, set[str]] = {}
    for module in sorted(root.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        if module.name == "rewrite_slot.py" or "rewrite_slot" not in called:
            continue
        holders[module.name] = called

    assert holders, "no module acquires a rewrite slot — the walk found nothing, so it proves nothing"
    uncounted = sorted(name for name, called in holders.items() if "record_committed_rewrite" not in called)
    assert uncounted == [], (
        f"{uncounted} hold a rewrite slot and never count the pass — the [[LH-183]] budget would bound "
        "every OTHER path and silently not this one. Call `record_committed_rewrite()` once per "
        "completed compaction there, at the same granularity as the distributed commit."
    )


def test_the_IN_POD_path_counts_a_pass_that_moved_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behavioural half of the gate above: the in-pod compaction must advance the same counter
    the distributed commit does, or the two paths disagree about what this worker has spent."""
    from types import SimpleNamespace
    from typing import Any, cast

    from maintenance.services import optimize as optimize_module

    counted: list[int] = []
    monkeypatch.setattr(optimize_module, "record_committed_rewrite", lambda: (counted.append(1), len(counted))[1])
    monkeypatch.setattr(optimize_module, "_rewrite", lambda *_a, **_k: SimpleNamespace(fragments_removed=6, fragments_added=1))

    result = SimpleNamespace(refused=None, refused_by=None, fragments_removed=0, fragments_added=0, error=None, error_type=None)
    optimize_module._compact_files(
        cast(Any, SimpleNamespace(has_stable_row_ids=True)),
        cast(Any, result),
        uri="s3://b/t",
        refusal=None,
        target_rows_per_fragment=None,
        scan_batch_size=None,
        max_source_bytes=None,
        repack_mode=None,
        compact_threads=None,
        rewrite_slots=1,
        table_id="ns$t",
    )
    assert result.fragments_removed == 6
    assert counted == [1], f"the in-pod pass was not counted: {counted}"


def test_the_IN_POD_path_does_NOT_count_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. A compaction that moved nothing allocated nothing, and counting it would retire a
    worker that has spent no budget — which on an estate where nearly every unit is a no-op would
    restart the worker constantly."""
    from types import SimpleNamespace
    from typing import Any, cast

    from maintenance.services import optimize as optimize_module

    counted: list[int] = []
    monkeypatch.setattr(optimize_module, "record_committed_rewrite", lambda: (counted.append(1), len(counted))[1])
    monkeypatch.setattr(optimize_module, "_rewrite", lambda *_a, **_k: SimpleNamespace(fragments_removed=0, fragments_added=0))

    result = SimpleNamespace(refused=None, refused_by=None, fragments_removed=0, fragments_added=0, error=None, error_type=None)
    optimize_module._compact_files(
        cast(Any, SimpleNamespace(has_stable_row_ids=True)),
        cast(Any, result),
        uri="s3://b/t",
        refusal=None,
        target_rows_per_fragment=None,
        scan_batch_size=None,
        max_source_bytes=None,
        repack_mode=None,
        compact_threads=None,
        rewrite_slots=1,
        table_id="ns$t",
    )
    assert counted == []


# --------------------------------------------------------------------------- #
# THE LANE THAT GROWS IS THE LANE THE COUNT CANNOT SEE
# --------------------------------------------------------------------------- #


def test_a_worker_holding_its_budget_retires_even_with_zero_commits() -> None:
    """The pass count bounds the COMMITTING lane and the estate's workers do not commit.

    Measured live 2026-09-25: two `rask-maintenance-worker` pods at **866 and 861 MiB** against a 4Gi
    limit, **0 restarts across 34.5 hours**, and `maintenance_worker_retiring` never once logged —
    while `compaction_versions_removed_total = 0` and `compaction_bytes_reclaimed_total = 0` over 374
    no-op plans per worker per thirty minutes. `record_committed_rewrite` therefore never fires,
    `passes_committed()` stays 0, and the retirement this module exists for has never run on the only
    lane that actually grows.

    The per-pass figure is not wrong, it is for a different lane: ~12 MiB per COMMIT where commits
    happen, ~1.06 KiB per dataset-operation where they do not (two pods agreeing to 1% over 270,000
    operations each). One budget cannot be both, so the gate is on the thing both lanes share — the
    resident set against the container's own limit.
    """
    from maintenance.services.rewrite_slot import should_retire_for_memory

    four_gi = 4 * 1024**3
    assert should_retire_for_memory(int(four_gi * 0.71), limit=four_gi, fraction=0.70) is True


def test_a_worker_below_its_memory_budget_stays() -> None:
    """The control. Without it the assertion above passes on a gate that retires on every unit."""
    from maintenance.services.rewrite_slot import should_retire_for_memory

    four_gi = 4 * 1024**3
    assert should_retire_for_memory(920 * 1024**2, limit=four_gi, fraction=0.70) is False


@pytest.mark.parametrize("fraction", [0.0, -0.5])
def test_a_non_positive_fraction_is_off_rather_than_always_on(fraction: float) -> None:
    """Same posture as the pass mark: a misconfiguration fails toward NOT recycling, because the
    opposite reading turns a typo into a worker that exits after its first unit."""
    from maintenance.services.rewrite_slot import should_retire_for_memory

    assert should_retire_for_memory(4 * 1024**3, limit=4 * 1024**3, fraction=fraction) is False


@pytest.mark.parametrize("limit,rss", [(-1, 4 * 1024**3), (0, 4 * 1024**3), (4 * 1024**3, -1)])
def test_an_unreadable_reading_disables_the_gate(limit: int, rss: int) -> None:
    """`resident_bytes` and the cgroup reader both answer -1 rather than raising, so the gate has to
    treat a missing number as "cannot say" — never as "budget spent"."""
    from maintenance.services.rewrite_slot import should_retire_for_memory

    assert should_retire_for_memory(rss, limit=limit, fraction=0.70) is False


def test_the_limit_comes_from_the_CGROUP_and_not_from_the_host(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """A worker's budget is its container's, and reading the host's RAM would size it for a machine
    nobody runs it on — the same mistake `MALLOC_ARENA_MAX` made on this row.

    `max` is cgroup v2's spelling of "no limit" and must read as -1, or an unlimited pod retires on
    the first unit.
    """
    from maintenance.services import rewrite_slot

    v2 = tmp_path / "memory.max"
    v2.write_text("4294967296\n", encoding="utf-8")
    monkeypatch.setattr(rewrite_slot, "_CGROUP_V2_MAX", v2)
    rewrite_slot.container_memory_limit.cache_clear()
    assert rewrite_slot.container_memory_limit() == 4 * 1024**3

    v2.write_text("max\n", encoding="utf-8")
    rewrite_slot.container_memory_limit.cache_clear()
    assert rewrite_slot.container_memory_limit() == -1

    monkeypatch.setattr(rewrite_slot, "_CGROUP_V2_MAX", tmp_path / "absent")
    monkeypatch.setattr(rewrite_slot, "_CGROUP_V1_LIMIT", tmp_path / "also-absent")
    rewrite_slot.container_memory_limit.cache_clear()
    assert rewrite_slot.container_memory_limit() == -1
    rewrite_slot.container_memory_limit.cache_clear()


def test_the_HANDLER_retires_on_memory_with_zero_committed_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hop, and the one that was missing. `should_retire_for_memory` being correct says nothing
    about `handle_unit` consulting it — and the live defect is exactly that hop: the handler asked
    only `passes_committed()`, which is 0 forever on this lane, so a worker walked to its OOM with a
    working mitigation in the same file.

    The ORDER still matters for the same reason it does above: the signal comes after the ack.
    """
    import asyncio
    from types import SimpleNamespace
    from typing import Any, cast

    from maintenance.api import work as work_module

    order: list[str] = []

    async def _no_lineage(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(work_module, "execute_unit", lambda item, **_k: SimpleNamespace(error_type=None, uri=item.uri, table_id=item.table_id))
    monkeypatch.setattr(work_module, "emit_sweep_lineage", _no_lineage)
    monkeypatch.setattr(work_module, "ack_for", lambda _r: (order.append("acked"), "SUCCESS")[1])
    monkeypatch.setattr(work_module.base_refs, "sibling_base_refs", lambda *_a, **_k: SimpleNamespace(is_protected=lambda _u: False))
    monkeypatch.setattr(work_module, "passes_committed", lambda: 0)
    monkeypatch.setattr(work_module, "memory_readings", lambda: {"rss_bytes": int(4 * 1024**3 * 0.9), "python_blocks": 1, "session_bytes": 1})
    monkeypatch.setattr(work_module, "container_memory_limit", lambda: 4 * 1024**3)
    monkeypatch.setattr(work_module, "retire_this_worker", lambda *, passes, reason: order.append(f"retired@{passes}:{reason}"))

    settings = cast(Any, SimpleNamespace(storage_options=lambda: {}, delimiter="$", recycle_after_passes=200, recycle_at_memory_fraction=0.70))
    got = asyncio.run(work_module.handle_unit({"data": {"uri": "s3://b/t", "table_id": "ns$t", "plan": {}}}, settings, cast(Any, object())))

    assert got == {"status": "SUCCESS"}, got
    assert order == ["acked", "retired@0:memory"], f"a no-commit worker over its budget must still leave, after the ack: {order}"


def test_the_handler_stays_when_BOTH_budgets_are_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for the hop: zero passes and a resident set well under the limit is the estate's
    normal state, and it must not retire on every unit."""
    import asyncio
    from types import SimpleNamespace
    from typing import Any, cast

    from maintenance.api import work as work_module

    retired: list[str] = []

    async def _no_lineage(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(work_module, "execute_unit", lambda item, **_k: SimpleNamespace(error_type=None, uri=item.uri, table_id=item.table_id))
    monkeypatch.setattr(work_module, "emit_sweep_lineage", _no_lineage)
    monkeypatch.setattr(work_module, "ack_for", lambda _r: "SUCCESS")
    monkeypatch.setattr(work_module.base_refs, "sibling_base_refs", lambda *_a, **_k: SimpleNamespace(is_protected=lambda _u: False))
    monkeypatch.setattr(work_module, "passes_committed", lambda: 0)
    monkeypatch.setattr(work_module, "memory_readings", lambda: {"rss_bytes": 920 * 1024**2, "python_blocks": 1, "session_bytes": 1})
    monkeypatch.setattr(work_module, "container_memory_limit", lambda: 4 * 1024**3)
    monkeypatch.setattr(work_module, "retire_this_worker", lambda *, passes, reason: retired.append(reason))

    settings = cast(Any, SimpleNamespace(storage_options=lambda: {}, delimiter="$", recycle_after_passes=200, recycle_at_memory_fraction=0.70))
    asyncio.run(work_module.handle_unit({"data": {"uri": "s3://b/t", "table_id": "ns$t", "plan": {}}}, settings, cast(Any, object())))
    assert retired == []


def test_the_INDEX_lane_asks_the_same_two_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both lanes share one process, so both have to ask — and the required `reason` keyword is what
    surfaced this second call site at all.

    The index lane counts no pass (it builds indices, not rewrites), so on a worker that has never
    committed a rewrite its only possible answer was 0 and its retirement was dead in exactly the way
    the work lane's was. An index build holds the process for up to an hour, which is the case a
    commit counter is least able to see.
    """
    import asyncio
    from types import SimpleNamespace
    from typing import Any, cast

    from maintenance.api import index_work as index_module

    retired: list[str] = []

    async def _emit(*_a: Any, **_k: Any) -> None:
        return None

    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.index_build import IndexOutcome
    from service_kit.lakehouse.work_items import SCALAR_INDEX

    monkeypatch.setattr(index_module.credentials, "write_options_for", lambda *_a, **_k: {})
    monkeypatch.setattr(index_module, "build_index", lambda *_a, **_k: IndexOutcome(name="id_idx", column="id", kind=SCALAR_INDEX, version=3))
    monkeypatch.setattr(index_module, "passes_committed", lambda: 0)
    monkeypatch.setattr(index_module, "memory_readings", lambda: {"rss_bytes": int(4 * 1024**3 * 0.9), "python_blocks": 1, "session_bytes": 1})
    monkeypatch.setattr(index_module, "container_memory_limit", lambda: 4 * 1024**3)
    monkeypatch.setattr(index_module, "retire_this_worker", lambda *, passes, reason: retired.append(reason))

    # The REAL settings object, not a stand-in: the whole point of this leg is that the handler reads
    # `recycle_at_memory_fraction` off it, and a SimpleNamespace would simply grow whatever attribute
    # the assertion needed while the shipped default went unchecked.
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    assert settings.recycle_at_memory_fraction > 0, "the shipped default disables the gate this leg proves"
    emitter = cast(Any, SimpleNamespace(emit_maintenance=_emit))
    event = {"data": {"uri": "s3://b/t.lance", "table_id": "", "column": "id", "kind": SCALAR_INDEX, "index_type": "BTREE", "name": "id_idx"}}
    asyncio.run(index_module.handle_index_unit(event, settings, emitter))

    assert retired == ["memory"], f"the index lane must leave on the budget it can actually reach: {retired}"
