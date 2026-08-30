"""One outage, one explanation (MAINT-10).

`build_report` gates each category on `_first(...)` over that category's inputs, and three categories
read the SAME pair of sources — the FGA tuple scan and the project registry. Two of them listed the
pair `(tuples, projects)` and the third listed it `(projects, tuples)`, so when both stores were down
the report explained the identical outage two different ways depending on which category the reader
looked at. `_first` returns the first NON-EMPTY reason in argument order, which makes the argument
order a silent policy decision — and one that nothing recorded or checked.
"""

from __future__ import annotations

from maintenance.services.reconcile import Sources, build_report


#: The categories whose answer is derived from BOTH the FGA tuple scan and the project registry.
_TUPLES_AND_PROJECTS = {"ghost_projects", "unreferenced_projects", "orphaned_annotation_tasks"}


def test_categories_over_the_same_two_sources_report_the_same_reason() -> None:
    sources = Sources()
    sources.tuples_error = "openfga: ServiceUnavailableError"
    sources.project_records_error = "registry: PermissionError"

    report = build_report(sources, warehouses_enabled=False, platform_buckets=set(), fga_root_object="warehouse:root")

    reasons = {u.category: u.reason for u in report.unavailable if u.category in _TUPLES_AND_PROJECTS}
    assert set(reasons) == _TUPLES_AND_PROJECTS, f"a category over these two sources went missing: {reasons}"
    assert len(set(reasons.values())) == 1, f"the same outage was explained {len(set(reasons.values()))} different ways: {reasons}"


def test_a_single_failing_source_is_still_the_reason_every_time() -> None:
    """The control: with only ONE of the pair down, every category must name that one."""
    sources = Sources()
    sources.project_records_error = "registry: PermissionError"

    report = build_report(sources, warehouses_enabled=False, platform_buckets=set(), fga_root_object="warehouse:root")

    reasons = {u.reason for u in report.unavailable if u.category in _TUPLES_AND_PROJECTS}
    assert reasons == {"registry: PermissionError"}
