"""ING-07 / ingest-flow-09 — this service reads its configuration from ONE declaration.

The plane had no operational settings model at all: `IngestAuthSettings` covered the auth half and
every other knob was a bare `os.getenv` at the point of use — 44 reads across 15 of 27 modules,
several of them frozen at module import. Three things followed, and all three are in the tree's own
history:

* one convention with THREE readers — `RASK_CATALOG_DELIMITER` was read by `naming.delimiter()`, by a
  dead `lineage._delimiter()`, and by an import-frozen `catalog_service.DELIMITER`. A delimiter the
  writers disagree about addresses a different table rather than failing.
* knobs fixed per POD, invisible to `kubectl set env` and to any test that sets the variable after
  the module was first imported. `sizing.py` and `workflow.RunLimits` had already been dragged off
  that pattern, one module at a time.
* nothing enumerating what the service reads, so the chart and the code could only be compared by
  grep.

The gate below is the one that keeps it closed: a NEW `os.getenv` anywhere in the plane fails here.
"""

from __future__ import annotations

import pytest


def test_the_catalog_delimiter_has_ONE_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """The convention that was read three ways, and one of the three was frozen at import.

    Moving the variable AFTER import must move every reader, or two writers of one convention name
    two different tables.
    """
    import pyarrow as pa

    from ingest.catalog_service import CatalogServiceClient
    from ingest.naming import bronze_table_id, delimiter

    monkeypatch.setenv("RASK_CATALOG_DELIMITER", "~")
    monkeypatch.delenv("MEDALLION_BRONZE_NAMESPACE", raising=False)

    client = CatalogServiceClient(pa.schema([pa.field("id", pa.string())]), base_url="http://catalog.test", token="t")

    assert delimiter() == "~"
    assert client.table_id("bronze", "pages") == "bronze~pages"
    assert bronze_table_id("", "pages") == "bronze~pages"


def test_a_MISTYPED_catalog_flag_refuses_instead_of_silently_writing_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag whose false reading is a data incident.

    `os.getenv("RASK_INGEST_USE_CATALOG").lower() in ("1", "true", "yes")` read `"ture"` as FALSE, and
    a false reading means the run writes governed bytes no catalog knows about and no stage runner will ever
    be told of — the silent local fallback `catalog_enabled`'s own docstring forbids.
    """
    from pydantic import ValidationError

    from ingest.config import settings

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "ture")
    with pytest.raises(ValidationError):
        settings()

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "true")
    assert settings().use_catalog is True
