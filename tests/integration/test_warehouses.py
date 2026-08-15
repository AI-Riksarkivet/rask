"""#3-A — the warehouse admin control-plane API over the app (our layer only; boto3 provision mocked).

Reuses the shared ``client`` fixture (mocked namespace backend). ``control_root`` points at a LOCAL temp
dir so the registry round-trips on the local FS with no object storage. Covers: create → list → get
(feature on, FGA off); the 501 when the feature is disabled; and the admin gate (FGA on) denying a
non-project-admin with 403.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from catalog.core.config import Settings, get_settings
from catalog.services import projects as proj_svc
from catalog.services import warehouses as wh_svc
from fastapi.testclient import TestClient

from service_kit.governed import fga as fga_module
from service_kit.governed.oidc import IDToken


def _settings(tmp_path: Any, *, enabled: bool = True, fga: bool = False, reserved: str = "") -> Settings:
    data: dict[str, Any] = {
        "impl": "dir",
        "root": "s3://lance-catalog",
        "warehouses_enabled": enabled,
        "control_root": f"file://{tmp_path}",
        "reserved_buckets": reserved,
        "s3_access_key_id": "x",
        "s3_secret_access_key": "x",
    }
    if fga:
        data.update(
            {
                "fga_enabled": True,
                "oidc_enabled": True,
                "oidc_issuer": "https://idp",
                "oidc_audience": "lance",
            }
        )
    return Settings.model_validate(data)


def _mk_projects(client: TestClient, *project_ids: str) -> None:
    """Mint the tenants a test needs, through the REAL door (Decision 1: a warehouse requires an
    EXISTING project, and POST /v1/projects is the one way to make one). FGA is off in this suite, so
    the estate gate no-ops and the registry record is the whole effect — which is exactly the layer
    these tests exercise. Attacker tenants (`evil`) are minted too: Mallory legitimately owns her own
    project; what she must NOT be able to do is claim someone else's bucket or warehouse id."""
    for project_id in project_ids:
        resp = client.post("/v1/projects", json={"id": project_id})
        assert resp.status_code == 200, f"project {project_id}: {resp.text}"


