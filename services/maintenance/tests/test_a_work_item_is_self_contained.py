"""The sweep's plan must be a DOCUMENT a worker can act on alone.

Today one cron request maintains the whole estate: discover every dataset, then compact and GC each in
a loop inside the handler. That shape has three costs, and all three come from the work being
inseparable from the tick rather than from anything maintenance needs:

* **A tick that overruns is DROPPED, not queued.** The handler's single-flight guard skips an
  overlapping tick, so on an estate whose sweep outgrows its interval, work is silently lost.
* **A poison dataset stops everything after it.** The sweep shuffles its discovery order specifically to
  rotate which datasets sit behind a recurring failure point — a workaround for having no per-dataset
  failure boundary.
* **It cannot scale.** The guard is an `asyncio.Lock`, correct only because `replicas: 1` is hardcoded
  in the template.

Every one of those dissolves once a dataset's maintenance is a self-contained unit. That is what these
pin, and self-containment is the whole property: the unit must need NOTHING computed across the estate.

The hard part is real and is why this is a step of its own. `_protected_roots` is a whole-estate
pre-pass — a shallow clone in bucket B is the only thing that knows bucket A's dataset must not be
touched — so it cannot be computed per dataset. But `compact_one` consumes it through exactly one call,
`is_protected(uri)`, whose answer is one string. The pre-pass stays whole-estate at PLANNING time and
reduces to that string in the unit.
"""

from __future__ import annotations

from datetime import timedelta


def test_the_reduced_verdict_still_REFUSES_a_dataset_another_manifest_resolves_through(tmp_path: object) -> None:
    """The behavioural half, on a real dataset: the reduction must not weaken the refusal.

    This is the risk the change carries. `protected_by` replaces a whole-estate `BaseRefs` with one
    string, and if the rehydration were even slightly off, a dataset that a shallow clone resolves
    through would be compacted — destroying precisely the files the clone reads. Driven through
    `maintain_one_item` against real Lance so it is the shipped path being refused, not a double.
    """
    from pathlib import Path

    import lance
    import pyarrow as pa

    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.sweep import DatasetPlan, DatasetWorkItem, maintain_one_item

    root = Path(str(tmp_path))
    uri = str(root / "src.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(50), pa.int64())}), uri)
    for start in range(50, 200, 50):
        lance.write_dataset(pa.table({"id": pa.array(range(start, start + 50), pa.int64())}), uri, mode="append")
    version_before = lance.dataset(uri).version
    assert len(lance.dataset(uri).get_fragments()) == 4, "the fixture must have something worth compacting"

    settings = MaintenanceSettings.model_validate({"s3_bucket": "unused"})
    # The planner's verdict for this dataset: some other manifest resolves through it.
    protected_item = DatasetWorkItem(uri=uri, plan=DatasetPlan(older_than=timedelta(0)), protected_by=uri.removeprefix("s3://"))
    refused = maintain_one_item(protected_item, settings=settings, options={})

    assert refused.refused is not None, "a protected dataset was maintained anyway"
    assert refused.fragments_removed == 0
    assert lance.dataset(uri).version == version_before, "the protected dataset was rewritten"

    # And the control: the SAME dataset with no referrer is compacted, so the refusal above is the
    # verdict doing its job rather than the work being broken for every dataset.
    allowed = maintain_one_item(DatasetWorkItem(uri=uri, plan=DatasetPlan(older_than=timedelta(0))), settings=settings, options={})
    assert allowed.refused is None, f"an unprotected dataset was refused: {allowed.refused}"
    assert allowed.fragments_removed > 0, "the control did no work, so the refusal above proves nothing"

    # HOW LONG THE PASS TOOK, measured on the shipped path rather than asserted on a double
    # ([[LH-098]]: the trail recorded what a pass achieved and never its duration). Asserted HERE, on a
    # run that really opened and rewrote a Lance dataset, because the audit-record test can only show
    # the field TRAVELS — delete the timing and that one still passes on its double while this one
    # reds. A positive float, not merely non-None: a timer wired to a constant would satisfy the
    # weaker check.
    assert allowed.duration_seconds is not None, "a real compaction reported no duration — the pass is not timed"
    assert allowed.duration_seconds > 0, f"the pass reported a non-positive duration: {allowed.duration_seconds}"
