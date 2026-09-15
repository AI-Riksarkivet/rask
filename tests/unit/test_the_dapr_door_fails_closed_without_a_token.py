"""An unconfigured Dapr door REFUSES. Leaving it open is a choice somebody has to make explicitly.

[[LH-162]]. `require_dapr_token` compared with `if expected and not compare_digest(...)`, so an absent
`APP_API_TOKEN` skipped the comparison and ACCEPTED the request. An unset secret opened the door rather
than closing it — the opposite of every other seam in this estate, and of the sibling check in this same
module (`service_principal`, which already raises `ServiceDoorClosed` when the token is unset).

IT WAS NOT THEORETICAL. Measured on the deployed estate 2026-09-15: `rask-annotator` renders no
`APP_API_TOKEN`, `DaprDoorSettings().app_api_token` is `None` in the running pod, and
`GET /dapr/config` — a route `guard_actor_routes` protects — answered **200 with no token**. The
annotator is an actor host, so its actor plane accepted any caller that could reach the pod.

WHY A FLAG AND NOT A BARE BAN (owner decision 2026-09-15). The skip was a documented dev convenience,
so refusing outright would break any deployment running Dapr ingest without a token and leave no way
back. The flag inverts the DEFAULT rather than removing the behaviour: closed unless somebody SETS
`RASK_ALLOW_UNAUTHENTICATED_DAPR`, so an open door is chosen and greppable instead of inherited from an
empty variable nobody noticed.

AND IT UNBLOCKS THE SECRETS MIGRATION, which is how it was found. [[LH-160]] moves `APP_API_TOKEN` out
of pod env; at this line that makes `expected` absent for EVERY service at once, turning a
secrets-hygiene change into an estate-wide authentication bypass. The door has to fail closed first.
"""

from __future__ import annotations

import pytest
from lance_namespace import PermissionDeniedError

from service_kit.governed.dapr_auth import require_dapr_token


def test_an_unconfigured_door_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect itself: no token configured must mean REFUSE, never 'skip the check'."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    monkeypatch.delenv("RASK_ALLOW_UNAUTHENTICATED_DAPR", raising=False)

    with pytest.raises(PermissionDeniedError, match="not configured"):
        require_dapr_token(dapr_api_token="anything", dapr_caller_app_id="")


def test_an_unconfigured_door_refuses_even_with_no_token_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact shape the annotator was in — no configured token AND no presented token.

    Pinned separately because it is the case the old `if expected and ...` handled most wrongly: both
    sides empty read as "nothing to check" rather than "nothing is protecting this".
    """
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    monkeypatch.delenv("RASK_ALLOW_UNAUTHENTICATED_DAPR", raising=False)

    with pytest.raises(PermissionDeniedError):
        require_dapr_token(dapr_api_token=None, dapr_caller_app_id="")


def test_the_escape_hatch_must_be_set_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """An open door is a decision somebody records, not a state inherited from an empty variable."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    monkeypatch.setenv("RASK_ALLOW_UNAUTHENTICATED_DAPR", "true")

    require_dapr_token(dapr_api_token=None, dapr_caller_app_id="")


def test_the_escape_hatch_does_not_weaken_a_CONFIGURED_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag says "unconfigured is acceptable", never "wrong tokens are acceptable".

    Without this, the hatch would be a master key: set it once for a dev convenience and every
    deployment that inherited the setting would accept forged tokens too.
    """
    monkeypatch.setenv("APP_API_TOKEN", "the-real-token")
    monkeypatch.setenv("RASK_ALLOW_UNAUTHENTICATED_DAPR", "true")

    with pytest.raises(PermissionDeniedError, match="invalid or missing"):
        require_dapr_token(dapr_api_token="a-forged-token", dapr_caller_app_id="")


def test_a_configured_door_still_accepts_the_right_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path, so the fix cannot pass by refusing everything."""
    monkeypatch.setenv("APP_API_TOKEN", "the-real-token")
    monkeypatch.delenv("RASK_ALLOW_UNAUTHENTICATED_DAPR", raising=False)

    require_dapr_token(dapr_api_token="the-real-token", dapr_caller_app_id="")
