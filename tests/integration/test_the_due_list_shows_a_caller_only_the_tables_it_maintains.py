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
from uuid import uuid4

from fastapi.testclient import TestClient

from catalog.core.config import Settings, get_settings
from catalog.core.identifiers import parse_identifier
from catalog.services import table_claims
from service_kit.lakehouse import location_claims
from service_kit.lakehouse.maintenance_due import CompactionDue, DueRecord, write_due
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}


def test_a_caller_sees_only_the_due_tables_it_maintains(real_ns_client: TestClient, tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "root": str(tmp_path),
            "oidc_enabled": True,
            "oidc_connectors": {"local": "dex"},
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
    claims = table_claims.claim_store(settings)
    uuids: dict[str, str] = {}
    tables = ("acme$mine", "acme$theirs", "acme$gone")
    for table in tables:
        table_uuid, location = str(uuid4()), f"{tmp_path}/{uuid4()}"
        uuids[table] = table_uuid
        location_claims.take(claims, location, table, parse_identifier(table, "$"), is_gone=lambda _claim: False, uuid=table_uuid)
        record = DueRecord(
            table=table, uuid=table_uuid, location=location, version=6, compaction=due, clones=["s3://other/clone"], evaluated_at=datetime.now(UTC)
        )
        write_due(settings.registry_root, settings.storage_options(), record)
    # `acme$gone` was dropped and `acme$mine` is a name a table holds now ([[LH-523]]): the record's table is gone, so it
    # names no table and is shown to no one, whatever its name or its uuid could be granted. alice still holds a grant on
    # the gone table's uuid, as one a purge has not yet revoked would be.
    location_claims.release(claims, location, "acme$gone")
    write_due(settings.registry_root, settings.storage_options(), record.model_copy(update={"table": "acme$mine"}))
    grants.grant("alice", ("can_maintain", f"table:{uuids['acme$mine']}"), ("can_maintain", f"table:{uuids['acme$gone']}"))
    act_as(client, "alice")

    listed = client.get("/management/v1/maintenance/due", headers=AUTH)

    assert listed.status_code == 200, listed.text
    assert [(table["table"], table["uuid"], table["clones"]) for table in listed.json()["tables"]] == [("acme$mine", uuids["acme$mine"], 1)]
