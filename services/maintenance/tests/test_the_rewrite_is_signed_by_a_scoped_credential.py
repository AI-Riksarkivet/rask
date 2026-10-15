"""An expiry pass's WRITE must be signed by a table-scoped credential, not the root object-store key.

Version expiry deletes files and disarming commit-path cleanup commits a version, so a service doing both across
every bucket with one long-lived key is the exact posture the catalog's vending door exists to end. Proven end-to-end
for the ingest plane 2026-09-03 (a credential vended for one table read AND wrote it, and was refused on a sibling
with 403 AccessDenied); this is the same door.

WHAT MUST NOT CHANGE, and each has a test below because each would be a silent regression:

* the whole-estate protection PRE-PASS keeps the ambient credential. It must open every manifest in
  every bucket before any dataset is expired, and no per-table credential can express that. It is a
  READ; the clause is about write paths;
* an unvendable dataset still gets maintained. A location the flat-layout parser declines, a
  deployment with no catalog URL, a door answering `server_mediated` — all fall back to the ambient
  credential, because a hardening that can FAIL a maintenance run turns an optional improvement into a
  new way to stop reclaiming disk;
* the vend asks for the `maintain` tier. A read-tier credential returns 200 and a perfectly valid
  credential, and the pass then dies at the object store as `403 AccessDenied` on a DELETE — reading as a
  missing grant on the table rather than a request that asked for the wrong thing.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from maintenance.services.credentials import MaintenanceDenied, TableNotGoverned


pytestmark = pytest.mark.usefixtures("catalog_identity_token")


#: Distinctive values on purpose. A one-character secret is a substring of ordinary prose, so the
#: "never logged" assertion below would fail on any message containing that letter and prove nothing.
_SCOPED = {
    "aws_access_key_id": "VENDEDKEY",
    "aws_secret_access_key": "vended-secret-9f3a",
    "aws_session_token": "vended-token-7c1e",
    "endpoint": "https://s3.public.example",
    "allow_http": "false",
}
_AMBIENT = {"aws_access_key_id": "minioadmin", "aws_secret_access_key": "minioadmin", "endpoint": "http://rustfs:9000"}


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


def test_the_pass_is_signed_by_the_vended_credential(door: list[dict[str, Any]]) -> None:
    """[[LH-489]] At maintenance's own store address: the catalog vends the one a client off the cluster reaches."""
    settings = _settings()
    options = credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", settings, fallback=_AMBIENT)
    assert settings.s3_endpoint.startswith("http://")
    assert options == {**_SCOPED, "endpoint": settings.s3_endpoint, "allow_http": "true"}
    assert options["aws_access_key_id"] != "minioadmin"


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
        _Response(200, {"mode": "direct", "credentials": {}}),
        httpx.Response(404, json={"code": 0, "detail": "Not Found"}),
        httpx.Response(404, text="not found"),
    ],
)
def test_a_door_that_could_not_ANSWER_degrades_rather_than_failing_the_run(monkeypatch: pytest.MonkeyPatch, answer: _Response | httpx.Response) -> None:
    """Shapes of "no credential is on offer", and none of them is a refusal.

    `server_mediated` is a supported posture, and a `direct` answer carrying no options offered nothing.
    A 404 that names no absent table or namespace (code 0 is a path no door serves) is the door missing,
    not an answer about the id. Reclaiming disk through any of these is why the ambient fallback exists.
    A 5xx is not among them: the door was asked and could not decide, which stops the dataset ([[LH-279]]).
    """
    monkeypatch.setattr(credentials.httpx, "post", lambda url, **kwargs: answer)
    assert credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT) == _AMBIENT


@pytest.mark.parametrize(("status", "remedy"), [(401, "service credential"), (403, "can_maintain")])
def test_a_door_that_said_NO_refuses_instead_of_reaching_for_the_ambient_key(monkeypatch: pytest.MonkeyPatch, status: int, remedy: str) -> None:
    """A DENIAL sat in the degrading list above, and it is a different question from the three there.

    "We could not ask" permits a fallback; "you may not" cannot, because the fallback is the ambient
    credential and it reaches every bucket in the estate — so the answer to being refused one table
    would be to maintain it with a key that reaches all of them. Measured on the live estate 2026-09-10:
    eight tables refused 403 and rewritten under the root key, visible only as an INFO line.

    It refuses the DATASET, not the run: the sweep records a refusal and maintains everything else. The
    remedy differs: a 403 is a grant on the table, a 401 is maintenance's own credential.
    """
    monkeypatch.setattr(credentials.httpx, "post", lambda url, **kwargs: _Response(status, {}))
    with pytest.raises(MaintenanceDenied) as caught:
        credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT)
    assert remedy in str(caught.value), f"the refusal must name what would allow it: {caught.value}"


@pytest.mark.parametrize(("code", "name"), [(4, "TABLE_NOT_FOUND"), (1, "NAMESPACE_NOT_FOUND")])
def test_a_door_that_found_NO_SUCH_TABLE_refuses_instead_of_reaching_for_the_ambient_key(monkeypatch: pytest.MonkeyPatch, code: int, name: str) -> None:
    """A 404 naming the table or namespace absent is an answer about the id, as a 403 is, not an outage.

    Measured 2026-09-26: the ambient fallback cleaned a table the vend door had answered 404 TableNotFound
    (versions 7 to 1). A trashed table answers this way, and it is frozen until undrop or purge.
    """
    answer = httpx.Response(404, json={"code": code, "detail": "absent"})
    monkeypatch.setattr(credentials.httpx, "post", lambda url, **kwargs: answer)
    with pytest.raises(TableNotGoverned) as caught:
        credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT)
    assert name in str(caught.value) and "acme-bronze$events" in str(caught.value), str(caught.value)


def test_an_unreachable_catalog_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, **kwargs: Any) -> _Response:
        raise credentials.httpx.ConnectError("no route to host")

    monkeypatch.setattr(credentials.httpx, "post", _boom)
    assert credentials.write_options_for("s3://acme-bucket/4c49d010_acme-bronze$events", _settings(), fallback=_AMBIENT) == _AMBIENT
