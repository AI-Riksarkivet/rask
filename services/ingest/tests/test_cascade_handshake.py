"""The handshake: ingest's bronze write must actually FIRE the medallion's cascade head.

Every other test in this plane checks one side. That is exactly how the defect survived — ingest's
emit was self-consistent and correct-looking, the head's matcher was self-consistent and
correct-looking, and they described different pairs:

    ingest emitted   namespace="bind86"        name="bind86$e2ewin"
    the head wanted  namespace="bind86-bronze" name="bind86-bronze$e2ewin"

Nothing failed. The data landed, the catalog registered the version, lineage recorded the run, A8
reported no provenance defect — and silver was never woken, because a tier is announced by the record
of what happened (R23) and the record said something the head does not listen for.

So this test does not restate either side's convention. It calls THE HEAD'S OWN MATCHER against THE
WRITER'S OWN OUTPUT. A future edit to either one that breaks the handshake fails here, whichever side
moved — which a test that hardcoded the expected strings could not do, because it would have to be
edited too and would then agree with whichever side was changed last.
"""

from __future__ import annotations

import pytest
from openlineage.client.serde import Serde

from ingest.lineage import _output_datasets, _tenant_facet


pytest.importorskip("medallion", reason="the cascade head lives in the medallion service")


def _ingest_event(project: str, dataset: str, *, event_type: str = "COMPLETE") -> dict:
    """A terminal ingest event exactly as it goes on the wire — outputs AND the run facet bag.

    Both halves matter and only together: the head derives its expected namespace from the facet's
    project and then looks for that namespace among the outputs. Emitting the qualified name without
    the facet makes the head fall back to the single-tenant pair and miss; emitting the facet without
    the qualified name makes it look for a pair nothing wrote.
    """
    outputs = [Serde.to_dict(o.to_openlineage_output()) for o in _output_datasets(project, dataset, 7, 1204)]
    return {"eventType": event_type, "outputs": outputs, "run": {"facets": _tenant_facet(project)}}


#: The head's `bronze_dataset` is the FULL table id, namespace prefix included (`bronze$events` is the
#: shipped default) — NOT a bare dataset name. It qualifies that whole string, so the pair it wants is
#: `project_namespace(project, "bronze$pages")` = `bind86-bronze$pages`, which is exactly what the
#: writer composes as `{project_namespace(project, bronze_namespace)}${dataset}`. Getting this wrong in
#: the fixture is itself instructive: the first version passed a bare `"pages"` here and every
#: per-tenant assertion failed for a reason that had nothing to do with the writer.
LANE_TABLE = "bronze$pages"


def _settings():
    from medallion.core.config import MedallionSettings

    return MedallionSettings(MEDALLION_BRONZE_DATASET=LANE_TABLE)


def _head_verdict(event: dict) -> str | None:
    """What the medallion's `/bronze-arrival` head makes of this event — its real code, not a mirror."""
    from medallion.services.ingest_trigger import _bronze_write_dataset, _cascade_project

    return _bronze_write_dataset(event, _settings(), _cascade_project(event))


def test_a_PER_TENANT_ingest_write_fires_the_head_for_that_tenant() -> None:
    """The case the estate actually runs: an ETL submitted inside project `bind86`.

    This is the assertion that was false for the entire life of the plane.
    """
    assert _head_verdict(_ingest_event("bind86", "pages")) is not None, (
        "the head did not recognise ingest's own bronze write — the cascade cannot fire, and nothing else reports it"
    )


def test_a_SINGLE_TENANT_write_fires_the_unqualified_head() -> None:
    """No project → `bronze` / `bronze$pages`, byte-identical to the pre-#84 estate. Both sides must
    degrade the same way or the fallback misses in exactly the case it exists for."""
    assert _head_verdict(_ingest_event("", "pages")) is not None


def test_a_FAILED_run_does_NOT_fire_the_cascade() -> None:
    """A run that failed has no landed batch to transform. Firing on one would kick the pipeline over
    data that is not there — which is why the head takes only COMPLETE, and why the writer must not
    reach for a terminal shortcut that makes a FAIL look like one."""
    assert _head_verdict(_ingest_event("bind86", "pages", event_type="FAIL")) is None


