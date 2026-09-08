"""A maintainer may obtain the credential its own rewrite needs, and nobody else gains anything.

`open_lakehouse_diff_left.md` § H13. MEASURED LIVE 2026-09-08: of 285 rewrite attempts in one sweep
tick, 207 were refused a vended credential and fell back to the deployment's ROOT key —
`credentials.write_options_for` treats a refused vend as a reason to reach for the ambient credential,
and the pod says so itself ("this rewrite is signed by the root key"). Checking the live store
explained the split exactly: `user:service-maintenance` holds `can_write_data` on some tables ad hoc
and none on the rest, while `can_maintain` was false everywhere because no `maintainer` tuple had ever
been written.

THE TWO RUNGS MEAN DIFFERENT THINGS and the model keeps them apart — a maintainer is denied read,
write, drop and promote (`model.fga.yaml`'s own test block asserts all four). What they share is the
STORAGE verb: compaction, index optimization and reclamation rewrite files, and an object store cannot
express "rewrite but do not change content" — both are `PutObject`. So a maintainer receives a
write-TIER credential scoped to its own table for 900 s, which is the narrower of the two available
postures by an enormous margin; the other is the root key.

Owner ruling 2026-09-08. Driven through the REAL route rather than a helper, because what is being
pinned is a credential-vending authorization decision.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import DescribeTableResponse

from catalog.core.vending import Tier, VendedCredentials


class _Vendor:
    def vend(self, *, table_location: str, tier: Tier, web_identity_token: str | None = None, bases: Sequence[str] = ()) -> VendedCredentials:
        return VendedCredentials(storage_options={"access_key_id": "AK", "secret_access_key": "SK", "session_token": "ST"}, expires_at_millis=1)


@pytest.fixture
def governed(fake_ns: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[TestClient, list[str]]]:
    """The vend door with FGA ON and a caller identity, plus the list of relations actually checked."""
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")

    from catalog.core.config import get_settings

    get_settings.cache_clear()

    from catalog.api import security
    from catalog.api.dependencies import get_fga_client, get_namespace, get_storage_options, get_vendor
    from catalog.api.dependencies import get_settings as settings_dep
    from catalog.main import app

    # FGA ON via the DEPENDENCY, not the environment: `fga_enabled` refuses to construct without
    # `oidc_enabled`, and standing up a whole OIDC posture would test the settings model rather than
    # this door.
    governed_settings = get_settings().model_copy(update={"fga_enabled": True})

    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://lance-catalog/db$t")
    app.dependency_overrides[get_namespace] = lambda: fake_ns
    app.dependency_overrides[get_storage_options] = lambda: {}
    app.dependency_overrides[get_vendor] = lambda: _Vendor()
    app.dependency_overrides[get_fga_client] = lambda: object()
    app.dependency_overrides[settings_dep] = lambda: governed_settings
    app.dependency_overrides[security.authenticate] = lambda: _Token()

    checked: list[str] = []
    with TestClient(app) as test_client:
        yield test_client, checked
    app.dependency_overrides.clear()
    get_settings.cache_clear()


class _Token:
    sub = "service-maintenance"


#: The ROUTER's own rung, enforced by `fga_deps` before this door is reached. Always granted here and
#: never recorded: it gates whether the caller may address the table at all, which is a different
#: question from which rung opens the WRITE tier, and letting it deny would mask the answer under a 403
#: from one layer up.
_ROUTER_RUNG = "can_read_data"


def _grant(
    monkeypatch: pytest.MonkeyPatch,
    checked: list[str],
    *,
    allow: set[str],
    router_grants: set[str] | None = None,
) -> None:
    """Stub the model, not the door: record the write-tier rungs asked about, allow only `allow`.

    ``router_grants`` defaults to granting the router's own rung, so the tests about the DOOR are not
    answered by the gate in front of it. Pass an explicit set to exercise the router itself.
    """
    from catalog.api.v1.endpoints import credentials as door

    granted_at_router = {_ROUTER_RUNG} if router_grants is None else router_grants

    async def _check(_client: object, *, user: str, relation: str, obj: str, **_: object) -> bool:
        if relation in granted_at_router:
            return True
        checked.append(relation)
        return relation in allow

    monkeypatch.setattr(door.fga, "check", _check)


def test_a_maintainer_gets_the_write_tier_credential(governed: tuple[TestClient, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    client, checked = governed
    _grant(monkeypatch, checked, allow={"can_maintain"})

    resp = client.post("/v1/table/db$t/credentials?tier=write")

    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "direct"
    assert "can_write_data" in checked and "can_maintain" in checked, f"both rungs must be consulted: {checked}"


def test_a_writer_still_gets_it_WITHOUT_the_second_probe(governed: tuple[TestClient, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The common path must not pay for the new rung, and a writer must not be recorded as a maintainer."""
    client, checked = governed
    _grant(monkeypatch, checked, allow={"can_write_data"})

    resp = client.post("/v1/table/db$t/credentials?tier=write")

    assert resp.status_code == 200, resp.text
    assert checked == ["can_write_data"], f"a granted first rung must short-circuit: {checked}"


