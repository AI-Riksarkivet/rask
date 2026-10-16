"""service-kit Dapr wiring — the shared door guard on sidecar-delivered routes (no sidecar needed).

The CLIENT FACTORY that used to be tested here is gone. It built
`DaprClient("http://127.0.0.1:3500")` — the sidecar's HTTP port handed to a gRPC client — and the
test below it asserted that wrong constant as though it were the contract, which is how a defect
acquires a green gate. Two re-verifications found no caller at all: every service that publishes
builds its own client. The settings survive because other code reads the same environment variables;
only the factory and its dependency are gone.
"""

import pytest


def test_a_bad_token_is_refused_with_a_mappable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is a `PermissionDeniedError`, the envelope every other error in the estate uses.

    `PermissionDeniedError` rather than `service_kit.exceptions.ForbiddenError`: this module is shared by
    both planes, and the four lance apps (catalog, lineage, maintenance, medallion) install
    `install_problem_handlers` only, so a fleet `ForbiddenError` would fall to the catch-all and answer
    500 there. A `LanceNamespaceError` is mapped by both planes.
    """
    from lance_namespace import PermissionDeniedError

    from service_kit.governed import dapr_auth

    monkeypatch.setenv("APP_API_TOKEN", "expected")
    with pytest.raises(PermissionDeniedError):
        dapr_auth.require_dapr_token(dapr_caller_app_id="medallion-producer", dapr_api_token="wrong")


def test_the_LEGITIMATE_delivery_paths_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delivery presenting the app token is admitted, whether or not daprd named an invoking app.

    Pub/sub delivery, binding delivery and a direct Service-DNS call carry no caller app-id, and those are
    every legitimate way onto the routes this dependency guards.
    """
    from service_kit.governed.dapr_auth import require_dapr_token

    monkeypatch.setenv("APP_API_TOKEN", "shared-secret")

    require_dapr_token(dapr_api_token="shared-secret")  # no caller id — must not raise
    require_dapr_token(dapr_api_token="shared-secret", dapr_caller_app_id="medallion")
