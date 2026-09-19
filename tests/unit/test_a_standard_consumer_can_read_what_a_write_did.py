"""A catalog write says WHAT it did and WHICH ENGINE did it, in fields the spec defines.

[[LIN-002]]. The estate stamped both facts only inside its own `lance` run facet — `operation` as a
rask-private name (`CREATE_TABLE`, `DROP_COLUMNS`, …) and the engine nowhere at all. To any
OpenLineage-native reader that makes a create, a drop and an alter the same event carrying a different
opaque string, and leaves "what produced this table" unanswerable.

MEASURED AGAINST UPSTREAM, not assumed. `LifecycleStateChangeDatasetFacet` defines exactly six values
(ALTER / CREATE / DROP / OVERWRITE / RENAME / TRUNCATE) and `ProcessingEngineRunFacet` requires only
`version` — both read from `OpenLineage/OpenLineage/spec/facets` on 2026-09-19, where every one of the
estate's eight pinned facet versions also matched.

THE RASK NAME STAYS. The two are not redundant: `lance.operation` is more specific than the enum
admits (`CREATE_INDEX` and `DROP_COLUMNS` are both `ALTER`), so collapsing onto the standard field
would lose information the estate's own consumers read. Both are emitted.

WHY A DATA OPERATION GETS NO LIFECYCLE FACET AT ALL is the part worth holding: the enum has no member
meaning "wrote rows", and mapping `INSERT` onto `OVERWRITE` would tell a reader the table was replaced.
An absent facet is the honest answer; a wrong one is acted upon.
"""

from __future__ import annotations

from typing import Any

import pytest

from service_kit.openlineage import (
    LIFECYCLE_FACET_SCHEMA_URL,
    PROCESSING_ENGINE_FACET_SCHEMA_URL,
    lifecycle_facet,
    processing_engine_facet,
)


#: The spec's own enum, from `spec/facets/LifecycleStateChangeDatasetFacet.json`.
_SPEC_VALUES = frozenset({"ALTER", "CREATE", "DROP", "OVERWRITE", "RENAME", "TRUNCATE"})


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("CREATE_TABLE", "CREATE"),
        ("create_table", "CREATE"),
        ("CREATE_TABLE_VERSION", "CREATE"),
        ("DECLARE_TABLE", "CREATE"),
        ("DROP_TABLE", "DROP"),
        ("DEREGISTER_TABLE", "DROP"),
        ("ADD_COLUMNS", "ALTER"),
        ("ALTER_COLUMNS", "ALTER"),
        ("DROP_COLUMNS", "ALTER"),
        ("CREATE_INDEX", "ALTER"),
        ("DROP_INDEX", "ALTER"),
    ],
)
def test_a_ddl_operation_states_what_it_did(operation: str, expected: str) -> None:
    assert lifecycle_facet("p", operation)["lifecycleStateChange"] == expected


@pytest.mark.parametrize("operation", ["INSERT", "DELETE", "MERGE_INSERT", "COMPACT_TABLE"])
def test_a_data_operation_claims_no_lifecycle_change(operation: str) -> None:
    """These change ROWS, not the dataset's existence or shape. `OVERWRITE` would be a lie a reader acts on."""
    assert lifecycle_facet("p", operation) == {}


def test_an_unknown_operation_guesses_nothing() -> None:
    """An operation nobody has mapped is one the estate added without deciding what it does. Silence is
    recoverable; a wrong `DROP` in a consumer's hands is not."""
    assert lifecycle_facet("p", "SOME_NEW_VERB") == {}


def test_every_value_this_maps_to_is_in_the_SPECS_enum() -> None:
    """The map is hand-written, so the one way it can be wrong is inventing a value the spec rejects."""
    # `str(...)`: the builder returns `dict[str, object]` because a facet payload is arbitrary JSON,
    # so the value is `object` until something narrows it — and a set of `object` cannot be sorted.
    emitted = {str(facet["lifecycleStateChange"]) for op in _ALL_DDL if (facet := lifecycle_facet("p", op))}

    assert emitted <= _SPEC_VALUES, f"not in the spec's enum: {sorted(emitted - _SPEC_VALUES)}"


_ALL_DDL = (
    "CREATE_TABLE",
    "create_table",
    "CREATE_TABLE_VERSION",
    "DECLARE_TABLE",
    "DROP_TABLE",
    "DEREGISTER_TABLE",
    "ADD_COLUMNS",
    "ALTER_COLUMNS",
    "DROP_COLUMNS",
    "CREATE_INDEX",
    "DROP_INDEX",
)


def test_both_facets_carry_a_spec_schema_url() -> None:
    """A custom facet takes `BaseFacet`; a STANDARD one must point at its own document, which is what a
    validating consumer fetches."""
    assert lifecycle_facet("p", "DROP_TABLE")["_schemaURL"] == LIFECYCLE_FACET_SCHEMA_URL
    assert processing_engine_facet("p", name="lance", version="11.0.0")["_schemaURL"] == PROCESSING_ENGINE_FACET_SCHEMA_URL


def test_the_engine_facet_names_the_engine_and_its_version() -> None:
    """`version` is the spec's only REQUIRED field — an engine named without one is unreproducible."""
    facet = processing_engine_facet("p", name="lance", version="11.0.0")

    assert (facet["name"], facet["version"]) == ("lance", "11.0.0")


# --- the catalog actually stamps them ------------------------------------------------------------ #


def _event(operation: str) -> dict[str, Any]:
    """One built write event. Every required argument is named so a signature change reds this rather
    than silently building a different event."""
    from catalog.core import lineage_emit

    return lineage_emit.build_write_event(
        operation=operation,
        namespace="acme-bronze",
        table_id="acme-bronze$events",
        author="user:alice",
        version=3,
        run_id="00000000-0000-5000-8000-000000000001",
        event_time="2026-09-19T00:00:00Z",
        job_namespace="lance-catalog",
    )


def test_a_catalog_write_names_the_engine_that_made_it() -> None:
    """Without it every write path in a multi-engine lakehouse is an anonymous producer."""
    import lance

    facet = _event("CREATE_TABLE")["run"]["facets"]["processing_engine"]

    assert (facet["name"], facet["version"]) == ("lance", lance.__version__)


def test_a_catalog_DDL_write_states_its_lifecycle_change() -> None:
    assert _event("DROP_TABLE")["outputs"][0]["facets"]["lifecycleStateChange"]["lifecycleStateChange"] == "DROP"


def test_a_catalog_DATA_write_carries_no_lifecycle_facet() -> None:
    """The control. Without it, a builder that stamped every event would pass the case above."""
    assert "lifecycleStateChange" not in (_event("INSERT")["outputs"][0].get("facets") or {})


def test_the_rask_operation_name_survives_beside_the_standard_one() -> None:
    """They are not redundant: `CREATE_INDEX` and `DROP_COLUMNS` are both `ALTER`, so the enum alone
    loses what the estate's own consumers read."""
    event = _event("CREATE_INDEX")

    assert event["run"]["facets"]["lance"]["operation"] == "CREATE_INDEX"
    assert event["outputs"][0]["facets"]["lifecycleStateChange"]["lifecycleStateChange"] == "ALTER"