def test_holding_NEITHER_rung_is_still_refused(governed: tuple[TestClient, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that makes the other two mean something: this door still refuses."""
    client, checked = governed
    _grant(monkeypatch, checked, allow=set())

    resp = client.post("/v1/table/db$t/credentials?tier=write")

    assert resp.status_code == 403, resp.text
    assert checked == ["can_write_data", "can_maintain"]


def test_the_READ_tier_never_consults_either_rung(governed: tuple[TestClient, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is write-tier only; a read vend is gated by the router, not here."""
    client, checked = governed
    _grant(monkeypatch, checked, allow=set())

    resp = client.post("/v1/table/db$t/credentials")

    assert resp.status_code == 200, resp.text
    assert checked == []


# --------------------------------------------------------------------------- #
# the ROUTER rung — the gate in front of the door above
# --------------------------------------------------------------------------- #


def test_a_maintainer_REACHES_the_vend_route_at_all(governed: tuple[TestClient, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The door above is unreachable without this, which is how it landed and changed nothing.

    MEASURED 2026-09-08: `cd4697ab` deployed, 92 `maintainer` tuples written, `can_maintain` verified
    TRUE on the live store — and the sweep tick was byte-identical (AMBIENT 207, SCOPED 78, 207 x 403).
    `fga_deps` lists `credentials` in `_DATA_READ_ACTIONS`, so the ROUTER required `can_read_data` and
    refused before the endpoint ran. Measured on a table that was falling back:

        can_read_data=False  can_get_metadata=False  can_write_data=False  can_maintain=True

    The model's own separation is what collided: a maintainer is deliberately not a reader, and this
    route assumed every credential request implies a data read.
    """
    client, checked = governed
    _grant(monkeypatch, checked, allow={"can_maintain"}, router_grants=set())

    resp = client.post("/v1/table/db$t/credentials?tier=write")

    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "direct"


def test_holding_NEITHER_router_rung_is_still_refused_at_the_route(governed: tuple[TestClient, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that makes the above mean something: the route still refuses a caller with no rung."""
    client, checked = governed
    _grant(monkeypatch, checked, allow=set(), router_grants=set())

    resp = client.post("/v1/table/db$t/credentials?tier=write")

    assert resp.status_code == 403, resp.text


def test_can_maintain_opens_NOTHING_ELSE_on_the_table(governed: tuple[TestClient, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The blast radius, and the reason this is a per-action second door rather than a wider rung.

    `query` is a DATA READ like `credentials` is, and a maintainer must NOT gain it — that is the whole
    content of "a maintainer is not a reader". If this ever passes, the alternative has leaked from one
    action to the reader tier at large.
    """
    client, checked = governed
    _grant(monkeypatch, checked, allow={"can_maintain"}, router_grants=set())

    resp = client.post("/v1/table/db$t/query", json={})

    assert resp.status_code == 403, f"can_maintain must not open a data read: {resp.status_code} {resp.text[:200]}"
