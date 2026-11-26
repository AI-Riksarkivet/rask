"""A request the catalog door's own model refuses is never sent, and is raised as maintenance's own unsendable request:
the catalog judged nothing, so it is no refusal of the catalog's (`maintenance.services.credentials`).

The catalog is behind a respx router holding no route, so a request that left would fail the test where it was sent.
"""

from collections.abc import Callable

import pytest
import respx

from lakehouse.maintenance_doors import MAX_OBJECTS_PER_REQUEST
from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials


SETTINGS = MaintenanceSettings(MAINTENANCE_CATALOG_URL="http://catalog.test")
RECORD = "0b6f7c1e-5a2d-4c8e-9f31-6d2a4e7b9c10"

pytestmark = pytest.mark.usefixtures("catalog_calls_carry_no_identity")


@pytest.mark.parametrize(
    ("ask", "named"),
    [
        pytest.param(
            lambda: credentials.note_trash_refusal("table", RECORD, SETTINGS, reason=""),
            rf"a note of its refusal for table:{RECORD} \(reason: String should have at least 1 character\), so none was sent",
            id="a-refusal-that-gives-no-reason",
        ),
        pytest.param(
            lambda: credentials.revoke_ghosts(SETTINGS, []),
            r"a ghost's revoke for the estate's grants \(objects: List should have at least 1 item",
            id="a-revoke-of-no-object",
        ),
        pytest.param(
            lambda: credentials.rebuild_tenancy(SETTINGS, [f"wh-{n}" for n in range(MAX_OBJECTS_PER_REQUEST + 1)]),
            rf"a tenancy rebuild for the estate's grants \(warehouses: List should have at most {MAX_OBJECTS_PER_REQUEST} items",
            id="a-rebuild-of-more-warehouses-than-one-request-names",
        ),
    ],
)
@respx.mock
def test_a_request_the_doors_model_refuses_is_raised_as_unsendable_and_not_sent(ask: Callable[[], object], named: str) -> None:
    with pytest.raises(credentials.UnsendableRequest, match=named):
        ask()