def test_create_list_get_warehouse(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    provisioned: list[str] = []
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: provisioned.append(bucket))
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    _mk_projects(client, "acme")

    resp = client.post("/v1/warehouses", json={"id": "wh-a", "project": "acme"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "wh-a" and body["bucket"] == "wh-a" and body["root_uri"] == "s3://wh-a"
    assert provisioned == ["wh-a"]  # the physical bucket was provisioned at runtime

    listed = client.get("/v1/warehouses")
    assert listed.status_code == 200
    assert "wh-a" in [w["id"] for w in listed.json()]

    got = client.get("/v1/warehouses/wh-a")
    assert got.status_code == 200 and got.json()["bucket"] == "wh-a"


def test_create_warehouse_cross_project_collision_409(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cross-tenant takeover guard (audit F1): an admin of a DIFFERENT project must NOT re-register an
    # existing warehouse id under their project (which would ADD their project's read-cascade over the
    # victim's tables). A same-project re-create stays idempotent for the partial-failure retry path.
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    _mk_projects(client, "acme", "evil")
    assert client.post("/v1/warehouses", json={"id": "wh-x", "project": "acme"}).status_code == 200
    r = client.post("/v1/warehouses", json={"id": "wh-x", "project": "evil"})
    assert r.status_code == 409, r.text
    assert client.post("/v1/warehouses", json={"id": "wh-x", "project": "acme"}).status_code == 200


def test_create_warehouse_bucket_claimed_by_another_project_409(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Mallory layer 1 (audit 2026-07-23): the bucket is caller-chosen and provisioning an EXISTING bucket
    # is a silent no-op, so a rival project could register its OWN warehouse id over the victim's bucket —
    # then set a project policy governing (and destroying version history in) the victim's data. The
    # bucket-claim scan mirrors the warehouse-id guard: a DIFFERENT project's claim → 409; the same
    # project re-claiming its own bucket under another warehouse id stays allowed.
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    _mk_projects(client, "acme", "evil")
    assert client.post("/v1/warehouses", json={"id": "wh-a", "project": "acme", "bucket": "acme-wh"}).status_code == 200
    r = client.post("/v1/warehouses", json={"id": "wh-evil", "project": "evil", "bucket": "acme-wh"})
    assert r.status_code == 409, r.text
    same = client.post("/v1/warehouses", json={"id": "wh-a2", "project": "acme", "bucket": "acme-wh"})
    assert same.status_code == 200, same.text


def test_create_warehouse_reserved_bucket_refused(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Mallory layer 1b: the shared catalog root bucket (settings.root) and the medallion zone buckets
    # (LANCE_RESERVED_BUCKETS, populated by the chart from medallion.buckets) are platform storage — no
    # project may register a warehouse over them.
    provisioned: list[str] = []
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: provisioned.append(bucket))
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, reserved="lance-source")
    _mk_projects(client, "evil")
    for bucket in ("lance-catalog", "lance-source"):  # the shared root + a configured zone bucket
        r = client.post("/v1/warehouses", json={"id": "wh-evil", "project": "evil", "bucket": bucket})
        assert r.status_code == 400, r.text
    assert provisioned == []  # refused before any bucket/registry/FGA side effect


def test_list_warehouses_tolerates_a_corrupt_record(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # One corrupt registry object must not 500 the listing (nor void the project-policy resolution that
    # consumes the same list) — skip-with-warning, keep the readable records.
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    _mk_projects(client, "acme")
    assert client.post("/v1/warehouses", json={"id": "wh-a", "project": "acme"}).status_code == 200
    (tmp_path / "_warehouses" / "zzz-corrupt.json").write_text("{truncated")
    (tmp_path / "_warehouses" / "zzz-idless.json").write_text('{"bucket": "x"}')
    listed = client.get("/v1/warehouses")
    assert listed.status_code == 200, listed.text
    assert [w["id"] for w in listed.json()] == ["wh-a"]


def test_namespace_binding_is_write_once_409(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Namespace-binding hijack guard (audit F2): a top-level namespace already bound to one warehouse cannot
    # be re-bound to another (which would overwrite routing + strand the first tenant's tables). The collision
    # is caught BEFORE any namespace create, so no live backend connection is needed.
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    s = _settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    _mk_projects(client, "acme")
    client.post("/v1/warehouses", json={"id": "wh-b", "project": "acme"})
    wh_svc.bind_namespace(s.registry_root, s.storage_options(), "shared", "wh-a", "s3://wh-a")
    r = client.post("/v1/warehouses/wh-b/namespaces", json={"namespace": "shared"})
    assert r.status_code == 409, r.text


def test_explicit_bucket_name(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    _mk_projects(client, "acme")
    resp = client.post("/v1/warehouses", json={"id": "wh-b", "project": "acme", "bucket": "tenant-b-data"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["root_uri"] == "s3://tenant-b-data"


def test_get_missing_warehouse_404(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    assert client.get("/v1/warehouses/nope").status_code == 404


def test_invalid_id_rejected_400(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    _mk_projects(client, "acme")
    # Uppercase + underscore is not a DNS-safe bucket fragment → 400 (never a malformed bucket create).
    assert client.post("/v1/warehouses", json={"id": "Bad_Name", "project": "acme"}).status_code == 400


def test_disabled_returns_501(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, enabled=False)
    _mk_projects(client, "acme")
    assert client.post("/v1/warehouses", json={"id": "wh-x", "project": "acme"}).status_code == 501


def test_recreate_does_not_reactivate_a_deactivated_warehouse(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # AUDIT #1: an idempotent re-create (GitOps reconcile / retry) must NOT silently lift a quarantine. The
    # mutable lifecycle fields (status, created_at) are carried forward on a same-project re-create;
    # reactivation goes ONLY through /activate.
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    s = _settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    _mk_projects(client, "acme")
    created = client.post("/v1/warehouses", json={"id": "wh-a", "project": "acme"})
    assert created.status_code == 200
    created_at = created.json()["created_at"]
    assert client.post("/v1/warehouses/wh-a/deactivate").json()["status"] == "deactivated"

    re = client.post("/v1/warehouses", json={"id": "wh-a", "project": "acme"})  # idempotent re-create
    assert re.status_code == 200
    assert re.json()["status"] == "deactivated", "re-create silently REACTIVATED a quarantined warehouse"
    assert re.json()["created_at"] == created_at, "re-create reset created_at"


def test_namespace_create_refused_on_deactivated_warehouse(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # AUDIT #2/#6: create_warehouse_namespace resolves the bucket DIRECTLY (bypassing the resolver's
    # deactivation gate), so it must gate on status itself — else a namespace + fresh FGA grants could be
    # provisioned inside a QUARANTINED bucket (a persistence foothold surviving a naive offboarding).
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    s = _settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    _mk_projects(client, "acme")
    client.post("/v1/warehouses", json={"id": "wh-a", "project": "acme"})
    client.post("/v1/warehouses/wh-a/deactivate")
    r = client.post("/v1/warehouses/wh-a/namespaces", json={"namespace": "newns"})
    assert r.status_code == 403, r.text
    assert "deactivated" in r.text.lower()


def test_deactivate_missing_warehouse_404(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    assert client.post("/v1/warehouses/ghost/deactivate").status_code == 404


def test_deactivate_hides_existence_from_non_admin_404(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # AUDIT #4: deactivate/activate read the record BEFORE the admin gate (they need its project). A missing
    # warehouse 404s; a present-but-unauthorized one must ALSO 404 (not 403) so a non-admin cannot probe which
    # warehouse ids exist. The fix collapses denied → not-found.
    s = _settings(tmp_path, fga=True)
    client.app.dependency_overrides[get_settings] = lambda: s
    # Registry seeded DIRECTLY: this test runs FGA-ON with a deny/mock client, so the estate gate
    # on POST /v1/projects would interfere — and project existence is not the layer under test.
    proj_svc.put_project(f"file://{tmp_path}", {}, {"id": "acme", "created_at": "t", "created_by": "seed", "protected": "false"})
    wh_svc.put_warehouse(
        s.registry_root,
        s.storage_options(),
        {
            "id": "wh-real",
            "bucket": "wh-real",
            "root_uri": "s3://wh-real",
            "project": "acme",
            "status": "active",
        },
    )
    verifier = MagicMock()
    verifier.verify.return_value = IDToken(iss="i", sub="mallory", aud="lance", exp=1, iat=1)
    client.app.state.oidc = verifier
    client.app.state.fga = MagicMock()

    async def deny(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(fga_module, "check", deny)
    r = client.post("/v1/warehouses/wh-real/deactivate", headers={"authorization": "Bearer t"})
    assert r.status_code == 404, r.text  # NOT 403 — a non-admin cannot learn wh-real exists


def test_mallory_cross_tenant_bucket_takeover_fails_at_every_layer(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The full Mallory walkthrough (audit 2026-07-23): claiming another tenant's bucket must be refused at
    # EVERY layer — the warehouse create (front door), the project-policy set (defense in depth if a rival
    # record got into the registry anyway), and the sweep's resolution (if a contested policy record got
    # written anyway). No layer may fall back to first-encountered-wins over the victim's data.
    from service_kit.lakehouse import maintenance_policies as mp

    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    s = _settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    _mk_projects(client, "acme", "evil")
    so = s.storage_options()
    assert client.post("/v1/warehouses", json={"id": "wh-a", "project": "acme", "bucket": "acme-wh"}).status_code == 200

    # Layer 1 — the shared-bucket claim is refused at warehouse create.
    r = client.post("/v1/warehouses", json={"id": "wh-evil", "project": "evil", "bucket": "acme-wh"})
    assert r.status_code == 409, r.text

    # Layer 2 — force the rival record straight into the registry: the policy set still refuses.
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-evil", "bucket": "acme-wh", "project": "evil", "status": "active"})
    r = client.post("/v1/project/evil/policy/set", json={"retention_days": 1, "retain_versions": 1})
    assert r.status_code == 409, r.text

    # Layer 3 — force contested policy records anyway: resolution warns and matches NEITHER.
    contested = [
        {"kind": "project", "id": "acme", "path": "", "buckets": ["acme-wh"], "retention_days": 365},
        {"kind": "project", "id": "evil", "path": "", "buckets": ["acme-wh"], "retention_days": 1},
    ]
    assert mp.resolve_policy(contested, "s3://acme-wh/medallion/gold/features") is None


def test_create_denied_for_non_admin_403(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, fga=True)
    # Registry seeded DIRECTLY: this test runs FGA-ON with a deny/mock client, so the estate gate
    # on POST /v1/projects would interfere — and project existence is not the layer under test.
    proj_svc.put_project(f"file://{tmp_path}", {}, {"id": "acme", "created_at": "t", "created_by": "seed", "protected": "false"})
    verifier = MagicMock()
    verifier.verify.return_value = IDToken(iss="i", sub="mallory", aud="lance", exp=1, iat=1)
    client.app.state.oidc = verifier
    fga_client = MagicMock()
    fga_client.close = AsyncMock()
    client.app.state.fga = fga_client

    async def deny(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(fga_module, "check", deny)
    resp = client.post("/v1/warehouses", json={"id": "wh-a", "project": "acme"}, headers={"authorization": "Bearer t"})
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# F1 (open_lakehouse_diff2.md) — the mint race is closed AT THE STORE
# --------------------------------------------------------------------------- #


def test_create_warehouse_lost_race_cannot_take_over(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The TOCTOU the conditional create closes: the takeover guard's read happens BEFORE the rival's
    # record lands (simulated by a first read that returns None), so the guard passes for BOTH
    # creates — only store arbitration can refuse the second. Before F1 this ended in a silent
    # last-writer-wins overwrite: evil's record replaced acme's, with both projects' FGA edges live.
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    _mk_projects(client, "acme", "evil")
    assert client.post("/v1/warehouses", json={"id": "wh-race", "project": "acme"}).status_code == 200

    real_get = wh_svc.get_warehouse
    reads = {"n": 0}

    def racy_get(root: str, so: dict[str, str], wid: str) -> dict[str, str] | None:
        reads["n"] += 1
        if reads["n"] == 1:  # the guard's read: the rival's write is "not yet visible"
            return None
        return real_get(root, so, wid)  # the post-conflict re-read sees the truth

    monkeypatch.setattr(wh_svc, "get_warehouse", racy_get)
    # evil names a DIFFERENT bucket so the (unblinded) bucket-claim scan cannot catch this — the
    # id-mint conditional create is the only remaining defense, which is the point of the test.
    r = client.post("/v1/warehouses", json={"id": "wh-race", "project": "evil", "bucket": "evil-bkt"})
    assert r.status_code == 409, r.text
    assert reads["n"] >= 2  # the conflict forced a re-read — the store, not the guard, refused
    monkeypatch.setattr(wh_svc, "get_warehouse", real_get)
    assert wh_svc.get_warehouse(f"file://{tmp_path}", {}, "wh-race")["project"] == "acme"  # type: ignore[index]


def test_create_warehouse_lost_race_same_project_converges(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same race, same project (a GitOps controller re-POSTing concurrently): must converge like the
    # sequential idempotent path — 200, and the winner's mutable lifecycle fields carried forward.
    # The quarantine rule is the load-bearing half: the loser's rebuilt record must NOT resurrect a
    # deactivated warehouse (the F4 interleaving, closed on the racing-create path by the re-read).
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    _mk_projects(client, "acme")
    assert client.post("/v1/warehouses", json={"id": "wh-conv", "project": "acme"}).status_code == 200
    root = f"file://{tmp_path}"
    wh_svc.set_warehouse_status(root, {}, "wh-conv", "deactivated")

    real_get = wh_svc.get_warehouse
    reads = {"n": 0}

    def racy_get(rt: str, so: dict[str, str], wid: str) -> dict[str, str] | None:
        reads["n"] += 1
        if reads["n"] == 1:
            return None
        return real_get(rt, so, wid)

    monkeypatch.setattr(wh_svc, "get_warehouse", racy_get)
    r = client.post("/v1/warehouses", json={"id": "wh-conv", "project": "acme"})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(wh_svc, "get_warehouse", real_get)
    surviving = wh_svc.get_warehouse(root, {}, "wh-conv")
    assert surviving is not None, "the loser's rebuild must not have DELETED the record it lost the race for"
    assert surviving["status"] == "deactivated"


# --------------------------------------------------------------------------- #
# diff2 F3 state 3 — the warehouse-scoped namespace create, the last of the three
#
# The namespace lands natively, the binding is written, then FGA is seeded. A failed seed left the
# namespace created-and-bound but owned by NOBODY, and the plain retry 409'd at native
# `NamespaceAlreadyExists` — so the only converging path was passing `adopt_existing=true`, a
# bytes-first MIGRATION flag pressed into service as an undocumented repair for a fault the caller
# neither caused nor could diagnose.
#
# The backend is FAKED here, deliberately. This suite has no object store, and the real
# `_namespace_for_root` dials the warehouse's own bucket (`s3://<bucket>` → minio:9000). A fake also
# makes the assertion sharper than a state check could: the question is whether the compensating
# `drop_namespace` was ISSUED, and the fake answers it directly. `native.call` is plain `getattr`
# dispatch, so any object with the right method names is a valid backend.
# --------------------------------------------------------------------------- #


_BEARER = {"Authorization": "Bearer t"}


class _FakeNamespace:
    """Records the calls the endpoint makes, and can be told to already hold a namespace."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.calls: list[str] = []

    def create_namespace(self, req: Any) -> Any:
        from lance_namespace import CreateNamespaceResponse, NamespaceAlreadyExistsError

        name = "$".join(req.id)
        self.calls.append(f"create:{name}")
        if name in self.existing:
            raise NamespaceAlreadyExistsError(f"exists: {name}")
        self.existing.add(name)
        return CreateNamespaceResponse()

    def drop_namespace(self, req: Any) -> Any:
        from lance_namespace import DropNamespaceResponse

        name = "$".join(req.id)
        self.calls.append(f"drop:{name}")
        self.existing.discard(name)
        return DropNamespaceResponse()


async def _failing_grant(*_a: object, **_kw: object) -> None:
    from lance_namespace import ServiceUnavailableError

    raise ServiceUnavailableError("fga down mid-create")


def _wh_with_fake_backend(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, wid: str, existing: set[str] | None = None) -> _FakeNamespace:
    """A warehouse + an FGA-ON stack whose backend is the fake above.

    FGA must be ON or there is nothing to fail: `seed_ownership` no-ops when it is off, so the whole
    compensation path is unreachable and the endpoint would answer 200 — which is exactly how the
    first draft of these tests passed while asserting nothing. The registry is seeded DIRECTLY for
    the same reason the suite's other FGA-ON tests do it: the estate gate on POST /v1/projects would
    interfere, and project existence is not the layer under test.
    """
    import catalog.api.v1.endpoints.warehouses as wh_ep

    fake = _FakeNamespace(existing)
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: None)
    monkeypatch.setattr(wh_ep, "_namespace_for_root", lambda request, settings, root_uri: fake)
    s = _settings(tmp_path, fga=True)
    client.app.dependency_overrides[get_settings] = lambda: s
    proj_svc.put_project(f"file://{tmp_path}", {}, {"id": "acme", "created_at": "t", "created_by": "seed", "protected": "false"})
    wh_svc.put_warehouse(
        s.registry_root,
        s.storage_options(),
        {"id": wid, "bucket": wid, "root_uri": f"s3://{wid}", "project": "acme", "status": "active"},
    )
    verifier = MagicMock()
    verifier.verify.return_value = IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    client.app.state.oidc = verifier
    fga_client = MagicMock()
    fga_client.close = AsyncMock()
    # `write` is AWAITED by grant_on_create, and the convergence test's retry reaches the real one —
    # a plain MagicMock returns a Mock, which `await` rejects. AsyncMock is what makes the SUCCESS
    # path drivable here, not just the failure path.
    fga_client.write = AsyncMock()
    client.app.state.fga = fga_client

    async def allow(_c: object, **_kw: object) -> bool:
        return True

    monkeypatch.setattr(fga_module, "check", allow)
    return fake


def test_warehouse_namespace_create_converges_after_a_failed_grant(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed seed must leave NOTHING behind — no namespace, no binding — so the ordinary retry
    succeeds instead of needing a migration flag."""
    fake = _wh_with_fake_backend(client, tmp_path, monkeypatch, "wh-f3")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fga_module, "grant_on_create", _failing_grant)
        failed = client.post("/v1/warehouses/wh-f3/namespaces", json={"namespace": "f3ns"}, headers=_BEARER)
    assert failed.status_code == 503, failed.text

    # The compensating drop was ISSUED, and the namespace is gone from the backend.
    assert fake.calls == ["create:f3ns", "drop:f3ns"], fake.calls
    assert "f3ns" not in fake.existing
    # The binding went with it: a binding naming a namespace that no longer exists is drift the
    # reconciler would later report as a mystery.
    assert wh_svc.binding_for_namespace(f"file://{tmp_path}", {}, "f3ns") is None

    # …and the PLAIN retry converges, with no `adopt_existing` anywhere near it.
    retried = client.post("/v1/warehouses/wh-f3/namespaces", json={"namespace": "f3ns"}, headers=_BEARER)
    assert retried.status_code == 200, retried.text
    binding = wh_svc.binding_for_namespace(f"file://{tmp_path}", {}, "f3ns")
    assert binding is not None and binding["warehouse_id"] == "wh-f3"


def test_adopted_namespace_is_never_compensated_away(client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE CARVE-OUT, and the one that would cost real data if it were wrong.

    `adopt_existing=true` is the bytes-first migration converging: the datasets were copied into the
    bucket FIRST and this call only attaches governance. So a failed seed must NOT drop the
    namespace — that would delete the very data the migration was moving, to repair a missing tuple,
    in response to a transient FGA blip. Same distinction the Arrow create door draws with
    `_compensation_allowed` for ExistOk: never compensate away something this request did not create.
    """
    fake = _wh_with_fake_backend(client, tmp_path, monkeypatch, "wh-adopt", existing={"migrated"})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fga_module, "grant_on_create", _failing_grant)
        failed = client.post("/v1/warehouses/wh-adopt/namespaces", json={"namespace": "migrated", "adopt_existing": True}, headers=_BEARER)
    assert failed.status_code == 503, failed.text

    # NO drop was issued, and the pre-existing namespace survived.
    assert "drop:migrated" not in fake.calls, "an adopted namespace was compensated away — migrated data is at risk"
    assert "migrated" in fake.existing
