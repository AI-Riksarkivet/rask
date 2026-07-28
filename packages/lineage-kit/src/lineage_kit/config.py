"""Env-driven transport configuration — ``RASK_*`` first, official OpenLineage names accepted.

Follows the repo convention (`RASK_*` env vars, ``AliasChoices`` so the official client's
own variable names keep working — the same pattern as ``RASK_S3_ENDPOINT_URL`` in
``packages/storage``). No endpoint configured is a VALID configuration: the emitter
degrades to a logged no-op and the pipeline runs unlineaged rather than crashing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LineageSettings(BaseSettings):
    """Where (and whether) lineage events go. All fields env-overridable."""

    model_config = SettingsConfigDict(env_prefix="RASK_LINEAGE_", extra="ignore")

    #: The OpenLineage HTTP endpoint base URL (e.g. ``http://localhost:5000``). Unset → no-op emitter.
    endpoint: str | None = Field(default=None, validation_alias=AliasChoices("RASK_LINEAGE_ENDPOINT", "OPENLINEAGE_URL"))
    #: Bearer api key for the HTTP transport (optional).
    api_key: str | None = Field(default=None, validation_alias=AliasChoices("RASK_LINEAGE_API_KEY", "OPENLINEAGE_API_KEY"))
    #: The shared app token for rask's own **service door** on the lineage ingest.
    #:
    #: rask's ingest does not authenticate in-cluster producers with a bearer: ``lineage.api.security``
    #: opens the service door only when BOTH ``dapr-api-token`` and ``x-lance-service-identity`` are
    #: present, and otherwise falls through to OIDC. A bearer api key therefore does not authenticate a
    #: service at all — it 401s, and ``ClientEmitter`` catches transport errors, so the events vanish
    #: with one log line. That is not hypothetical: it is the 2026-07-13 incident recorded in
    #: ``ServicePrincipal``'s docstring, where "every training RunEvent 401'd, silently losing all
    #: training provenance in a governed deployment".
    #:
    #: ``LINEAGE_SERVICE_TOKEN`` is the estate's EXISTING name for this, not a new one: the Ray train
    #: job passes it through the job's runtime_env (``ray_submit.py``) and the frontend zones get it from
    #: ``lance.frontendEnv``. Reading that name means every producer already provisioned with the service
    #: door authenticates through this transport with no additional wiring — and a producer without it
    #: keeps working on the open (auth-off) path.
    app_token: str | None = Field(default=None, validation_alias=AliasChoices("RASK_LINEAGE_APP_TOKEN", "LINEAGE_SERVICE_TOKEN", "APP_API_TOKEN"))
    #: The subject this producer claims at the service door — the estate's ``LINEAGE_SERVICE_ID``. Must be
    #: in the ingest's ``LINEAGE_SERVICE_SUBJECTS`` allowlist (chart: services.yaml), which fails CLOSED on
    #: anything unlisted, so this is a claim the ingest verifies rather than trusts.
    service_identity: str | None = Field(default=None, validation_alias=AliasChoices("RASK_LINEAGE_SERVICE_IDENTITY", "LINEAGE_SERVICE_ID"))
    #: Path under the endpoint events are POSTed to (the client's default).
    endpoint_path: str = Field(default="api/v1/lineage", validation_alias=AliasChoices("RASK_LINEAGE_ENDPOINT_PATH", "OPENLINEAGE_ENDPOINT"))
    #: Default job namespace stamped on runs when a caller does not name one.
    namespace: str = Field(default="rask", validation_alias=AliasChoices("RASK_LINEAGE_NAMESPACE", "OPENLINEAGE_NAMESPACE"))
    #: HTTP transport timeout, seconds.
    timeout: float = Field(default=5.0, validation_alias=AliasChoices("RASK_LINEAGE_TIMEOUT"))
    #: ``auto`` = http when an endpoint is configured, else no-op. ``console`` logs events
    #: through the official ConsoleTransport (debugging); ``noop`` forces lineage off.
    transport: Literal["auto", "http", "console", "noop"] = Field(default="auto", validation_alias=AliasChoices("RASK_LINEAGE_TRANSPORT"))
