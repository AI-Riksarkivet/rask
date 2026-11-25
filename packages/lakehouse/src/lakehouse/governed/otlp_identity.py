"""The identity a lakehouse service presents on every OTLP export: its pod's projected ServiceAccount token.

The OpenTelemetry SDK's OTLP/HTTP exporters load their `requests.Session` from the entry point that
`OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER` names (group `opentelemetry_otlp_credential_provider`); this module
is the one the chart names, `rask_service_account`. The session reads the token from `RASK_OTLP_TOKEN_FILE` on every
request, because the kubelet replaces the file at 80% of the token's lifetime, and an export whose token cannot be read
is not sent rather than sent anonymously. The platform's OTLP gateway verifies the token against the cluster's
issuer, so no telemetry credential is configured anywhere.
"""

from typing import override

import requests
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from requests.auth import AuthBase

from lakehouse.governed.machine_identity import identity_bearer


class OtlpIdentitySettings(BaseSettings):
    """Where this pod's projected token for the OTLP gateway's audience sits, as the chart renders it."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    #: Empty when the chart named none, which no export may be sent under.
    token_file: str = Field(default="", alias="RASK_OTLP_TOKEN_FILE")


class _ProjectedToken(AuthBase):
    def __init__(self, path: str) -> None:
        self._path = path

    @override
    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        r.headers.update(identity_bearer(self._path))
        return r


def session() -> requests.Session:
    """The session every OTLP/HTTP exporter of this process sends with, presenting the pod's token on each request."""
    path = OtlpIdentitySettings().token_file
    if not path:
        raise RuntimeError("RASK_OTLP_TOKEN_FILE names no token file, so an export could not say who sent it")
    sender = requests.Session()
    sender.auth = _ProjectedToken(path)
    return sender


__all__ = [
    "OtlpIdentitySettings",
    "session",
]
