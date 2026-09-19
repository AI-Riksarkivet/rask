"""An unconfigured `/produce` door admits NOBODY, like every sibling door in the estate.

[[LH-175]]. Owner ruling 2026-09-19 (`docs/DECISIONS.md`). `require_dapr_token` already states the rule
for every other sidecar-delivered door — "the door cannot authenticate anybody, so it admits nobody" —
and `authorize_produce` was the one that opened instead, returning `None` and running the request as a
service. The two answers to the same condition were unreachable from each other: nothing in the code
said which was intended, so an operator reading either one learned the wrong rule about the other.

THE HATCH IS THE POINT, not a softening. `RASK_ALLOW_UNAUTHENTICATED_DAPR` already exists for a
deployment that means to run open, and it is explicit and greppable where an empty setting is neither.
Wanting an open door is now something a deployment SAYS rather than something it fails to say.

WHY THE ACCESSOR FIX CAME FIRST AND WHY THIS IS NOT THE SAME BUG. The gate read
`settings.app_api_token` — `''` on this estate — while `dapr_auth.expected_app_token()` resolves a real
token from the Dapr secret store, so it took this dev-open path on a fully authenticated estate:
measured from inside the cluster with NO credential, `GET /stage-runners` answered 200 with real data.
That was an accessor defect and is fixed. This is the remaining POLICY question: what the door should do
when the token genuinely is unset, which no accessor change can answer.
"""

from __future__ import annotations

import pytest
from lance_namespace import PermissionDeniedError

from medallion.api import produce_auth


@pytest.fixture(autouse=True)
def _no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case below is the UNSET-token condition, so neither source may resolve one."""
    monkeypatch.setattr(produce_auth.dapr_auth, "expected_app_token", lambda: "")


class _Settings:
    app_api_token = ""
    produce_admin_project = "acme"


def _hatch(monkeypatch: pytest.MonkeyPatch, *, open_door: bool) -> None:
    monkeypatch.setenv("RASK_ALLOW_UNAUTHENTICATED_DAPR", "true" if open_door else "false")


async def _call(request: object = object()) -> str | None:
    from typing import Any, cast

    return await produce_auth.authorize_produce(cast(Any, request), cast(Any, _Settings()), cast(Any, object()))


@pytest.mark.asyncio
async def test_an_unconfigured_door_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _hatch(monkeypatch, open_door=False)

    with pytest.raises(PermissionDeniedError):
        await _call()


@pytest.mark.asyncio
async def test_the_refusal_names_the_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 that does not say how to run open deliberately sends an operator to guess at a setting."""
    _hatch(monkeypatch, open_door=False)

    with pytest.raises(PermissionDeniedError) as caught:
        await _call()

    assert "RASK_ALLOW_UNAUTHENTICATED_DAPR" in str(caught.value)


@pytest.mark.asyncio
async def test_the_hatch_still_opens_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. Without it, a door that refused unconditionally would pass the two cases above.

    A deployment that means to run open says so, and gets exactly what it had before: admitted, with no
    verified subject to carry as an originator.
    """
    _hatch(monkeypatch, open_door=True)

    assert await _call() is None
