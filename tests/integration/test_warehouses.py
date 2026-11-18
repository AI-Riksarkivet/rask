"""#3-A — the warehouse admin control-plane API over the app (our layer only; boto3 provision mocked).

Reuses the shared ``client`` fixture (mocked namespace backend). ``control_root`` points at a LOCAL temp
dir so the registry round-trips on the local FS with no object storage. Covers: create → list → get
(feature on, FGA off); the Unsupported answer when the feature is disabled; and the admin gate (FGA on) denying a
non-project-admin with 403.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from lance_namespace import DescribeNamespaceRequest, LanceNamespace, ListNamespacesRequest, ServiceUnavailableError, connect
from moto.server import ThreadedMotoServer
from opentelemetry.metrics import CallbackOptions
from pydantic import SecretStr

from catalog.api.dependencies import get_vendor
from catalog.api.v1.endpoints import warehouses as wh_ep
from catalog.core.config import Settings, get_settings
from catalog.core.vending import EncryptionAtRest, VendedSession
from catalog.services import control_records, seal_posture
from catalog.services import projects as proj_svc
from catalog.services import warehouses as wh_svc
from lakehouse import stored_objects
from lakehouse.governed import fga as fga_module
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.naming import UUID_PROPERTY
from lakehouse.storage_options import StorageOptions
from tests.integration.openfga_grants import Grants


def _settings(tmp_path: Any, store: str, *, fga: bool = False, reserved: str = "", detection_only: str = "") -> Settings:
    data: dict[str, Any] = {
        "impl": "dir",
        "root": "s3://lance-catalog",
        "s3_endpoint": store,
        "control_root": f"file://{tmp_path}",
        "reserved_buckets": reserved,
        "seal_detection_only": detection_only,
        "s3_access_key_id": "x",
        "s3_secret_access_key": "x",
    }
    if fga:
        data.update(
            {
                "fga_enabled": True,
                "oidc_enabled": True,
                "oidc_idp_id": "dex",
                "oidc_connectors": {"local": "dex"},
                "oidc_issuer": "https://idp",
                "oidc_audience": "lance",
            }
        )
    return Settings.model_validate(data)


@pytest.fixture
def store() -> Iterator[str]:
    """The estate's object store, one per test: a moto server the warehouse create door provisions buckets in and opens
    each new warehouse's directory catalog on."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def _catalog(settings: Settings, bucket: str) -> LanceNamespace:
    """The directory catalog at the root of ``bucket``, opened as the catalog opens a warehouse's."""
    return connect("dir", settings.namespace_properties(root=f"s3://{bucket}"))


def _mk_projects(client: TestClient, *project_ids: str) -> None:
    """Mint the tenants a test needs, through the REAL door (Decision 1: a warehouse requires an
    EXISTING project, and POST /management/v1/projects is the one way to make one). FGA is off in this suite, so
    the estate gate no-ops and the registry record is the whole effect — which is exactly the layer
    these tests exercise. Attacker tenants (`evil`) are minted too: Mallory legitimately owns her own
    project; what she must NOT be able to do is claim someone else's bucket or warehouse id."""
    for project_id in project_ids:
        resp = client.post("/management/v1/projects", json={"id": project_id})
        assert resp.status_code == 200, f"project {project_id}: {resp.text}"


def test_create_list_get_warehouse(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path, store)
    client.app.dependency_overrides[get_settings] = lambda: s
    _mk_projects(client, "acme")

    # A new warehouse declared primary is primary from its first write, as its project's routing reads it ([[LH-548]]);
    # one declaring no protection carries none, so the answer omits it rather than stating `false`.
    resp = client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme", "primary": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["id"], body["bucket"], body["root_uri"], body.get("primary"), "protected" in body) == ("wh-a", "wh-a", "s3://wh-a", True, False)
    # Its bucket is provisioned, and its directory catalog holds the warehouse's own row with a uuid, the parent every
    # name beneath it needs ([[LH-593]]).
    rows = _catalog(s, "wh-a").list_namespaces(ListNamespacesRequest(id=[])).namespaces
    described = _catalog(s, "wh-a").describe_namespace(DescribeNamespaceRequest(id=["wh-a"])).properties or {}
    assert (rows, UUID_PROPERTY in described) == (["wh-a"], True)

    listed = client.get("/management/v1/warehouses")
    assert listed.status_code == 200
    assert "wh-a" in [w["id"] for w in listed.json()]

    got = client.get("/management/v1/warehouses/wh-a")
    assert got.status_code == 200
    assert (got.json()["bucket"], got.json().get("primary")) == ("wh-a", True)


