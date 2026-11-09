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

import pytest
from fastapi.testclient import TestClient

from catalog.core.config import Settings, get_settings
from catalog.core.identifiers import parse_identifier
from catalog.services import table_claims
from lakehouse import location_claims, maintenance_due
from lakehouse.maintenance_due import CompactionDue, DueRecord, write_due
from lakehouse.storage_options import StorageOptions
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}
DUE = CompactionDue(fragments=5, candidate_fragments=5, tasks=1)


def _governed(client: TestClient, tmp_path: Path) -> tuple[Settings, Grants]:
    """The catalog with OIDC and FGA on, its control root ``tmp_path``, and the in-memory OpenFGA store it asks."""
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
    client.app.dependency_overrides[get_settings] = lambda: settings
    client.app.state.oidc = MagicMock()
    grants = Grants()
    client.app.state.fga = grants
    return settings, grants


def _recorded(settings: Settings, table: str, tmp_path: Path) -> DueRecord:
    """A due record for ``table``, a table the catalog holds at a location of its own."""
    table_uuid, location = str(uuid4()), f"{tmp_path}/{uuid4()}"
    location_claims.take(table_claims.claim_store(settings), location, table, parse_identifier(table), is_gone=lambda _claim: False, uuid=table_uuid)
    record = DueRecord(table=table, uuid=table_uuid, location=location, version=6, compaction=DUE, evaluated_at=datetime.now(UTC))
    write_due(settings.registry_root, settings.storage_options(), record)
    return record


def test_a_caller_sees_only_the_due_tables_it_maintains(real_ns_client: TestClient, tmp_path: Path) -> None:
    client = real_ns_client
    settings, grants = _governed(client, tmp_path)
    mine, _theirs, gone = (_recorded(settings, table, tmp_path) for table in ("acme$mine", "acme$theirs", "acme$gone"))
    # `acme$gone` was dropped and `acme$mine` is a name a table holds now ([[LH-523]]): the record's table is gone, so it
    # names no table and is shown to no one, whatever its name or its uuid could be granted. alice still holds a grant on
    # the gone table's uuid, as one a purge has not yet revoked would be.
    location_claims.release(table_claims.claim_store(settings), gone.location, "acme$gone")
    write_due(settings.registry_root, settings.storage_options(), gone.model_copy(update={"table": "acme$mine"}))
    grants.grant("alice", ("can_maintain", f"table:{mine.uuid}"), ("can_maintain", f"table:{gone.uuid}"))
    act_as(client, "alice")

    listed = client.get("/management/v1/maintenance/due", headers=AUTH)

    assert listed.status_code == 200, listed.text
    assert [(table["table"], table["uuid"]) for table in listed.json()["tables"]] == [("acme$mine", mine.uuid)]


def test_a_walk_a_page_at_a_time_reads_each_record_a_bounded_number_of_times(
    real_ns_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[[LH-562]]: the door pages on the records' keys and reads, names and judges records only as a page fills, so
    walking the list a page at a time reads each record fewer than twice here, where reading every record for every
    page read the list once per page."""
    client = real_ns_client
    settings, grants = _governed(client, tmp_path)
    records = [_recorded(settings, f"acme$t{index}", tmp_path) for index in range(4)]
    grants.grant("alice", *(("can_maintain", f"table:{record.uuid}") for record in records[1:]))
    act_as(client, "alice")
    read: list[str] = []
    reads_one = maintenance_due.read_due_named

    def _reads_one(control_root: str, options: StorageOptions, name: str) -> DueRecord | None:
        read.append(name)
        return reads_one(control_root, options, name)

    monkeypatch.setattr(maintenance_due, "read_due_named", _reads_one)

    walked: list[str] = []
    token: str | None = None
    for _ in range(len(records) + 1):
        page = client.get("/management/v1/maintenance/due", params={"limit": 1, **({"page_token": token} if token else {})}, headers=AUTH)
        assert page.status_code == 200, page.text
        walked.extend(table["table"] for table in page.json()["tables"])
        if (token := page.json()["page_token"]) is None:
            break

    assert sorted(walked) == ["acme$t1", "acme$t2", "acme$t3"]
    assert len(read) < 2 * len(records), f"the walk read {len(read)} records for {len(records)} held"
