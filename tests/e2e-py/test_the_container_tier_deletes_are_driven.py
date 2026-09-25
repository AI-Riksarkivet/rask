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
from urllib.parse import quote

import pyarrow as pa
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
    _purge(catalog, name)


def _purge(catalog: str, name: str) -> None:
    """Remove the warehouse AND the bucket it minted, whichever of the two still exists.

    THE 404 IS THE LEAK. Several legs delete the warehouse themselves, so the fixture's own delete then
    answers 404 — and a 404 means the record is gone and `purge_bucket` never ran, leaving the bucket
    behind. Measured 2026-09-16: orphan buckets in the estate's drift report went 13 -> 40 across four
    runs of this file, a test manufacturing the finding it is meant to be independent of. Accepting the
    404 as success was the bug; re-creating a record over the same bucket so the purge has something to
    delete through is the fix, and it uses only the doors this suite already drives.
    """
    removed = requests.delete(f"{catalog}/v1/warehouses/{name}?cascade=true&purge_bucket=true&force=true", headers=_auth(), timeout=90)
    if removed.status_code == 200:
        return
    # The record went with a leg's own delete; the bucket did not. Re-register it just long enough to
    # purge through the door, so no leg has to know whether it was the one that deleted the record.
    again = requests.post(f"{catalog}/v1/warehouses", json={"id": name, "project": PROJECT, "bucket": name}, headers=_auth(), timeout=30)
    if again.status_code not in (200, 201, 409):
        return
    final = requests.delete(f"{catalog}/v1/warehouses/{name}?cascade=true&purge_bucket=true&force=true", headers=_auth(), timeout=90)
    assert final.status_code == 200, f"the fixture leaked bucket {name!r}: {final.status_code} {final.text[:300]}"


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
        _purge(catalog, first)


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
        _purge(catalog, name)


#: The identifier delimiter this estate serves, matching `test_catalog_live.py`'s table ids. A nested
#: namespace and a table under it are both `parent<DELIM>child`, so one constant spells both.
DELIM = "$"


def _empty_arrow_stream() -> bytes:
    schema = pa.schema([pa.field("id", pa.int64())])
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema) as writer:
        writer.write_table(pa.table({"id": pa.array([], pa.int64())}, schema=schema))
    return sink.getvalue().to_pybytes()


