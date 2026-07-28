"""Ray token-auth plumbing (gate 7 / R3).

Ray >= 2.52 supports ``RAY_AUTH_MODE=token``: the dashboard HTTP surface (:8265)
and the gRPC surfaces (GCS :6379, client :10001) require
``Authorization: Bearer <token>`` — 401 when missing, 403 when invalid, surfaced
by the Job SDK as ``ray.exceptions.AuthenticationError`` (already in
``RAY_TRANSIENT_ERRORS``).

ray-kit reads the token from ``RASK_RAY_AUTH_TOKEN`` (repo env convention) or
Ray's native ``RAY_AUTH_TOKEN`` and attaches it *explicitly* on both client
surfaces, so a consumer works whether or not its own process also exports
``RAY_AUTH_MODE=token``:

- ``build_client`` passes it as the ``JobSubmissionClient`` default headers
  (Ray never overrides a caller-supplied ``Authorization`` header);
- the raw-httpx dashboard paths expect an ``httpx.AsyncClient`` constructed with
  ``headers=auth_headers()`` (the ray service's lifespan does this).

Absent a token, both surfaces behave exactly as before (auth off). The token is
read at call time, not import time, so env changes (tests, late injection) take
effect on the next client build.
"""

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RayAuthSettings(BaseSettings):
    """The Ray auth-token env surface. ``RASK_RAY_AUTH_TOKEN`` wins over
    ``RAY_AUTH_TOKEN`` (AliasChoices priority order)."""

    model_config = SettingsConfigDict(extra="ignore")

    token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("RASK_RAY_AUTH_TOKEN", "RAY_AUTH_TOKEN"),
    )


def auth_headers() -> dict[str, str]:
    """``{"Authorization": "Bearer <token>"}`` when a token env var is set, else ``{}``."""
    token = RayAuthSettings().token
    if token is None or not token.get_secret_value():
        return {}
    return {"Authorization": f"Bearer {token.get_secret_value()}"}
