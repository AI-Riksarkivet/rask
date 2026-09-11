"""The provenance column must be JSON-typed, and a failure must be loud (LH-012).

`medallion_demo.write_gold` used to fall back to `pa.string()` when `pa.json_()` raised. A string
column looks identical in a schema print and is SILENTLY UNQUERYABLE as provenance: every JSON
function raises a coercion error on it, and the JSON scalar index refuses it outright ("can only be
created on a Binary or LargeBinary field"). The fallback therefore did not degrade the column — it
produced one that cannot answer the question the column exists for, and reported success.

THIS PINS THE PREMISE, not the absence of the code. Deleting the fallback is only safe while
`pa.json_()` actually works in the pinned pyarrow; if that stops being true, this test says so in one
line instead of the demo failing somewhere further down with an index error nobody traces back.
"""

from __future__ import annotations

import pyarrow as pa


def test_the_pinned_pyarrow_really_builds_a_json_array() -> None:
    """`pa.json_()` must exist AND be built — a present-but-unbuilt extension raises on use."""
    array = pa.array(['{"run_id": "r-1"}'], type=pa.json_())

    assert array.type != pa.string(), "a JSON column that is really a string is unqueryable provenance"
    assert "json" in str(array.type).lower(), f"unexpected provenance column type: {array.type}"


def test_a_string_column_is_not_an_acceptable_substitute() -> None:
    """States the reason the fallback was wrong, in a form that stays true if pyarrow changes.

    Not a test OF the demo — a test of the property the demo relies on: the two types are not
    interchangeable, so silently swapping one for the other cannot be a degradation.
    """
    assert pa.array(['{"a": 1}'], type=pa.string()).type != pa.array(['{"a": 1}'], type=pa.json_()).type