def test_a_project_holding_warehouses_refuses_its_delete_naming_each_one(
    client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletes are bottom-up (`open_hierarchy_lifecycle.md` Decision 3): a project holding warehouses refuses 409 and names
    each one with the door that empties it, never another tenant's, and the refusal removes nothing."""
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme", "globex")
    for warehouse, project in (("wh-b", "acme"), ("wh-a", "acme"), ("wh-g", "globex")):
        made = client.post("/management/v1/warehouses", json={"id": warehouse, "project": project})
        assert made.status_code == 200, made.text

    refused = client.delete("/management/v1/projects/acme")

    detail = refused.json()["detail"]
    assert (refused.status_code, refused.json()["code"]) == (409, 3), refused.text
    assert ("wh-a" in detail, "wh-b" in detail, "wh-g" in detail, "DELETE /management/v1/warehouses/" in detail) == (True, True, False, True), detail
    assert client.get("/management/v1/projects/acme").status_code == 200, "a refusal removes nothing"


def test_an_empty_project_is_retired_with_no_grant_to_revoke(client: TestClient, tmp_path: Any, store: str) -> None:
    """With authorization off the delete revokes nothing and answers so: a count of grants it never revoked would be a
    record of a revocation that did not happen."""
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme")

    retired = client.delete("/management/v1/projects/acme")

    assert (retired.status_code, retired.json()) == (200, {"project": "acme", "tuples_revoked": 0})
    assert client.get("/management/v1/projects/acme").status_code == 404


def _answered(status: int, code: str, operation: str) -> ClientError:
    """The error boto3 raises for a store's ``status`` answer with S3 error ``code``."""
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"RequestId": "", "HostId": "", "HTTPStatusCode": status, "HTTPHeaders": {}, "RetryAttempts": 0}},
        operation,
    )


