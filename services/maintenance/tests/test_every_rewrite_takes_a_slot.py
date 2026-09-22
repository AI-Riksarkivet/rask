"""Every call that rewrites bytes passes the MEMORY bound, and none of them defaults it.

The bound only works if every path to a rewrite contends for the same semaphore. There are two paths
— `compact_distributed` (plan elsewhere, rewrite here) and `compact_one` (in-pod) — and this estate
has already shipped exactly this defect once: `create_table` reached `_write_blob` at two sites and
only one was wired, with every test green, because no case drove the other branch.

READ AS AN AST, not by importing and calling: a runtime check would exercise whichever branch the
fixture happens to take, which is the same blindness that let the half-wiring through.

`rewrite_slots` is REQUIRED on `compact_distributed` on purpose. A defaulted parameter makes an
un-wired chain look clean — the caller compiles, the tests pass, and the bound silently is not the
one the chart declares.
"""

from __future__ import annotations

import ast
import pathlib


SRC = pathlib.Path(__file__).resolve().parents[1] / "src/maintenance"
SWEEP = SRC / "services/sweep.py"

#: The functions that reach a rewrite. Each must be handed the bound, from settings, at every call.
_REWRITERS = {"compact_distributed", "compact_one"}


def _calls_to(name: str, tree: ast.AST) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        label = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if label == name:
            found.append(node)
    return found


def test_the_walk_finds_both_rewrite_paths() -> None:
    """An empty walk would pass everything — the shape this file is about."""
    tree = ast.parse(SWEEP.read_text(encoding="utf-8"))
    for name in _REWRITERS:
        assert _calls_to(name, tree), f"no call to {name} found in sweep.py; the walk is reading the wrong thing"


def test_every_rewrite_call_passes_the_bound_FROM_SETTINGS() -> None:
    """Not merely present — sourced from the setting, so the chart's number is the one in force."""
    tree = ast.parse(SWEEP.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for name in _REWRITERS:
        for call in _calls_to(name, tree):
            passed = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            slot = passed.get("rewrite_slots")
            if slot is None:
                offenders.append(f"{name} at line {call.lineno} passes no rewrite_slots")
                continue
            source = ast.unparse(slot)
            if "max_concurrent_compactions" not in source:
                offenders.append(f"{name} at line {call.lineno} passes rewrite_slots={source}, not the setting")

    assert not offenders, (
        "these rewrite calls do not carry the memory bound from settings, so the semaphore they "
        "contend for is not the one the chart declares:\n  " + "\n  ".join(offenders)
    )


def test_the_bound_is_NOT_the_throughput_number() -> None:
    """The mistake this separation exists to prevent, asserted where someone would make it again.

    `max_concurrent_units` sizes anyio's thread limiter — a THROUGHPUT bound. Using it here throttled
    the whole lane: measured 2026-09-22, the drain fell to ~0.67 units/sec against the 4.7 it must
    sustain and `Unprocessed Messages` climbed past 2,000 with nothing draining.
    """
    source = SWEEP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in _REWRITERS:
        for call in _calls_to(name, tree):
            for kw in call.keywords:
                if kw.arg == "rewrite_slots":
                    assert "max_concurrent_units" not in ast.unparse(kw.value), (
                        f"{name} at line {call.lineno} bounds MEMORY with the THROUGHPUT setting; a no-op "
                        "unit holds no bytes, and throttling the lane to the rewrite ceiling stalls it"
                    )
