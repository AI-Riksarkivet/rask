"""catalog-api-10 — an unknown relation NAME in a request body is the CLIENT's error, on every access surface.

The check (`/access/check`) and mutate (`/access/grant|revoke`) doors guard the body's ``relation``
against the compiled model, exactly as the estate-admin door does (``access_admin.py`` raises
``InvalidInputError`` → 400). The two per-object doors raised ``UnsupportedOperationError`` for the
same class of input, which the spec taxonomy maps to HTTP **406** (``ns_errors.py``) — a
"server not implemented" answer to a caller's typo, and a different spec code than the sibling
surface gives the identical mistake. Both doors must answer 400.

Sync via ``asyncio.run`` and direct calls into the shared primitives, the same harness as
``test_access_grant.py`` — no async-plugin dependency.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import InvalidInputError
from lance_namespace.errors import ErrorCode

from catalog.api.v1.endpoints import access
from catalog.core.config import Settings
from service_kit.control_emit import NoopControlEmitter
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse.ns_errors import status_for


def _client() -> Any:
    """The injected OpenFGA client — these doors take it as a dependency now (catalog-api-09) rather
    than digging it out of ``request.app.state``. Never used: both cases refuse before any call."""
    return object()


def _settings() -> Settings:
    return cast(Settings, SimpleNamespace(fga_enabled=True, delimiter="$"))


def _token() -> IDToken:
    return cast(IDToken, SimpleNamespace(sub="alice"))


def test_check_with_an_unknown_relation_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """``access/check`` with a relation the model does not define answers InvalidInput (400), not 406."""
    monkeypatch.setattr(access, "_can_relations", lambda fga_type: ("can_get_metadata", "can_read_data"))
    body = access.AccessCheckRequest(user="gina", relation="not_a_real_relation")
    with pytest.raises(InvalidInputError) as exc:
        asyncio.run(access._access_check(_client(), _settings(), _token(), "table", "db1$users", body))
    assert status_for(int(exc.value.code)) == 400, "a client-supplied bad relation must surface as the client's error"


def test_grant_with_an_unknown_rung_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """``access/grant`` with a non-grantable rung answers InvalidInput (400) — parity with access_admin's door."""
    monkeypatch.setattr(access, "_grantable_relations", lambda fga_type: ("owner", "writer", "reader"))
    body = access.AccessGrantRequest(user="gina", relation="not_a_real_rung")
    with pytest.raises(InvalidInputError) as exc:
        asyncio.run(access._access_mutate(_client(), NoopControlEmitter(), _settings(), _token(), "table", "db1$users", body, grant=True))
    assert status_for(int(exc.value.code)) == 400


def test_the_auth_off_answer_stays_unsupported() -> None:
    """The capability statement ("this stack runs auth-off") is genuinely UNSUPPORTED — it must NOT be
    swept into 400 by the bad-relation fix: the caller's request is well-formed, the deployment lacks
    the feature."""
    settings = cast(Settings, SimpleNamespace(fga_enabled=False, delimiter="$"))
    body = access.AccessCheckRequest(user="gina", relation="can_read_data")
    with pytest.raises(Exception) as exc:
        asyncio.run(access._access_check(_client(), settings, _token(), "table", "db1$users", body))
    assert (
        status_for(int(getattr(exc.value, "code", ErrorCode.UNSUPPORTED))) == 406
    )  # 406 since Q3 (2026-09-02): the spec's UnsupportedOperationErrorResponse is 406, and Lance's own reference server maps ErrorCode::Unsupported to NOT_ACCEPTABLE.
