"""The authz store every catalog door waits on is scraped, and its runtime noise is not.

[[XC-047]]. OpenFGA exposes 653 Prometheus series on a port named `metrics` and nothing collected one
of them — measured 2026-09-20 against the deployed pod. Every governed read and write in this estate
blocks on a Check, so an authz store that is slow or thrashing its datastore is the first thing an
operator needs to see and was the one thing they could not.

IT IS UNREACHABLE BY CONSTRUCTION, not unscraped by oversight — the same shape the Ray job was in
before `test_ray_pods_are_a_scrape_target`. The Collector's `dapr-sidecars` and `dapr-control-plane`
jobs both `keep` on a `dapr.io/*` pod annotation at their FIRST relabel step, and an OpenFGA pod
carries neither, so it is dropped before any later rule runs. A fourth job is the only way in.

AND THE CARDINALITY IS BOUNDED BEFORE IT IS ADDED, because this estate has already paid for the other
outcome: the Ray job shipped 4,099 series against a prior estate of 3,250 and broke the store
(`test_the_ray_scrape_does_not_ship_the_per_operator_ray_data_firehose`). The distinction that matters
there is UNBOUNDED, not merely large — `ray_data_*` grows per dataset and per operator. OpenFGA's do
not: `openfga_request_duration_ms` is gRPC-method × bucket, fixed by the API surface. What is dropped
here is the Go runtime (`go_*`, `process_*`, 44 of the 653), which carries no OpenFGA signal at all
and which every Go service in the estate would otherwise duplicate.
"""

from __future__ import annotations

from tests.unit.test_invariants import _collector_scrape_jobs, _rendered_docs


def _openfga_jobs() -> list[dict]:
    return [j for j in _collector_scrape_jobs(_rendered_docs()) if "openfga" in str(j.get("job_name", ""))]


def test_there_are_scrape_jobs_at_all() -> None:
    """Without this the suite below passes by reading an empty receiver."""
    assert _collector_scrape_jobs(_rendered_docs()), "the Collector renders no prometheus scrape_configs at all"


def test_openfga_is_a_scrape_target() -> None:
    """THE DEFECT: 653 series exposed, none collected, and no rule or panel about authz could fire."""
    assert _openfga_jobs(), (
        "no OpenFGA scrape job — the existing jobs `keep` on a `dapr.io/*` annotation an OpenFGA pod "
        "does not carry, so it is dropped before any other rule runs"
    )


def test_the_openfga_job_does_not_depend_on_a_dapr_annotation() -> None:
    """The reason the gap existed. A job that selected on `dapr.io/*` would match nothing and this
    suite would still be green — the vacuous shape the Ray job was written to avoid."""
    for job in _openfga_jobs():
        for rule in job.get("relabel_configs", []):
            if rule.get("action") == "keep":
                sources = " ".join(str(s) for s in rule.get("source_labels", []))
                assert "dapr_io" not in sources, f"{job['job_name']} keeps on a Dapr annotation: {rule}"


def test_the_openfga_job_drops_the_go_runtime() -> None:
    """44 of the 653 are `go_*`/`process_*` — no OpenFGA signal, and every Go service would duplicate
    them. The Ray incident is why cardinality is bounded at the job rather than after the fact."""
    jobs = _openfga_jobs()
    assert jobs, "no OpenFGA job to check"

    drops = [rc for rc in jobs[0].get("metric_relabel_configs", []) if rc.get("action") == "drop"]
    pattern = " ".join(str(rc.get("regex", "")) for rc in drops)

    assert "go_" in pattern and "process_" in pattern, f"the OpenFGA job ships the Go runtime families; metric_relabel_configs drops are {drops or 'ABSENT'}"


def test_the_openfga_job_KEEPS_the_authz_signal() -> None:
    """The other side of the drop. A filter that removed `openfga_*` would satisfy the test above and
    leave the row's own closing metric uncollected."""
    jobs = _openfga_jobs()
    drops = [rc for rc in jobs[0].get("metric_relabel_configs", []) if rc.get("action") == "drop"]
    pattern = " ".join(str(rc.get("regex", "")) for rc in drops)

    assert "openfga_" not in pattern, f"an OpenFGA family is being dropped: {drops}"
