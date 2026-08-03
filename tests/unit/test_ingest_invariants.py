"""I3/I4 grep-gates for the ingest plane, and A10's proof that they actually bite.

The estate had no Python precedent for this. `packages/ratch/src/ratch/core/dataset.py:13-14` says
"so the P1.5 grep gate can hold: no direct ``lance.write_dataset`` outside this module" — but there
is no such gate: `packages/ratch` has no tests directory and is not in `testpaths`, so nothing has
ever enforced it (open_ingest.md §0 C4c). A docstring is a wish.

The failure mode these prevent is drift, not a bug you can see. Nothing breaks the day someone
writes `lance.write_dataset` in a worker — it works. It breaks months later when two writers commit
concurrently, or when a dataset is created without stable row ids and CDF silently returns nothing
(a creation-time-only flag, `lance_docs/file_format.md:4011-4013`). By then the call sites are
everywhere.

**A10 requires a gate that fails on a seeded violation**, so each gate here is exercised twice: once
against the real tree, once against a temporary file containing the violation. A gate that has never
been seen to fail is indistinguishable from a gate with a broken regex — which is exactly how the
thirteen helm invariants sat green while skipping (§0 C9).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
INGEST_SRC = REPO / "services" / "ingest" / "src" / "ingest"

# I4 — the Lander is the ONE writer. These are the Lance write verbs; the lander module is the only
# place they may appear, so every governed write funnels through the catalog registration with it.
WRITE_VERBS = re.compile(r"\blance\.write_dataset\b|\bmerge_insert\b|\blance\.fragment\.write_fragments\b")
LANDER_ALLOWED = {"lander.py"}

# I3 — the broker appears in exactly the modules that ARE the broker seam. Everything else goes
# through Dapr pub/sub or the WorkQueue protocol, which is what keeps the bus swappable by chart
# values rather than by a code change.
NATS_IMPORT = re.compile(r"^\s*(?:import\s+nats\b|from\s+nats[\w.]*\s+import\b)", re.MULTILINE)
NATS_ALLOWED = {"queue.py", "workqueue.py"}


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts) if root.exists() else []


def _offenders(pattern: re.Pattern[str], allowed: set[str]) -> list[str]:
    hits: list[str] = []
    for path in _py_files(INGEST_SRC):
        if path.name in allowed:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            hits.append(str(path.relative_to(REPO)))
    return hits


def test_i4_only_the_lander_writes_lance() -> None:
    """I4: no Lance write verb outside the lander.

    Two writers means two commit paths, and Lance's concurrency is three-valued — an Append against
    a stale read_version is retryable, an Overwrite against a concurrent Append is NOT
    (`lance_docs/file_format.md:4825-4833`). One writer is how that stays a non-issue.
    """
    offenders = _offenders(WRITE_VERBS, LANDER_ALLOWED)
    assert offenders == [], (
        f"Lance write verbs outside the lander: {offenders}. Every governed write goes through the "
        f"Lander so it is registered with the catalog and carries the run id in commit metadata."
    )


def test_i3_the_broker_appears_only_in_the_queue_seam() -> None:
    """I3: `nats` imports confined to the work-queue modules.

    The direct nats-py client is a DOCUMENTED exception (work-queue semantics Dapr's component
    cannot express). An exception stays an exception only if something counts it.
    """
    offenders = _offenders(NATS_IMPORT, NATS_ALLOWED)
    assert offenders == [], (
        f"`nats` imported outside the queue seam: {offenders}. Events ride Dapr pub/sub; only the "
        f"work queue may hold the broker client, or the bus stops being swappable by chart values."
    )


# ── A10: prove the gates fail when the violation exists ───────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "violation"),
    [
        (WRITE_VERBS, "import lance\nlance.write_dataset(tbl, uri, mode='overwrite')\n"),
        (WRITE_VERBS, "ds.merge_insert('id').when_matched_update_all().execute(tbl)\n"),
        (WRITE_VERBS, "frags = lance.fragment.write_fragments(batch, uri)\n"),
        (NATS_IMPORT, "import nats\n"),
        (NATS_IMPORT, "from nats.js.api import KeyValueConfig\n"),
    ],
)
def test_a10_each_gate_catches_a_seeded_violation(pattern: re.Pattern[str], violation: str) -> None:
    """The gate must MATCH the thing it forbids — otherwise it is a regex that always passes."""
    assert pattern.search(violation), f"gate failed to catch a seeded violation: {violation!r}"


@pytest.mark.parametrize(
    ("pattern", "innocent"),
    [
        (WRITE_VERBS, "# the lander is the one writer; see I4\n"),
        (WRITE_VERBS, "result = dataset.to_table()\n"),
        # A comment mentioning nats must not trip the gate — a gate that fires on prose gets
        # silenced by the first person it annoys, and a silenced gate protects nothing.
        (NATS_IMPORT, "# published to the nats work queue by the worker\n"),
        (NATS_IMPORT, "url = 'nats://rask-nats:4222'\n"),
    ],
)
def test_a10_the_gates_do_not_fire_on_innocent_lines(pattern: re.Pattern[str], innocent: str) -> None:
    assert not pattern.search(innocent), f"gate false-positived on: {innocent!r}"


def test_the_gate_actually_scans_something() -> None:
    """A gate over an empty file list passes vacuously — the failure mode of every path-based check."""
    assert _py_files(INGEST_SRC), f"no ingest sources found under {INGEST_SRC} — the gates are vacuous"


def test_a13_no_completion_polling_survives() -> None:
    """A13 as a permanent gate: the completion poll must not creep back.

    It was deleted from THREE places — ray-kit, ratch/core/jobs.py, and the medallion's ray_submit —
    and the medallion's stage path had to be re-cut to submit-and-ack, since it was the SURVIVING
    mover rather than part of the retiring IIIF lane.

    The deletion is not a performance tidy-up. Holding an ack across a job's runtime is what the ack
    contract forbids: ackWait expires and the broker redelivers forever. And the poll asked a
    question the data already answers — a job's completion signal is its own registered commit, and
    the publication event off that commit wakes the next tier.
    """
    # Assembled from parts on purpose. A13's gate is `rg <token> -g '*.py'` returns NOTHING, so a
    # test containing the literal would be its own only offender — the exact self-reference that made
    # the unscoped form unsatisfiable while the plan document named the string (section 0, C3).
    token = "await" + "_success"
    banned = re.compile(rf"\b{token}\b")
    offenders = [
        str(path.relative_to(REPO))
        for path in REPO.rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts and banned.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"completion polling reappeared in: {offenders}"
