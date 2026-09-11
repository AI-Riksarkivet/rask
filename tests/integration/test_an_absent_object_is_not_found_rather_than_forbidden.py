"""A caller who can read the PARENT learns an absent child is absent, not that it is forbidden.

`authorize` is a router-wide dependency, so it answers before any endpoint runs: an object with no
tuples fails the FGA check and the request is refused 403 `PermissionDenied`, whether the object is
forbidden or simply does not exist. Measured on the deployed catalog 2026-09-11: an absent table under
a namespace alice holds `can_get_metadata`, `can_create_table` AND `can_delete` on answered 403 code 15
where the Lance Namespace spec prescribes 404 `TableNotFound` — a stock client cannot tell "ask someone
for access" from "you typed the wrong name".

THE OWNER'S RULE (2026-09-11): **404 on READ doors, 403 kept on destructive ones.** The estate's
no-existence-oracle property is deliberate and documented for `delete_warehouse` / `delete_project` /
`_set_warehouse_status`, which collapse PermissionDenied into NotFound precisely so a destructive door
cannot enumerate ids. That stays. A read door is different: a caller holding the parent's read rung can
already LIST the parent, so "does this child exist" is not information the 403 was protecting.

THE SAFETY PROPERTY, which is the half worth more than the feature: an object that EXISTS and is
forbidden must still answer 403. Turning that into 404 would tell an unauthorized caller which names
are real — the oracle this estate refuses — so the probe converts only an absence, and only for a
caller who already holds the parent rung. Nothing is read: the request never reaches the endpoint.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import NamespaceNotFoundError, TableNotFoundError

from catalog.core.config import get_settings


def _auth_on(client: TestClient) -> None:
    """Turn on OIDC + FGA with a verifier that returns alice, mirroring `test_authz.py::_wire`."""
    from catalog.core.config import Settings
    from service_kit.governed.oidc import IDToken

    def _settings() -> Settings:
        # `model_validate`, not `Settings(...)`: the fields carry LANCE_* aliases and
        # `populate_by_name` accepts the field names at runtime — the same shape `test_authz.py` uses.
        return Settings.model_validate(
            {
                "oidc_enabled": True,
                "oidc_issuer": "https://idp.example",
                "oidc_audience": "lance",
                "fga_enabled": True,
                "fga_api_url": "http://openfga:8080",
                "s3_access_key_id": "x",
                "s3_secret_access_key": "x",
            }
        )

    client.app.dependency_overrides[get_settings] = _settings
    verifier = MagicMock()
    verifier.verify.return_value = IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    client.app.state.oidc = verifier
    fga_client = MagicMock()
    fga_client.close = AsyncMock()
    client.app.state.fga = fga_client


def _deny_child_allow_parent(monkeypatch: pytest.MonkeyPatch, seen: list[dict]) -> None:
    """alice holds every rung on the PARENT namespace and none on the child table."""
    import service_kit.governed.fga as fga_module

    async def fake_check(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        seen.append({"user": user, "relation": relation, "obj": obj})
        return obj.startswith("namespace:")

    monkeypatch.setattr(fga_module, "check", fake_check)


def test_an_absent_table_under_a_readable_parent_is_404(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. The spec's answer for a name that is not there, to a caller who may look."""
    _auth_on(real_ns_client)
    seen: list[dict] = []
    _deny_child_allow_parent(monkeypatch, seen)

    resp = real_ns_client.post("/v1/table/db1$ghost/describe", json={}, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 404, f"an absent table answered {resp.status_code}; the spec prescribes 404 TableNotFound"
    assert seen, "the authz check never ran — this test would pass vacuously"


def test_an_EXISTING_forbidden_table_is_still_403(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE SAFETY PROPERTY. A 404 here would be the existence oracle the estate refuses.

    The object is present on the backend and alice holds nothing on it; the parent rung must not buy
    her the knowledge that this name is real, and it must certainly not buy her the data.
    """
    _auth_on(real_ns_client)
    seen: list[dict] = []
    _deny_child_allow_parent(monkeypatch, seen)

    # A backend that answers "it exists" for every probe — so only the authz verdict can decide.
    real_ns_client.app.state.namespace = SimpleNamespace(
        table_exists=lambda *_a, **_k: None,
        namespace_exists=lambda *_a, **_k: None,
        describe_table=lambda *_a, **_k: SimpleNamespace(location="s3://b/t"),
    )

    resp = real_ns_client.post("/v1/table/db1$real/describe", json={}, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 403, f"an existing forbidden table answered {resp.status_code} — that is an existence oracle"


def test_a_destructive_door_keeps_its_403_for_an_absent_object(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented exception stays: a destructive door must not become an enumeration surface.

    `drop` is owner-tier, and the no-existence-oracle rule exists so a caller cannot walk the id space
    by watching which deletes answer differently. Read doors were never part of that argument.
    """
    _auth_on(real_ns_client)
    seen: list[dict] = []
    _deny_child_allow_parent(monkeypatch, seen)

    resp = real_ns_client.post("/v1/table/db1$ghost/drop", json={}, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 403, f"a destructive door answered {resp.status_code}; it must stay 403 whether or not the object exists"


def test_the_probe_is_skipped_when_the_caller_cannot_read_the_parent(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """No parent rung, no answer — and no backend round trip either.

    A caller holding nothing gets exactly today's 403, so the change cannot be used to probe absence
    from outside the hierarchy, and a denial costs no extra IO on the path that denies most often.
    """
    import service_kit.governed.fga as fga_module

    _auth_on(real_ns_client)

    async def deny_all(_c: object, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(fga_module, "check", deny_all)

    resp = real_ns_client.post("/v1/table/db1$ghost/describe", json={}, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 403


def test_the_spec_errors_are_the_ones_raised() -> None:
    """The 404 must be the SPEC'S typed error, not a hand-picked status.

    A generated client dispatches on the numeric code, so answering 404 with anything but
    `TableNotFound` / `NamespaceNotFound` would be a status that reads right and deserialises wrong —
    the failure `ns_errors` exists to prevent.
    """
    assert TableNotFoundError("absent").code == 4
    assert NamespaceNotFoundError("absent").code == 1  # NOT 3 — read off ErrorCode, not assumed