def test_a_cascade_dropped_SUBTREE_undrops_at_every_old_id(catalog: str, warehouse: str) -> None:
    """The plural undrop (`namespaces.py:678`), driven against the deployed release.

    THE UNIT OF A CASCADE IS THE SUBTREE, so the unit of its recovery is too — and a leg that drops
    one flat namespace proves neither half of that. What has to hold is the shallowest-first
    re-create, the re-registration of a table at a depth the drop was never given, and the trash
    record clearing as its object comes back. So the subtree here is two deep and carries a table at
    EACH level, and every id is asserted on its own rather than through the top one's 200.

    THE LOCATION IS ASSERTED, not just the id. A table re-registered somewhere else is still a 200 on
    describe and is not a recovery — the door's contract is the old id pointing at the still-present
    bytes, and only comparing the pre-drop location says which one happened.

    Everything this leg destroys it created seconds earlier, in this test's own warehouse; the drop is
    the recoverable kind by construction (no `mode`), and the `finally` purges what recovery left.
    """
    top = f"e2eundrop{uuid.uuid4().hex[:6]}"
    child = f"{top}{DELIM}kids"
    bound = requests.post(f"{catalog}/v1/warehouses/{warehouse}/namespaces", json={"namespace": top}, headers=_auth(), timeout=30)
    assert bound.status_code in (200, 201), f"cannot bind the subtree's root: {bound.status_code} {bound.text[:300]}"
    made = requests.post(f"{catalog}/v1/namespace/{quote(child, safe='')}/create", json={"id": [top, "kids"]}, headers=_auth(), timeout=30)
    assert made.status_code in (200, 201), f"cannot nest a child namespace: {made.status_code} {made.text[:300]}"

    tables = {f"{top}{DELIM}top_rows": [top, "top_rows"], f"{child}{DELIM}kid_rows": [top, "kids", "kid_rows"]}
    namespaces = {top: [top], child: [top, "kids"]}
    for table_id in tables:
        created = requests.post(
            f"{catalog}/v1/table/{quote(table_id, safe='')}/create",
            data=_empty_arrow_stream(),
            headers={**_auth(), "content-type": "application/vnd.apache.arrow.stream"},
            timeout=60,
        )
        assert created.status_code == 200, f"cannot create {table_id!r} in the subtree: {created.status_code} {created.text[:300]}"

    try:
        before = {}
        for table_id, identifier in tables.items():
            described = requests.post(f"{catalog}/v1/table/{quote(table_id, safe='')}/describe", json={"id": identifier}, headers=_auth(), timeout=30)
            assert described.status_code == 200, f"{table_id!r} is not there before the drop: {described.status_code}"
            before[table_id] = described.json().get("location")
            assert before[table_id], f"{table_id!r} describes with no location, so recovery cannot be checked against one"

        dropped = requests.post(f"{catalog}/v1/namespace/{quote(top, safe='')}/drop", json={"id": [top], "behavior": "CASCADE"}, headers=_auth(), timeout=120)
        assert dropped.status_code == 200, f"the recoverable cascade did not run: {dropped.status_code} {dropped.text[:300]}"

        for namespace_id, identifier in namespaces.items():
            gone = requests.post(f"{catalog}/v1/namespace/{quote(namespace_id, safe='')}/describe", json={"id": identifier}, headers=_auth(), timeout=30)
            assert gone.status_code == 404, f"the cascade left namespace {namespace_id!r} resolvable ({gone.status_code}) — it dropped less than it claimed"
        for table_id, identifier in tables.items():
            gone = requests.post(f"{catalog}/v1/table/{quote(table_id, safe='')}/describe", json={"id": identifier}, headers=_auth(), timeout=30)
            assert gone.status_code == 404, f"the cascade left table {table_id!r} resolvable ({gone.status_code})"

        # The deadline the owner has to act within. An undrop window nobody can see is not a safety
        # feature, so the door that shows it is driven here rather than assumed.
        queued = requests.get(f"{catalog}/management/v1/namespace/{quote(top, safe='')}/tasks", headers=_auth(), timeout=30)
        assert queued.status_code == 200, f"the trash door did not answer: {queued.status_code} {queued.text[:200]}"
        assert queued.json(), "a recoverable cascade queued no expiry, so the owner cannot see how long recovery is open"
        assert queued.json()[0].get("expires_at"), f"the queued entry names no deadline: {queued.text[:300]}"

        recovered = requests.post(f"{catalog}/management/v1/namespace/{quote(top, safe='')}/undrop", headers=_auth(), timeout=120)
        assert recovered.status_code == 200, f"the plural undrop failed: {recovered.status_code} {recovered.text[:400]}"

        for namespace_id, identifier in namespaces.items():
            back = requests.post(f"{catalog}/v1/namespace/{quote(namespace_id, safe='')}/describe", json={"id": identifier}, headers=_auth(), timeout=30)
            assert back.status_code == 200, f"namespace {namespace_id!r} did not come back at its old id: {back.status_code} {back.text[:300]}"
        for table_id, identifier in tables.items():
            back = requests.post(f"{catalog}/v1/table/{quote(table_id, safe='')}/describe", json={"id": identifier}, headers=_auth(), timeout=30)
            assert back.status_code == 200, f"table {table_id!r} did not come back at its old id: {back.status_code} {back.text[:300]}"
            assert back.json().get("location") == before[table_id], (
                f"{table_id!r} re-registered at {back.json().get('location')!r}, not at its pre-drop {before[table_id]!r} — "
                "the id resolves but not to the bytes it named"
            )

        # Each table under the namespace that owned it, not all of them under the root: the listing is
        # what proves the subtree's SHAPE returned, where four describes only prove its members did.
        for namespace_id, expected in ((top, "top_rows"), (child, "kid_rows")):
            listed = requests.get(f"{catalog}/v1/namespace/{quote(namespace_id, safe='')}/table/list", headers=_auth(), timeout=30)
            assert listed.status_code == 200, f"cannot list {namespace_id!r} after recovery: {listed.status_code}"
            assert expected in listed.json().get("tables", []), f"{namespace_id!r} came back without {expected!r}: {listed.text[:300]}"

        settled = requests.get(f"{catalog}/management/v1/namespace/{quote(top, safe='')}/tasks", headers=_auth(), timeout=30)
        assert settled.json() == [], f"the trash record survived the recovery it completed: {settled.text[:300]}"
    finally:
        # RECOVER FIRST, then purge — in that order, because a failure ANYWHERE above can leave the
        # subtree sitting in the trash, and a purge-drop of a namespace that is not live answers 404
        # and clears nothing. Measured 2026-09-18: a deliberately broken variant of this leg left a
        # trash record that then could not be recovered at all, because the fixture had already
        # deleted its warehouse and `undrop`'s deactivation gate reads a missing warehouse as
        # not-active (403). Seven days of a dead record pointing at a destroyed bucket is exactly the
        # residue `conftest`'s session cleanup exists to stop this suite manufacturing.
        requests.post(f"{catalog}/management/v1/namespace/{quote(top, safe='')}/undrop", headers=_auth(), timeout=120)
        # PURGE, not a second recoverable drop — and `purge` is a QUERY parameter. The body's `mode` is
        # the orthogonal Fail/Skip field for a namespace that is not there, and `DropMode.parse` refuses
        # `{"mode": "PURGE"}` as InvalidInput, so this cleanup must spell the opt-out where the door
        # reads it. What the refusal prevents, measured 2026-09-18: a body-spelled purge accepted as the
        # default drops recoverably, and four such stale trash records sat on the live estate, each
        # reading as a successful purge.
        purged = requests.post(
            f"{catalog}/v1/namespace/{quote(top, safe='')}/drop?purge=true",
            json={"id": [top], "behavior": "CASCADE"},
            headers=_auth(),
            timeout=120,
        )
        # ASSERTED, not best-effort: a purge that quietly failed leaves a record alive for the whole
        # grace window, and the only reader who would notice is a future run of this file. The removal
        # itself is checked at the `settled` assertion above rather than here — a purge revokes the
        # subtree's grants along with its bytes, so reading `tasks` afterwards answers 403 on a
        # namespace that no longer has an owner, which says nothing about the record.
        assert purged.status_code in (200, 404), f"the cleanup purge left the subtree in the trash: {purged.status_code} {purged.text[:300]}"