def test_one_tenant_cannot_fire_ANOTHER_tenants_head() -> None:
    """The isolation #84 exists for. `acme`'s write must not wake `bind86`'s movers.

    Qualification is what provides this: the two writes land on distinct graph nodes and distinct
    expected pairs. Emitting a shared `bronze` namespace for every tenant would have collided them
    onto one node — the risk the qualification was introduced to close.
    """
    acme = _ingest_event("acme", "pages")
    from medallion.services.ingest_trigger import _bronze_write_dataset

    settings = _settings()

    assert _bronze_write_dataset(acme, settings, "bind86") is None, "acme's write fired bind86's cascade"
    assert _bronze_write_dataset(acme, settings, "acme") is not None, "acme's write did not fire its own"


def test_an_UNSAFE_project_cannot_fire_a_tenants_head() -> None:
    """A forged or garbage project must not become a path segment on either side.

    The writer and the head apply the same `is_safe_project` guard, so an unsafe value degrades to the
    single-tenant pair in both — it cannot fire a tenant's cascade, and it does not silently produce a
    write the head ignores for a reason invisible from the writer.
    """
    event = _ingest_event("../etc", "pages")

    assert _head_verdict(event) is not None, "it should still be a valid single-tenant write"
    for output in event["outputs"]:
        assert ".." not in output["namespace"] and ".." not in output["name"]


def test_the_head_only_fires_for_its_CONFIGURED_lane_table() -> None:
    """A constraint the writer cannot satisfy alone, pinned here so it is not mistaken for a bug in it.

    `_bronze_write_dataset` matches the output name against `project_namespace(project,
    settings.bronze_dataset)` — ONE configured table per lane (`bronze$events` by default,
    `medallion.producer.bronzeDataset` in the chart). So an ETL run that writes an arbitrary dataset
    name does NOT wake the cascade, however correctly it is qualified.

    That is by design for the two fixed lanes the head serves, and it is a real limit on the ETL form,
    which lets a user name any table. It belongs to `/bronze-arrival`'s retirement (#34), not to the
    naming fix — the point of asserting it is that the next person to find an unfired cascade reads
    this instead of re-deriving the qualification from scratch.
    """
    assert _head_verdict(_ingest_event("bind86", "pages")) is not None, "the configured lane fires"
    assert _head_verdict(_ingest_event("bind86", "some-other-table")) is None, (
        "an unconfigured table fired the head — the lane constraint has changed and #34 should be revisited"
    )


def test_the_CATALOG_and_the_GRAPH_name_the_SAME_TABLE() -> None:
    """The invariant the whole plane rests on, and the one that had no gate.

    Two consumers name this table and they must agree: the catalog keys on `table_id` (and OpenFGA
    authorizes against exactly that object), while the lineage output is what the cascade head
    matches. A graph naming `bind86-bronze$pages` while the catalog holds `bind86$pages` describes a
    table that does not exist — and NOTHING reports it. The write succeeds, the lineage record is
    well-formed, every consumer resolving the name gets nothing back.

    That is not hypothetical: fixing only the lineage side produced exactly this state mid-change.
    Both now compose through `ingest.naming`, and this fails if either grows its own copy again.
    """
    from ingest.catalog_service import CatalogServiceClient
    from ingest.lineage import _output_datasets
    from ingest.naming import bronze_namespace_for

    client = object.__new__(CatalogServiceClient)

    for project, dataset in [("bind86", "pages"), ("", "pages"), ("acme", "e2ewin")]:
        # Through the BOUNDARY, exactly as `runtime` calls it: the catalog is handed `spec.namespace`,
        # never `spec.project`. Passing the project here instead is the original defect in miniature,
        # and writing this test the lazy way reproduced it — the assertion fired on `bind86$pages`.
        catalog_name = CatalogServiceClient.table_id(client, bronze_namespace_for(project), dataset)
        graph = Serde.to_dict(_output_datasets(project, dataset, 1, 1)[0].to_openlineage_output())

        assert catalog_name == graph["name"], (
            f"the catalog calls it {catalog_name!r} and the graph calls it {graph['name']!r} — one of them names a table nothing else can resolve"
        )
        assert graph["name"].startswith(graph["namespace"]), "the table id must live under the namespace it declares"
