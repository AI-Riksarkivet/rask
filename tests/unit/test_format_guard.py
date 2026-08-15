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
from catalog.api.v1.endpoints.data import _reject_unsupported_format
from lance_namespace import InvalidInputError


@pytest.mark.parametrize(
    "props",
    [
        {"write.format.default": "parquet"},
        {"write.format.default": "ORC"},
        {"data_source_format": "avro"},
        {"data_source_format": "DELTA"},
    ],
)
def test_rejects_non_lance_format(props: dict[str, str]) -> None:
    with pytest.raises(InvalidInputError):
        _reject_unsupported_format(props)


@pytest.mark.parametrize(
    "props",
    [
        None,
        {},
        {"some.other.prop": "value"},
        {"write.format.default": "lance"},  # explicitly Lance is fine
        {"write.format.default": "LANCE"},
    ],
)
def test_allows_lance_or_absent(props: object) -> None:
    _reject_unsupported_format(props)  # no raise
