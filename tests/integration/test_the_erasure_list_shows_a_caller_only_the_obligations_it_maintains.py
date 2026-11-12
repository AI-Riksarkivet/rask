"""`GET /management/v1/erasures` lists an erasure obligation only to a caller holding `can_maintain` on its table ([[LH-493]]).

An obligation names its table and the person who asked for the erasure, so the list is filtered per table as the due
list beside it is. On the real app with OIDC and FGA on, the OpenFGA store an in-memory double that allows exactly the
grants named, so what is asserted is the filter. The erasure list and the promotions list beside it name and judge
their records only as a page fills ([[LH-576]] E10b), as the due list does.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientBatchCheckResponse

from catalog.core.config import Settings, get_settings
from catalog.core.identifiers import parse_identifier
from catalog.services import erasure_obligations, promotions, table_claims
from catalog.services.erasure_obligations import ErasureObligation
from lakehouse import location_claims
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}


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


def _obligation(settings: Settings, table: str, tmp_path: Path, *, deadline: datetime) -> ErasureObligation:
    """An open obligation on ``table``, a table the catalog holds at a location of its own."""
    table_uuid, location = str(uuid4()), f"{tmp_path}/{uuid4()}"
    location_claims.take(table_claims.claim_store(settings), location, table, parse_identifier(table), is_gone=lambda _claim: False, uuid=table_uuid)
    obligation = ErasureObligation(
        obligation_id=uuid4().hex,
        table=table,
        table_uuid=table_uuid,
        object_id=f"table:{table_uuid}",
        location=location,
        predicate_digest=erasure_obligations.digest("pii = 'alice'"),
        retain_days=0,
        refs={"main": 3},
        requested_by="user:dex~olivia",
        requested_at=deadline - timedelta(days=30),
        deadline=deadline,
    )
    erasure_obligations.record(settings.registry_root, settings.storage_options(), obligation)
    return obligation


def _hold(settings: Settings, table: str, tmp_path: Path, *, deadline: datetime) -> promotions.Hold:
    """An open promotion hold on ``table``, a table the catalog holds at a location of its own."""
    table_uuid, location = str(uuid4()), f"{tmp_path}/{uuid4()}"
    location_claims.take(table_claims.claim_store(settings), location, table, parse_identifier(table), is_gone=lambda _claim: False, uuid=table_uuid)
    hold = promotions.Hold(
        table=table,
        uuid=table_uuid,
        location=location,
        version=2,
        published_version=1,
        reasons=["row count moved past the band"],
        requested_by="user:dex~olivia",
        opened_at=deadline - timedelta(hours=24),
        expires_at=deadline,
    )
    return promotions.open_hold(settings.registry_root, settings.storage_options(), hold)


def test_a_caller_sees_only_the_obligations_it_maintains(real_ns_client: TestClient, tmp_path: Path) -> None:
    client = real_ns_client
    settings, grants = _governed(client, tmp_path)
    deadline = datetime.now(UTC) + timedelta(days=30)
    mine, _theirs = (_obligation(settings, table, tmp_path, deadline=deadline) for table in ("acme$mine", "acme$theirs"))
    grants.grant("alice", ("can_maintain", f"table:{mine.table_uuid}"))
    act_as(client, "alice")

    listed = client.get("/management/v1/erasures", headers=AUTH)

    assert listed.status_code == 200, listed.text
    assert [(entry["table"], entry["state"]) for entry in listed.json()["obligations"]] == [("acme$mine", "open")]


@pytest.mark.parametrize(
    ("door", "rows", "relation", "recorded"),
    [
        pytest.param("/management/v1/erasures", "obligations", "can_maintain", lambda s, t, p, d: _obligation(s, t, p, deadline=d).table_uuid, id="erasures"),
        pytest.param("/management/v1/promotions", "holds", "can_get_metadata", lambda s, t, p, d: _hold(s, t, p, deadline=d).uuid, id="promotions"),
    ],
)
def test_a_page_judges_only_the_records_it_needs(
    real_ns_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    door: str,
    rows: str,
    relation: str,
    recorded: Callable[[Settings, str, Path, datetime], str],
) -> None:
    """A first page of one row judges about one record, where judging every record held asked OpenFGA about all of them."""
    client = real_ns_client
    settings, grants = _governed(client, tmp_path)
    start = datetime.now(UTC) + timedelta(days=1)
    uuids = [recorded(settings, f"acme$t{index}", tmp_path, start + timedelta(hours=index)) for index in range(8)]
    grants.grant("alice", *((relation, f"table:{table_uuid}") for table_uuid in uuids))
    act_as(client, "alice")
    judged: list[str] = []
    checks = grants.batch_check

    async def _counted(body: ClientBatchCheckRequest, options: dict[str, Any] | None = None) -> ClientBatchCheckResponse:
        judged.extend(item.object for item in body.checks)
        return await checks(body, options)

    monkeypatch.setattr(grants, "batch_check", _counted)

    first = client.get(door, params={"limit": 1}, headers=AUTH)

    assert first.status_code == 200, first.text
    assert [row["table"] for row in first.json()[rows]] == ["acme$t0"]
    assert first.json()["page_token"] is not None
    assert len(judged) <= 2, f"the first page of one row judged {len(judged)} of {len(uuids)} records"
