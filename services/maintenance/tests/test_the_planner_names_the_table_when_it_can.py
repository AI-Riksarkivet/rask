"""The two planning lanes stamp the table identity on the unit whenever they can recover it.

The executor asks the catalog for a credential scoped to the dataset it is about to rewrite, and the
catalog is addressed by identifier. A unit with no id can still be executed — it just cannot be
executed with a scoped credential, so it falls back to whatever ambient key the process holds.

`table_id_from_uri` recovers the id from the flat `<uuid8>_<table_id>` layout only. That is a
MINORITY of the live warehouse (six of eleven top-level roots), which is why the field is carried by
producers that know it rather than derived at the executor. But where the sweep CAN derive it, not
doing so would leave the estate's own catalog-created tables — the majority of individually-registered
datasets — unnecessarily on the ambient credential.

`None` must stay reachable. Inventing an id for a directory the parser cannot read would vend a
credential for a DIFFERENT table, which is worse than vending none: it turns a missing hardening into
a wrong authorization.
"""

from __future__ import annotations

from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import table_id_from_uri


def _settings() -> MaintenanceSettings:
    """The real settings object, not a stub — `plan_one` is typed against it, and a duck-typed double
    is exactly the drift `ty` is configured to catch (`error-on-warning`). Storage is never touched:
    every collaborator that would reach it is patched out below."""
    return MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog")


def test_the_flat_layout_yields_an_identity() -> None:
    assert table_id_from_uri("s3://lance-catalog/6ecbe11e_transcripts_v2$annotations") == "transcripts_v2$annotations"


def test_a_medallion_path_yields_none_rather_than_a_guess() -> None:
    """The shape that made carrying the id necessary. `medallion/bronze` IS `bronze$events`, and the
    parser has no way to know that — so it must decline, not approximate."""
    assert table_id_from_uri("s3://lance-catalog/medallion/bronze") is None


def test_plan_one_stamps_the_identity_it_derived(monkeypatch) -> None:
    from maintenance.services import sweep

    uri = "s3://lance-catalog/6ecbe11e_transcripts_v2$annotations"
    monkeypatch.setattr(sweep, "_trash_exclusions", lambda settings, options: set())
    monkeypatch.setattr(sweep, "_load_policies", lambda settings, options: [])
    monkeypatch.setattr(sweep, "_resolve_plan", lambda *a, **k: sweep.DatasetPlan())
    monkeypatch.setattr(sweep.base_refs, "sibling_base_refs", lambda u, o: sweep.base_refs.BaseRefs())

    item = sweep.plan_one(uri, _settings())
    assert item is not None
    assert item.table_id == "transcripts_v2$annotations"


def test_plan_one_leaves_the_identity_unset_when_it_cannot_derive_one(monkeypatch) -> None:
    from maintenance.services import sweep

    uri = "s3://lance-catalog/medallion/bronze"
    monkeypatch.setattr(sweep, "_trash_exclusions", lambda settings, options: set())
    monkeypatch.setattr(sweep, "_load_policies", lambda settings, options: [])
    monkeypatch.setattr(sweep, "_resolve_plan", lambda *a, **k: sweep.DatasetPlan())
    monkeypatch.setattr(sweep.base_refs, "sibling_base_refs", lambda u, o: sweep.base_refs.BaseRefs())

    item = sweep.plan_one(uri, _settings())
    assert item is not None
    assert item.table_id is None, "a guessed identity vends a credential for the wrong table"
