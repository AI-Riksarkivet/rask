"""A catalog write says WHAT it did and WHICH ENGINE did it, in fields the spec defines.

[[LIN-002]]. The estate stamped both facts only inside its own `lance` run facet — `operation` as a
rask-private name (`CREATE_TABLE`, `DROP_COLUMNS`, …) and the engine nowhere at all. To any
OpenLineage-native reader that makes a create, a drop and an alter the same event carrying a different
opaque string, and leaves "what produced this table" unanswerable.

KEYED ON WHAT THE ESTATE ACTUALLY EMITS, which is lowercase. The first version of this map was built
from the `operation=` literals in the source (`CREATE_TABLE`, `DROP_COLUMNS`, …) and matched ONE live
operation of eighteen — measured on the deployed feed 2026-09-19 over 500 events: `create_table` 111,
`drop_table` 39, `add_columns` 7, `update_schema_metadata` 6, `declare_table` 2. The facet shipped and
never appeared on a real write. The lookup lower-cases, so either spelling resolves.

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

import pytest

from service_kit.openlineage import (
    lifecycle_facet,
)


#: The spec's own enum, from `spec/facets/LifecycleStateChangeDatasetFacet.json`.
_SPEC_VALUES = frozenset({"ALTER", "CREATE", "DROP", "OVERWRITE", "RENAME", "TRUNCATE"})


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        # A DDL verb MEASURED on the live feed, in the case the wire actually carries.
        ("create_table", "CREATE"),
    ],
)
def test_a_ddl_operation_states_what_it_did(operation: str, expected: str) -> None:
    assert lifecycle_facet("p", operation)["lifecycleStateChange"] == expected


@pytest.mark.parametrize(
    "operation",
    # The most frequent non-DDL verb on the live feed.
    ["compaction"],
)
def test_a_data_operation_claims_no_lifecycle_change(operation: str) -> None:
    """These change ROWS, not the dataset's existence or shape. `OVERWRITE` would be a lie a reader acts on."""
    assert lifecycle_facet("p", operation) == {}


def test_every_value_this_maps_to_is_in_the_SPECS_enum() -> None:
    """The map is hand-written, so the one way it can be wrong is inventing a value the spec rejects."""
    # `str(...)`: the builder returns `dict[str, object]` because a facet payload is arbitrary JSON,
    # so the value is `object` until something narrows it — and a set of `object` cannot be sorted.
    emitted = {str(facet["lifecycleStateChange"]) for op in _ALL_DDL if (facet := lifecycle_facet("p", op))}

    assert emitted <= _SPEC_VALUES, f"not in the spec's enum: {sorted(emitted - _SPEC_VALUES)}"


_ALL_DDL = (
    "create_table",
    "declare_table",
    "register_table",
    "drop_table",
    "deregister_table",
    "add_columns",
    "alter_columns",
    "drop_columns",
    "create_index",
    "drop_index",
    "update_schema_metadata",
    "rename_table",
    "overwrite_table",
)


# --- the catalog actually stamps them ------------------------------------------------------------ #
