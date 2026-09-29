"""The window fragment batching opened, and the in-cluster kill test cannot reach.

`staging.py` argues its own correctness from one premise: a retried unit overwrites its own manifest
and nothing else, so a redelivered unit converges instead of double-committing. That premise held
while a fragment covered exactly one unit. Batching made a fragment cover N, and `flush()` staged it
under `units[0][0]` — one arbitrary member of the batch — leaving the other N-1 with no manifest at
all. A redelivered unit then had nothing of its own to overwrite, so the old fragment and the new one
both survived into `discover_staged`, and the lander appended both: FOUR units in, SIX rows out.

Nothing downstream would have caught it. The commit is a blind `LanceOperation.Append`, and
`merge_insert` — the one thing that would collapse duplicate rows — cannot take the staged fragments:
it coerces a reader of rows, and the lander holds `FragmentMetadata` (`staging.py` records the measurement).

A3 cannot reach this. It kills the pod immediately after the 202, so the crash lands before any
flush; and the window is one ack round trip wide, which no wall-clock kill can be aimed at. Hence a
deterministic test at the staging seam, where the invariant actually lives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest.staging import discover_staged, stage_fragments


RUN = "run-partial-ack"

#: A fragment's JSON is opaque to staging, so these stand in for real `FragmentMetadata` — the test
#: is about WHICH fragments are selected, and naming them for their units keeps the assertions
#: readable when one fails.
FRAGMENT_F = '{"id":0,"units":["u0","u1","u2","u3"]}'
FRAGMENT_G = '{"id":0,"units":["u2","u3"]}'


def _rows(fragments: list[str]) -> int:
    """How many rows the lander would append, given that it commits each fragment whole."""
    return sum(len(json.loads(fragment)["units"]) for fragment in fragments)


def test_a_partially_acked_batch_commits_each_unit_ONCE(tmp_path: Path) -> None:
    """The defect, stated as arithmetic: four units fetched must be four rows committed.

    Batch one covers u0..u3 and is staged as fragment F. The ack loop gets through u0 and u1, then
    the pod dies. u2 and u3 are still owed, come back as their own batch, and are written again as
    fragment G — whose rows F already holds.

    F is the one to keep: a fragment commits whole, and F covers everything G does plus the two units
    that were already acked and will never be redelivered. Dropping F to keep G would lose them.
    """
    dataset = str(tmp_path / "bronze")

    stage_fragments(dataset, RUN, ["u0", "u1", "u2", "u3"], [FRAGMENT_F])
    stage_fragments(dataset, RUN, ["u2", "u3"], [FRAGMENT_G])

    staged = discover_staged(dataset, RUN)

    assert _rows(staged) == 4, f"the run fetched 4 units but the lander would commit {_rows(staged)} rows from {staged}"
    assert staged == [FRAGMENT_F], "the superseding fragment must be the one that covers every unit"


def test_re_running_the_SAME_batch_overwrites_its_own_manifest(tmp_path: Path) -> None:
    """The convergence the module has always claimed, now keyed on the set rather than one member.

    A batch redelivered whole writes a second fragment; only the newest may commit. Both hash to the
    same manifest name, so the retry replaces the record instead of adding one — which is why the
    orphaned first fragment is never selected.
    """
    dataset = str(tmp_path / "bronze")
    batch = ["u0", "u1", "u2", "u3"]

    stage_fragments(dataset, RUN, batch, ['{"attempt":1,"units":["u0","u1","u2","u3"]}'])
    stage_fragments(dataset, RUN, list(reversed(batch)), ['{"attempt":2,"units":["u0","u1","u2","u3"]}'])

    staged = discover_staged(dataset, RUN)

    assert len(staged) == 1, f"the same batch staged twice produced {len(staged)} fragments: {staged}"
    assert json.loads(staged[0])["attempt"] == 2


# ── the same defect, one layer up ─────────────────────────────────────────────


def test_finalize_does_not_OVERRULE_the_exact_cover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`discover_staged` selects; `finalize_run` used to un-select.

    The selection above is only half the fix. `finalize_run` computed
    `[*discover_staged(...), *fragments]` — the exact cover UNIONED with the fragment list the
    workflow carried — deduplicated by string identity. Every carried fragment was staged first
    (`worker.py` calls `stage_fragments` on the line immediately before `outcome.fragments.extend`),
    so the carried list can contribute exactly one thing the cover does not already account for: a
    fragment the cover deliberately SUPERSEDED.

    Re-adding it commits both, which is precisely the "four units in, six rows out" arithmetic the
    tests above close — reintroduced one layer above the layer that closed it. The scenario is the
    ordinary partial-ack one: batch F covers u0..u3, the pod dies mid-ack, u2/u3 come back and are
    written as G, and the workflow's outcome still carries G from the attempt that produced it.
    """
    from ingest import runtime

    dataset = str(tmp_path / "bronze")
    stage_fragments(dataset, RUN, ["u0", "u1", "u2", "u3"], [FRAGMENT_F])
    stage_fragments(dataset, RUN, ["u2", "u3"], [FRAGMENT_G])

    # What the finalizer is handed: the cover picks F alone, but the workflow still carries G.
    assert discover_staged(dataset, RUN) == [FRAGMENT_F]

    committed: list[list[str]] = []

    class _Catalog:
        def ensure(self, project: str, dataset_name: str) -> str:
            return dataset

    class _Lander:
        def __init__(self, catalog: object) -> None: ...
        # `read_version` is accepted (and ignored — this test is about the fragment cover) because the
        # real `Lander.commit_fragments` takes it: a structural fake that omits a parameter the caller
        # passes fails as a TypeError from inside the code under test, which reads as a product bug.
        def commit_fragments(self, uri: str, frags: list[str], *, run_id: str, read_version: int | None = None) -> object:
            committed.append(list(frags))
            from ingest.lander import CommitResult

            return CommitResult(dataset_uri=uri, version=2, rows=_rows(frags), rows_added=_rows(frags), fragments_committed=len(frags))

    monkeypatch.setattr(runtime, "_catalog", lambda: _Catalog())
    monkeypatch.setattr("ingest.lander.Lander", _Lander)
    monkeypatch.setattr(runtime, "purge_staged", lambda *a, **k: None, raising=False)

    from ingest.workflow import RunSpec

    runtime.finalize_run(RunSpec(run_id=RUN, kind="local-dir", project="p", dataset="d"), [FRAGMENT_G], {})

    assert committed, "finalize never reached the commit"
    assert _rows(committed[0]) == 4, f"four units were fetched but {_rows(committed[0])} rows would commit from {committed[0]}"
    assert committed[0] == [FRAGMENT_F], "the carried, superseded fragment was re-added over the exact cover"
