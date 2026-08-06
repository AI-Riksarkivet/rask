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


# ── the selection must not refuse work it can actually do ─────────────────────────────


def _exact_cover_exists(sets: list[frozenset[str]], universe: frozenset[str]) -> bool:
    """Brute force: is there ANY subset of these fragments covering every unit exactly once?

    Exponential and deliberately so — it is the independent oracle the selection is judged against,
    and reusing the selection's own logic to score the selection would prove nothing.
    """
    from itertools import combinations

    for size in range(1, len(sets) + 1):
        for combo in combinations(sets, size):
            covered: set[str] = set()
            for s in combo:
                if s & covered:
                    break
                covered |= s
            else:
                if covered == universe:
                    return True
    return False


def test_selection_does_not_REFUSE_an_input_it_can_resolve(tmp_path: Path) -> None:
    """The measured defect: the sweep raised on 24% of inputs that HAD a valid exact cover.

    The plainest case is three equal fragments, A={u0,u1} B={u1,u2} C={u2,u3}. The old loop took A,
    met B, and raised on the spot — while A+C covers every unit exactly once and was sitting right
    there. Raising mid-sweep never gave C the chance.

    Fuzzed against a brute-force oracle rather than against hand-picked cases, because the failure is
    about ORDER and hand-picked examples are chosen by the same intuition that wrote the bug. Any
    input the oracle says is resolvable must not raise.
    """
    import random

    # Seeded, so a failure is reproducible — the whole value of a fuzz here is that it explores
    # orderings a hand-picked case would not, and an unreproducible one cannot be debugged.
    rng = random.Random(20260804)  # noqa: S311 — shuffling test inputs, not minting secrets
    universe = [f"u{i}" for i in range(6)]
    refused_but_resolvable = 0
    resolvable = 0

    for case in range(400):
        # Random batches over a small universe — the shape a partially-acked run produces.
        sets = []
        for _ in range(rng.randint(2, 4)):
            k = rng.randint(1, 4)
            sets.append(frozenset(rng.sample(universe, k)))
        covered_universe = frozenset().union(*sets)
        if not _exact_cover_exists(sets, covered_universe):
            continue
        resolvable += 1

        dataset = str(tmp_path / f"case{case}")
        for index, unit_set in enumerate(sets):
            units = sorted(unit_set)
            fragment = json.dumps({"n": index, "units": units})
            stage_fragments(dataset, RUN, units, [fragment])

        try:
            discover_staged(dataset, RUN)
        except StagingOverlapError:
            refused_but_resolvable += 1

    assert resolvable > 50, f"the fuzz produced only {resolvable} resolvable cases — it is testing nothing"
    assert refused_but_resolvable == 0, (
        f"{refused_but_resolvable}/{resolvable} resolvable inputs were REFUSED — the sweep is raising on work it could have done"
    )


def test_a_TRULY_unresolvable_overlap_still_raises(tmp_path: Path) -> None:
    """Deferring the judgement must not soften it.

    F={u0..u3} against H={u2,u3,u4,u5}: neither contains the other, committing both duplicates u2/u3,
    committing either alone loses two units. No selection resolves that, and it must still stop —
    with every byte on the store and named by its manifest.
    """
    dataset = str(tmp_path / "bronze")

    stage_fragments(dataset, RUN, ["u0", "u1", "u2", "u3"], [FRAGMENT_F])
    stage_fragments(dataset, RUN, ["u2", "u3", "u4", "u5"], ['{"id":0,"units":["u2","u3","u4","u5"]}'])

    with pytest.raises(StagingOverlapError, match="u4"):
        discover_staged(dataset, RUN)


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
        def commit_fragments(self, uri: str, frags: list[str], *, run_id: str) -> object:
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


def test_SINGLETON_redeliveries_keep_the_staged_family_LAMINAR(tmp_path: Path) -> None:
    """The state the exact cover could only REPORT is now structurally unreachable.

    The reachable scenario, from `drain_chunk`: two batches crash, and their remainders arrive in the
    SAME fetch. Batching all redeliveries together produced ONE fragment spanning both remainders —
    F={u0..u3} against H={u2,u3,u4,u5}, neither containing the other, so no selection is correct and
    `discover_staged` could only raise and strand the run.

    Staging each redelivered unit ALONE fixes it by construction: a singleton is contained by any
    fragment holding it, so every pair in the staged family is nested or disjoint — laminar — and an
    exact cover always exists. This test stages what the new worker loop stages and asserts the
    finalizer resolves it rather than raising.

    The unresolvable SHAPE itself is pinned by `test_a_TRULY_unresolvable_overlap_still_raises`
    above — F={u0..u3} against H={u2,u3,u4,u5} with nothing else covering u4,u5. What the singleton
    rule removes is the only way `drain_chunk` could ever CREATE an H: it no longer emits a fragment
    spanning more than one redelivered unit, so a merged remainder cannot exist to overlap anything.
    """
    dataset = str(tmp_path / "bronze")

    # Two batches that crashed mid-ack.
    stage_fragments(dataset, RUN, ["u0", "u1", "u2", "u3"], ['{"n":"F","units":["u0","u1","u2","u3"]}'])
    stage_fragments(dataset, RUN, ["u4", "u5", "u6"], ['{"n":"K","units":["u4","u5","u6"]}'])

    # Their remainders come back together in one fetch. The OLD loop batched them into a single
    # fragment {u2,u3,u4,u5}; the new one stages each alone.
    for unit in ("u2", "u3", "u4", "u5"):
        stage_fragments(dataset, RUN, [unit], [f'{{"n":"{unit}","units":["{unit}"]}}'])

    staged = discover_staged(dataset, RUN)

    # Resolvable, and exactly once per unit — the singletons are all superseded by F and K.
    assert _rows(staged) == 7, f"7 units staged but {_rows(staged)} rows would commit from {staged}"
    covered = [u for f in staged for u in json.loads(f)["units"]]
    assert sorted(covered) == ["u0", "u1", "u2", "u3", "u4", "u5", "u6"]
    assert len(covered) == len(set(covered)), "a unit would commit twice"