class _VendingStore:
    """An object store reached with vended credentials, which honours or ignores what Lance relies on.

    Ignoring a credential's prefix is what a store that accepts the inline session policy and drops it looks
    like from outside; ignoring `If-None-Match` lets two writers both commit one Lance version. Its role holds no grant on
    any KMS key, so it refuses every write encrypting with one. `seal` is what it does with the catalog's seal (ADR 0242):
    `enforced` refuses a put over a tagged key, `unenforced` keeps the tag and refuses nothing, `untaggable` keeps no tags.
    It is both the vended writer's client and the catalog's own, so the probe's seal lands where its writer put. Each
    refusal is the store's own answer (403 AccessDenied, 412 PreconditionFailed). `over_sealed`, `multipart_over_sealed`
    and `on_seal` are another answer (a throttling, a 5xx) the store gives instead of its verdict: to every write over a
    sealed key, to a multipart begin of one alone, or to the seal.
    """

    def __init__(
        self,
        *,
        honours_scope: bool,
        honours_conditional_put: bool,
        seal: str = "enforced",
        over_sealed: int | None = None,
        on_seal: int | None = None,
        multipart_over_sealed: int | None = None,
    ) -> None:
        self.honours_scope = honours_scope
        self.honours_conditional_put = honours_conditional_put
        self.seal = seal
        self.over_sealed = over_sealed
        self.on_seal = on_seal
        self.multipart_over_sealed = multipart_over_sealed
        self.scope = ""
        self.keys: dict[str, bytes] = {}
        self.sealed: set[str] = set()

    def _check(self, key: str, operation: str) -> None:
        if self.honours_scope and not key.startswith(self.scope):
            raise _answered(403, "AccessDenied", operation)
        if key in self.sealed and self.over_sealed is not None:
            raise _answered(self.over_sealed, "SlowDown", operation)
        if self.seal == "enforced" and key in self.sealed:
            raise _answered(403, "AccessDenied", operation)

    def put_object(  # noqa: N803 — boto3's casing
        self, *, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str | None = None, SSEKMSKeyId: str | None = None, **_sse: object
    ) -> dict[str, Any]:
        self._check(Key, "PutObject")
        if SSEKMSKeyId is not None:
            raise _answered(403, "AccessDenied", "PutObject")
        if IfNoneMatch is not None and self.honours_conditional_put and Key in self.keys:
            raise _answered(412, "PreconditionFailed", "PutObject")
        self.keys[Key] = Body
        return {}

    def put_object_tagging(self, *, Bucket: str, Key: str, Tagging: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        if self.on_seal is not None:
            raise _answered(self.on_seal, "TooManyRequests", "PutObjectTagging")
        if self.seal == "untaggable":
            raise _answered(501, "NotImplemented", "PutObjectTagging")
        if Key not in self.keys:
            raise _answered(404, "NoSuchKey", "PutObjectTagging")
        self.sealed.add(Key)
        return {}

    def create_multipart_upload(self, *, Bucket: str, Key: str, **_sse: object) -> dict[str, Any]:  # noqa: N803
        if Key in self.sealed and self.multipart_over_sealed is not None:
            raise _answered(self.multipart_over_sealed, "InternalError", "CreateMultipartUpload")
        self._check(Key, "CreateMultipartUpload")
        return {"UploadId": "u"}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> dict[str, Any]:  # noqa: N803
        return {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"ContentLength": len(self.keys[Key])}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if self.honours_scope and not Key.startswith(self.scope):
            raise _answered(403, "AccessDenied", "GetObject")
        return {"Body": self.keys[Key]}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if self.honours_scope and not Key.startswith(self.scope):
            raise _answered(403, "AccessDenied", "DeleteObject")
        self.keys.pop(Key, None)
        return {}


class _ScopedVendor:
    """Vends a credential for one prefix and tells the store which prefix that is; `unreachable` is an STS that is down."""

    def __init__(self, store: _VendingStore, *, unreachable: bool, encrypts_with: str | None) -> None:
        self.store = store
        self.unreachable = unreachable
        self.encrypts_with = encrypts_with

    def vend(self, *, table_location: str, tier: str, **_kw: Any) -> VendedSession:
        if self.unreachable:
            raise ConnectionError("STS endpoint unreachable")
        self.store.scope = table_location.split("/", 3)[3]
        encryption = EncryptionAtRest() if self.encrypts_with is None else EncryptionAtRest(algorithm="aws:kms", kms_key_id=self.encrypts_with)
        return VendedSession(
            key_pair=S3KeyPair(access_key_id="k", secret_access_key=SecretStr("s")),
            endpoint="http://store:9000",
            region="us-east-1",
            encryption=encryption,
            expires_at_millis=1,
        )


_KMS_KEY = "arn:aws:kms:eu-north-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"


@pytest.mark.parametrize(
    ("honours_scope", "honours_conditional_put", "unreachable", "encrypts_with", "seal", "answers", "detection_only", "status", "code", "named"),
    [
        (False, True, False, None, "enforced", {}, "", 400, 13, "write_outside_refused"),
        (True, False, False, None, "enforced", {}, "", 400, 13, "conditional_put_refused"),
        (True, True, True, None, "enforced", {}, "", 503, 17, None),
        (True, True, False, None, "enforced", {}, "", 200, None, None),
        (True, True, False, _KMS_KEY, "enforced", {}, "", 400, 13, "encrypted_write"),
        (True, True, False, None, "unenforced", {}, "", 400, 13, "sealed: unenforced"),
        (True, True, False, None, "untaggable", {}, "", 400, 13, "sealed: untaggable"),
        (True, True, False, None, "unenforced", {}, "wh-p", 200, None, None),
        (True, True, False, None, "unenforced", {"over_sealed": 503}, "", 503, 17, None),
        (True, True, False, None, "unenforced", {"multipart_over_sealed": 500}, "", 400, 13, "sealed: unenforced"),
        (True, True, False, None, "enforced", {"on_seal": 429}, "", 503, 17, None),
    ],
    ids=[
        "store-ignores-scope",
        "store-ignores-conditional-put",
        "sts-unreachable",
        "store-conforms",
        "store-refuses-the-vended-kms-key",
        "store-takes-a-put-over-a-sealed-file",
        "store-keeps-no-tags",
        "git-declares-the-warehouse-detection-only",
        "store-answers-a-put-over-a-sealed-file-with-a-5xx",
        "store-takes-a-put-over-a-sealed-file-and-answers-its-multipart-with-a-5xx",
        "store-throttles-the-seal",
    ],
)
def test_a_warehouse_is_routable_only_on_a_store_that_passes_the_storage_probe(
    client: TestClient,
    tmp_path: Any,
    store: str,
    monkeypatch: pytest.MonkeyPatch,
    honours_scope: bool,
    honours_conditional_put: bool,
    unreachable: bool,
    encrypts_with: str | None,
    seal: str,
    answers: dict[str, int],
    detection_only: str,
    status: int,
    code: int | None,
    named: str | None,
) -> None:
    """Every Lance commit rests on the store refusing a second conditional put, and every vended credential on
    it honouring the session policy. A warehouse on a store that ignores either must not become routable, so the
    create runs the probe before it writes the registry record ([[LH-481]], ADR 0089). A store that broke a
    guarantee is the request's problem (InvalidInput, 13); a probe that could not reach the store learned
    nothing and asks for a retry (ServiceUnavailable, 17). A store refusing the encryption its vends name, its role
    holding no grant on the KMS key, broke a guarantee every writer needs ([[LH-262]]). A store's refusal names its check.

    The seal fails closed (ADR 0242): a store that takes a vended write credential's put over a sealed file, or keeps no
    tags, is refused unless git declares the warehouse detection-only, which registers it with what was measured. Only the
    store's explicit refusal (403) is one (ADR 0242 § Found by review, second round): a 5xx to the put over the sealed file,
    or a throttling of the seal, measured nothing, and the create answers 503 with nothing registered: a store that never
    refused the put is not `enforced`, and a throttled one is not `untaggable`. A put the store took over the sealed file
    is the proof of `unenforced` whatever the multipart begin answered (ADR 0242 § Found by review, third round, B2): a
    500 to the begin leaves it refused 400 `sealed: unenforced`."""
    vending = _VendingStore(honours_scope=honours_scope, honours_conditional_put=honours_conditional_put, seal=seal, **answers)
    monkeypatch.setattr(wh_ep, "s3_client", lambda *a, **k: vending)
    monkeypatch.setattr(stored_objects, "object_client", lambda *a, **k: vending)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store, detection_only=detection_only)
    client.app.dependency_overrides[get_vendor] = lambda: _ScopedVendor(vending, unreachable=unreachable, encrypts_with=encrypts_with)
    _mk_projects(client, "acme")

    resp = client.post("/management/v1/warehouses", json={"id": "wh-p", "project": "acme"})

    registered = wh_svc.get_warehouse(f"file://{tmp_path}", StorageOptions(), "wh-p") is not None
    assert (resp.status_code, registered) == (status, status == 200), resp.text
    if code is not None:
        assert (resp.headers["content-type"], resp.json()["code"]) == ("application/problem+json", code), resp.text
    if named is not None:
        assert f"({named})" in resp.json()["detail"], resp.text
    if status == 200:
        assert resp.json()["sealed"] == seal, resp.text
        assert vending.sealed and not set(vending.keys) & vending.sealed, "the probe leaves no sealed file behind"
        exported = {str((point.attributes or {})["lance.warehouse"]): dict(point.attributes or {}) for point in seal_posture._observe(CallbackOptions())}
        assert exported["wh-p"] == {"lance.warehouse": "wh-p", "lance.seal": seal, "lance.seal_required": "false" if detection_only else "true"}


