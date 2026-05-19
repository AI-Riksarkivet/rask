"""Tests for app.api.errors helper functions."""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from app.api.errors import parse_json_response, raise_for_hcp_status, run_mapi
from app.services.mapi_errors import (
    MapiError,
    MapiResponseError,
    MapiTransportError,
)
from app.services.storage.errors import (
    StorageError,
    StorageOperationNotSupported,
    StorageTransportError,
    from_client_error,
)


# ── raise_for_hcp_status ────────────────────────────────────────────


def _mock_hcp_response(
    status_code: int, text: str = "", headers: dict | None = None
) -> httpx.Response:
    resp = httpx.Response(
        status_code=status_code,
        text=text,
        headers=headers or {},
    )
    return resp


def test_hcp_status_2xx_does_not_raise():
    resp = _mock_hcp_response(200)
    raise_for_hcp_status(resp)  # Should not raise


def test_hcp_status_204_does_not_raise():
    resp = _mock_hcp_response(204)
    raise_for_hcp_status(resp)


@pytest.mark.parametrize(
    "hcp_code,expected_status",
    [
        (302, 404),
        (400, 400),
        (401, 401),
        (403, 403),
        (404, 404),
        (405, 405),
        (409, 409),
        (414, 414),
        (415, 415),
        (500, 502),
        (503, 503),
    ],
)
def test_hcp_status_code_mapping(hcp_code: int, expected_status: int):
    resp = _mock_hcp_response(hcp_code, text="error details")
    with pytest.raises(HTTPException) as exc_info:
        raise_for_hcp_status(resp, "test-resource")
    assert exc_info.value.status_code == expected_status


def test_hcp_status_unknown_code_maps_to_502():
    resp = _mock_hcp_response(418, text="I'm a teapot")
    with pytest.raises(HTTPException) as exc_info:
        raise_for_hcp_status(resp, "resource")
    assert exc_info.value.status_code == 502


def test_hcp_status_uses_x_hcp_error_message_header():
    resp = _mock_hcp_response(404, headers={"X-HCP-ErrorMessage": "Custom error"})
    with pytest.raises(HTTPException) as exc_info:
        raise_for_hcp_status(resp, "object")
    assert exc_info.value.status_code == 404


# ── parse_json_response ─────────────────────────────────────────────


def test_parse_json_response_200_with_json():
    resp = httpx.Response(200, json={"key": "value"})
    assert parse_json_response(resp) == {"key": "value"}


def test_parse_json_response_201_with_json():
    resp = httpx.Response(201, json={"id": "new"})
    assert parse_json_response(resp) == {"id": "new"}


def test_parse_json_response_204_empty():
    resp = httpx.Response(204)
    assert parse_json_response(resp) == {}


def test_parse_json_response_200_empty_body():
    resp = httpx.Response(200)
    assert parse_json_response(resp) == {}


def test_parse_json_response_200_invalid_json():
    resp = httpx.Response(200, text="not json")
    assert parse_json_response(resp) == {}


def test_parse_json_response_500_returns_empty():
    resp = httpx.Response(500, json={"error": "bad"})
    assert parse_json_response(resp) == {}


# ── MapiError domain exceptions ─────────────────────────────────────


def test_mapi_error_attributes():
    err = MapiError("something failed", http_status=503)
    assert str(err) == "something failed"
    assert err.message == "something failed"
    assert err.http_status == 503


def test_mapi_transport_error_is_mapi_error():
    err = MapiTransportError("timeout", http_status=504)
    assert isinstance(err, MapiError)
    assert err.http_status == 504


def test_mapi_response_error_has_hcp_status():
    err = MapiResponseError("not found", http_status=404, hcp_status=404)
    assert isinstance(err, MapiError)
    assert err.http_status == 404
    assert err.hcp_status == 404


# ── run_mapi ──────────────────────────────────────────────────────────


async def test_run_mapi_success():
    async def _ok():
        return {"data": "ok"}

    result = await run_mapi(_ok(), "test")
    assert result == {"data": "ok"}


async def test_run_mapi_translates_mapi_error():
    async def _fail():
        raise MapiTransportError("HCP unreachable", http_status=502)

    with pytest.raises(HTTPException) as exc_info:
        await run_mapi(_fail(), "tenants")
    assert exc_info.value.status_code == 502
    assert "HCP unreachable" in exc_info.value.detail


# ── StorageError domain exceptions ────────────────────────────────


def test_storage_error_attributes():
    err = StorageError("NoSuchBucket", "Bucket not found", 404)
    assert str(err) == "Bucket not found"
    assert err.code == "NoSuchBucket"
    assert err.message == "Bucket not found"
    assert err.http_status == 404


def test_storage_error_default_status():
    err = StorageError("Unknown", "something broke")
    assert err.http_status == 502


def test_storage_transport_error_is_storage_error():
    err = StorageTransportError("endpoint unreachable")
    assert isinstance(err, StorageError)
    assert err.code == "TransportError"
    assert err.http_status == 502


def test_storage_transport_error_custom_status():
    err = StorageTransportError("read timed out", 504)
    assert err.http_status == 504


def test_storage_operation_not_supported():
    err = StorageOperationNotSupported("get_bucket_acl", "minio")
    assert isinstance(err, StorageError)
    assert err.code == "NotSupported"
    assert err.http_status == 501
    assert "minio" in err.message
    assert "get_bucket_acl" in err.message


@pytest.mark.parametrize(
    "error_code,expected_status",
    [
        ("NoSuchBucket", 404),
        ("NoSuchKey", 404),
        ("AccessDenied", 403),
        ("BucketNotEmpty", 409),
        ("BucketAlreadyExists", 409),
        ("InvalidBucketName", 400),
        ("SlowDown", 503),
    ],
)
def test_from_client_error_maps_known_codes(error_code: str, expected_status: int):
    exc = type("ClientError", (Exception,), {})()
    exc.response = {  # type: ignore[attr-defined]
        "Error": {"Code": error_code, "Message": "test message"},
        "ResponseMetadata": {"HTTPStatusCode": 500},
    }
    result = from_client_error(exc)
    assert result.code == error_code
    assert result.http_status == expected_status
    assert result.message == "test message"


def test_from_client_error_unknown_code_uses_http_status():
    exc = type("ClientError", (Exception,), {})()
    exc.response = {  # type: ignore[attr-defined]
        "Error": {"Code": "WeirdError", "Message": "weird"},
        "ResponseMetadata": {"HTTPStatusCode": 422},
    }
    result = from_client_error(exc)
    assert result.code == "WeirdError"
    assert result.http_status == 422
