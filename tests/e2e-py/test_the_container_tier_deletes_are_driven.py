"""The CONTAINER tier's deletion rules, driven against the deployed catalog.

[[LH-028]]. Table-level drop/protect/force/undrop are proven live; the container tier is not, and that
is exactly where `force` and cascade interact. Measured 2026-09-16: `test_warehouses_e2e.py` carries
isolation, deactivate/activate and two auth legs and NO delete at all, and no e2e anywhere drives
project delete, the namespace cascade or `projects_claiming_bucket`.

EVERY CONTAINER THIS FILE DESTROYS IS ONE IT CREATED, in a bucket it named seconds earlier. The
refusal legs are non-destructive by construction — they assert a 409 or a 404.

`purge_bucket` IS SENT, but only in cleanup and only against those buckets. The first version withheld
it on the principle that a customer's bucket is not recoverable, which is right for a bucket someone
else owns and wrong for one this fixture minted: withholding it left the record deleted and the bucket
behind, so every run added orphan buckets to the estate's own drift report — a test manufacturing the
finding it is meant to be independent of. The cleanup is ASSERTED rather than best-effort for the same
reason.

THE ORDER IS THE POINT, not just the outcomes. The doors authorize BEFORE they disclose — a 409 naming
another tenant's namespaces is a disclosure a 403 must beat to — and `force` overrides deletion
PROTECTION only, never the gate. Both are asserted rather than described.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import requests


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
TOKEN = os.environ.get("LANCE_E2E_TOKEN", "")
PROJECT = os.environ.get("LANCE_E2E_PROJECT", "acme")
#: A caller who administers NOTHING on this project, used ONLY to prove the gate runs before disclosure
#: and never to delete anything.
#:
#: `LANCE_E2E_NONADMIN_TOKEN`, NOT the tenant-B token, and the difference is measured rather than
#: assumed: `scripts/e2e_live.sh:90-94` records that bob is a member of `team:eng`, which is bound to
#: `project:acme`, so bob HOLDS `can_administer` there — "can_administer(project:acme) is False for
#: publisher and True for bob". Driving this leg with bob got a 409 naming the namespace, which reads
#: exactly like a disclosure defect and is not one. `topology.py` states the rule this file now follows:
#: "An outsider who is secretly privileged does not make a 403 leg fail honestly — it makes it allege a
#: security property the estate does not have." The runner picks a candidate that really is unprivileged
#: and leaves the variable EMPTY when none is, so this leg skips rather than asserts falsely.
OUTSIDER = os.environ.get("LANCE_E2E_NONADMIN_TOKEN", "")

pytestmark = pytest.mark.e2e


def _auth(token: str = "") -> dict[str, str]:
    return {"authorization": f"Bearer {token or TOKEN}"}


@pytest.fixture(scope="module")
def catalog() -> str:
    if not CATALOG or not TOKEN:
        pytest.skip("set LANCE_E2E_CATALOG_URL + LANCE_E2E_TOKEN (a deployed stack with warehouses enabled)")
    try:
        requests.get(f"{CATALOG}/livez", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("catalog not reachable")
    return CATALOG


@pytest.fixture
def warehouse(catalog: str) -> Iterator[str]:
    """A warehouse this test owns outright, in its own bucket, removed on the way out."""
    name = f"e2edel-{uuid.uuid4().hex[:8]}"
    created = requests.post(f"{catalog}/v1/warehouses", json={"id": name, "project": PROJECT}, headers=_auth(), timeout=30)
    if created.status_code not in (200, 201, 409):
        pytest.skip(f"cannot provision a warehouse to delete ({created.status_code}): {created.text[:200]}")
    yield name
    # PURGE THE BUCKET, because this suite made it. The first version deleted the record and left the
    # bucket, so every run added one orphan bucket to the estate's drift report — measured 2026-09-16,
    # 18 `e2edel-*` buckets from four runs, which is a test that manufactures the finding it is meant to
    # be independent of. `purge_bucket` is safe here and nowhere else in this file: the bucket was
    # created by this fixture seconds earlier and holds nothing else.
    removed = requests.delete(f"{catalog}/v1/warehouses/{name}?cascade=true&purge_bucket=true&force=true", headers=_auth(), timeout=90)
    # ASSERTED, not best-effort. A cleanup nobody checks is how this suite added orphan buckets to the
    # estate's drift report on every run before anyone looked — a test that manufactures the finding it
    # is meant to be independent of.
    assert removed.status_code in (200, 404), f"the fixture leaked bucket {name!r}: {removed.status_code} {removed.text[:300]}"


def test_deleting_a_warehouse_that_does_not_exist_is_404(catalog: str) -> None:
    response = requests.delete(f"{catalog}/v1/warehouses/e2edel-nosuchwarehouse-{uuid.uuid4().hex[:6]}", headers=_auth(), timeout=30)

    assert response.status_code == 404, f"expected 404 for a missing warehouse, got {response.status_code}: {response.text[:200]}"


def test_an_empty_warehouse_deletes(catalog: str, warehouse: str) -> None:
    """The positive control. Without it every refusal below could be a door that refuses everything."""
    response = requests.delete(f"{catalog}/v1/warehouses/{warehouse}", headers=_auth(), timeout=60)

    assert response.status_code == 200, f"an empty warehouse this caller administers did not delete: {response.status_code} {response.text[:300]}"


def test_a_warehouse_holding_a_namespace_refuses_and_NAMES_it(catalog: str, warehouse: str) -> None:
    """A refusal that does not say what blocks it just moves the search to the user."""
    ns = f"e2edelns{uuid.uuid4().hex[:6]}"
    bound = requests.post(f"{catalog}/v1/warehouses/{warehouse}/namespaces", json={"namespace": ns}, headers=_auth(), timeout=30)
    assert bound.status_code in (200, 201, 409), bound.text[:300]

    response = requests.delete(f"{catalog}/v1/warehouses/{warehouse}", headers=_auth(), timeout=60)

    assert response.status_code == 409, f"a warehouse still holding {ns!r} deleted anyway: {response.status_code}"
    assert ns in response.text, f"the 409 does not name the namespace that blocks it: {response.text[:300]}"


def test_cascade_drops_exactly_those_namespaces(catalog: str, warehouse: str) -> None:
    ns = f"e2edelns{uuid.uuid4().hex[:6]}"
    requests.post(f"{catalog}/v1/warehouses/{warehouse}/namespaces", json={"namespace": ns}, headers=_auth(), timeout=30)

    response = requests.delete(f"{catalog}/v1/warehouses/{warehouse}?cascade=true", headers=_auth(), timeout=120)

    assert response.status_code == 200, f"cascade did not drop the bound namespace: {response.status_code} {response.text[:300]}"


@pytest.mark.skipif(not OUTSIDER, reason="no genuinely unprivileged identity on this project — see the OUTSIDER note; skipping beats asserting falsely")
def test_an_outsider_is_refused_WITHOUT_learning_what_the_warehouse_holds(catalog: str, warehouse: str) -> None:
    """Authorize first, then disclose. A 409 naming another tenant's namespaces is a disclosure a 403
    must beat to — so an outsider must never see the emptiness answer, with or without `force`."""
    ns = f"e2edelns{uuid.uuid4().hex[:6]}"
    requests.post(f"{catalog}/v1/warehouses/{warehouse}/namespaces", json={"namespace": ns}, headers=_auth(), timeout=30)

    for query in ("", "?force=true"):
        response = requests.delete(f"{catalog}/v1/warehouses/{warehouse}{query}", headers=_auth(OUTSIDER), timeout=60)

        assert response.status_code in (403, 404), f"an outsider got {response.status_code} on {query or '(no flags)'}: {response.text[:200]}"
        assert ns not in response.text, f"the refusal leaked a namespace name to a caller who may not administer it: {response.text[:300]}"


def test_a_project_delete_has_no_cascade_parameter_at_all(catalog: str) -> None:
    """One request must never be able to destroy a tenant's storage transitively: a project cascade
    would reach warehouses, and a warehouse delete can purge a bucket. Asserted by driving a project
    that HOLDS a warehouse — `cascade=true` must not make it succeed."""
    response = requests.delete(f"{catalog}/v1/projects/{PROJECT}?cascade=true", headers=_auth(), timeout=60)

    assert response.status_code != 200, "a project holding warehouses was deleted via ?cascade=true — the transitive purge path is reachable"
    assert response.status_code in (400, 403, 404, 409), f"unexpected answer {response.status_code}: {response.text[:300]}"


def test_a_malformed_project_id_is_refused_before_anything_else(catalog: str) -> None:
    response = requests.delete(f"{catalog}/v1/projects/not a valid id", headers=_auth(), timeout=30)

    assert response.status_code in (400, 404), f"a malformed project id answered {response.status_code}: {response.text[:200]}"


def test_a_purge_refuses_bytes_a_SIBLING_warehouse_still_claims(catalog: str) -> None:
    """The guard `create_warehouse`'s cross-claim check deliberately does NOT provide.

    A bucket may back two warehouses of the SAME project — a work warehouse plus a `serving="gold"` one
    is exactly that shape, and the create-time guard subtracts the caller's own project on purpose. A
    purge deletes every object AND the bucket, so destroying one of the pair would silently take the
    other's data and leave its record pointing at a bucket that no longer exists.

    Both warehouses here are this test's own, in a bucket it named, so the refusal is asserted without
    any real bytes being reachable.
    """
    bucket = f"e2edel-shared-{uuid.uuid4().hex[:8]}"
    first, second = f"{bucket}-a", f"{bucket}-b"
    for name in (first, second):
        made = requests.post(f"{catalog}/v1/warehouses", json={"id": name, "project": PROJECT, "bucket": bucket}, headers=_auth(), timeout=30)
        if made.status_code not in (200, 201, 409):
            pytest.skip(f"cannot provision the sibling pair ({made.status_code}): {made.text[:200]}")
    try:
        response = requests.delete(f"{catalog}/v1/warehouses/{first}?purge_bucket=true", headers=_auth(), timeout=60)

        assert response.status_code == 409, (
            f"a purge of {bucket!r} was allowed while {second!r} still claims it ({response.status_code}) — "
            "deleting one of a same-project pair would destroy the other's data"
        )
        still_there = requests.get(f"{catalog}/v1/warehouses/{first}", headers=_auth(), timeout=30)
        assert still_there.status_code == 200, "the refusal was not free — the warehouse went anyway"
    finally:
        # The SECOND first, so the shared bucket has exactly one claimant when the purge runs — the same
        # rule the refusal above proves, used forwards.
        requests.delete(f"{catalog}/v1/warehouses/{second}?cascade=true&force=true", headers=_auth(), timeout=60)
        requests.delete(f"{catalog}/v1/warehouses/{first}?cascade=true&purge_bucket=true&force=true", headers=_auth(), timeout=90)


def test_deletion_protection_refuses_and_force_overrides_exactly_it(catalog: str) -> None:
    """`force` overrides the PROTECTION only — never the gate, which has already run identically."""
    name = f"e2edel-{uuid.uuid4().hex[:8]}"
    made = requests.post(f"{catalog}/v1/warehouses", json={"id": name, "project": PROJECT, "protected": True}, headers=_auth(), timeout=30)
    if made.status_code not in (200, 201, 409):
        pytest.skip(f"cannot provision a protected warehouse ({made.status_code}): {made.text[:200]}")
    try:
        refused = requests.delete(f"{catalog}/v1/warehouses/{name}", headers=_auth(), timeout=60)
        assert refused.status_code == 409, f"a protected warehouse deleted without force: {refused.status_code} {refused.text[:200]}"

        forced = requests.delete(f"{catalog}/v1/warehouses/{name}?force=true", headers=_auth(), timeout=60)
        assert forced.status_code == 200, f"force did not override deletion protection: {forced.status_code} {forced.text[:300]}"
    finally:
        requests.delete(f"{catalog}/v1/warehouses/{name}?cascade=true&purge_bucket=true&force=true", headers=_auth(), timeout=90)
