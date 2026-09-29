"""A catalog write says WHICH catalog governs the dataset and WHAT KIND of thing it is ([[LIN-002]]).

Without the `catalog` facet, two estates writing `bronze$events` are one node in a shared lineage
graph — the failure mode a standard consumer hits first when more than one producer reports to it.
Without `datasetType`, a reader cannot tell a governed table from a file the run merely read.

BOTH ARE FILLED FROM WHAT THE CATALOG ALREADY KNOWS, not from new configuration. `framework` is the
constant `lance` because the format is closed by ruling; `type` is the Lance Namespace implementation
the catalog is reached through; `name` REUSES the lineage job namespace, so the facet and the events
it rides on cannot disagree about who is speaking.
"""

from __future__ import annotations

from typing import Any

from service_kit.openlineage import CATALOG_FRAMEWORK, catalog_facet, dataset_type_facet


_PRODUCER = "https://example.invalid/catalog"


def _facet(**over: Any) -> dict[str, Any]:
    args = {"impl": "dir", "name": "lance-catalog", "warehouse_uri": "s3://lance-catalog", **over}
    return catalog_facet(_PRODUCER, **args)


def test_the_catalog_names_itself() -> None:
    facet = _facet()

    assert facet["framework"] == CATALOG_FRAMEWORK == "lance"
    assert facet["type"] == "dir"
    assert facet["name"] == "lance-catalog"
    assert facet["warehouseUri"] == "s3://lance-catalog"


def test_an_unnamed_catalog_renders_NO_facet() -> None:
    """`type` and `name` are the fields a consumer JOINS on, so a blank one is worse than an absent
    facet: it merges every unnamed catalog into a single node instead of leaving them unjoined."""
    assert _facet(name="") == {}
    assert _facet(impl="") == {}


def test_the_warehouse_uri_is_optional() -> None:
    """It is not required by the spec, and an estate that does not publish where its bytes live should
    omit the key rather than send an empty string a consumer would render as a location."""
    assert "warehouseUri" not in _facet(warehouse_uri="")


def test_a_governed_dataset_is_a_TABLE_and_a_source_is_a_FILE() -> None:
    """The two values this estate can claim truthfully. The enum also admits VIEW, TOPIC, STREAM, MODEL
    and JOB_OUTPUT — none of which the catalog serves, so none is emitted."""
    assert dataset_type_facet(_PRODUCER, external=False)["datasetType"] == "TABLE"
    assert dataset_type_facet(_PRODUCER, external=True)["datasetType"] == "FILE"
