"""#78 format honesty — the create path rejects a client that tries to select a non-Lance file format
instead of silently echoing the ignored property back.

STANDING RULING (owner, 2026-08-15): **rask will only and always only support Lance tables — no other
format, ever.** This is not a current-scope note or a "not yet"; it is permanent, and it makes this
guard a PRODUCT INVARIANT rather than an implementation detail of the create door.

What that settles, so nobody reopens it as a feature request:

* The 400 here is the correct and final answer, not a temporary gap. A future PR adding
  Parquet/Iceberg/Delta support is out of scope by ruling, not by effort.
* The catalog is deliberately format-AWARE — the exact inverse of Lakekeeper's Generic Table
  boundary ("no Lance in the catalog", commit coordination an explicit non-goal). rask imports
  pylance, serves the data plane in-process, and coordinates commits, and it can do all three
  BECAUSE the format is closed. Every one of those becomes unsound the moment a second format
  exists.
* It is also what lets the estate skip a relational database: Iceberg puts the commit pointer in the
  catalog (so every commit is a DB transaction), Lance puts the CAS in the object store. Supporting
  both formats would reintroduce the very requirement the architecture is built to avoid.
* Consequence for the opaque-asset rung (diff2 F9): an `asset` type may govern NON-TABULAR bytes —
  model artefacts are the first and only known consumer — but it must NEVER become a second TABLE
  lane carrying a format tag. Lakekeeper's Generic Table is a format-agnostic table; rask's asset
  rung, if it lands, is a governed blob. Those are different things and this ruling is the line
  between them. (Do not enumerate future consumers by workload: rask is a format-agnostic multimodal
  platform, and HTR/IIIF is one example task, not its identity.)
"""

from __future__ import annotations

import pytest
from lance_namespace import InvalidInputError

from catalog.core.formats import reject_unsupported_properties


@pytest.mark.parametrize(
    "props",
    [
        pytest.param({"write.format.default": "parquet"}, id="non-lance-format"),
        # [[LH-245]]: the key that arms Lance to delete versions inside any later commit, past every hold.
        pytest.param({"lance.auto_cleanup.interval": "1"}, id="commit-path-auto-cleanup"),
    ],
)
def test_rejects_a_property_no_door_honours(props: dict[str, str]) -> None:
    with pytest.raises(InvalidInputError):
        reject_unsupported_properties(props)


@pytest.mark.parametrize(
    "props",
    [
        None,
    ],
)
def test_allows_lance_or_absent(props: object) -> None:
    reject_unsupported_properties(props)  # no raise


# --------------------------------------------------------------------------------------------------
# The guard must be CALLED. Testing the function proves the rule; it does not wire it to a door.
# --------------------------------------------------------------------------------------------------
