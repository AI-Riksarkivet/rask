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

import inspect
from datetime import timedelta

from maintenance.services.sweep import DatasetWorkItem, maintain_one_item


def test_a_work_item_round_trips_through_a_queue() -> None:
    """A unit that cannot survive JSON cannot reach a worker."""
    from maintenance.services.sweep import DatasetPlan

    item = DatasetWorkItem(
        uri="s3://bucket/db/t.lance",
        plan=DatasetPlan(older_than=timedelta(days=7), retain_versions=5, index_columns=["id"]),
        protected_by="bucket/other/base.lance",
    )
    restored = DatasetWorkItem.model_validate_json(item.model_dump_json())
    assert restored == item
    assert restored.plan.older_than == timedelta(days=7)


def test_the_worker_entry_point_takes_nothing_computed_across_the_estate() -> None:
    """The structural half of self-containment, checked on the signature rather than trusted.

    `BaseRefs` is the whole-estate value. If it — or a bucket list, or the discovered URIs — reappears
    as a parameter here, the unit is not a unit any more and the queue in front of it would be a lie.
    """
    params = inspect.signature(maintain_one_item).parameters
    assert "item" in params
    forbidden = {"protected", "uris", "buckets", "policy_records", "trashed_by_path", "results"}
    assert not (forbidden & set(params)), f"a whole-estate argument reached the worker entry point: {forbidden & set(params)}"


def test_the_protection_verdict_survives_the_reduction() -> None:
    """The reduced string must produce the SAME refusal the whole-estate pre-pass would.

    `is_protected` matches by CONTAINMENT, so a dataset under a referenced root is protected too.
    Reconstructing from the matched root has to preserve that, or a clone's source gets compacted.
    """
    from service_kit.lakehouse import base_refs

    whole_estate = base_refs.BaseRefs(protected={"bucket/src.lance"})
    for uri in ("s3://bucket/src.lance", "s3://bucket/src.lance/data"):
        matched = whole_estate.is_protected(uri)
        assert matched is not None
        reduced = base_refs.BaseRefs(protected={matched})
        assert reduced.is_protected(uri) == matched


def test_an_unprotected_dataset_reduces_to_no_protection() -> None:
    from service_kit.lakehouse import base_refs

    whole_estate = base_refs.BaseRefs(protected={"bucket/src.lance"})
    assert whole_estate.is_protected("s3://bucket/unrelated.lance") is None
    assert base_refs.BaseRefs(protected=set()).is_protected("s3://bucket/unrelated.lance") is None


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


def test_the_planner_CARRIES_the_pre_passs_verdict_into_the_item(monkeypatch: object) -> None:
    """The reduction must actually happen, and nothing else in the suite notices if it stops.

    Dropping `protected_by` from the planner leaves every other test green — the units still execute,
    the results still come back — while a shallow clone's source silently becomes compactable. The
    pre-pass is whole-estate and cannot be recovered downstream, so if the planner does not carry its
    verdict, nothing does.

    Driven with the IO phases stubbed rather than through moto: what is under test is the ONE line that
    reduces a whole-estate value into a unit, and a real bucket would not make that line more true.
    """
    import pytest

    from maintenance.core.config import MaintenanceSettings
    from maintenance.services import sweep as sweep_mod
    from service_kit.lakehouse import base_refs

    patch = pytest.MonkeyPatch() if not isinstance(monkeypatch, pytest.MonkeyPatch) else monkeypatch
    protected_uri = "s3://bucket/clone-source.lance"
    plain_uri = "s3://bucket/ordinary.lance"
    seen: dict[str, object] = {}

    def fake_protected_roots(uris: list[str], options: dict[str, str]) -> base_refs.BaseRefs:
        seen["uris"] = list(uris)
        return base_refs.BaseRefs(protected={base_refs.normalise(protected_uri)})

    patch.setattr(sweep_mod, "_s3fs", lambda settings: None)
    patch.setattr(sweep_mod, "_buckets_to_sweep", lambda settings, options: ["bucket"])
    patch.setattr(sweep_mod, "_discover_all", lambda fs, buckets: [protected_uri, plain_uri])
    patch.setattr(sweep_mod, "_load_policies", lambda settings, options: [])
    patch.setattr(sweep_mod, "_trash_exclusions", lambda settings, options: {})
    patch.setattr(sweep_mod, "_protected_roots", fake_protected_roots)

    items, decided = sweep_mod.plan_sweep(MaintenanceSettings.model_validate({"s3_bucket": "bucket"}))

    assert decided == []
    assert seen["uris"] == [protected_uri, plain_uri], "the pre-pass must see EVERY discovered dataset, not one bucket's"
    by_uri = {item.uri: item for item in items}
    assert by_uri[protected_uri].protected_by == base_refs.normalise(protected_uri), "the verdict never reached the unit"
    assert by_uri[plain_uri].protected_by is None, "an unreferenced dataset was marked protected"
