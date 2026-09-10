"""CONTRACT: a process may not budget more cache than the container it runs in can hold.

THE ESTATE'S ONE BOUNDED SESSION WAS BOUNDED WRONG, which is what makes this a gate rather than a
comment. `maintenance` caps its Lance session at `lance_metadata_cache_mb=128` +
`lance_index_cache_mb=256` = 384 MB, inside a pod whose limit is 512Mi and whose measured process
baseline is 153Mi. 384 + 153 = 537 MB, so a fully-warmed session exceeds the limit by construction —
and `lance_session`'s own docstring says the caps are LRU SOFT BOUNDS, i.e. the size the cache grows
toward rather than a ceiling it will not cross.

Observed 2026-09-10: `rask-maintenance` OOMKilled (exit 137) and restarted after repeated reconcile
passes, which is precisely the workload that warms a session — it opens every dataset across 93
buckets.

The bound is derived from the CONTAINER rather than restated, because a literal cannot track a chart
value: `resources.limits.memory` can be raised or lowered without anyone revisiting a constant in
Python, and the failure that follows is an OOMKill with no line to blame. Reading the cgroup makes the
two impossible to drift apart.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.lance_session import cache_budget_bytes


def test_it_reads_the_cgroup_v2_limit(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cgroup v2 states the limit in `memory.max` as a plain byte count."""
    limit = tmp_path / "memory.max"
    limit.write_text("536870912\n")  # 512Mi
    monkeypatch.setattr("service_kit.lakehouse.lance_session._CGROUP_V2", limit)
    assert cache_budget_bytes(fraction=0.5) == 536870912 // 2


def test_an_UNLIMITED_container_gets_no_derived_budget(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`max` means no limit, and a fraction of no limit is not a number.

    Returning None rather than a huge integer is the point: a caller must then fall back to its own
    configured value, which is the honest answer for a process nobody has constrained.
    """
    limit = tmp_path / "memory.max"
    limit.write_text("max\n")
    monkeypatch.setattr("service_kit.lakehouse.lance_session._CGROUP_V2", limit)
    assert cache_budget_bytes() is None


def test_no_cgroup_at_all_is_not_an_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off a container — a developer laptop, a CI runner — there is nothing to read and that is fine."""
    monkeypatch.setattr("service_kit.lakehouse.lance_session._CGROUP_V2", tmp_path / "absent")
    monkeypatch.setattr("service_kit.lakehouse.lance_session._CGROUP_V1", tmp_path / "absent-too")
    assert cache_budget_bytes() is None


def test_the_budget_CLAMPS_a_configured_cap_it_cannot_afford(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this exists for: a configured 384 MB inside a 512Mi container is clamped, not honoured.

    An operator may always ask for LESS than the container allows. What they may not do is ask for more
    and have the process agree — that is the shape that OOMKilled maintenance.
    """
    from service_kit.lakehouse.lance_session import affordable_cache_bytes

    limit = tmp_path / "memory.max"
    limit.write_text("536870912\n")  # 512Mi
    monkeypatch.setattr("service_kit.lakehouse.lance_session._CGROUP_V2", limit)

    metadata, index = affordable_cache_bytes(128 << 20, 256 << 20, fraction=0.5)
    assert metadata + index <= 536870912 // 2, "the clamped total still exceeds the affordable share"
    assert metadata < (128 << 20) and index < (256 << 20), "nothing was clamped"
    # The RATIO the operator asked for is preserved — they said index should be twice metadata.
    assert index == pytest.approx(metadata * 2, rel=0.01)


def test_a_cap_that_already_fits_is_left_alone(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clamping must not become a second, invisible policy for configurations that were always fine."""
    from service_kit.lakehouse.lance_session import affordable_cache_bytes

    limit = tmp_path / "memory.max"
    limit.write_text("2147483648\n")  # 2Gi
    monkeypatch.setattr("service_kit.lakehouse.lance_session._CGROUP_V2", limit)

    assert affordable_cache_bytes(128 << 20, 256 << 20, fraction=0.5) == (128 << 20, 256 << 20)


def test_the_LIMIT_is_re_read_and_never_cached(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A container's limit can change under a running process, so the budget may not be memoised.

    Kubernetes supports in-place pod resize: `resources.limits.memory` can move without a restart. A
    cached read would leave the process budgeting against a number that is no longer true — the exact
    drift that reading the cgroup exists to prevent, reintroduced one decorator later.

    Caught by this file rather than reasoned about: an `@cache` on `cache_budget_bytes` was tried, and
    `test_a_cap_that_already_fits_is_left_alone` failed because the first test's 512Mi was still being
    returned after the limit had been rewritten to 2Gi. The cache now sits on the LOG line instead,
    where repetition is the only thing at stake.
    """
    limit = tmp_path / "memory.max"
    monkeypatch.setattr("service_kit.lakehouse.lance_session._CGROUP_V2", limit)

    limit.write_text("536870912\n")  # 512Mi
    assert cache_budget_bytes(fraction=0.5) == 536870912 // 2

    limit.write_text("2147483648\n")  # resized to 2Gi, no restart
    assert cache_budget_bytes(fraction=0.5) == 2147483648 // 2, "the budget was memoised across a resize"
