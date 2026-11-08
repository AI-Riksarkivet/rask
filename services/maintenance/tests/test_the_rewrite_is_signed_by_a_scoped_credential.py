"""An expiry pass's WRITE is signed by the catalog's vend for its table, and by nothing else ([[LH-219]]).

Version expiry deletes files and disarming commit-path cleanup commits a version, so a service doing both across
every bucket with one long-lived key is the exact posture the catalog's vending door exists to end. Proven end-to-end
for the ingest plane 2026-09-03 (a credential vended for one table read AND wrote it, and was refused on a sibling
with 403 AccessDenied); this is the same door.

WHAT EACH TEST BELOW HOLDS, because each would be a silent regression:

* the vend asks for the `maintain` tier, which the door's route is matched on. A read-tier credential returns 200 and a
  perfectly valid credential, and the pass then dies at the object store as `403 AccessDenied` on a DELETE — reading as
  a missing grant on the table rather than a request that asked for the wrong thing;
* an answer that is not a vend stops the dataset. The only other credential maintenance holds is its own session, which
  reads the whole estate, so a deployment with no door, a door vending no direct credential, a door that could not
  answer and a door that said no each leave the rewrite unsigned, and the sweep records which;
* a pass holding no vend writes nothing, whatever it finds. The sweep asks for a vend only where its probe finds
  something to write, and a table that changes between the probe and the pass would otherwise be written under the
  session the pass reads with.
"""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import lance
import pyarrow as pa
import pytest
import respx

from lakehouse.context import DiagnosticFormatter
from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from maintenance.services.credentials import MaintenanceDenied, MaintenanceUnauthenticated, TableNotGoverned, VendingNotDirect, VendUndecided
from maintenance.services.expiry import ExpiryPolicy, ExpiryTarget, expire_versions


pytestmark = pytest.mark.usefixtures("catalog_identity_token")


CATALOG = "http://catalog:2333"
_TABLE = "acme-bronze$events"
_URI = "s3://acme-bucket/4c49d010_acme-bronze$events"
_DOOR = f"{CATALOG}/management/v1/table/{_TABLE}/credentials"
#: Distinctive values on purpose. A one-character secret is a substring of ordinary prose, so the
#: "never logged" assertion below would fail on any message containing that letter and prove nothing.
_SCOPED = {
    "aws_access_key_id": "VENDEDKEY",
    "aws_secret_access_key": "vended-secret-9f3a",
    "aws_session_token": "vended-token-7c1e",
    "aws_endpoint": "https://s3.public.example",
    "allow_http": "false",
}
_VEND = {"mode": "direct", "credentials": {"storage_options": _SCOPED}}


def _settings(catalog_url: str = CATALOG) -> MaintenanceSettings:
    """One explicit keyword rather than a `**overrides` splat: pydantic-settings' own constructor
    keywords (`_cli_settings_source`, `_case_sensitive`, …) are in the same namespace, so a splat of
    `str` values type-checks against every one of them and `ty` reports 36 diagnostics for one call."""
    return MaintenanceSettings(MAINTENANCE_CATALOG_URL=catalog_url)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    """The catalog's side of the wire. A request no route matches fails the test, so one sent for another tier does."""
    with respx.mock() as mock:
        yield mock


def _door(router: respx.MockRouter) -> respx.Route:
    """The vend door, matched only at the ``maintain`` tier."""
    return router.post(_DOOR, params={"tier": "maintain"})


def test_the_pass_is_signed_by_the_vended_credential_at_maintenances_own_store_address(router: respx.MockRouter) -> None:
    """[[LH-489]] At maintenance's own store address: the catalog vends the one a client off the cluster reaches."""
    _door(router).mock(return_value=httpx.Response(200, json=_VEND))
    settings = _settings()

    options = credentials.write_options_for(_URI, settings, table_id=_TABLE)

    assert settings.s3_endpoint.startswith("http://")
    assert options.in_clear() == {
        **_SCOPED,
        "aws_endpoint": settings.s3_endpoint,
        "aws_endpoint_url_s3": settings.s3_endpoint,
        "aws_s3_express": "false",
        "allow_http": "true",
        "aws_provider_scheme": "token",
        "aws_skip_signature": "false",
    }


def test_the_credential_maintenance_holds_renders_neither_its_secret_nor_its_session_token(router: respx.MockRouter, caplog: pytest.LogCaptureFixture) -> None:
    """[[LH-072]] Maintenance holds the vend from the door's answer to the call that signs, and nothing that turns it into
    text in between shows its secret or session token."""
    _door(router).mock(return_value=httpx.Response(200, json=_VEND))
    held = credentials.write_options_for(_URI, _settings(), table_id=_TABLE)

    refused = RuntimeError("the store refused the vend", held)
    with caplog.at_level(logging.WARNING, logger=credentials.logger.name):
        credentials.logger.warning("vend_refused %s %r", held, held, extra={"vended": held}, exc_info=refused)

    rendered = {
        "repr": repr(held),
        "str": str(held),
        "format": f"{held}",
        "model_dump": repr(held.model_dump()),
        "model_dump json": json.dumps(held.model_dump(mode="json")),
        "model_dump_json": held.model_dump_json(),
        "log": "\n".join(DiagnosticFormatter("%(message)s").format(record) for record in caplog.records),
        "error echo": "".join(traceback.format_exception(refused)),
    }
    secrets = (_SCOPED["aws_secret_access_key"], _SCOPED["aws_session_token"])
    assert [how for how, text in rendered.items() if any(secret in text for secret in secrets)] == []


