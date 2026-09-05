"""Which tenants the lag detector measures — read from the registry, never from a static list.

`(edge, project)` is the gauge's key, so an unmeasured project is a cascade with no series at all:
indistinguishable, on every dashboard, from a cascade with no lag. The producer rendered no project
list, so every edge was measured at `project=""` — the single-tenant shape — and every tenant's cascade
was unmonitored while the detector reported cleanly.

A VALUES LIST WOULD NOT HAVE FIXED IT. A project is minted by `POST /v1/projects` at runtime with an
operator-chosen id, exactly as a warehouse bucket is; a list in the chart is stale the moment a tenant
is onboarded, and the estate has just paid for that lesson once in
`chart/templates/rustfs-scoped-users.yaml`, whose bucket allow-list could not name the runtime-minted
warehouse the whole cascade writes to. `sweep.py::_buckets_to_sweep` is the standing precedent:
configured set UNION what the registry says exists.
"""

from __future__ import annotations

from service_kit.lakehouse.warehouse_records import measurable_projects


def test_a_project_with_a_live_warehouse_is_measured() -> None:
    assert measurable_projects([{"project": "acme", "bucket": "acme-bucket"}]) == ["acme"]


def test_two_warehouses_for_one_project_are_ONE_tenant() -> None:
    """`acme` holds both `acme-bucket` and `tracka-wh` on the live estate. Measuring it twice would
    publish two identical series under one key and double every count that reads them."""
    assert measurable_projects(
        [{"project": "acme", "bucket": "acme-bucket"}, {"project": "acme", "bucket": "tracka-wh"}]
    ) == ["acme"]


def test_a_DEACTIVATED_tenant_is_not_measured() -> None:
    """Deactivate is offboarding step one — the resolver 403s every operation on the tenant's
    namespaces, so its edges cannot advance. Measuring it publishes a lag that grows forever and that
    nobody is permitted to do anything about."""
    assert measurable_projects([{"project": "gone", "bucket": "b", "status": "deactivated"}]) == []


def test_an_ABSENT_status_counts_as_active() -> None:
    """Records written before the lifecycle feature carry no status and are live — the same reading
    `warehouse_status` and `maintainable_buckets` already take."""
    assert measurable_projects([{"project": "old", "bucket": "b"}]) == ["old"]


def test_a_record_with_no_project_is_skipped_rather_than_named_empty() -> None:
    """`""` is the single-tenant key. A malformed record borrowing it would silently merge a broken
    tenant's edges into the untenanted series."""
    assert measurable_projects([{"bucket": "orphan"}, {"project": "", "bucket": "b"}]) == []
