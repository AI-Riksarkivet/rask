"""Staging — the ledger that makes "fragment on disk before the ack" a recoverable promise.

The failure these cover is invisible by construction: a run completes, reports success, and holds
fewer rows than it fetched. Nothing errors, nothing logs, and the missing pages are only discoverable
by counting. So the tests assert on RECOVERY, not on the happy path — a staged fragment must survive
the process that staged it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest.staging import discover_staged, manifest_name, purge_staged, stage_fragments, staging_root


@pytest.fixture
def dataset(tmp_path: Path) -> str:
    return str(tmp_path / "bronze.lance")


def test_a_staged_fragment_is_discoverable_by_a_DIFFERENT_process(dataset: str) -> None:
    """The whole point: discovery reads storage, not the stager's memory.

    `stage_fragments` and `discover_staged` share no state here beyond the filesystem, which is what
    a finalize activity running on another pod after a crash actually has.
    """
    stage_fragments(dataset, "run-1", "file:///pages/0001.tif", ['{"id":0}'])

    assert discover_staged(dataset, "run-1") == ['{"id":0}']


def test_a_run_that_staged_nothing_discovers_nothing(dataset: str) -> None:
    """An absent prefix is an empty list, not an error.

    A run with zero units never stages, and raising here would turn a legitimate no-op run into a
    failed one — the exact over-strictness that makes an empty ingest look like a broken plane.
    """
    assert discover_staged(dataset, "never-ran") == []


def test_a_RETRIED_unit_overwrites_its_own_manifest_and_commits_once(dataset: str) -> None:
    """Keyed by unit, so a redelivery converges instead of double-committing.

    Pre-commit fragment ids all collide at 0, so identity cannot come from the fragment. It comes
    from the unit key — and the second attempt must replace the first attempt's record rather than
    adding to it, or the retry would commit the same page twice.
    """
    stage_fragments(dataset, "run-1", "file:///pages/0001.tif", ['{"attempt":1}'])
    stage_fragments(dataset, "run-1", "file:///pages/0001.tif", ['{"attempt":2}'])

    assert discover_staged(dataset, "run-1") == ['{"attempt":2}']


def test_fragments_from_units_that_a_DEAD_pod_acked_are_still_recovered(dataset: str) -> None:
    """A3's core claim, at the unit level.

    Units 1 and 2 were acked by a pod that then died — their fragments exist only in staging, because
    the drain that produced them never returned. Unit 3 was drained by the replacement. Finalize must
    commit all three; committing only what the surviving attempt handed back is the silent row loss.
    """
    for index in (1, 2):
        stage_fragments(dataset, "run-1", f"file:///pages/000{index}.tif", [f'{{"unit":{index}}}'])
    stage_fragments(dataset, "run-1", "file:///pages/0003.tif", ['{"unit":3}'])

    assert sorted(discover_staged(dataset, "run-1")) == ['{"unit":1}', '{"unit":2}', '{"unit":3}']


def test_a_TRUNCATED_manifest_is_skipped_not_fatal(dataset: str) -> None:
    """A half-written manifest means the pod died mid-write — so that unit was never acked.

    It is therefore still on the queue and will be refetched. Skipping is correct; failing the
    finalize over it would strand a run the queue can still complete, and would do so at the very
    end, after all the fetching had already been paid for.
    """
    root = Path(staging_root(dataset, "run-1"))
    root.mkdir(parents=True)
    (root / manifest_name("file:///pages/0001.tif")).write_text('{"unit": "trunc', encoding="utf-8")
    stage_fragments(dataset, "run-1", "file:///pages/0002.tif", ['{"ok":true}'])

    assert discover_staged(dataset, "run-1") == ['{"ok":true}']


def test_staging_is_scoped_per_run(dataset: str) -> None:
    """Two runs against one dataset must not see each other's uncommitted fragments.

    Concurrent runs on a shared dataset are normal — one volume per run, same bronze table. Without
    per-run scoping, run B's finalize would commit run A's in-flight fragments and both would report
    a version neither produced.
    """
    stage_fragments(dataset, "run-a", "file:///a.tif", ['{"run":"a"}'])
    stage_fragments(dataset, "run-b", "file:///b.tif", ['{"run":"b"}'])

    assert discover_staged(dataset, "run-a") == ['{"run":"a"}']
    assert discover_staged(dataset, "run-b") == ['{"run":"b"}']


def test_purge_clears_only_the_named_run(dataset: str) -> None:
    """Purge runs AFTER a commit lands, so it must not reach a run still in flight."""
    stage_fragments(dataset, "run-a", "file:///a.tif", ['{"run":"a"}'])
    stage_fragments(dataset, "run-b", "file:///b.tif", ['{"run":"b"}'])

    assert purge_staged(dataset, "run-a") == 1
    assert discover_staged(dataset, "run-a") == []
    assert discover_staged(dataset, "run-b") == ['{"run":"b"}']


def test_purging_a_run_twice_is_harmless(dataset: str) -> None:
    """Finalize is a retryable activity, so its purge runs more than once by design."""
    stage_fragments(dataset, "run-1", "file:///a.tif", ['{"x":1}'])

    assert purge_staged(dataset, "run-1") == 1
    assert purge_staged(dataset, "run-1") == 0


def test_a_source_key_with_slashes_and_spaces_is_a_legal_manifest_name(dataset: str) -> None:
    """Source keys are URLs: slashes, query strings, unicode. The manifest name must be flat.

    A naive name derived from the key would nest directories under the staging root (or be rejected
    outright by an object store), and the overwrite-on-retry behaviour would then depend on how a
    given store normalises a path.
    """
    key = "https://lbiiif.riksarkivet.se/arkis!A0068688/sida 1/full/max/0/default.jpg?q=1"
    name = manifest_name(key)

    assert "/" not in name
    assert name.endswith(".json")

    stage_fragments(dataset, "run-1", key, ['{"ok":true}'])
    assert discover_staged(dataset, "run-1") == ['{"ok":true}']


def test_staging_lives_inside_the_dataset_it_belongs_to(dataset: str) -> None:
    """So a warehouse move cannot separate a run's uncommitted fragments from their dataset."""
    assert staging_root(dataset, "run-1").startswith(dataset)


def test_the_manifest_records_which_unit_produced_the_fragment(dataset: str) -> None:
    """Recovery is one job; DIAGNOSIS is the other.

    When a run commits fewer rows than expected, the question is always "which pages?". The manifest
    answers it directly, rather than leaving an operator to diff a fragment list against a source
    listing.
    """
    stage_fragments(dataset, "run-1", "file:///pages/0007.tif", ['{"id":7}'])
    written = json.loads((Path(staging_root(dataset, "run-1")) / manifest_name("file:///pages/0007.tif")).read_text())

    assert written["units"] == ["file:///pages/0007.tif"]
    assert written["fragments"] == ['{"id":7}']


def test_a_fresh_manifest_carries_no_legacy_unit_field(dataset: str) -> None:
    """`unit` is the pre-batching spelling, kept only as a READ fallback for manifests already on a
    store (open_python-audit `ingest-flow-18`). Writing it on every new manifest kept the vestigial
    field alive forever — the fallback can only ever retire if new writes stop producing it."""
    stage_fragments(dataset, "run-1", ["file:///pages/0001.tif", "file:///pages/0002.tif"], ['{"id":1}'])
    written = json.loads((Path(staging_root(dataset, "run-1")) / manifest_name(["file:///pages/0001.tif", "file:///pages/0002.tif"])).read_text())

    assert set(written) == {"units", "fragments"}