def test_a_deployment_with_no_vending_door_refuses_every_rewrite_before_asking(router: respx.MockRouter) -> None:
    with pytest.raises(VendingNotDirect, match="no catalog door is configured"):
        credentials.write_options_for(_URI, _settings(catalog_url=""), table_id=_TABLE)

    assert router.calls.call_count == 0


@pytest.mark.parametrize(
    ("answer", "stopped_by"),
    [
        pytest.param(httpx.Response(200, json={"mode": "server_mediated"}), VendingNotDirect, id="server-mediated"),
        pytest.param(httpx.Response(404, json={"code": 0, "detail": "Not Found"}), VendingNotDirect, id="no-door-at-the-path"),
        pytest.param(httpx.Response(200, json={"mode": "direct", "credentials": {}}), VendUndecided, id="no-options"),
        pytest.param(
            httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": {**_SCOPED, "aws_secret_access_key": ""}}}),
            VendUndecided,
            id="half-a-credential",
        ),
        pytest.param(
            httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": {"aws_endpoint": "https://s3.public.example"}}}),
            VendUndecided,
            id="no-key-pair",
        ),
        pytest.param(httpx.Response(200, text="<html>"), VendUndecided, id="unparseable"),
        pytest.param(httpx.Response(429, json={"code": 21, "detail": "shed"}), VendUndecided, id="throttled"),
        pytest.param(httpx.Response(400, json={"code": 13, "detail": "bad tier"}), MaintenanceDenied, id="another-4xx"),
    ],
)
def test_an_answer_that_is_not_a_vend_stops_the_dataset(router: respx.MockRouter, answer: httpx.Response, stopped_by: type[Exception]) -> None:
    """Every shape of "no credential is on offer" leaves the rewrite unsigned, as its own refusal or failure.

    Read as no credential, any of these would leave the pass to sign with maintenance's own session, which reaches the
    whole estate ([[LH-219]]). ``server_mediated`` and a 404 naming no absent object (code 0 is a path no door serves)
    mean this deployment is vended nothing direct, so the sweep refuses without counting the table; a body naming no
    whole credential, one that does not parse, and a throttled door decided nothing, so the next tick retries; any other
    4xx is the door's refusal of this table.
    """
    _door(router).mock(return_value=answer)

    with pytest.raises(stopped_by) as caught:
        credentials.write_options_for(_URI, _settings(), table_id=_TABLE)

    assert type(caught.value) is stopped_by


@pytest.mark.parametrize(
    ("status", "refused_as", "remedy"),
    [
        pytest.param(401, MaintenanceUnauthenticated, "service credential", id="unauthenticated"),
        pytest.param(403, MaintenanceDenied, "or no such table is registered", id="denied"),
    ],
)
def test_a_door_that_said_NO_refuses_the_dataset_and_names_the_remedy(
    router: respx.MockRouter, status: int, refused_as: type[MaintenanceDenied], remedy: str
) -> None:
    """ "You may not" refuses the DATASET, not the run: the sweep records a refusal and maintains everything else.

    Measured on the live estate 2026-09-10, when a fallback still existed: eight tables refused 403 and rewritten under
    the root key, visible only as an INFO line. A 401 is about this service's own credential, which the sweep never
    counts under the table, and a 403 about the id; the catalog's gate runs before existence resolution, so the 403 names
    both of its causes ([[LH-164]]).
    """
    _door(router).mock(return_value=httpx.Response(status, json={}))

    with pytest.raises(MaintenanceDenied) as caught:
        credentials.write_options_for(_URI, _settings(), table_id=_TABLE)

    assert type(caught.value) is refused_as
    assert remedy in str(caught.value) and _TABLE in str(caught.value), str(caught.value)


@pytest.mark.parametrize(("code", "name"), [(4, "TABLE_NOT_FOUND"), (1, "NAMESPACE_NOT_FOUND")])
def test_a_door_that_found_NO_SUCH_TABLE_refuses_the_dataset(router: respx.MockRouter, code: int, name: str) -> None:
    """A 404 naming the table or namespace absent is an answer about the id, as a 403 is, not an outage.

    Measured 2026-09-26, when a fallback still existed: it cleaned a table the vend door had answered 404 TableNotFound
    (versions 7 to 1). A trashed table answers this way, and it is frozen until undrop or purge.
    """
    _door(router).mock(return_value=httpx.Response(404, json={"code": code, "detail": "absent"}))

    with pytest.raises(TableNotGoverned) as caught:
        credentials.write_options_for(_URI, _settings(), table_id=_TABLE)

    assert name in str(caught.value) and _TABLE in str(caught.value), str(caught.value)


def test_an_unreachable_catalog_stops_the_dataset_for_the_next_tick(router: respx.MockRouter) -> None:
    _door(router).mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(VendUndecided, match="could not be asked"):
        credentials.write_options_for(_URI, _settings(), table_id=_TABLE)


def test_a_pass_holding_no_vend_writes_nothing_whatever_it_finds(tmp_path: Path) -> None:
    uri = str(tmp_path / "t.lance")
    rows = pa.table({"id": pa.array([1], pa.int64())})
    lance.write_dataset(rows, uri)
    for _ in range(3):
        lance.write_dataset(rows, uri, mode="overwrite")

    result = expire_versions(ExpiryTarget(uri=uri, storage_options={}), ExpiryPolicy(older_than=timedelta(0), retain_versions=1))

    assert (result.reclaimable_versions, result.old_versions_removed, len(lance.dataset(uri).versions())) == (3, 0, 4)
