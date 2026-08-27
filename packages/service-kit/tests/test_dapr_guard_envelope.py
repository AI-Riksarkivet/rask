"""The Dapr-token guard's refusal must be the envelope every other error in the estate uses.

open_fastapi-audit — "The shared Dapr-token guard raises a bare HTTPException, so the same 403 comes
back as plain {"detail"} JSON in nine services whose every other error is problem+json".

`require_dapr_token` raised `HTTPException`, which no app maps — so its 403 arrived as FastAPI's
default `{"detail": ...}` while every other refusal in the same service is RFC 9457 problem+json with
a `code`.

THE AUDIT'S CORRECTION IS KEPT: `exception-handlers.md`'s carve-out is "auth deps excepted", and this
IS an auth dependency, so the rule-violation framing does not hold. What remains is a real envelope
inconsistency whose audience is mostly daprd — every guarded route is sidecar-delivered by
construction and daprd reads the status, not the body. Only the public-front-door refusal is ever seen
by a human. Cosmetic, and low, and still worth one line.

WHY `PermissionDeniedError` AND NOT `ForbiddenError`, which is verified rather than preferred: this
module is shared by BOTH planes, and they install different handlers. The four lance apps (catalog,
lineage, maintenance, medallion) install `install_problem_handlers` ONLY — `grep -c register_handlers`
returns 0 for each — so a `service_kit.exceptions.ForbiddenError` would fall through to the catch-all
and answer 500 there. `PermissionDeniedError` is a `LanceNamespaceError`, which both planes map: the
lance apps directly, and the fleet apps since `make_service_app` began installing the same translator.
"""

from __future__ import annotations

import inspect

import pytest
from lance_namespace import PermissionDeniedError

from service_kit.governed import dapr_auth


def test_the_guard_raises_no_bare_httpexception() -> None:
    source = inspect.getsource(dapr_auth.require_dapr_token)
    code = "\n".join(line for line in source.split("\n") if not line.strip().startswith("#"))
    assert "HTTPException" not in code, (
        "the guard still raises HTTPException, which no app maps — its 403 arrives as FastAPI's default "
        "{'detail'} body while every other refusal in the same service is problem+json"
    )


def test_a_public_caller_is_refused_with_a_mappable_error() -> None:
    with pytest.raises(PermissionDeniedError):
        dapr_auth.require_dapr_token(dapr_caller_app_id="gateway", dapr_api_token=None)


def test_a_bad_token_is_refused_with_a_mappable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", "expected")
    with pytest.raises(PermissionDeniedError):
        dapr_auth.require_dapr_token(dapr_caller_app_id="medallion-producer", dapr_api_token="wrong")


def test_a_good_token_still_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must keep admitting what it always admitted."""
    monkeypatch.setenv("APP_API_TOKEN", "expected")
    dapr_auth.require_dapr_token(dapr_caller_app_id="medallion-producer", dapr_api_token="expected")


def test_the_error_maps_to_403_in_both_planes() -> None:
    """`PermissionDeniedError`, not `ForbiddenError`: the four lance apps install only the
    lance_namespace translator, so a fleet `DomainError` would answer 500 there."""
    from service_kit.lakehouse.ns_errors import problem_detail

    status, body = problem_detail(PermissionDeniedError("nope"))
    assert status == 403
    assert body["code"] is not None
    assert str(body["type"]).endswith("permissiondeniederror")
