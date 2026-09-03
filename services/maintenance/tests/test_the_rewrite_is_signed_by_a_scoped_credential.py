"""A compaction's WRITE must be signed by a table-scoped credential, not the root object-store key.

Maintenance held `rustfsadmin` and rewrote fragments with it. Compaction is a write — it lands new
data files and commits a new manifest — so this was a service performing writes across every bucket in
the estate with one long-lived key, which is the exact posture the catalog's vending door exists to
end. Proven end-to-end for the ingest plane 2026-09-03 (a credential vended for one table read AND
wrote it, and was refused on a sibling with 403 AccessDenied); this is the same door, same tier.

WHAT MUST NOT CHANGE, and each has a test below because each would be a silent regression:

* the whole-estate protection PRE-PASS keeps the ambient credential. It must open every manifest in
  every bucket before any dataset is compacted, and no per-table credential can express that. It is a
  READ; the clause is about write paths;
* an unvendable dataset still gets maintained. A location the flat-layout parser declines, a
  deployment with no catalog URL, a door answering `server_mediated` — all fall back to the ambient
  credential, because a hardening that can FAIL a maintenance run turns an optional improvement into a
  new way to stop reclaiming disk;
* the vend asks for the WRITE tier. A read-tier credential returns 200 and a perfectly valid
  credential, and the rewrite then dies at the object store as `403 AccessDenied` on a PUT — minutes
  later, reading as a missing grant on the table rather than a request that asked for the wrong thing.
"""

from __future__ import annotations

from typing import Any

import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials


#: Distinctive values on purpose. A one-character secret is a substring of ordinary prose, so the
#: "never logged" assertion below would fail on any message containing that letter and prove nothing.
_SCOPED = {
    "aws_access_key_id": "VENDEDKEY",
    "aws_secret_access_key": "vended-secret-9f3a",
    "aws_session_token": "vended-token-7c1e",
    "endpoint": "http://rustfs:9000",
}
_AMBIENT = {"aws_access_key_id": "rustfsadmin", "aws_secret_access_key": "rustfsadmin", "endpoint": "http://rustfs:9000"}


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _settings(catalog_url: str = "http://catalog:2333") -> MaintenanceSettings:
    """One explicit keyword rather than a `**overrides` splat: pydantic-settings' own constructor
    keywords (`_cli_settings_source`, `_case_sensitive`, …) are in the same namespace, so a splat of
    `str` values type-checks against every one of them and `ty` reports 36 diagnostics for one call."""
    return MaintenanceSettings(MAINTENANCE_CATALOG_URL=catalog_url)


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Records every vend request and answers with a direct credential."""
    calls: list[dict[str, Any]] = []

    def _post(url: str, **kwargs: Any) -> _Response:
        calls.append({"url": url, **kwargs})
        return _Response(200, {"mode": "direct", "credentials": {"storage_options": _SCOPED}})

    monkeypatch.setattr(credentials.httpx, "post", _post)
    return calls


def test_the_rewrite_is_signed_by_the_vended_credential(door: list[dict[str, Any]]) -> None:
    options = credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT)
    assert options == _SCOPED
    assert options["aws_access_key_id"] != "rustfsadmin"


def test_the_vend_names_the_table_and_asks_for_the_write_tier(door: list[dict[str, Any]]) -> None:
    credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT)
    assert door[0]["url"].endswith("/v1/table/acme-bronze$events/credentials")
    assert door[0]["params"] == {"tier": "write"}, "a read-tier credential 200s and then 403s on the PUT"


def test_the_service_presents_its_identity(door: list[dict[str, Any]]) -> None:
    credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT)
    assert door[0]["headers"]["x-lance-service-identity"] == "service-maintenance"


@pytest.mark.parametrize(
    ("uri", "catalog_url", "reason"),
    [
        ("s3://acme-bucket/medallion/bronze", "http://catalog:2333", "a nested layout yields no identifier — declining beats guessing"),
        ("s3://acme-bucket/4c49d010_acme-bronze$events", "", "no catalog configured"),
    ],
)
def test_an_unvendable_dataset_is_still_maintained(door: list[dict[str, Any]], uri: str, catalog_url: str, reason: str) -> None:
    assert credentials.write_options_for(uri, _settings(catalog_url=catalog_url), fallback=_AMBIENT) == _AMBIENT, reason


@pytest.mark.parametrize(
    "answer",
    [
        _Response(200, {"mode": "server_mediated"}),
        _Response(503, {}),
        _Response(403, {}),
        _Response(200, {"mode": "direct", "credentials": {}}),
    ],
)
def test_every_non_answer_degrades_rather_than_failing_the_run(monkeypatch: pytest.MonkeyPatch, answer: _Response) -> None:
    monkeypatch.setattr(credentials.httpx, "post", lambda url, **kwargs: answer)
    assert credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT) == _AMBIENT


def test_an_unreachable_catalog_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, **kwargs: Any) -> _Response:
        raise credentials.httpx.ConnectError("no route to host")

    monkeypatch.setattr(credentials.httpx, "post", _boom)
    assert credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT) == _AMBIENT


def test_the_credential_is_reported_but_never_logged(door: list[dict[str, Any]], caplog: pytest.LogCaptureFixture) -> None:
    """Which credential signed a rewrite must be readable, or the posture is unauditable — and the
    secret must not be, or the log store undoes the scoping it is reporting on."""
    import logging

    with caplog.at_level(logging.INFO, logger="maintenance.services.credentials"):
        credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "SCOPED" in messages and "acme-bronze$events" in messages
    assert _SCOPED["aws_secret_access_key"] not in messages
    assert _SCOPED["aws_session_token"] not in messages
