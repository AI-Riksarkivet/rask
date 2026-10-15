"""`GET /management/v1/maintenance/due` lists a table's due record only to a caller holding `can_maintain` on it (ADR 0162).

The records are maintenance's, written by the sweep on the control root, and each names a table and how many datasets
resolve their files through it. The door is the catalog's: on the real app with OIDC and FGA on, the OpenFGA store an
in-memory double that allows exactly the grants named, so what is asserted is the filter, and that a record's clone
locations reach the caller only as a count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from catalog.core.config import Settings, get_settings
from service_kit.lakehouse.maintenance_due import CompactionDue, DueRecord, write_due
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}


def test_a_caller_sees_only_the_due_tables_it_maintains(real_ns_client: TestClient, tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "root": str(tmp_path),
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )
    client = real_ns_client
    client.app.dependency_overrides[get_settings] = lambda: settings
    client.app.state.oidc = MagicMock()
    grants = Grants()
    client.app.state.fga = grants
    due = CompactionDue(fragments=5, candidate_fragments=5, tasks=1)
    for table in ("acme$mine", "acme$theirs"):
        record = DueRecord(table=table, location=f"{tmp_path}/{table}", version=6, compaction=due, clones=["s3://other/clone"], evaluated_at=datetime.now(UTC))
        write_due(settings.registry_root, settings.storage_options(), record)
    grants.grant("alice", ("can_maintain", "table:acme$mine"))
    act_as(client, "alice")

    listed = client.get("/management/v1/maintenance/due", headers=AUTH)

    assert listed.status_code == 200, listed.text
    assert [(table["table"], table["clones"]) for table in listed.json()["tables"]] == [("acme$mine", 1)]
