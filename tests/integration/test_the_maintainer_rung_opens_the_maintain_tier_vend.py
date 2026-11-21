"""A maintainer may obtain the credential its own rewrite needs, and each writing tier opens to its own rung only.

the lakehouse register, row H13 (drained 2026-09-10; in git history). MEASURED LIVE 2026-09-08: of 285 rewrite attempts in one sweep
tick, 207 were refused a vended credential and fell back to the deployment's ROOT key —
`credentials.write_options_for` treats a refused vend as a reason to reach for the ambient credential,
and the pod says so itself ("this rewrite is signed by the root key"). Checking the live store
explained the split exactly: `user:service-maintenance` holds `can_write_data` on some tables ad hoc
and none on the rest, while `can_maintain` was false everywhere because no `maintainer` tuple had ever
been written.

THE TWO RUNGS MEAN DIFFERENT THINGS and the model keeps them apart — a maintainer is denied read,
write, drop and promote (`model.fga.yaml`'s own test block asserts all four). What they share is the
STORAGE verb: compaction, index optimization and reclamation rewrite files, and an object store cannot
express "rewrite but do not change content" — both are `PutObject`. So a maintainer receives a
whole-prefix credential scoped to its own table for 900 s, which is the narrower of the two available
postures by an enormous margin; the other is the root key.

ONE RUNG PER TIER ([[LH-202]]). The maintain tier reaches `_versions/` and deletes, the write tier only
`data/`, so a door that accepted either rung for either tier would hand a writer the policy that commits
any transaction around `version/create`.

Owner ruling 2026-09-08. Driven through the REAL route rather than a helper, because what is being
pinned is a credential-vending authorization decision, against the catalog as deployed with every decision the real
model's on an OpenFGA store of the test's own. The maintenance identity holds ``maintenance`` on the estate's warehouse,
the only rung that opens ``can_vend_maintain``; a maintainer, a writer and a reader each hold their rung on the table.
The caller is authenticated as the maintenance identity, and the vendor mints a fixed credential.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from pydantic import SecretStr

from catalog.core.vending import TableRecords, Tier, VendedSession
from lakehouse.governed.fga_ids import user_object
from lakehouse.governed.machine_identity import ServicePrincipal
from lakehouse.storage.credential import S3KeyPair
from tests.governed_catalog import GovernedCatalog


class _Vendor:
    def vend(
        self,
        *,
        table_location: str,
        tier: Tier,
        session_name: str,
        bases: Sequence[str] = (),
        data_directories: Sequence[str] = (),
        branch: str = "",
        records: TableRecords | None = None,
    ) -> VendedSession:
        key_pair = S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK"), session_token=SecretStr("ST"))
        return VendedSession(key_pair=key_pair, endpoint=None, region="us-east-1", expires_at_millis=1)


#: The maintenance service's own principal, the caller every test here vends as.
_MAINTENANCE = ServicePrincipal.for_account("system:serviceaccount:rask:maintenance")
TABLE = "lance_catalog$db$t"


@pytest.fixture
def governed(governed_catalog: GovernedCatalog) -> Iterator[tuple[GovernedCatalog, str]]:
    """bob's ``db$t``, made through the doors; every later request is the maintenance identity's. Yields the catalog and
    the table's object."""
    from catalog.api import security
    from catalog.api.dependencies import get_vendor

    assert governed_catalog.create_in_the_estate("bob", "db").status_code == 200
    governed_catalog.create_table("bob", TABLE)
    table = f"table:{governed_catalog.uuid('table', TABLE, 'bob')}"
    app = governed_catalog.client.app
    app.dependency_overrides[get_vendor] = lambda: _Vendor()
    app.dependency_overrides[security.authenticate] = lambda: _MAINTENANCE
    yield governed_catalog, table


_CREDENTIALS = f"/management/v1/table/{TABLE}/credentials"


@pytest.mark.parametrize(
    ("held", "door", "status"),
    [
        pytest.param({"maintenance": GovernedCatalog.ESTATE}, f"{_CREDENTIALS}?tier=maintain", 200, id="the-maintenance-identity-gets-the-maintain-tier"),
        pytest.param({"maintainer": "table"}, f"{_CREDENTIALS}?tier=maintain", 403, id="a-maintainer-is-refused-the-maintain-tier"),
        pytest.param({"writer": "table"}, f"{_CREDENTIALS}?tier=maintain", 403, id="a-writer-is-refused-the-maintain-tier"),
        pytest.param({"maintainer": "table"}, f"{_CREDENTIALS}?tier=write", 403, id="a-maintainer-is-refused-the-write-tier"),
        pytest.param({"writer": "table"}, f"{_CREDENTIALS}?tier=write", 200, id="a-writer-gets-the-write-tier"),
        pytest.param({"reader": "table"}, _CREDENTIALS, 200, id="a-reader-gets-the-read-tier"),
        pytest.param({}, f"{_CREDENTIALS}?tier=maintain", 403, id="holding-no-rung-is-refused-at-the-route"),
        pytest.param({"maintainer": "table"}, f"/v1/table/{TABLE}/query", 403, id="can-maintain-opens-no-data-read"),
    ],
)
def test_each_writing_tier_opens_to_its_own_rung_only(governed: tuple[GovernedCatalog, str], held: dict[str, str], door: str, status: int) -> None:
    """A maintainer holds no read rung, so the route opening to the maintain tier without one is what lets the
    maintenance identity reach it at all (measured 2026-09-08: the route asked `can_read_data` and refused every rewrite);
    `query` is a data read like `credentials`, and a maintainer must not gain it."""
    catalog, table = governed
    if held:
        catalog.store.write(*((user_object(_MAINTENANCE.subject), rung, table if on == "table" else on) for rung, on in held.items()))

    resp = catalog.client.post(door, json={}) if door.endswith("/query") else catalog.client.post(door)

    assert resp.status_code == status, resp.text
    if status == 200:
        assert resp.json()["mode"] == "direct"
    else:
        assert resp.json()["code"] == 15, resp.text
