"""The sweep reports the Lance session's occupancy, because that is the number the OOM turns on.

THE DEFECT. `rask-maintenance` has now been OOMKilled twice for the same reason (2026-09-10, and again
2026-09-21 with six restarts at exit 137 against a 512Mi limit). The bound that decides it is the Lance
session's cache, and `config.py::shared_lance_session` says in its own comment that the bound is soft:
*"the caps are LRU SOFT bounds — the size the cache grows toward, not a ceiling it stops at."*

Measured inside the running pod 2026-09-21: cgroup **512 MB**, configured caps **128 + 256 = 384 MB**,
clamped by `affordable_cache_bytes` to **68 + 136 = 204 MB**. So the clamp is working and the pod still
died — which means the question is how close the cache actually runs to its cap and what else is
resident, and NOTHING IN THE ESTATE REPORTS EITHER. The sweep emits fifteen fields per tick about what
it reclaimed and not one about what it is holding.

`lance.Session` answers it. Verified against the installed pylance 11.0.0: `Session` carries exactly
`index_cache_size_bytes`, `is_same_as` and `size_bytes`, and `size_bytes` is a **method_descriptor** —
reading it yields a bound method, so it must be CALLED. That distinction is not pedantry: the first
implementation read it as a property, `int()` of a bound method raised, and the guard reported -1, which
is a diagnostic that is always "unavailable" and therefore worse than having none. The
`value >= 0` leg below is what caught it.

The session is genuinely process-wide — `lance_session` is `@cache`d on the two cap ints, so equal caps
share one object — which is what makes a single number meaningful for the whole process rather than per
call site.

WHY THIS IS THE FIRST STEP AND NOT THE FIX. A fix chosen now would be chosen blind: tick-scoped
sessions, explicit eviction, or lower caps all bound the cache, and they differ in what they cost the
cache-hit rate the session exists for. Which one is right depends on whether the cache is at 20% of its
cap or pinned at 100%, and that is currently unobservable on a live estate. The estate has already paid
once for reasoning about this workload from a number that measured something else — see
`docs/DECISIONS.md` § *`compaction_mode` is not a measure of where bytes moved*.
"""

from __future__ import annotations

from maintenance.services.sweep import summarize


def test_the_tick_summary_carries_the_session_size() -> None:
    """RED before the fix: `summarize` reported fifteen fields and none of them this one.

    Asserted on the KEY rather than on a value, because the value is whatever the process is holding
    and a test that pinned a number would fail for being right.
    """
    summary = summarize([])

    assert "lance_session_bytes" in summary, "a tick that cannot say what it is holding cannot explain an OOM"


def test_the_reported_size_is_a_real_measurement_not_a_placeholder() -> None:
    """A zero here would be the failure this file exists to prevent — a field that always answers.

    The session is created on first use and is never empty once anything has opened a dataset, but an
    EMPTY sweep legitimately touches nothing, so the bar is the type and the sign rather than a
    threshold: an int, not None, and never negative. `size_bytes` is the session's own accounting.
    """
    value = summarize([])["lance_session_bytes"]

    assert isinstance(value, int), f"expected an int of bytes, got {type(value).__name__}"
    assert value >= 0, "a negative occupancy is a broken read, not a small cache"


def test_the_cap_is_reported_beside_the_size() -> None:
    """Occupancy alone cannot say whether the cache is the problem — it needs its own denominator.

    204 MB of cache is fine in a 2 GB pod and fatal in a 512 MB one, and `affordable_cache_bytes`
    derives the caps from the cgroup at runtime, so the number the process actually got is not
    recoverable from any values file. Reporting both makes one log line answer "how full", which is the
    question every later decision on [[LH-183]] turns on.
    """
    summary = summarize([])

    assert "lance_session_cap_bytes" in summary, "a size with no cap beside it cannot be read as a fraction"
    assert summary["lance_session_cap_bytes"] > 0, "the cap is derived from the cgroup and is never zero"
