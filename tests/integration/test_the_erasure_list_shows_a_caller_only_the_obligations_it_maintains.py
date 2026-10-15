"""`GET /management/v1/erasures` lists an erasure obligation only to a caller holding `can_maintain` on its table ([[LH-493]]).

An obligation names its table and the person who asked for the erasure, so the list is filtered per table as the due
list beside it is. On the real app with OIDC and FGA on, the OpenFGA store an in-memory double that allows exactly the
grants named, so what is asserted is the filter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient

from catalog.core.config import Settings, get_settings
from catalog.services import erasure_obligations
from catalog.services.erasure_obligations import ErasureObligation
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}


def test_a_caller_sees_only_the_obligations_it_maintains(real_ns_client: TestClient, tmp_path: Path) -> None:
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
    now = datetime.now(UTC)
    for table in ("acme$mine", "acme$theirs"):
        obligation = ErasureObligation(
            obligation_id=uuid4().hex,
            table=table,
            object_id=f"table:{table}",
            location=f"{tmp_path}/{table}",
            predicate_digest=erasure_obligations.digest("pii = 'alice'"),
            retain_days=0,
            refs={"main": 3},
            requested_by="user:olivia",
            requested_at=now,
            deadline=now + timedelta(days=30),
        )
        erasure_obligations.record(settings.registry_root, settings.storage_options(), obligation)
    grants.grant("alice", ("can_maintain", "table:acme$mine"))
    act_as(client, "alice")

    listed = client.get("/management/v1/erasures", headers=AUTH)

    assert listed.status_code == 200, listed.text
    assert [(entry["table"], entry["state"]) for entry in listed.json()["obligations"]] == [("acme$mine", "open")]
