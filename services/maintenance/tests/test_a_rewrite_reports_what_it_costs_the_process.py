"""Every committed rewrite reports the process's cumulative pass count and its resident bytes.

[[LH-183]] measured what a rewrite costs and the answer is not transient: a compaction PASS leaves
**~10-14 MiB resident that the process never returns** (9.6 / 14.4 / 54.2 MiB across runs of 1, 1 and
4 commits), while the peak — 644, 724, 677Mi — releases cleanly. So a worker's floor rises with every
pass, and at ~12 MiB a pass a 4Gi pod over a ~300Mi baseline affords roughly 300 of them.

THAT MAKES THE PASS COUNT THE ONE NUMBER THAT PREDICTS THE OOM, and nothing emitted it. The planner
reports its own memory every tick (`memory_readings`), but the REWRITE path — the only place the
retention happens — reported none of it: an operator could see a worker was at 3Gi and had no way to
know whether that was forty passes or four hundred.

MEASURED ONCE BY AN EXPERIMENT IS NOT MEASURED. The row's closing bar asks that what bounds the worker
be "named and measured rather than inferred"; a figure established by one afternoon's fixture tables
is named, but it is not being watched. `passes x 12 MiB` against the reported RSS is a prediction the
estate can now check continuously, and a divergence is the interesting signal either way.

COUNTED PER COMMIT, not per unit or per dataset, because that is what the measurement says the cost
tracks: two runs over identically-shaped tables differed 4x in retention and 4x in commits.
"""

from __future__ import annotations

import ast
import pathlib


SRC = pathlib.Path(__file__).resolve().parents[1] / "src/maintenance"
EXECUTOR = SRC / "services/compaction_executor.py"


def _committed_log_call() -> ast.Call:
    """The `log.info("compaction_distributed_committed", ...)` call, as an AST node."""
    tree = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == "compaction_distributed_committed":
            return node
    raise AssertionError("no compaction_distributed_committed log call found; the walk is reading the wrong thing")


def test_the_walk_finds_the_commit_log() -> None:
    """An absent call would make the assertions below vacuous rather than false."""
    assert _committed_log_call() is not None


def _extra_keys() -> set[str]:
    call = _committed_log_call()
    for kw in call.keywords:
        if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
            return {k.value for k in kw.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


def test_a_committed_rewrite_reports_the_PASS_COUNT() -> None:
    """The number that predicts the OOM, on the line that causes it."""
    assert "rewrite_passes" in _extra_keys(), (
        "the commit log carries no `rewrite_passes`, so nothing says how many rewrites this process "
        "has done — and at ~12 MiB retained per pass that count is what predicts when it dies"
    )


def test_a_committed_rewrite_reports_RESIDENT_BYTES() -> None:
    """Beside the count, because the pair is the measurement: passes x ~12 MiB should track RSS."""
    assert "rss_bytes" in _extra_keys(), (
        "the commit log carries no `rss_bytes`, so the predicted retention cannot be compared with what the process actually holds"
    )
