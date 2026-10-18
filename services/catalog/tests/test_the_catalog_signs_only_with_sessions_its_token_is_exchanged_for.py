"""[[XC-083]] The catalog holds no store key: its own IO and every vend sign with sessions its ServiceAccount token is exchanged for.

The pod projects a token for the object store's audience, and STS `AssumeRoleWithWebIdentity` exchanges it for a
temporary credential under the policy the store binds to the token's subject (ADR 0170). The store here is a moto server
enforcing IAM policy (`conftest.two_stores`): a request signed with a session moto no longer holds is refused, which is how
a session's expiry at the store is played. moto checks no token, so what each exchange asked for is read back from moto's
STS backend in this process: the session's name, its policy and its expiry. The catalog's clock is its session cache's
(`service_kit.governed.store_session.SESSIONS`), moved forward, and the catalog's holder is asked whether its session is
due, the call its background loop makes on every wake. The store's clock is moto's (`moto.sts.models.utcnow`), which
dates the expiry STS answers, so a store whose clock disagrees with the catalog's is played by shifting it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from moto.sts import models as sts_models

from service_kit.governed import store_session
from service_kit.governed.machine_identity import ServicePrincipal


#: The account moto files every role and session under.
ACCOUNT = "123456789012"
ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
ROWS = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": ["a", "b", "c"]})
#: What the catalog asks for its own IO: an hour, so a session it renews at the half still has thirty minutes for a
#: request already holding it.
SESSION_SECONDS = 3600


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class _Rig:
    """The running catalog, the token file its pod projects, the role its token maps to, and moto's record of its sessions."""

    def __init__(self, *, catalog: TestClient, clock: _Clock, token: Path, role_arn: str, bucket: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.catalog = catalog
        self.clock = clock
        self.token = token
        self.role_arn = role_arn
        self.bucket = bucket
        self._monkeypatch = monkeypatch

    def sessions(self) -> list[Any]:
        """Every session moto issued for this rig's role, oldest first."""
        return [role for role in sts_models.sts_backends[ACCOUNT]["aws"].assumed_roles if role.role_arn == self.role_arn]

    def own(self) -> Any:  # noqa: ANN401 — moto's AssumedRole
        """The newest session the catalog exchanged for its own IO."""
        return [role for role in self.sessions() if role.session_name == "catalog"][-1]

    def expire(self, session: Any) -> None:  # noqa: ANN401 — moto's AssumedRole
        """Drop ``session`` at the store, as its expiry would: a request it signs is refused from now on."""
        sts_models.sts_backends[ACCOUNT]["aws"].assumed_roles.remove(session)

    def later(self, seconds: float) -> None:
        """Move the catalog's clock forward, then wake its session holder once."""
        self.clock.now += seconds
        self.catalog.app.state.store_session.refresh_if_due()

    def with_warehouses(self) -> None:
        """Serve the warehouse doors, vending nothing: their scope probe asks for a session policy moto does not enforce."""
        from catalog.api.dependencies import get_vendor
        from catalog.core.config import get_settings
        from catalog.core.vending import ModeBVendor

        self._monkeypatch.setenv("LANCE_WAREHOUSES_ENABLED", "true")
        get_settings.cache_clear()
        self.catalog.app.dependency_overrides[get_vendor] = ModeBVendor


@pytest.fixture
def store_clock(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """How many seconds the store's clock runs ahead of the catalog's; none unless a case names it."""
    ahead = getattr(request, "param", 0)
    if ahead:
        moto_now = sts_models.utcnow
        monkeypatch.setattr(sts_models, "utcnow", lambda: moto_now() + timedelta(seconds=ahead))


@pytest.fixture
def sessions(store_clock: None, two_stores: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:  # noqa: ANN401 — conftest.TwoStores
    """The real catalog app with a projected token and no key pair anywhere: not in its environment, not in a store."""
    from catalog.core.config import get_settings

    clock = _Clock()
    monkeypatch.setattr(store_session, "SESSIONS", store_session.SessionCache(clock=clock))
    # The estate bucket, and every bucket the catalog provisions beside it.
    role_arn = two_stores.role_on(f"{two_stores.estate_bucket}*")
    token = tmp_path / "projected-rask-object-store-token"
    token.write_text("projected-for-rask-object-store")
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{two_stores.estate_bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{two_stores.estate_bucket}",
        "LANCE_S3_ENDPOINT": two_stores.url,
        "LANCE_S3_WEB_IDENTITY_TOKEN_FILE": str(token),
        "LANCE_S3_STS_ENDPOINT": two_stores.url,
        "LANCE_S3_ASSUME_ROLE_ARN": role_arn,
        "LANCE_S3_SESSION_SECONDS": str(SESSION_SECONDS),
        "LANCE_VENDING_MODE": "web_identity",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LANCE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("LANCE_S3_SECRET_ACCESS_KEY", raising=False)
    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield _Rig(catalog=client, clock=clock, token=token, role_arn=role_arn, bucket=two_stores.estate_bucket, monkeypatch=monkeypatch)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _a_namespace_create(rig: _Rig) -> Callable[[], httpx.Response]:
    return lambda: rig.catalog.post(f"/v1/namespace/ses{uuid.uuid4().hex[:10]}/create", json={})


def _a_table_create(rig: _Rig) -> Callable[[], httpx.Response]:
    namespace = f"ses{uuid.uuid4().hex[:10]}"
    assert rig.catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
    return lambda: rig.catalog.post(f"/v1/table/{namespace}${uuid.uuid4().hex[:10]}/create", content=_ipc(ROWS), headers=ARROW)


def _a_bucket_it_provisions(rig: _Rig) -> Callable[[], httpx.Response]:
    rig.with_warehouses()
    project = f"ses{uuid.uuid4().hex[:10]}"
    assert rig.catalog.post("/management/v1/projects", json={"id": project}).status_code == 200

    def create() -> httpx.Response:
        warehouse = f"wh{uuid.uuid4().hex[:10]}"
        return rig.catalog.post("/management/v1/warehouses", json={"id": warehouse, "project": project, "bucket": f"{rig.bucket}-{warehouse}"})

    return create


@pytest.mark.parametrize(
    ("door", "store_clock"),
    [
        pytest.param(_a_namespace_create, 0, id="the-native-namespace-connection"),
        pytest.param(_a_table_create, 0, id="a-pylance-write"),
        pytest.param(_a_bucket_it_provisions, 0, id="a-bucket-it-provisions"),
        pytest.param(_a_namespace_create, 3600, id="a-store-whose-clock-is-an-hour-ahead"),
    ],
    indirect=["store_clock"],
)
def test_the_catalogs_session_is_exchanged_again_before_it_expires_without_a_restart(
    sessions: _Rig, door: Callable[[_Rig], Callable[[], httpx.Response]]
) -> None:
    call = door(sessions)
    first = call()
    assert first.status_code == 200, f"the catalog did not sign with the session it was issued: {first.text}"
    held = sessions.own()

    sessions.later(SESSION_SECONDS / 2 + 1)
    sessions.expire(held)
    response = call()

    assert response.status_code == 200, f"the running catalog still signs with the session the store expired: {response.text}"


def test_a_vend_is_a_session_of_its_own_named_for_the_caller(sessions: _Rig) -> None:
    """What the session may reach is pinned per rung by `tests/integration/test_a_write_vend_reaches_only_what_its_rung_may_change.py`."""
    table = f"ses{uuid.uuid4().hex[:10]}$t"
    assert sessions.catalog.post(f"/v1/namespace/{table.split('$')[0]}/create", json={}).status_code == 200
    assert sessions.catalog.post(f"/v1/table/{table}/create", content=_ipc(ROWS), headers=ARROW).status_code == 200
    from catalog.api.security import authenticate

    sessions.catalog.app.dependency_overrides[authenticate] = lambda: ServicePrincipal(service_account="system:serviceaccount:rask:rask-sa-maintenance")

    vended = sessions.catalog.post(f"/management/v1/table/{table}/credentials")

    assert vended.status_code == 200, vended.text
    body = vended.json()
    options = body["credentials"]["storage_options"]
    session = next(role for role in sessions.sessions() if role.access_key_id == options["aws_access_key_id"])
    assert session.access_key_id != sessions.own().access_key_id, "a vend handed out the catalog's own session"
    assert session.session_name == "kubernetes-system-serviceaccount-rask-rask-sa-maintenance"
    assert options["aws_session_token"] == session.session_token
    # moto keeps a session's expiry as a naive UTC datetime.
    assert body["credentials"]["expires_at_millis"] == int(session.expiration.replace(tzinfo=UTC).timestamp() * 1000)


def test_a_session_the_catalog_cannot_renew_is_refused_once_it_expires(sessions: _Rig) -> None:
    """Fail closed: once the projected token is gone, nothing can be exchanged, and past its expiry the session signs nothing.

    The door answers the spec's ServiceUnavailable (code 17, 503), which a client retries. A catalog holding the token
    it read at boot would renew regardless and keep signing for an identity the kubelet no longer vouches for.
    """
    call = _a_namespace_create(sessions)
    assert call().status_code == 200
    sessions.token.unlink()

    sessions.later(SESSION_SECONDS)
    response = call()

    assert (response.status_code, response.headers["content-type"]) == (503, "application/problem+json"), response.text
    assert response.json()["code"] == 17, response.text
