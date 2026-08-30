"""Job-retention config belongs to the settings model, not to `int(os.environ...)` at import.

`compute/pruner.py` read three environment variables at MODULE IMPORT — the binding/route name and
the two retention bounds — with `int()` around two of them and no settings class in sight
(FLEET-ENV-SCATTER). Two consequences:

* **A typo crashed the import, not the config.** `RASK_PRUNE_KEEP_JOBS=fivehundred` raised a bare
  `ValueError: invalid literal for int()` from inside a module import, naming no variable and
  relating to none of the other 40-odd settings the service validates at startup. Every other knob
  in the estate answers a pydantic-settings validation error that names the variable that was set.
* **The bounds were frozen at import.** They could not be injected, overridden per deployment after
  the module was first touched, or exercised by a test that did not reload the module — which is why
  the retention policy had no test at all.

The route path stays import-time by necessity (a Dapr binding is delivered to `POST /<name>`, so the
route must exist when the app is built), but it now comes from the same validated model as
everything else.
"""

from __future__ import annotations

import importlib
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from ray_kit import JobSubmissionClient


def test_a_bad_retention_value_is_a_settings_error_not_an_import_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_PRUNE_KEEP_JOBS", "fivehundred")

    import compute.pruner

    importlib.reload(compute.pruner)  # importing a module must not depend on the value being parseable

    from compute.config import ComputeSettings

    with pytest.raises(ValidationError) as caught:
        ComputeSettings()
    # The message names the ENVIRONMENT VARIABLE an operator actually set, which the bare
    # `ValueError: invalid literal for int() with base 10: 'fivehundred'` never did.
    assert "RASK_PRUNE_KEEP_JOBS" in str(caught.value)


def test_the_retention_bounds_are_declared_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_PRUNE_KEEP_JOBS", "7")
    monkeypatch.setenv("RASK_PRUNE_KEEP_FAILED_JOBS", "3")
    monkeypatch.setenv("RASK_PRUNE_BINDING", "renamed-cron")

    from compute.config import ComputeSettings

    settings = ComputeSettings()
    assert settings.prune_keep_jobs == 7
    assert settings.prune_keep_failed_jobs == 3
    assert settings.prune_binding == "renamed-cron"


def test_a_negative_retention_bound_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`keep_newest=-1` would delete every terminal job including the post-mortems."""
    monkeypatch.setenv("RASK_PRUNE_KEEP_JOBS", "-1")

    from compute.config import ComputeSettings

    with pytest.raises(ValidationError):
        ComputeSettings()


@pytest.mark.asyncio
async def test_the_configured_bounds_reach_prune_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler spends the SETTINGS values, so a deployment's numbers are the ones applied."""
    import compute.pruner as pruner

    importlib.reload(pruner)

    from compute.config import ComputeSettings
    from ray_kit.prune import PruneResult

    seen: dict[str, Any] = {}

    def _fake_prune(client: Any, *, keep_newest: int, keep_newest_failed: int) -> PruneResult:
        seen.update(keep_newest=keep_newest, keep_newest_failed=keep_newest_failed)
        return PruneResult(total=0, kept_newest=keep_newest)

    monkeypatch.setattr(pruner, "prune_jobs", _fake_prune)

    settings = ComputeSettings.model_validate({"RASK_PRUNE_KEEP_JOBS": 11, "RASK_PRUNE_KEEP_FAILED_JOBS": 4})
    # `cast`: the handler only asks whether the client is None before handing it to the (faked)
    # `prune_jobs`, so a bare sentinel is the honest double — the real `JobSubmissionClient` would
    # dial a dashboard.
    await pruner.on_prune_cron(cast(JobSubmissionClient, object()), settings)

    assert seen == {"keep_newest": 11, "keep_newest_failed": 4}


def test_the_binding_route_is_built_from_the_setting() -> None:
    """The cron route path IS the binding name; a factory makes it a value, not a module constant."""
    import compute.pruner as pruner
    from compute.config import ComputeSettings

    router = pruner.make_pruner_router(ComputeSettings.model_validate({"RASK_PRUNE_BINDING": "renamed-cron"}))
    # `cast`: `APIRouter.routes` is typed as the starlette `BaseRoute` base, which has no `path`;
    # every route this factory adds is an `APIRoute`, which does.
    paths = {cast(APIRoute, route).path for route in router.routes}
    assert paths == {"/renamed-cron"}
