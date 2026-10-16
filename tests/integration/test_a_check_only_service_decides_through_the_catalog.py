"""A check-only service asks the catalog's decision door, and the door answers only what its decision role opens ([[LH-442]]).

OpenFGA authorizes none of its callers, so a service holding an OpenFGA token can write any tuple. The services that
only check hold none and ask the catalog: `service_kit.governed.decider.CatalogDecider`, presenting the pod's
projected `rask-catalog` token, against the real catalog app on a real socket. Each route opens on its own estate
role, so lineage (a `decider` and `governance_prober`) may check and probe, the viewer (a `decider`) may only check, and
an account holding neither is refused everything. What may be asked is closed too: a role such as `owner` is not a
question a decision answers. Every refusal reaches the service as `ServiceUnavailableError`, its gates' 503.

OpenFGA is a stand-in at the client the catalog holds (`openfga_grants.Grants`): it answers each check from the grants
the test names, holds the tuples `governed` reads, and the user ids it is asked about are the ones asserted on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from lance_namespace import ServiceUnavailableError
from openfga_sdk.client.models.list_users_request import ClientListUsersRequest
from openfga_sdk.models import ListUsersResponse, ReadRequestTupleKey, ReadResponse, Tuple, TupleKey, User
from openfga_sdk.models.fga_object import FgaObject

from service_kit.governed.decider import CatalogDecider
from service_kit.governed.fga import user_object
from tests.integration.openfga_grants import Grants, Options


_ROOT = "estate:rask"
#: The tables a decision is asked about; `table:governed` is named by a tuple, `table:stray` by none.
_GOVERNED, _STRAY = "table:governed", "table:stray"


def _account(sa: str) -> str:
    return user_object(f"kubernetes~system:serviceaccount:default:{sa}")


class _Store(Grants):
    """`Grants`, plus the tuple on `table:governed` and the members of `role:reviewers`."""

    async def read(self, body: ReadRequestTupleKey, options: Options = None) -> ReadResponse:
        del options
        held = [Tuple(key=TupleKey(user="user:alice", relation="owner", object=_GOVERNED), timestamp=time.time())] if body.object == _GOVERNED else []
        return ReadResponse(tuples=held, continuation_token="")

    async def list_users(self, body: ClientListUsersRequest, options: Options = None) -> ListUsersResponse:
        del options
        asked = body.object
        members = ["bob", "carol"] if asked is not None and (asked.type, asked.id, body.relation) == ("role", "reviewers", "assignee") else []
        return ListUsersResponse(users=[User(object=FgaObject(type="user", id=m)) for m in members])


@pytest.fixture
def catalog(sa_issuer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, _Store]]:
    """The catalog app on a live socket, verifying service-account tokens, with OpenFGA on at the stand-in."""
    env = {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid",
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-catalog",
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import Settings, get_settings

    get_settings.cache_clear()
    from catalog.main import app

    store = _Store()
    store.allowed = {
        (_account("rask-sa-lineage"), "can_decide_access", _ROOT),
        (_account("rask-sa-lineage"), "can_probe_governance", _ROOT),
        (_account("rask-sa-viewer"), "can_decide_access", _ROOT),
        (_account("rask-sa-notifications"), "can_expand_members", _ROOT),
        (_account("rask-sa-flows"), "can_decide_access", _ROOT),
        ("user:alice", "writer", _ROOT),
        ("user:alice", "can_get_metadata", "table:a"),
    }
    # Authorization on for the requests, after a lifespan that built the verifiers and no OpenFGA client.
    governed = Settings.model_validate({**env, "RASK_FGA_ENABLED": True, "RASK_FGA_API_URL": "http://openfga.invalid:8080"})
    app.dependency_overrides[get_settings] = lambda: governed
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    app.state.fga = store
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}", store
    server.should_exit = True
    thread.join(timeout=30)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


#: (the account asking, the question, its answer, or None for a refusal).
_CASES = [
    pytest.param("rask-sa-lineage", ("check", "can_get_metadata", ["table:a", "table:b"]), {"table:a": True, "table:b": False}, id="a-decider-checks"),
    pytest.param("rask-sa-lineage", ("governed", [_GOVERNED, _STRAY]), {_GOVERNED: True, _STRAY: False}, id="a-governance-prober-probes"),
    pytest.param("rask-sa-viewer", ("check", "can_get_metadata", ["table:a"]), {"table:a": True}, id="the-viewer-checks"),
    pytest.param("rask-sa-viewer", ("governed", [_GOVERNED]), None, id="the-viewer-may-not-probe"),
    pytest.param("rask-sa-notifications", ("list-users", "assignee", "role:reviewers"), ["bob", "carol"], id="a-member-expander-expands"),
    pytest.param("rask-sa-lineage", ("list-users", "assignee", "role:reviewers"), None, id="lineage-may-not-expand-members"),
    pytest.param("rask-sa-notifications", ("list-users", "can_get_metadata", "table:a"), None, id="only-a-membership-userset-expands"),
    pytest.param("rask-sa-flows", ("check", "writer", [_ROOT]), {_ROOT: True}, id="flows-asks-the-estate-tier-it-runs-a-flow-on"),
    pytest.param("rask-sa-search", ("check", "can_get_metadata", ["table:a"]), None, id="an-account-holding-no-decision-role"),
    pytest.param("rask-sa-lineage", ("check", "owner", ["table:a"]), None, id="a-role-is-not-a-question"),
    pytest.param("rask-sa-lineage", ("check", "can_decide_access", [_ROOT]), None, id="the-decision-rungs-are-not-a-question"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("account", "question", "answer"), _CASES)
async def test_a_check_only_service_is_answered_only_what_its_decision_role_opens(
    catalog: tuple[str, _Store], sa_issuer: Any, account: str, question: tuple[Any, ...], answer: dict[str, bool] | list[str] | None
) -> None:
    url, _store = catalog
    token = sa_issuer.mint(account, audience="rask-catalog")
    decider = CatalogDecider(url, token=lambda: token, timeout_seconds=10)
    route, *args = question

    async def ask() -> dict[str, bool] | list[str]:
        if route == "check":
            return await decider.batch_check(user="alice", relation=args[0], objects=args[1])
        if route == "governed":
            return await decider.governed(args[0])
        return await decider.list_users(relation=args[0], obj=args[1])

    try:
        if answer is None:
            with pytest.raises(ServiceUnavailableError):
                await ask()
        else:
            assert await ask() == answer
    finally:
        await decider.close()