def test_create_warehouse_cross_project_collision_409(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cross-tenant takeover guard (audit F1): an admin of a DIFFERENT project must NOT re-register an
    # existing warehouse id under their project (which would ADD their project's read-cascade over the
    # victim's tables). A same-project re-create stays idempotent for the partial-failure retry path.
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme", "evil")
    assert client.post("/management/v1/warehouses", json={"id": "wh-x", "project": "acme"}).status_code == 200
    r = client.post("/management/v1/warehouses", json={"id": "wh-x", "project": "evil"})
    assert r.status_code == 409, r.text
    assert client.post("/management/v1/warehouses", json={"id": "wh-x", "project": "acme"}).status_code == 200


def test_create_warehouse_bucket_claimed_by_another_project_409(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Mallory layer 1 (audit 2026-07-23): the bucket is caller-chosen and provisioning an EXISTING bucket
    # is a silent no-op, so a rival project could register its OWN warehouse id over the victim's bucket —
    # then set a project policy governing (and destroying version history in) the victim's data. The
    # bucket-claim scan mirrors the warehouse-id guard: a DIFFERENT project's claim → 409; the same
    # project re-claiming its own bucket under another warehouse id stays allowed.
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme", "evil")
    assert client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme", "bucket": "acme-wh"}).status_code == 200
    r = client.post("/management/v1/warehouses", json={"id": "wh-evil", "project": "evil", "bucket": "acme-wh"})
    assert r.status_code == 409, r.text
    same = client.post("/management/v1/warehouses", json={"id": "wh-a2", "project": "acme", "bucket": "acme-wh"})
    assert same.status_code == 200, same.text


def test_create_warehouse_reserved_bucket_refused(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Mallory layer 1b: the shared catalog root bucket (settings.root) and the platform's buckets
    # (LANCE_RESERVED_BUCKETS, populated by the chart from objectStore.buckets) are platform storage — no
    # project may register a warehouse over them.
    provisioned: list[str] = []
    monkeypatch.setattr(wh_svc, "provision_bucket", lambda bucket, so: provisioned.append(bucket))
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store, reserved="lance-source")
    _mk_projects(client, "evil")
    for bucket in ("lance-catalog", "lance-source"):  # the shared root + a configured zone bucket
        r = client.post("/management/v1/warehouses", json={"id": "wh-evil", "project": "evil", "bucket": bucket})
        assert r.status_code == 400, r.text
    assert provisioned == []  # refused before any bucket/registry/FGA side effect


def test_list_warehouses_tolerates_a_corrupt_record(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # One corrupt registry object must not 500 the listing (nor void the project-policy resolution that
    # consumes the same list) — skip-with-warning, keep the readable records.
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme")
    assert client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme"}).status_code == 200
    (tmp_path / "_warehouses" / "zzz-corrupt.json").write_text("{truncated")
    (tmp_path / "_warehouses" / "zzz-idless.json").write_text('{"bucket": "x"}')
    listed = client.get("/management/v1/warehouses")
    assert listed.status_code == 200, listed.text
    assert [w["id"] for w in listed.json()] == ["wh-a"]


def test_explicit_bucket_name(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme")
    resp = client.post("/management/v1/warehouses", json={"id": "wh-b", "project": "acme", "bucket": "tenant-b-data"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["root_uri"] == "s3://tenant-b-data"


def test_invalid_id_rejected_400(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme")
    # Uppercase + underscore is not a DNS-safe bucket fragment → 400 (never a malformed bucket create).
    assert client.post("/management/v1/warehouses", json={"id": "Bad_Name", "project": "acme"}).status_code == 400
    # An id shaped like the uuid a creation is named by in OpenFGA is refused, so an id and a uuid are never one string
    # (ADR 0245); a project's create refuses one the same way.
    assert client.post("/management/v1/warehouses", json={"id": "0" * 32, "project": "acme"}).status_code == 400
    assert client.post("/management/v1/projects", json={"id": "0" * 32}).status_code == 400


def test_recreate_does_not_reactivate_a_deactivated_warehouse(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # AUDIT #1: an idempotent re-create (GitOps reconcile / retry) must NOT silently lift a quarantine. The
    # mutable lifecycle fields (status, created_at) are carried forward on a same-project re-create;
    # reactivation goes ONLY through /activate.
    s = _settings(tmp_path, store)
    client.app.dependency_overrides[get_settings] = lambda: s
    _mk_projects(client, "acme")
    created = client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme"})
    assert created.status_code == 200
    created_at = created.json()["created_at"]
    assert client.post("/management/v1/warehouses/wh-a/deactivate").json()["status"] == "deactivated"

    re = client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme"})  # idempotent re-create
    assert re.status_code == 200
    assert re.json()["status"] == "deactivated", "re-create silently REACTIVATED a quarantined warehouse"
    assert re.json()["created_at"] == created_at, "re-create reset created_at"


@pytest.mark.parametrize(
    ("armed", "declared", "protected"),
    [
        pytest.param(False, {"protected": True}, True, id="naming-true-arms-it"),
        pytest.param(True, {}, True, id="naming-nothing-carries-it"),
        pytest.param(True, {"protected": False}, None, id="naming-false-clears-it"),
    ],
)
def test_a_re_create_applies_the_protection_it_names_and_carries_it_when_it_names_none(
    client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch, armed: bool, declared: dict[str, bool], protected: bool | None
) -> None:
    """[[LH-548]] The create door is a declaration a reconciler re-POSTs, so it converges on the protection it names
    either way, and a re-POST naming none leaves it as it stands: absent carried forward, never read as `false`."""
    s = _settings(tmp_path, store)
    client.app.dependency_overrides[get_settings] = lambda: s
    _mk_projects(client, "acme")
    assert client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme", "protected": armed}).status_code == 200

    answer = client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme"} | declared)

    assert (answer.status_code, answer.json().get("protected")) == (200, protected)


def test_a_warehouse_declared_protected_refuses_its_delete(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The protection a create declares is the flag the delete door's guard reads (#123), so even a purging delete is
    refused 409 code 3, naming `force=true`, before anything irreversible: the warehouse stands and no byte is purged."""
    purged: list[str] = []
    monkeypatch.setattr(wh_svc, "purge_bucket", lambda bucket, so: purged.append(bucket) or 0)
    s = _settings(tmp_path, store)
    client.app.dependency_overrides[get_settings] = lambda: s
    _mk_projects(client, "acme")
    assert client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme", "protected": True}).status_code == 200

    refused = client.delete("/management/v1/warehouses/wh-a", params={"purge_bucket": "true"})

    problem = refused.json()
    assert (refused.status_code, refused.headers["content-type"], problem["code"], "force=true" in problem["detail"]) == (
        409,
        "application/problem+json",
        3,
        True,
    )
    assert (client.get("/management/v1/warehouses/wh-a").status_code, purged) == (200, [])


def test_a_purged_buckets_claim_goes_with_its_last_warehouse(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A warehouse delete that purged its bucket releases the bucket's claim, so another project may register the bucket
    once its bytes are gone; a delete keeping the bytes keeps the claim, so no other project adopts what the bucket holds."""
    monkeypatch.setattr(wh_svc, "purge_bucket", lambda *_args, **_kwargs: 0)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme", "globex")
    for warehouse, bucket in (("wh-kept", "kept-bkt"), ("wh-purged", "purged-bkt")):
        assert client.post("/management/v1/warehouses", json={"id": warehouse, "project": "acme", "bucket": bucket}).status_code == 200
    assert client.delete("/management/v1/warehouses/wh-kept").status_code == 200
    assert client.delete("/management/v1/warehouses/wh-purged", params={"purge_bucket": "true"}).status_code == 200

    kept = client.post("/management/v1/warehouses", json={"id": "wh-g1", "project": "globex", "bucket": "kept-bkt"})
    purged = client.post("/management/v1/warehouses", json={"id": "wh-g2", "project": "globex", "bucket": "purged-bkt"})

    assert (kept.status_code, purged.status_code) == (409, 200), purged.text


def test_a_purge_that_fails_is_finished_by_the_same_call_again(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The warehouse's record goes after its bucket's purge, so a purge that fails answers PartiallyApplied with the
    warehouse still standing, and the same call issued again purges the bucket and removes the warehouse."""
    purged: list[str] = []

    def _a_store_that_fails_once(_root: str, _so: object, hold: wh_svc.PurgeHold, **_kwargs: object) -> int:
        purged.append(hold.bucket)
        if len(purged) == 1:
            raise OSError("the store did not answer the purge")
        return 3

    monkeypatch.setattr(wh_svc, "purge_bucket", _a_store_that_fails_once)
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme")
    assert client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme"}).status_code == 200

    failed = client.delete("/management/v1/warehouses/wh-a", params={"purge_bucket": "true"})
    standing = client.get("/management/v1/warehouses/wh-a").status_code
    again = client.delete("/management/v1/warehouses/wh-a", params={"purge_bucket": "true"})

    assert (failed.status_code, failed.json().get("partial"), standing) == (503, True, 200), failed.text
    assert (again.status_code, again.json().get("objects_purged"), purged) == (200, 3, ["wh-a", "wh-a"]), again.text
    assert client.get("/management/v1/warehouses/wh-a").status_code == 404


def test_deactivate_missing_warehouse_404(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    assert client.post("/management/v1/warehouses/ghost/deactivate").status_code == 404


def test_deactivate_hides_existence_from_non_admin_404(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # AUDIT #4: deactivate/activate read the record BEFORE the admin gate (they need its project). A missing
    # warehouse 404s; a present-but-unauthorized one must ALSO 404 (not 403) so a non-admin cannot probe which
    # warehouse ids exist. The fix collapses denied → not-found.
    s = _settings(tmp_path, store, fga=True)
    client.app.dependency_overrides[get_settings] = lambda: s
    # Registry seeded DIRECTLY: this test runs FGA-ON with a deny/mock client, so the estate gate
    # on POST /management/v1/projects would interfere — and project existence is not the layer under test.
    proj_svc.put_project(f"file://{tmp_path}", StorageOptions(), {"id": "acme", "created_at": "t", "created_by": "seed"})
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
    verifier.verify.return_value = PersonPrincipal(
        principal=Principal(idp_id="dex", subject="mallory"), claims=IDToken(iss="i", sub="mallory", aud="lance", exp=1, iat=1)
    )
    client.app.state.oidc = verifier
    client.app.state.fga = MagicMock()

    async def deny(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(fga_module, "check", deny)
    r = client.post("/management/v1/warehouses/wh-real/deactivate", headers={"authorization": "Bearer t"})
    assert r.status_code == 404, r.text  # NOT 403 — a non-admin cannot learn wh-real exists


def test_create_denied_for_non_admin_403(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store, fga=True)
    # Registry seeded DIRECTLY: this test runs FGA-ON with a deny/mock client, so the estate gate
    # on POST /management/v1/projects would interfere — and project existence is not the layer under test.
    proj_svc.put_project(f"file://{tmp_path}", StorageOptions(), {"id": "acme", "created_at": "t", "created_by": "seed"})
    verifier = MagicMock()
    verifier.verify.return_value = PersonPrincipal(
        principal=Principal(idp_id="dex", subject="mallory"), claims=IDToken(iss="i", sub="mallory", aud="lance", exp=1, iat=1)
    )
    client.app.state.oidc = verifier
    # A store that grants nothing and holds every project born (ADR 0241): the door opens its fence, then is refused.
    client.app.state.fga = Grants()
    resp = client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme"}, headers={"authorization": "Bearer t"})
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# The mint race is closed AT THE STORE
# --------------------------------------------------------------------------- #


def test_create_warehouse_lost_race_cannot_take_over(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # The TOCTOU the conditional create closes: the takeover guard's read happens BEFORE the rival's
    # record lands (simulated by a first read that returns None), so the guard passes for BOTH
    # creates — only store arbitration can refuse the second. Before F1 this ended in a silent
    # last-writer-wins overwrite: evil's record replaced acme's, with both projects' FGA edges live.
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme", "evil")
    assert client.post("/management/v1/warehouses", json={"id": "wh-race", "project": "acme"}).status_code == 200

    real_get = wh_svc.get_warehouse_record
    reads = {"n": 0}

    def racy_get(root: str, so: StorageOptions, wid: str) -> dict[str, str] | None:
        reads["n"] += 1
        if reads["n"] == 1:  # the guard's read: the rival's write is "not yet visible"
            return None
        return real_get(root, so, wid)  # the post-conflict re-read sees the truth

    # F4 moved the post-conflict re-read OUT of `get_warehouse` and INTO the conditional write
    # (`upsert_warehouse` → `mutate_json` → `read_json`), so counting the guard's record reads stopped
    # measuring anything. Count where the work actually happens now; the property under test —
    # "the store, not the guard, refused" — is unchanged.
    from lakehouse import records as rec

    real_read_json = rec.read_json
    conditional_reads = {"n": 0}

    def counting_read_json(root: str, so: StorageOptions, key: str) -> Any:
        conditional_reads["n"] += 1
        return real_read_json(root, so, key)

    monkeypatch.setattr(wh_svc, "get_warehouse_record", racy_get)
    monkeypatch.setattr(rec, "read_json", counting_read_json)
    # evil names a DIFFERENT bucket so the (unblinded) bucket-claim scan cannot catch this — the
    # id-mint conditional create is the only remaining defense, which is the point of the test.
    r = client.post("/management/v1/warehouses", json={"id": "wh-race", "project": "evil", "bucket": "evil-bkt"})
    assert r.status_code == 409, r.text
    assert conditional_reads["n"] >= 1, "the conditional write never re-read — the guard refused, not the store"
    assert reads["n"] == 1, "the guard read more than once; the blinding no longer models the race"
    monkeypatch.setattr(wh_svc, "get_warehouse_record", real_get)
    assert wh_svc.get_warehouse(f"file://{tmp_path}", StorageOptions(), "wh-race")["project"] == "acme"


def test_a_create_that_lost_the_race_for_its_id_made_nothing(client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same race, same project (a GitOps controller re-POSTing concurrently): the create that lost the mint wrote no
    # record, so it answers the id that exists, 409, seeding and announcing nothing under the uuid it minted, which names
    # no record (ADR 0245, review T9); the winner's record stands as it is, its quarantine with it, and asked again the
    # create converges onto it.
    client.app.dependency_overrides[get_settings] = lambda: _settings(tmp_path, store)
    _mk_projects(client, "acme")
    assert client.post("/management/v1/warehouses", json={"id": "wh-conv", "project": "acme"}).status_code == 200
    root = f"file://{tmp_path}"
    wh_svc.set_warehouse_status(root, StorageOptions(), "wh-conv", "deactivated", uuid=(wh_svc.get_warehouse(root, StorageOptions(), "wh-conv") or {})["uuid"])

    real_get = wh_svc.get_warehouse_record
    reads = {"n": 0}

    def racy_get(rt: str, so: StorageOptions, wid: str) -> dict[str, str] | None:
        reads["n"] += 1
        if reads["n"] == 1:
            return None
        return real_get(rt, so, wid)

    before = wh_svc.get_warehouse(root, StorageOptions(), "wh-conv")
    monkeypatch.setattr(wh_svc, "get_warehouse_record", racy_get)
    r = client.post("/management/v1/warehouses", json={"id": "wh-conv", "project": "acme"})
    assert (r.status_code, r.json()["code"]) == (409, 2), r.text
    monkeypatch.setattr(wh_svc, "get_warehouse_record", real_get)
    assert wh_svc.get_warehouse(root, StorageOptions(), "wh-conv") == before, "the create that lost changed the winner's record"
    again = client.post("/management/v1/warehouses", json={"id": "wh-conv", "project": "acme"})
    surviving = wh_svc.get_warehouse(root, StorageOptions(), "wh-conv") or {}
    assert (again.status_code, surviving.get("uuid"), surviving.get("status")) == (200, (before or {}).get("uuid"), "deactivated"), again.text


def test_the_catalog_probes_every_warehouse_it_serves_and_keeps_what_it_last_measured(
    client: TestClient, tmp_path: Any, store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0242 § Found by review, second round: the catalog's own seal probe (`catalog.api.seal_probe`, at its start and
    on `catalog.sealProbe.periodSeconds`) measures every warehouse it serves, the estate's own included, which no
    registration ever probed, and records and exports each, so `WarehouseSealLost` reads a signal that moves with no
    hand-run validate: the start measures them whatever became of the grant backfill before it, and a store that stops
    enforcing the seal is recorded `unenforced` on the next tick. A probe that
    learned nothing (the store answering a put over the sealed file with a 503) leaves each warehouse as last measured, at
    the tick and at validate, which reports the check inconclusive, so a store that refused nothing never reads as one
    that refused everything. A replica that did not probe exports what the records hold, a deleted warehouse included."""
    import asyncio

    import catalog.main as boot
    from catalog.api import seal_probe
    from catalog.services import warehouse_grant_backfill

    settings = _settings(tmp_path, store).model_copy(update={"seal_probe_period_seconds": 3600.0})
    vending = _VendingStore(honours_scope=True, honours_conditional_put=True, seal="enforced")
    monkeypatch.setattr(wh_ep, "s3_client", lambda *a, **k: vending)
    monkeypatch.setattr(stored_objects, "object_client", lambda *a, **k: vending)
    client.app.dependency_overrides[get_settings] = lambda: settings
    vendor = _ScopedVendor(vending, unreachable=False, encrypts_with=None)
    client.app.dependency_overrides[get_vendor] = lambda: vendor
    monkeypatch.setattr(client.app.state, "vendor", vendor, raising=False)
    _mk_projects(client, "acme")
    assert client.post("/management/v1/warehouses", json={"id": "wh-p", "project": "acme"}).status_code == 200
    control = f"file://{tmp_path}"

    def measured() -> dict[str, str | None]:
        exported = {
            str((point.attributes or {})["lance.warehouse"]): str((point.attributes or {})["lance.seal"]) for point in seal_posture._observe(CallbackOptions())
        }
        return {
            "estate": exported.get(settings.estate_warehouse),
            "wh-p": exported.get("wh-p"),
            "estate-recorded": (wh_svc.estate_seal(control, StorageOptions(), settings.estate_warehouse) or {}).get("sealed"),
            "wh-p-recorded": (wh_svc.get_warehouse(control, StorageOptions(), "wh-p") or {}).get("sealed"),
        }

    def measured_at() -> dict[str, float]:
        return {str((point.attributes or {})["lance.warehouse"]): float(point.value) for point in seal_posture._observe_measured(CallbackOptions())}

    async def unanswered(_settings: Any) -> None:
        raise OSError("the store did not answer the grant backfill")

    monkeypatch.setattr(warehouse_grant_backfill, "backfill", unanswered)
    monkeypatch.setattr(client.app.state, "settings", settings, raising=False)
    monkeypatch.setattr(seal_posture, "_never_measured", set())
    # A warehouse no probe has ever measured exports 0, so the alert on the measurement's age reads a series for it.
    vending.over_sealed = 503
    ticks = [asyncio.run(seal_probe.probe_all(client.app, settings))]
    assert measured_at()[settings.estate_warehouse] == 0.0 and measured_at()["wh-p"] > 0.0
    # A replica whose timer never won the window exports the same off the records alone (ADR 0242 § Found by review,
    # fourth round, C3).
    monkeypatch.setattr(seal_posture, "_postures", {})
    monkeypatch.setattr(seal_posture, "_never_measured", set())
    seal_posture.load(settings)
    assert measured_at()[settings.estate_warehouse] == 0.0 and measured_at()["wh-p"] > 0.0
    # Where no probe runs on a period, nothing is owed a measurement, and nothing unmeasured is exported.
    seal_posture.load(settings.model_copy(update={"seal_probe_period_seconds": 0.0}))
    assert settings.estate_warehouse not in measured_at()
    vending.over_sealed = None
    asyncio.run(boot.converge_at_boot(client.app))
    assert measured() == {"estate": "enforced", "wh-p": "enforced", "estate-recorded": "enforced", "wh-p-recorded": "enforced"}
    vending.seal = "unenforced"
    ticks.append(asyncio.run(seal_probe.probe_all(client.app, settings)))
    assert measured() == {"estate": "unenforced", "wh-p": "unenforced", "estate-recorded": "unenforced", "wh-p-recorded": "unenforced"}
    concluded = measured_at()
    vending.seal, vending.over_sealed = "enforced", 503
    ticks.append(asyncio.run(seal_probe.probe_all(client.app, settings)))
    validated = client.post("/management/v1/warehouses/wh-p/validate")

    assert [(tick.measured, tick.unprobed) for tick in ticks] == [(0, 2), (2, 0), (0, 2)]
    assert measured() == {"estate": "unenforced", "wh-p": "unenforced", "estate-recorded": "unenforced", "wh-p-recorded": "unenforced"}
    # The time of the last conclusive measurement stays where it was through the inconclusive tick and validate, and a
    # replica that starts again reads it off the records, so `WarehouseSealUnmeasured` sees its age grow (ADR 0242 § Found
    # by review, third round, B2).
    assert set(concluded) == {settings.estate_warehouse, "wh-p"} and measured_at() == concluded
    monkeypatch.setattr(seal_posture, "_postures", {})
    seal_posture.load(settings)
    assert measured_at() == concluded
    assert validated.status_code == 200, validated.text
    report = validated.json()
    assert (report["outcome"], report.get("sealed")) == ("inconclusive", None), report
    assert [check["outcome"] for check in report["checks"] if check["name"] == "overwrite_of_a_sealed_file_refused"] == ["inconclusive"], report

    # The warehouse deleted on another replica, which this one sees only by a load: a registry that does not answer
    # leaves every series as it was, and the next load drops the deleted warehouse's, so `WarehouseSealUnmeasured` does
    # not page for it here two periods later (ADR 0242 § Found by review, fourth round, C3).
    # A warehouse on another store, which the probe does not serve, is owed no measurement and exports none.
    elsewhere = {
        key: value for key, value in (wh_svc.get_warehouse(control, StorageOptions(), "wh-p") or {}).items() if key not in {"sealed", "sealed_at", "uuid"}
    }
    wh_svc.put_warehouse(control, StorageOptions(), {**elsewhere, "id": "wh-elsewhere", "endpoint": "http://another-store:9000"})
    ended = wh_svc.get_warehouse(control, StorageOptions(), "wh-p") or {}
    control_records.end_record(
        control, StorageOptions(), "warehouse", wh_svc.record_key("wh-p"), ended["uuid"], now=datetime.now(UTC), states=frozenset({"live"})
    )

    def unanswered_registry(*_args: Any) -> list[dict[str, str]]:
        raise ServiceUnavailableError("the object store did not answer reading the registry")

    with monkeypatch.context() as outage:
        outage.setattr(wh_svc, "list_warehouses", unanswered_registry)
        seal_posture.load(settings)
    assert measured_at() == concluded
    seal_posture.load(settings)
    assert set(measured_at()) == {settings.estate_warehouse}
    assert measured()["wh-p"] is None
