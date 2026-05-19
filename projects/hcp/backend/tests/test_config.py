"""Tests for app.core.config settings classes."""

from __future__ import annotations

import base64
import hashlib

from app.core.config import AuthSettings, MapiSettings, S3Settings


def test_mapi_settings_defaults():
    settings = MapiSettings(
        hcp_host="admin.hcp.example.com",
        hcp_username="user",
        hcp_password="pass",
    )
    assert settings.hcp_host == "admin.hcp.example.com"
    assert settings.hcp_port == 9090
    assert settings.hcp_auth_type == "hcp"
    assert settings.hcp_verify_ssl is False
    assert settings.hcp_timeout == 60


def test_s3_settings_access_key_is_base64_username():
    settings = S3Settings(hcp_username="admin", hcp_password="secret")
    expected = base64.b64encode(b"admin").decode()
    assert settings.access_key == expected


def test_s3_settings_secret_key_is_md5_password():
    settings = S3Settings(hcp_username="admin", hcp_password="secret")
    expected = hashlib.md5(b"secret").hexdigest()
    assert settings.secret_key == expected


def test_s3_settings_property_aliases():
    settings = S3Settings(
        s3_endpoint_url="https://s3.test.com",
        s3_region="eu-west-1",
        hcp_verify_ssl=True,
    )
    assert settings.endpoint_url == "https://s3.test.com"
    assert settings.region == "eu-west-1"
    assert settings.verify_ssl is True


def test_auth_settings_defaults():
    settings = AuthSettings()
    assert settings.api_secret_key == "change-me-in-production"
    assert settings.api_token_expire_minutes == 480


def test_auth_settings_custom_values():
    settings = AuthSettings(api_secret_key="my-key", api_token_expire_minutes=30)
    assert settings.api_secret_key == "my-key"
    assert settings.api_token_expire_minutes == 30


def test_mapi_hcp_domain_explicit_override():
    settings = MapiSettings(
        hcp_host="h", hcp_username="u", hcp_password="p", hcp_domain="custom.domain"
    )
    assert settings.hcp_domain == "custom.domain"


def test_mapi_hcp_host_derived_from_domain():
    settings = MapiSettings(
        hcp_username="u", hcp_password="p", hcp_domain="hcp.example.com"
    )
    assert settings.hcp_host == "admin.hcp.example.com"


def test_mapi_hcp_host_not_overwritten_when_set():
    settings = MapiSettings(
        hcp_host="custom.host.com",
        hcp_username="u",
        hcp_password="p",
        hcp_domain="hcp.example.com",
    )
    assert settings.hcp_host == "custom.host.com"


def test_s3_hcp_domain_explicit_override():
    settings = S3Settings(
        hcp_username="u", hcp_password="p", hcp_domain="custom.domain"
    )
    assert settings.hcp_domain == "custom.domain"
