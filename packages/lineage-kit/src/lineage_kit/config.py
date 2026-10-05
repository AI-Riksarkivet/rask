"""Env-driven transport configuration — ``RASK_*`` first, official OpenLineage names accepted.

Follows the repo convention: `RASK_*` env vars, with ``AliasChoices`` so the official client's own
variable names (``OPENLINEAGE_URL`` and friends) keep working. This used to cite
``packages/storage``'s ``RASK_S3_ENDPOINT_URL`` as the exemplar of that pattern; it is not one —
storage resolves the same kind of canonical-first precedence BY HAND (``_env_first`` over three name
tuples) and imports no pydantic-settings, deliberately, because two sealed runners take it as a path
dependency and neither carries pydantic. The convention is the env NAMES; the mechanism differs.

No endpoint configured is a VALID configuration: the emitter degrades to a logged no-op and the
pipeline runs unlineaged rather than crashing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


#: Where the chart projects the pod's ``rask-lineage`` ServiceAccount token.
DEFAULT_IDENTITY_TOKEN_FILE = Path("/var/run/secrets/rask/identity/rask-lineage/token")


class LineageSettings(BaseSettings):
    """Where (and whether) lineage events go. All fields env-overridable."""

    model_config = SettingsConfigDict(env_prefix="RASK_LINEAGE_", extra="ignore")

    #: The OpenLineage HTTP endpoint base URL (e.g. ``http://localhost:5000``). Unset → no-op emitter.
    #:
    #: A Ray job reads it from its pod (the head renders ``RASK_LINEAGE_ENDPOINT``), never from its submission: Ray
    #: merges ``runtime_env`` over the process env, so a submitted endpoint would outvote a repointed pod.
    endpoint: str | None = Field(default=None, validation_alias=AliasChoices("RASK_LINEAGE_ENDPOINT", "OPENLINEAGE_URL"))
    #: A static bearer, the OpenLineage client's own ``api_key`` convention: for a producer OUTSIDE the
    #: cluster that holds a bearer the door's OIDC issuer verifies. When set it is the credential, and
    #: the identity token file below is not read.
    api_key: str | None = Field(default=None, validation_alias=AliasChoices("RASK_LINEAGE_API_KEY", "OPENLINEAGE_API_KEY", "LINEAGE_TOKEN"))
    #: The pod's projected ServiceAccount token for audience ``rask-lineage``: an in-cluster producer's
    #: whole identity at the lineage door (`lineage_kit.identity`). Re-read on every emit, because the
    #: kubelet rotates it before its 600 s expiry. The default is where the chart projects it.
    #:
    #: An empty value presents no credential, for a lineage door running with auth off. A configured
    #: file that cannot be read is not that: the event is left unsent and counted as a transport drop,
    #: never sent anonymously.
    identity_token_file: Path | None = Field(default=DEFAULT_IDENTITY_TOKEN_FILE, validation_alias=AliasChoices("RASK_LINEAGE_IDENTITY_TOKEN_FILE"))
    #: Path under the endpoint events are POSTed to (the client's default).
    endpoint_path: str = Field(default="api/v1/lineage", validation_alias=AliasChoices("RASK_LINEAGE_ENDPOINT_PATH", "OPENLINEAGE_ENDPOINT"))
    #: Default job namespace stamped on runs when a caller does not name one.
    namespace: str = Field(default="rask", validation_alias=AliasChoices("RASK_LINEAGE_NAMESPACE", "OPENLINEAGE_NAMESPACE"))
    #: HTTP transport timeout, seconds.
    timeout: float = Field(default=5.0, validation_alias=AliasChoices("RASK_LINEAGE_TIMEOUT"))
    #: ``auto`` = http when an endpoint is configured, else no-op. ``console`` logs events
    #: through the official ConsoleTransport (debugging); ``noop`` forces lineage off.
    transport: Literal["auto", "http", "console", "noop"] = Field(default="auto", validation_alias=AliasChoices("RASK_LINEAGE_TRANSPORT"))

    @field_validator("identity_token_file", mode="before")
    @classmethod
    def _blank_presents_no_credential(cls, value: object) -> object:
        """``""`` is the explicit "no credential"; as a ``Path`` it would be ``.``, a directory, and every emit would fail."""
        return None if value == "" else value


@lru_cache(maxsize=1)
def lineage_settings() -> LineageSettings:
    """The process's transport configuration, read from the environment ONCE.

    Every run open used to construct `LineageSettings()` afresh — a full pydantic-settings
    environment read and validation of every field — and a ``@stage`` callable is invoked per batch
    inside a Ray Data pipeline, so that was per unit of work for a value that cannot change within a
    process.

    Cached, therefore, and deliberately not used by :func:`~lineage_kit.emitter.build_emitter`: that
    runs once at wiring time and takes an explicit ``settings`` argument, and freezing it would make
    a process that reconfigures its environment (every test that monkeypatches ``RASK_LINEAGE_*``)
    silently build the wrong transport. Call ``lineage_settings.cache_clear()`` when the environment
    legitimately changes under a live process.
    """
    return LineageSettings()
