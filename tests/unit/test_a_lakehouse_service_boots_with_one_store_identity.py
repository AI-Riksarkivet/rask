"""A lakehouse service's fail-closed store identity ([[XC-083]], [[XC-084]], ADR 0170): a service that would hold a key pair
where the secret store is on, hold a second identity, or hand its token to the wrong STS refuses to boot."""

from __future__ import annotations

import pytest
from pydantic_settings import BaseSettings

from catalog.core.config import Settings
from lineage.core.config import LineageSettings
from maintenance.core.config import MaintenanceSettings


@pytest.mark.parametrize(
    ("settings", "prefix"),
    [
        pytest.param(Settings, "LANCE", id="catalog"),
        pytest.param(LineageSettings, "LINEAGE", id="lineage"),
        pytest.param(MaintenanceSettings, "MAINTENANCE", id="maintenance"),
    ],
)
def test_a_service_reading_its_secrets_from_the_store_refuses_a_key_pair(settings: type[BaseSettings], prefix: str) -> None:
    """On the estate every service reads its secrets from the store, and its store identity is then its ServiceAccount
    token: a key pair beside it would be a static key in its environment."""
    with pytest.raises(ValueError, match=f"set {prefix}_S3_WEB_IDENTITY_TOKEN_FILE"):
        settings.model_validate({"secrets_from_dapr": True, "s3_access_key_id": "x", "s3_secret_access_key": "x"})


@pytest.mark.parametrize(
    ("given", "refusal"),
    [
        pytest.param({"vending_mode": "web_identity"}, "LANCE_S3_WEB_IDENTITY_TOKEN_FILE", id="a-vend-with-no-token-to-exchange"),
        pytest.param({"s3_web_identity_token_file": "/t"}, "LANCE_S3_STS_ENDPOINT", id="a-token-sent-to-the-public-aws-sts"),
        pytest.param(
            {"s3_web_identity_token_file": "/t", "s3_sts_endpoint": "http://sts:9000", "s3_access_key_id": "x", "s3_secret_access_key": "x"},
            "second identity",
            id="a-key-pair-beside-the-token",
        ),
        pytest.param(
            {
                "vending_mode": "web_identity",
                "s3_web_identity_token_file": "/t",
                "s3_sts_endpoint": "http://sts:9000",
                "s3_server_side_encryption": "aws:kms",
                "s3_sse_kms_key_id": "1234abcd-12ab-34cd-56ef-1234567890ab",
            },
            "key ARN",
            id="a-kms-key-no-vended-session-can-be-granted",
        ),
    ],
)
def test_the_catalogs_store_identity_fails_closed_at_boot(given: dict[str, object], refusal: str) -> None:
    """A catalog boots with exactly one store identity, and on the estate that is its ServiceAccount token (ADR 0170).

    Without an STS endpoint boto3 resolves the PUBLIC AWS STS endpoint and would POST the token there, and a pair beside
    the token is a second identity. A vend encrypting with KMS names its key by ARN, since a session policy grants a key
    only by its ARN ([[LH-262]]).
    """
    with pytest.raises(ValueError, match=refusal):
        Settings.model_validate(given)
