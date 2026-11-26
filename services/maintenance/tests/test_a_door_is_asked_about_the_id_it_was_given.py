"""Every catalog door maintenance asks is asked about the id it was given, whatever characters the id holds.

A name holds `?`, `#`, `%` and spaces (`catalog.core.identifiers.require_names`), and a door's path carries the id. Sent
bare, `?` and `#` end the path, so the table is never vended, disarmed or floor-raised, and a literal `%41` is decoded
by the catalog to `A`, so the disarm and the floor raise reach another table. Driven through the door client's real
HTTP pipeline with the catalog behind respx: what is asserted is the path on the wire, which the catalog decodes back
(`catalog.api.path_reading`). One route answers the whole host, so a wrongly built path is compared, not unrouted.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx
from tests.vend_answers import vended

from lakehouse.maintenance_doors import CloneEndingEntry
from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials


CATALOG = "http://catalog.test"
SETTINGS = MaintenanceSettings(MAINTENANCE_CATALOG_URL=CATALOG)
#: One id holding every character a bare path would misread, and the `$` that joins an id's names.
HOSTILE = "acme$ns$a?b#c%41 d"
HOSTILE_ON_THE_WIRE = "acme$ns$a%3Fb%23c%2541%20d"

pytestmark = pytest.mark.usefixtures("catalog_calls_carry_no_identity")


@pytest.mark.parametrize(
    ("table_id", "on_the_wire"),
    [
        pytest.param("acme$ns$a?b", "acme$ns$a%3Fb", id="question-mark"),
        pytest.param("acme$ns$a#b", "acme$ns$a%23b", id="hash"),
        pytest.param("acme$ns$t%41", "acme$ns$t%2541", id="percent-escape"),
        pytest.param("acme$ns$a b", "acme$ns$a%20b", id="space"),
        pytest.param("acme$ns$t", "acme$ns$t", id="delimiter-alone"),
    ],
)
@respx.mock
def test_a_vend_is_asked_for_the_table_its_id_names(table_id: str, on_the_wire: str) -> None:
    uri = "s3://acme-bucket/t"
    scoped = {"aws_access_key_id": "VENDEDKEY", "aws_secret_access_key": "vended-secret"}
    door = respx.post(host="catalog.test").mock(return_value=httpx.Response(200, json=vended(scoped, location=uri)))

    credentials.write_options_for(uri, SETTINGS, table_id=table_id)

    assert [call.request.url.raw_path for call in door.calls] == [f"/management/v1/table/{on_the_wire}/credentials?tier=maintain".encode()]


@pytest.mark.parametrize(
    ("ask", "method", "answer", "on_the_wire"),
    [
        pytest.param(
            lambda: credentials.disarm_cleanup(HOSTILE, SETTINGS, branch=None),
            "POST",
            {"version": 3},
            f"/management/v1/table/{HOSTILE_ON_THE_WIRE}/maintenance/disarm",
            id="disarm",
        ),
        pytest.param(
            lambda: credentials.raise_floor(HOSTILE, SETTINGS, dry_run=True),
            "POST",
            {},
            f"/management/v1/table/{HOSTILE_ON_THE_WIRE}/maintenance/floor",
            id="floor",
        ),
        pytest.param(
            lambda: credentials.end_trash("table", "u?1/../2", SETTINGS),
            "DELETE",
            {"tuples_revoked": 0},
            "/management/v1/trash/table/u%3F1%2F..%2F2",
            id="trash",
        ),
        pytest.param(
            lambda: credentials.finish_clone_ending(
                CloneEndingEntry(digest="d#1", filing="f/2", door="drop", filed_at=datetime(2026, 11, 26, tzinfo=UTC)), SETTINGS
            ),
            "POST",
            {"tags_released": 0, "kept": False},
            "/management/v1/clone-endings/d%231/f%2F2/finish",
            id="clone-ending",
        ),
    ],
)
@respx.mock
def test_a_door_keeps_an_id_one_path_segment(ask: Callable[[], object], method: str, answer: dict[str, object], on_the_wire: str) -> None:
    door = respx.route(method=method, host="catalog.test").mock(return_value=httpx.Response(200, json=answer))

    ask()

    assert [call.request.url.raw_path for call in door.calls] == [on_the_wire.encode()]
