"""The window fragment batching opened, and the in-cluster kill test cannot reach.

`staging.py` argues its own correctness from one premise: a retried unit overwrites its own manifest
and nothing else, so a redelivered unit converges instead of double-committing. That premise held
while a fragment covered exactly one unit. Batching made a fragment cover N, and `flush()` staged it
under `units[0][0]` — one arbitrary member of the batch — leaving the other N-1 with no manifest at
all. A redelivered unit then had nothing of its own to overwrite, so the old fragment and the new one
both survived into `discover_staged`, and the lander appended both: FOUR units in, SIX rows out.

Nothing downstream would have caught it. The commit is a blind `LanceOperation.Append`, and
`merge_insert` — the one thing that would collapse duplicate rows — is forbidden by
`tests/unit/test_ingest_invariants.py`.

A3 cannot reach this. It kills the pod immediately after the 202, so the crash lands before any
flush; and the window is one ack round trip wide, which no wall-clock kill can be aimed at. Hence a
deterministic test at the staging seam, where the invariant actually lives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ingest.staging import StagingOverlapError, discover_staged, stage_fragments


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


def test_the_choice_does_not_depend_on_which_manifest_is_read_first(tmp_path: Path) -> None:
    """Staging is a prefix listing, and a listing has no meaningful order.

    If selection depended on read order, the same crash would commit 4 rows or 6 depending on how a
    store happened to enumerate two objects — the worst kind of bug, since a local filesystem and S3
    would disagree.
    """
    dataset = str(tmp_path / "bronze")

    stage_fragments(dataset, RUN, ["u2", "u3"], [FRAGMENT_G])
    stage_fragments(dataset, RUN, ["u0", "u1", "u2", "u3"], [FRAGMENT_F])

    assert discover_staged(dataset, RUN) == [FRAGMENT_F]


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


def test_disjoint_batches_all_commit(tmp_path: Path) -> None:
    """The ordinary path must not be narrowed by the overlap rule.

    A run is normally many batches that share no units, and every one of them has to land. A
    selection rule that drops a fragment because another exists would silently truncate every
    multi-batch run — a far worse failure than the one it is guarding against.
    """
    dataset = str(tmp_path / "bronze")

    stage_fragments(dataset, RUN, ["u0", "u1"], ['{"n":1,"units":["u0","u1"]}'])
    stage_fragments(dataset, RUN, ["u2", "u3"], ['{"n":2,"units":["u2","u3"]}'])
    stage_fragments(dataset, RUN, ["u4"], ['{"n":3,"units":["u4"]}'])

    staged = discover_staged(dataset, RUN)

    assert _rows(staged) == 5
    assert len(staged) == 3


def test_an_UNDECIDABLE_overlap_raises_rather_than_guessing(tmp_path: Path) -> None:
    """Partial overlap in both directions is the one case no selection can fix.

    F={u0..u3} and H={u2,u3,u4,u5} each hold rows the other lacks. Committing both duplicates u2 and
    u3; committing either alone loses two units. Since a fragment cannot be split, the only honest
    answer is to stop with every byte still on the store and named by its manifest.

    This state is REACHABLE — an earlier version of this docstring said the worker prevented it, and
    that was wrong. `drain_chunk` separates redelivered messages from fresh ones within one fetch,
    but batches all redeliveries in that fetch TOGETHER regardless of which original batch each came
    from, so two batches' remainders merge into one fragment that overlaps both and is contained by
    neither. Two crashed batches whose remainders arrive in the same fetch produce exactly this.

    So the raise is an operational state, not a broken-invariant alarm. It is still the right
    answer — the alternatives are silent duplication or silent loss — but a finalizer that can
    resolve a partial overlap, or a batching rule that preserves the original grouping on
    redelivery, would remove the state instead of reporting it. Both are open work.
    """
    dataset = str(tmp_path / "bronze")

    stage_fragments(dataset, RUN, ["u0", "u1", "u2", "u3"], [FRAGMENT_F])
    stage_fragments(dataset, RUN, ["u2", "u3", "u4", "u5"], ['{"id":0,"units":["u2","u3","u4","u5"]}'])

    with pytest.raises(StagingOverlapError, match="u4"):
        discover_staged(dataset, RUN)


def test_a_manifest_from_BEFORE_batching_still_reads(tmp_path: Path) -> None:
    """A pod carrying the old code may have staged single-unit manifests that a new pod finalizes.

    Those records carry `unit` and no `units`. One unit is a one-element set, so they need no
    migration — but only if the reader looks for the old field, and a crash mid-upgrade is exactly
    when it matters.
    """
    dataset = str(tmp_path / "bronze")
    root = tmp_path / "bronze" / "_ingest_staging" / RUN
    root.mkdir(parents=True)
    (root / "legacy.json").write_text(json.dumps({"unit": "u9", "fragments": ['{"units":["u9"]}']}), encoding="utf-8")

    assert discover_staged(dataset, RUN) == ['{"units":["u9"]}']
